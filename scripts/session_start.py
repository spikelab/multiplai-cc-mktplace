"""Session start hook for multiplai plugin.

Loads memory files, injects context, logs client selection,
records session start timestamp and initializes session state.

Also checks the Dream 24h gate: when more than 24 hours have
elapsed since the last dream run and fresh learnings are pending,
emits a system nudge so Spike is prompted to run ``/multiplai:dream``
instead of the consolidation silently falling out of rhythm.
"""

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.venv_guard import ensure_venv_python
ensure_venv_python()

from lib.paths import get_paths
from lib.config import load_yaml, read_memory_files
from lib.log_utils import setup_logging

logger = setup_logging("session_start")

_AUTODREAM_GATE_HOURS = 24


def _log_client_selection() -> str:
    """Log which model client is available for this session.

    Uses the model_client module's detect_client_type() to determine
    which backend will be used (AgentSDK vs API key fallback).
    """
    from lib.model_client import detect_client_type
    client_type = detect_client_type()
    logger.info("Model client selected: %s", client_type)
    return client_type


def _dream_gate_open(dream_state_file: Path) -> bool:
    """Return True when >=24h have passed since the last dream run.

    Missing state or an unparseable timestamp is treated as gate-open
    (first run or recovery). Any YAML parse failure is swallowed — a
    corrupt state file shouldn't block session start; the user can
    recover by running ``/multiplai:dream`` manually.
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
    return datetime.now(timezone.utc) - last_dt >= timedelta(hours=_AUTODREAM_GATE_HOURS)


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


def _process_deferred_extractions(data_dir: Path, extract_script: Path) -> int:
    """Drain pending extraction markers left by previous SessionEnd hooks.

    Each marker is atomically moved from ``pending_extractions/`` to
    ``processing_extractions/``, its content (plus the transcript file
    if still readable) is piped to a detached ``extract_learnings.py``
    subprocess, and the marker is unlinked. Returns the number of
    markers processed.

    Atomic rename guarantees at-most-once dequeue if two SessionStart
    hooks race. Best-effort cleanup — if extraction crashes, the
    marker is already gone and won't be retried.
    """
    pending_dir = data_dir / "pending_extractions"
    if not pending_dir.exists() or not extract_script.exists():
        return 0

    processing_dir = data_dir / "processing_extractions"
    processing_dir.mkdir(parents=True, exist_ok=True)

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
            dest.unlink(missing_ok=True)
            continue

        transcript_path = marker.get("transcript_path", "")
        payload: dict = {"session_id": marker.get("session_id", "")}
        if transcript_path and Path(transcript_path).exists():
            try:
                payload["transcript"] = Path(transcript_path).read_text(
                    encoding="utf-8", errors="replace",
                )
            except OSError:
                logger.warning("Could not read transcript at %s", transcript_path)

        try:
            proc = subprocess.Popen(
                [sys.executable, str(extract_script)],
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
        finally:
            dest.unlink(missing_ok=True)

    return processed


def _emit_dream_nudge() -> None:
    """Print an additionalContext nudge prompting Spike to run /multiplai:dream."""
    print(
        "\n--- SYSTEM NUDGE ---\n"
        "Dream gate is open (>24h since last consolidation) with "
        "unprocessed learnings on disk. Surface this to Spike at the next "
        "natural stopping point: 'Dream reports look due — worth running "
        "/multiplai:dream?'"
    )


def main() -> None:
    paths = get_paths()
    data_dir = paths.plugin_data()
    data_dir.mkdir(parents=True, exist_ok=True)

    # Log which model client is available
    client_type = _log_client_selection()

    # Load memory files for context injection
    memory_dir = paths.memory_dir()
    memory_context = read_memory_files(memory_dir)

    session_id = str(uuid.uuid4())[:8]
    session_state = {
        "session_id": session_id,
        "start_time": datetime.now(timezone.utc).isoformat(),
        "plugin_mode": paths.is_plugin_mode(),
        "client_type": client_type,
        "memory_files_loaded": list(memory_context.keys()),
    }

    state_file = data_dir / "session_state.json"
    state_file.write_text(json.dumps(session_state, indent=2))

    # Inject memory context to stdout for Claude Code to consume
    if memory_context:
        for filename, content in memory_context.items():
            print(f"\n## Memory: {filename}\n{content}")

    # Drain any deferred extraction markers left by previous session_end
    # hooks. SessionEnd is kill-within-seconds, so the heavy LLM
    # extraction is intentionally deferred here where the SessionStart
    # hook has more headroom.
    extract_script = paths.scripts_dir() / "extract_learnings.py"
    try:
        processed = _process_deferred_extractions(data_dir, extract_script)
        if processed:
            logger.info("Launched %d deferred extraction(s)", processed)
    except Exception:
        logger.exception("Deferred extraction processing failed (non-fatal)")

    # Dream gate: emit a nudge when the 24h window has elapsed and
    # fresh learnings are waiting. The nudge is additionalContext only —
    # the actual dream still runs via /multiplai:dream when Spike
    # chooses.
    dream_state_file = paths.dream_state_file()
    learnings_file = paths.learnings_file()
    if (
        _dream_gate_open(dream_state_file)
        and _learnings_pending(learnings_file, dream_state_file)
    ):
        logger.info("Dream gate open with pending learnings; emitting nudge")
        _emit_dream_nudge()

    logger.info("Session started: %s (loaded %d memory files)", session_id, len(memory_context))


if __name__ == "__main__":
    main()
