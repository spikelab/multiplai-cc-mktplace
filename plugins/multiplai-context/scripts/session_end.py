"""Session end hook for multiplai plugin.

Two jobs, both of them a few milliseconds of work:

1. Save a deferred *extraction* marker for a later drain to pick up.
   Narrative diary entries are written by extract_learnings.py, not here —
   Claude Code kills SessionEnd hooks within a few seconds, so an inline
   LLM call would be interrupted mid-flight.
2. Save the session's working state before it is discarded. Nothing used to
   run on this edge at all, so ``/clear`` threw away everything since the
   last checkpoint.

Job 2 splits on ``reason``, and the split is not cosmetic:

* ``clear`` / ``resume`` — **the container survives.** Verified in the field:
  the pre- and post-``/clear`` halves of one tab (sessions ``24c0a766`` and
  ``2e29e3cb``, one second apart) share hostname ``claude-work-04221854``. So
  the detached ``checkpoint_writer.py`` outlives this hook and can take the
  minutes it needs. Spawning costs milliseconds.
* everything else (``logout``, ``prompt_input_exit``, ``other``, …) — **the
  container is exiting.** It runs under ``docker run --rm``, so when PID 1
  goes the detached child goes with it. Spawning here would look like it
  worked and produce nothing. Queue a marker instead and let the host-side
  drain (``drain_extractions.py``, run by the launcher after the container
  exits) do the write.

A missing ``reason`` is treated as ``"other"`` — the safe half of the split,
because a queued marker survives either way and a killed spawn does not.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.config import read_session_state
from multiplai_core.paths import get_paths
from multiplai_core.log_utils import hook_run, setup_logging, log_event

logger = setup_logging("session_end")

# Reasons for which the session container keeps running, so a detached child
# survives this hook. Everything else must go through the queue.
CONTAINER_SURVIVES_REASONS = frozenset({"clear", "resume"})


def _save_deferred_marker(
    data_dir: Path,
    session_state: dict,
    hook_input: dict,
) -> None:
    """Persist a marker file describing work the next SessionStart should run.

    Marker schema:
        - session_id:       id of the session that just ended
        - transcript_path:  absolute path to the transcript file (if
                            provided by Claude Code in the hook input)
        - cwd:              working directory of the ended session
        - timestamp:        UTC ISO-8601 timestamp
    """
    pending_dir = data_dir / "pending_extractions"
    pending_dir.mkdir(parents=True, exist_ok=True)

    # The hook input's session_id is authoritative for which session is
    # ending. The shared session_state.json may currently hold a *different*
    # concurrent session's id (last writer wins), so trusting it here would
    # file this session's marker under the wrong id — clobbering the other
    # session's marker and losing this one's diary/learnings extraction.
    session_id = hook_input.get("session_id") or session_state.get("session_id") or "unknown"
    marker = {
        "session_id": session_id,
        "transcript_path": hook_input.get("transcript_path", ""),
        "cwd": hook_input.get("cwd", session_state.get("cwd", "")),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    marker_path = pending_dir / f"{session_id}.json"
    tmp = marker_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(marker, indent=2))
    os.replace(str(tmp), str(marker_path))
    logger.info("Wrote deferred extraction marker: %s", marker_path)
    log_event(
        "session", "end",
        "session ended — queued deferred extraction for next startup",
        session_id=session_id,
    )


def _checkpoint_on_end(data_dir: Path, hook_input: dict, reason: str) -> str:
    """Save working state as the session ends. Returns what it did.

    One of ``"spawned"`` (a detached writer is running), ``"queued"`` (a
    marker is waiting for the host drain) or ``"skipped"`` (nothing to save,
    or a writer is already in flight). Never raises — this is a hook.
    """
    from lib import checkpoint as cp

    cfg = cp.load_config()
    if not cfg.enabled:
        return "skipped"

    session_id = hook_input.get("session_id") or ""
    transcript_path = hook_input.get("transcript_path") or ""
    cwd = hook_input.get("cwd") or ""
    if not session_id or not transcript_path:
        return "skipped"
    if cp.is_child_session(transcript_path):
        return "skipped"

    state = cp.load_state(data_dir, session_id)
    tokens = cp.read_context_tokens(transcript_path, after_ts=state.get("rebuild_ts"))
    if tokens <= 0:
        # An unreadable tail is not evidence the session is empty, and the
        # count is only bookkeeping here (bands and the rebuild banner), so
        # fall back to whatever the last write recorded rather than declining.
        tokens = int(state.get("last_checkpoint_tokens") or 0)

    payload = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": cwd,
        "tokens": tokens,
        # Forced: the band/refresh/stale triggers are a cadence question and
        # this is the last chance to write at all.
        "reason": f"session-end:{reason}",
        # This hook runs inside the session's own container, so it is the last
        # place that knows which one that was. The host drain writes the
        # rebuild pointer from a throwaway container minutes later, and keying
        # the pointer to *that* hostname orphaned it permanently (#182).
        "hostname": cp.session_hostname(),
    }

    if reason in CONTAINER_SURVIVES_REASONS:
        if cp.writer_inflight(data_dir, session_id):
            logger.info("Writer already in flight for %s; not spawning again", session_id)
            return "skipped"
        cp.claim_writer(data_dir, session_id)
        if cp.spawn_writer(payload):
            logger.info("Spawned end-of-session checkpoint writer (reason=%s)", reason)
            log_event(
                "checkpoint", "spawn",
                f"checkpoint writer launched on session end ({reason})",
                session_id=session_id, tokens=tokens, reason=reason,
            )
            return "spawned"
        cp.release_writer(data_dir, session_id)
        return "skipped"

    from lib.checkpoint_drain import queue_pending_checkpoint

    queue_pending_checkpoint(data_dir, payload)
    logger.info("Queued end-of-session checkpoint (reason=%s)", reason)
    log_event(
        "checkpoint", "queue",
        f"queued a checkpoint for the host drain ({reason}) — "
        "the container is exiting, so a detached writer would be killed",
        session_id=session_id, tokens=tokens, reason=reason,
    )
    return "queued"


def main() -> None:
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

    paths = get_paths()
    session_state = read_session_state(paths.plugin_data()) or {}

    # Bind the logger to the session id BEFORE anything logs. Binding it inside
    # _save_deferred_marker meant the checkpoint half of this hook — which runs
    # first — printed the `session:--------` placeholder, so `grep session:<id>`
    # silently omitted it and correlating a queued checkpoint with its session
    # meant matching timestamps against the adjacent line (#184).
    session_id = (
        hook_input.get("session_id") or session_state.get("session_id") or "unknown"
    )
    setup_logging("session_end", session_id=session_id)

    with hook_run("session_end", logger, session_id=session_id) as run:
        reason = str(hook_input.get("reason") or "other").strip().lower() or "other"
        run.note(reason=reason)

        # Hub session registry: mark the session ended (adoptable / GC-able).
        with run.stage("registry"):
            try:
                from lib import session_registry

                session_registry.record_event(paths.plugin_data(), hook_input, "end")
            except Exception:
                logger.warning("Session registry end-event failed", exc_info=True)

        # Save working state BEFORE queueing extraction: on ``/clear`` the tab
        # keeps running and the sooner the writer starts the less of the tail it
        # can miss.
        with run.stage("checkpoint"):
            try:
                _checkpoint_on_end(paths.plugin_data(), hook_input, reason)
            except Exception:
                logger.exception("End-of-session checkpoint failed (non-fatal)")

        with run.stage("marker"):
            try:
                _save_deferred_marker(paths.plugin_data(), session_state, hook_input)
            except Exception:
                logger.exception("Failed to write deferred extraction marker")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook must never crash the user's session (e.g. disk full, corrupt
        # state) — log and exit cleanly. Matches the guard on the sibling hooks.
        try:
            logger.exception("session_end hook failed; exiting cleanly")
        except Exception:
            pass
        sys.exit(0)
