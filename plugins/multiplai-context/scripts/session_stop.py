# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core @ git+https://github.com/spikelab/multiplai-core@v0.5.0"]
# ///
"""Stop hook for multiplai plugin.

Lightweight end-of-response checkpoint. Learning/diary extraction is
NOT performed here: it calls the model client, which is too slow for a
Stop hook and would be interrupted. Extraction is deferred — session_end.py
writes a marker that the next session_start.py drains into
extract_learnings.py.

This hook does two fast things:

1. Refreshes a liveness timestamp so health checks can tell the session
   is active.
2. **Context checkpointing** (lib/checkpoint.py): reads the session's
   current context size from the transcript tail and, when a token band
   is crossed (default 100K/200K) or a marathon session grows past the
   refresh step above the handoff threshold, spawns a *detached*
   ``checkpoint_writer.py``. At/above the handoff threshold it emits a
   ``systemMessage`` advising the user to ``/clear`` — the next
   SessionStart in the same project re-seeds the fresh session from the
   checkpoint.

Goal-safety invariants: this hook NEVER emits a ``decision`` (so it can
never block a Stop and never fights /goal loops), never runs an LLM call
inline, and skips child sessions (subagents / nested hook sessions)
entirely.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.config import read_session_state
from multiplai_core.paths import get_paths
from multiplai_core.log_utils import setup_logging, log_event
from lib import checkpoint as cp

logger = setup_logging("session_stop")


def _spawn_writer(payload: dict) -> bool:
    """Launch the detached checkpoint writer (never awaited)."""
    script = get_paths().scripts_dir() / "checkpoint_writer.py"
    if not script.exists():
        logger.warning("checkpoint_writer.py missing at %s", script)
        return False
    try:
        proc = subprocess.Popen(
            ["uv", "run", "--no-project", str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if proc.stdin is not None:
            proc.stdin.write(json.dumps(payload).encode("utf-8"))
            proc.stdin.close()
        return True
    except Exception:
        logger.exception("Failed to launch checkpoint writer")
        return False


def _nudge_file(data_dir: Path, session_id: str) -> Path:
    # Separate from state.json: the detached writer owns state.json; keeping
    # nudge bookkeeping apart avoids read-modify-write races between the two.
    return cp.session_dir(data_dir, session_id) / "nudge.json"


def _should_nudge(data_dir: Path, session_id: str, tokens: int, cfg) -> bool:
    """Nudge at first handoff crossing, then every ``refresh_tokens`` growth."""
    nfile = _nudge_file(data_dir, session_id)
    try:
        last = int(json.loads(nfile.read_text()).get("tokens") or 0)
    except (OSError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
        last = 0
    if last and tokens - last < cfg.refresh_tokens:
        return False
    try:
        nfile.parent.mkdir(parents=True, exist_ok=True)
        nfile.write_text(json.dumps({"tokens": tokens}))
    except OSError:
        pass
    return True


def _checkpoint_pass(hook_input: dict, data_dir: Path) -> str | None:
    """Run the checkpoint decision for this turn.

    Returns a user-facing systemMessage when handoff-ready, else None.
    Fully best-effort: any failure is logged and swallowed.
    """
    cfg = cp.load_config()
    if not cfg.enabled:
        return None

    session_id = hook_input.get("session_id") or ""
    setup_logging("session_stop", session_id=session_id)
    transcript_path = hook_input.get("transcript_path") or ""
    cwd = hook_input.get("cwd") or ""
    if not session_id or not transcript_path:
        return None
    if cp.is_child_session(transcript_path):
        return None

    state = cp.load_state(data_dir, session_id)
    tokens = cp.read_context_tokens(transcript_path, after_ts=state.get("rebuild_ts"))
    if tokens <= 0:
        return None

    reason = cp.checkpoint_trigger(tokens, state, cfg)

    if reason and not cp.writer_inflight(data_dir, session_id):
        cp.claim_writer(data_dir, session_id)
        spawned = _spawn_writer(
            {
                "session_id": session_id,
                "transcript_path": transcript_path,
                "cwd": cwd,
                "tokens": tokens,
                "reason": reason,
            }
        )
        if not spawned:
            cp.release_writer(data_dir, session_id)
        else:
            logger.info(
                "Checkpoint writer spawned (%s) at %d tokens for %s",
                reason, tokens, session_id,
            )
            log_event(
                "checkpoint", "spawn",
                f"checkpoint writer launched at {tokens:,} tokens ({reason})",
                session_id=session_id,
                tokens=tokens,
                reason=reason,
            )

    if tokens < cfg.handoff_tokens:
        return None

    # Auto mode: when the runtime steers native auto-compaction to fire near
    # the handoff threshold, the rebuild is fully automatic (compaction +
    # SessionStart source="compact" re-injection) — don't nag the user.
    # Only speak up if we've sailed PAST the expected trigger (compaction
    # disabled or misconfigured) by a full refresh step.
    auto_trigger = cp.autocompact_trigger_tokens()
    if auto_trigger is not None and tokens < auto_trigger + cfg.refresh_tokens:
        return None

    if not _should_nudge(data_dir, session_id, tokens, cfg):
        return None
    has_checkpoint = cp.checkpoint_file(data_dir, session_id).exists()
    status = (
        "work state is checkpointed and will restore automatically"
        if has_checkpoint
        else "a checkpoint is being written now"
    )
    if auto_trigger is not None:
        return (
            f"[multiplai] Context at {tokens:,} tokens but auto-compaction "
            f"(expected near {auto_trigger:,}) hasn't fired — check "
            f"CLAUDE_CODE_AUTO_COMPACT_WINDOW/CLAUDE_AUTOCOMPACT_PCT_OVERRIDE "
            f"or run /compact; {status}."
        )
    return (
        f"[multiplai] Context at {tokens:,} tokens (handoff threshold "
        f"{cfg.handoff_tokens:,}). Run /clear or /compact when convenient — "
        f"{status} in the rebuilt context for this project."
    )


def main() -> None:
    paths = get_paths()
    data_dir = paths.plugin_data()

    session_state = read_session_state(data_dir) or {}
    session_id = session_state.get("session_id", "unknown")

    # Read the hook payload (transcript path, session id, cwd) — needed for
    # the checkpoint pass. Read defensively; garbage means "skip checkpoint".
    hook_input: dict = {}
    if not sys.stdin.isatty():
        try:
            hook_input = json.loads(sys.stdin.read() or "{}")
        except Exception:
            hook_input = {}
    if not isinstance(hook_input, dict):
        hook_input = {}

    if session_state:
        session_state["last_stop"] = datetime.now(timezone.utc).isoformat()
        try:
            (data_dir / "session_state.json").write_text(
                json.dumps(session_state, indent=2)
            )
        except OSError as e:
            logger.debug("Could not update session_state.json: %s", e)

    # Context checkpoint pass — advisory only, never blocks the Stop.
    system_message: str | None = None
    try:
        system_message = _checkpoint_pass(hook_input, data_dir)
    except Exception:
        logger.exception("Checkpoint pass failed (non-fatal)")

    if system_message:
        # NOTE: deliberately no "decision" key — this hook must never block.
        print(json.dumps({"systemMessage": system_message}))

    logger.info("Stop hook completed for session %s", session_id)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook must never crash the user's session — log and exit cleanly.
        try:
            logger.exception("session_stop hook failed; exiting cleanly")
        except Exception:
            pass
        sys.exit(0)
