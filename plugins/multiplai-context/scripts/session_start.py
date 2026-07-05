# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core @ git+https://github.com/spikelab/multiplai-core@v0.2"]
# ///
"""Session start hook for multiplai plugin.

Logs client selection, records the session start timestamp, initializes
session state, and drains deferred extraction markers. Routed *memory*
injection is handled per-prompt by context_manager.py (UserPromptSubmit);
this hook deliberately does NOT dump memory into the session context.

It DOES inject the per-project "now" snapshot once, here at session start:
the session's ``cwd`` is resolved to a project (lib.project_identity) and the
matching ``now/<project>.md`` is emitted as additionalContext so the session
opens knowing where that project left off. This is one-time on purpose —
re-injecting project status on every prompt (the old behavior) was wasteful
and added no signal.

Also checks the Dream 24h gate: when more than 24 hours have
elapsed since the last dream run and fresh learnings are pending,
emits a system nudge so the user is prompted to run ``/multiplai-context:dream``
instead of the consolidation silently falling out of rhythm.
"""

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.paths import get_paths
from multiplai_core.config import load_yaml
from multiplai_core.log_utils import setup_logging, log_event

logger = setup_logging("session_start")

_DREAM_GATE_HOURS = 24

# Deferred-extraction retry policy. A detached extraction child should
# finish well within the stale window; markers older than this with no
# completion are assumed orphaned and requeued, capped at MAX_ATTEMPTS.
_EXTRACTION_STALE_SECONDS = 900
_EXTRACTION_MAX_ATTEMPTS = 3


def _log_client_selection() -> str:
    """Log which model client is available for this session.

    Uses the model_client module's detect_client_type() to determine
    which backend will be used (AgentSDK vs API key fallback).
    """
    from multiplai_core.model_client import detect_client_type
    client_type = detect_client_type()
    logger.info("Model client selected: %s", client_type)
    return client_type


def _dream_gate_open(dream_state_file: Path) -> bool:
    """Return True when >=24h have passed since the last dream run.

    Missing state or an unparseable timestamp is treated as gate-open
    (first run or recovery). Any YAML parse failure is swallowed — a
    corrupt state file shouldn't block session start; the user can
    recover by running ``/multiplai-context:dream`` manually.
    """
    try:
        state = load_yaml(dream_state_file) or {}
    except Exception:
        logger.warning("Could not read dream state %s; treating gate as open", dream_state_file)
        return True

    last_run = state.get("last_run")
    if not last_run:
        return True

    try:
        last_dt = datetime.fromisoformat(last_run)
    except (ValueError, TypeError):
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_dt >= timedelta(hours=_DREAM_GATE_HOURS)


def _learnings_pending(learnings_file: Path, dream_state_file: Path) -> bool:
    """Return True if learnings.md has content newer than the last dream run."""
    if not learnings_file.exists():
        return False
    if learnings_file.stat().st_size == 0:
        return False

    try:
        state = load_yaml(dream_state_file) or {}
    except Exception:
        return True

    last_run = state.get("last_run")
    if not last_run:
        return True

    try:
        last_dt = datetime.fromisoformat(last_run)
    except (ValueError, TypeError):
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)

    learnings_mtime = datetime.fromtimestamp(
        learnings_file.stat().st_mtime, tz=timezone.utc,
    )
    return learnings_mtime > last_dt


def _recover_stale_processing(processing_dir: Path, pending_dir: Path) -> None:
    """Requeue (or fail) markers stuck in ``processing_extractions/``.

    A detached extraction child deletes its own marker on success. If the
    child died (venv re-exec failure, crash, no model client) the marker
    lingers here. Markers older than the stale window are requeued for
    retry, capped at ``_EXTRACTION_MAX_ATTEMPTS`` before being moved to
    ``failed_extractions/`` so a permanently-bad transcript can't loop
    forever and stays visible for debugging.
    """
    if not processing_dir.exists():
        return
    failed_dir = processing_dir.parent / "failed_extractions"
    now = time.time()
    for m in list(processing_dir.glob("*.json")):
        try:
            if now - m.stat().st_mtime < _EXTRACTION_STALE_SECONDS:
                continue  # a live child may still be working on it
        except OSError:
            continue
        try:
            data = json.loads(m.read_text())
            if not isinstance(data, dict):
                data = {}
        except (json.JSONDecodeError, OSError):
            data = {}
        attempts = int(data.get("attempts", 0)) + 1
        data["attempts"] = attempts
        try:
            if attempts > _EXTRACTION_MAX_ATTEMPTS:
                failed_dir.mkdir(parents=True, exist_ok=True)
                m.write_text(json.dumps(data, indent=2))
                os.replace(str(m), str(failed_dir / m.name))
                logger.warning(
                    "Deferred extraction permanently failed after %d attempts: %s",
                    attempts - 1, m.name,
                )
            else:
                m.write_text(json.dumps(data, indent=2))
                os.replace(str(m), str(pending_dir / m.name))
                logger.info(
                    "Requeued stale extraction marker (attempt %d): %s",
                    attempts, m.name,
                )
        except OSError:
            logger.exception("Could not recover stale marker %s", m.name)


def _process_deferred_extractions(data_dir: Path, extract_script: Path) -> int:
    """Drain pending extraction markers left by previous SessionEnd hooks.

    Each marker is atomically moved from ``pending_extractions/`` to
    ``processing_extractions/`` and piped (with the transcript, if still
    readable) to a detached ``extract_learnings.py``. The child deletes
    its own marker on success; failed/crashed children leave the marker
    for :func:`_recover_stale_processing` to retry. Returns the number of
    markers launched this run.

    Atomic rename guarantees at-most-once dequeue if two SessionStart
    hooks race.
    """
    if not extract_script.exists():
        return 0

    pending_dir = data_dir / "pending_extractions"
    processing_dir = data_dir / "processing_extractions"
    pending_dir.mkdir(parents=True, exist_ok=True)
    processing_dir.mkdir(parents=True, exist_ok=True)

    # Retry anything a previous run launched but that never completed.
    _recover_stale_processing(processing_dir, pending_dir)

    processed = 0
    for marker_file in list(pending_dir.glob("*.json")):
        dest = processing_dir / marker_file.name
        try:
            os.rename(str(marker_file), str(dest))
        except OSError:
            continue

        try:
            marker = json.loads(dest.read_text())
        except (json.JSONDecodeError, OSError):
            # Unparseable marker will never succeed — discard it.
            dest.unlink(missing_ok=True)
            continue

        # Pass the transcript PATH, not its contents: the child distills it
        # into token-bounded chunks before the LLM call. Piping a raw
        # multi-MB transcript here previously forced a single >200K-token
        # request that tripped the long-context billing gate (429).
        payload: dict = {
            "session_id": marker.get("session_id", ""),
            "cwd": marker.get("cwd", ""),
            "transcript_path": marker.get("transcript_path", ""),
            # The child removes this marker once the session is handled.
            "marker_path": str(dest),
        }

        try:
            proc = subprocess.Popen(
                ["uv", "run", "--no-project", str(extract_script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            if proc.stdin is not None:
                proc.stdin.write(json.dumps(payload).encode("utf-8"))
                proc.stdin.close()
            processed += 1
        except Exception:
            logger.exception("Failed to launch deferred extraction subprocess")
            # Launch failed — return the marker to the queue so a later
            # SessionStart retries it instead of losing the session.
            try:
                os.replace(str(dest), str(pending_dir / dest.name))
            except OSError:
                logger.exception("Could not requeue marker after launch failure")

    return processed


def _emit_no_client_warning(data_dir: Path) -> None:
    """Surface a one-time user-visible warning when no model client is available.

    Without either claude-agent-sdk or anthropic_api_key, all LLM-backed
    features (extraction, dreams, catalog generation) silently no-op. We
    warn once per install (marker file) so the user knows to run setup;
    repeating it every session would be noise.
    """
    marker = data_dir / "no_client_warning_emitted"
    if marker.exists():
        return
    print(
        "[multiplai] No Anthropic API key configured and no Agent SDK "
        "detected — extraction, dreams, and catalog generation will be "
        "skipped. Run /multiplai-context:setup or set anthropic_api_key in plugin "
        "config to enable them."
    )
    try:
        marker.touch()
    except OSError:
        pass


def _inject_project_state(now_dir: Path, cwd: str) -> bool:
    """Emit the matching project's ``now`` snapshot as additionalContext.

    Resolves *cwd* to a project via the shared resolver and, if
    ``now/<project>.md`` exists, prints it once so the session opens with
    that project's status. Returns True when something was injected.
    Best-effort: a missing file or any error is swallowed — project state
    is a nicety, never a reason to fail session start.
    """
    if not cwd:
        return False
    try:
        from lib.project_identity import resolve_project

        project = resolve_project(cwd)
        if not project:
            return False
        project_file = now_dir / f"{project}.md"
        if not project_file.exists():
            return False
        content = project_file.read_text(encoding="utf-8").strip()
        if not content:
            return False
        print(f"\n--- PROJECT STATE ---\n{content}")
        logger.info("Injected project state for %s", project)
        return True
    except Exception:
        logger.warning("Could not inject project state (cwd=%s)", cwd, exc_info=True)
        return False


def _emit_dream_nudge() -> None:
    """Print an additionalContext nudge prompting the user to run /multiplai-context:dream."""
    print(
        "\n--- SYSTEM NUDGE ---\n"
        "Dream gate is open (>24h since last consolidation) with "
        "unprocessed learnings on disk. Surface this to the user at the next "
        "natural stopping point: 'Dream reports look due — worth running "
        "/multiplai-context:dream?'"
    )


def main() -> None:
    # SessionStart hook input carries the session cwd; we use it to pick the
    # project's now-snapshot to inject. Read defensively — a missing/garbage
    # payload just means "no project state".
    try:
        raw_stdin = sys.stdin.read()
    except OSError:
        raw_stdin = ""
    try:
        hook_input = json.loads(raw_stdin or "{}")
    except (json.JSONDecodeError, ValueError):
        hook_input = {}
    if not isinstance(hook_input, dict):
        hook_input = {}
    cwd = hook_input.get("cwd", "")

    paths = get_paths()
    data_dir = paths.plugin_data()
    data_dir.mkdir(parents=True, exist_ok=True)

    # Log which model client is available
    client_type = _log_client_selection()

    # Warn the user once if neither the SDK nor an API key is present.
    if client_type.startswith("none"):
        _emit_no_client_warning(data_dir)

    # List available memory files for the session-state record. Contents
    # are NOT read or injected here — context_manager.py performs routed,
    # per-prompt memory injection on UserPromptSubmit.
    memory_dir = paths.memory_dir()
    memory_files = (
        sorted(p.name for p in memory_dir.glob("*.md"))
        if memory_dir.is_dir()
        else []
    )

    # Use the real Claude Code session id so every hook (context, nudge,
    # extract, session-end) logs under one id and the activity stream is
    # followable end-to-end. The random fallback only applies when the hook
    # input omits it (older clients / tests).
    session_id = hook_input.get("session_id") or str(uuid.uuid4())[:8]
    session_state = {
        "session_id": session_id,
        "start_time": datetime.now(timezone.utc).isoformat(),
        "plugin_mode": paths.is_plugin_mode(),
        "client_type": client_type,
        "memory_files_available": memory_files,
        # Recorded so SessionEnd can tag the diary entry with the project's
        # working directory. context_manager refreshes this each prompt as a
        # fallback for environments where SessionStart input lacks cwd.
        "cwd": cwd,
    }

    state_file = data_dir / "session_state.json"
    state_file.write_text(json.dumps(session_state, indent=2))

    # One-time per-project "now" snapshot injection (additionalContext).
    _inject_project_state(paths.now_dir(), cwd)

    # Drain any deferred extraction markers left by previous session_end
    # hooks. SessionEnd is kill-within-seconds, so the heavy LLM
    # extraction is intentionally deferred here where the SessionStart
    # hook has more headroom.
    extract_script = paths.scripts_dir() / "extract_learnings.py"
    try:
        processed = _process_deferred_extractions(data_dir, extract_script)
        if processed:
            logger.info("Launched %d deferred extraction(s)", processed)
            log_event(
                "extract", "launch",
                f"launched {processed} deferred extraction(s) from prior session(s)",
                session_id=session_id,
                count=processed,
            )
    except Exception:
        logger.exception("Deferred extraction processing failed (non-fatal)")

    # Dream gate: emit a nudge when the 24h window has elapsed and
    # fresh learnings are waiting. The nudge is additionalContext only —
    # the actual dream still runs via /multiplai-context:dream when the user
    # chooses.
    dream_state_file = paths.dream_state_file()
    learnings_file = paths.learnings_file()
    if (
        _dream_gate_open(dream_state_file)
        and _learnings_pending(learnings_file, dream_state_file)
    ):
        logger.info("Dream gate open with pending learnings; emitting nudge")
        log_event(
            "nudge", "dream",
            "dream gate open (>24h, pending learnings) — surfaced to user",
            session_id=session_id,
        )
        _emit_dream_nudge()

    logger.info(
        "Session started: %s (%d memory files on disk; not injected — routed per-prompt)",
        session_id, len(memory_files),
    )
    log_event(
        "session", "start",
        f"session started — {len(memory_files)} memory files indexed "
        f"(not injected; routed per-prompt), client={client_type}",
        session_id=session_id,
        memory_files=len(memory_files),
        client=client_type,
    )


if __name__ == "__main__":
    main()
