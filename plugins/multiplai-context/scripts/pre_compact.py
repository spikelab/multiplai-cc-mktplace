# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core @ git+https://github.com/spikelab/multiplai-core@v0.5.0"]
# ///
"""PreCompact hook for multiplai plugin.

Conversation context is about to be compacted, so the full transcript
may not survive. Two jobs:

1. **Synchronous checkpoint** (lib/checkpoint.py): this is the LAST
   chance to capture session state before the window is summarized away.
   Stop-hook band checkpoints can be outrun by a single big turn (field
   log 2026-07-06: one turn jumped ~65K→90K+, compaction fired with only
   the stale 60K checkpoint on disk). Here we run the checkpoint writer
   and WAIT for it, so the SessionStart(source=compact) rebuild always
   injects fresh state. Time-bounded — on timeout/failure compaction
   proceeds with the previous checkpoint (graceful degradation, plus the
   native compaction summary covers the gap).

2. Enqueues a deferred extraction marker (same mechanism as
   session_end.py) pointing at the pre-compaction transcript. The next
   session_start.py drains it through extract_learnings.py, capturing
   learnings/diary before they're lost to compaction.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.config import read_session_state, write_session_state
from multiplai_core.paths import get_paths
from multiplai_core.log_utils import setup_logging, log_event
from lib import checkpoint as cp

logger = setup_logging("pre_compact")

# Poll step while waiting for an already-in-flight band writer to finish.
_INFLIGHT_POLL_S = 2.0


def _sync_checkpoint(hook_input: dict, data_dir) -> None:
    """Write a fresh checkpoint synchronously before compaction.

    Best-effort with a hard time bound (cfg.timeout_s, which also caps the
    writer's own model call). If a detached band writer is already running,
    wait for it instead of double-writing.
    """
    cfg = cp.load_config()
    if not cfg.enabled:
        return
    session_id = hook_input.get("session_id") or ""
    setup_logging("pre_compact", session_id=session_id)
    transcript_path = hook_input.get("transcript_path") or ""
    if not session_id or not transcript_path:
        return
    if cp.is_child_session(transcript_path):
        return

    state = cp.load_state(data_dir, session_id)
    tokens = cp.read_context_tokens(transcript_path, after_ts=state.get("rebuild_ts"))
    if tokens <= 0:
        return

    deadline = time.monotonic() + cfg.timeout_s

    # A band writer may already be mid-flight — let it finish (its result
    # is at most one turn stale) rather than racing it.
    if cp.writer_inflight(data_dir, session_id):
        logger.info("PreCompact: band writer in flight — waiting for it")
        while cp.writer_inflight(data_dir, session_id):
            if time.monotonic() >= deadline:
                logger.warning("PreCompact: in-flight writer didn't finish in time")
                return
            time.sleep(_INFLIGHT_POLL_S)
        log_event(
            "checkpoint", "precompact",
            f"pre-compaction checkpoint ready (band writer, {tokens:,} tokens)",
            session_id=session_id, tokens=tokens,
        )
        return

    script = get_paths().scripts_dir() / "checkpoint_writer.py"
    if not script.exists():
        return
    payload = json.dumps({
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": hook_input.get("cwd", ""),
        "tokens": tokens,
        "reason": "precompact",
    })
    cp.claim_writer(data_dir, session_id)
    try:
        # Synchronous on purpose: compaction is imminent and this state is
        # about to be summarized away. The writer releases the marker itself.
        subprocess.run(
            ["uv", "run", "--no-project", str(script)],
            input=payload.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=max(5.0, deadline - time.monotonic()),
        )
        logger.info("PreCompact: synchronous checkpoint completed at %d tokens", tokens)
        log_event(
            "checkpoint", "precompact",
            f"pre-compaction checkpoint written ({tokens:,} tokens)",
            session_id=session_id, tokens=tokens,
        )
    except subprocess.TimeoutExpired:
        logger.warning("PreCompact: checkpoint writer timed out — compaction proceeds")
    except Exception:
        logger.exception("PreCompact: synchronous checkpoint failed (non-fatal)")
    finally:
        cp.release_writer(data_dir, session_id)


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
    data_dir = paths.plugin_data()
    session_state = read_session_state(data_dir) or {}

    # Compaction summarizes the conversation, so any context the
    # UserPromptSubmit hook injected this session may no longer be
    # present verbatim. Clear the re-recommendation cooldown map so every
    # file becomes eligible again — otherwise a file injected just before
    # compaction would stay suppressed for X turns despite being gone.
    if session_state.get("recently_injected"):
        session_state["recently_injected"] = {}
        if write_session_state(data_dir, session_state):
            logger.info("PreCompact: cleared re-recommendation cooldown map")

    # Fresh checkpoint BEFORE compaction — this is the state the
    # SessionStart(source=compact) rebuild will inject. Never fatal.
    try:
        _sync_checkpoint(hook_input, data_dir)
    except Exception:
        logger.exception("PreCompact: checkpoint pass failed (non-fatal)")

    transcript_path = hook_input.get("transcript_path", "")
    if not transcript_path:
        logger.info("PreCompact: no transcript_path in payload — nothing to defer")
        return

    # Prefer the hook input's session_id: the shared session_state.json may
    # hold a different concurrent session's id, which would misattribute this
    # marker (see session_end.py for the same fix).
    session_id = (
        hook_input.get("session_id")
        or session_state.get("session_id")
        or "unknown"
    )
    marker = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": hook_input.get("cwd", session_state.get("cwd", "")),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trigger": "pre_compact",
    }

    pending_dir = data_dir / "pending_extractions"
    pending_dir.mkdir(parents=True, exist_ok=True)
    # Distinct name so a PreCompact marker never overwrites the SessionEnd
    # marker for the same session (and vice versa).
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    marker_path = pending_dir / f"precompact-{session_id}-{stamp}.json"
    tmp = marker_path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(marker, indent=2))
        os.replace(str(tmp), str(marker_path))
        logger.info("PreCompact: wrote deferred extraction marker %s", marker_path)
        log_event(
            "session", "precompact",
            "context compacting — queued deferred extraction to preserve learnings",
            session_id=session_id,
        )
    except OSError:
        logger.exception("PreCompact: failed to write deferred extraction marker")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook must never crash the user's session — log and exit cleanly.
        try:
            logger.exception("pre_compact hook failed; exiting cleanly")
        except Exception:
            pass
        sys.exit(0)
