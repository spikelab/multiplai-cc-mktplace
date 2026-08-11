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
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.config import read_session_state, write_session_state
from multiplai_core.paths import get_paths
from multiplai_core.log_utils import hook_run, setup_logging, log_event
from lib import checkpoint as cp
from lib.hook_input import read_hook_input

logger = setup_logging("session_stop")


def _spawn_writer(payload: dict) -> bool:
    """Launch the detached checkpoint writer (never awaited).

    Thin alias over :func:`lib.checkpoint.spawn_writer`, which is where the
    spawn moved so ``session_end.py`` can make the identical call on
    ``/clear``. Kept as a name here because the tests patch it.
    """
    return cp.spawn_writer(payload)


def _degraded_file(data_dir: Path, session_id: str) -> Path:
    # Same reasoning as _nudge_file: the detached writer owns state.json, so
    # this hook keeps its "have I already said this?" bookkeeping beside it,
    # never inside it.
    return cp.session_dir(data_dir, session_id) / "degraded.json"


def _degraded_message(data_dir: Path, session_id: str, state: dict) -> str | None:
    """Tell the user when checkpoint writes keep failing.

    The failure that started this: eight consecutive writer timeouts over 18
    hours, every one logged to a file nobody was tailing, and the first anyone
    knew was a ``/clear`` that restored the wrong session. A component whose
    job is not losing work must not fail silently.

    Said once per new failure, not once per Stop — the count only moves when a
    writer actually finishes badly.

    The high-water mark below is reset the moment a write succeeds. Without
    that reset it only ever ratchets up, so the *second* run of failures in a
    session's life is silently swallowed (``already`` 2 >= ``failures`` 2) and
    every later incident needs a strictly longer run than the last to be heard
    — which is the alerting guarantee quietly disappearing over time.
    """
    failures = cp.consecutive_failures(state)
    dfile = _degraded_file(data_dir, session_id)
    if failures == 0:
        # A write succeeded (the writer zeroes the counter only then). Drop the
        # mark so the next run of failures starts from zero and is heard.
        try:
            dfile.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    if failures < cp.DEGRADED_ALERT_AFTER:
        return None
    try:
        already = int(json.loads(dfile.read_text()).get("failures") or 0)
    except (OSError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
        already = 0
    if already >= failures:
        return None
    try:
        dfile.parent.mkdir(parents=True, exist_ok=True)
        dfile.write_text(json.dumps({"failures": failures}))
    except OSError:
        pass

    log_event(
        "checkpoint", "degraded",
        f"{failures} consecutive checkpoint writes degraded — "
        f"{state.get('last_failure', 'unknown cause')}",
        session_id=session_id,
        failures=failures,
    )
    kept = cp.checkpoint_file(data_dir, session_id).exists()
    return (
        f"[multiplai] The last {failures} checkpoint writes for this session "
        f"failed ({state.get('last_failure', 'unknown cause')}). "
        + (
            "The checkpoint on disk is being kept up to date with the raw "
            "unsummarised turns, so a /clear still restores something — but "
            "it is degraded."
            if kept
            else "There is no checkpoint on disk for this session yet."
        )
        + " See checkpoint_writer.log under the plugin's log directory."
    )


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
        # Unknown context size declines to fire — and this gate sits before
        # the staleness check, so it blocks the age-based trigger too: the
        # writer payload needs a real token count. Same philosophy as
        # staleness_trigger's unknown-age handling — no evidence is not
        # evidence of staleness, and doing nothing is the safe default.
        return None

    reason = cp.checkpoint_trigger(tokens, state, cfg)
    # Age-based fallback. The band triggers above are token-based, so a tab
    # that sat at 40K tokens for three days has no checkpoint at all — and
    # that is exactly the tab whose state you have lost track of.
    reason = reason or cp.staleness_trigger(data_dir, session_id, state, cfg)

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

    # Keep the rebuild pointer alive independently of whether *this* run's
    # write succeeds. A checkpoint that exists but has no pointer is
    # unreachable — that is how a clean 143K-token session left nothing
    # restorable — and a stale checkpoint that restores beats a fresh one that
    # does not exist.
    if cp.checkpoint_file(data_dir, session_id).exists():
        try:
            cp.write_pending_marker(data_dir, cwd, session_id, tokens)
        except OSError:
            logger.warning("Could not refresh pending marker for %s", session_id)

    degraded = _degraded_message(data_dir, session_id, state)
    if degraded:
        # Preferred over the handoff nudge when both are due: "your saves are
        # failing" is the more urgent of the two, and the hook emits at most
        # one systemMessage per Stop.
        return degraded

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
    # Read the hook payload (transcript path, session id, cwd) — needed for
    # the checkpoint pass. Read defensively; garbage means "skip checkpoint".
    hook_input = read_hook_input()

    # When another Stop hook blocked the stop, the harness re-runs the chain
    # with stop_hook_active set. This hook already did its passes on the first
    # run, and a systemMessage from the second is discarded anyway (P4).
    if hook_input.get("stop_hook_active"):
        return

    paths = get_paths()
    data_dir = paths.plugin_data()

    session_state = read_session_state(data_dir) or {}
    session_id = session_state.get("session_id", "unknown")

    setup_logging("session_stop", session_id=hook_input.get("session_id") or session_id)

    with hook_run(
        "session_stop", logger,
        session_id=hook_input.get("session_id") or session_id,
    ) as run:
        if session_state:
            session_state["last_stop"] = datetime.now(timezone.utc).isoformat()
            # Atomic temp+rename (shared with context_manager / session_start) so
            # a crash mid-write never leaves a half-written state file.
            # session_state was read-merged above, so turn_index /
            # recently_injected survive.
            with run.stage("state"):
                if not write_session_state(data_dir, session_state):
                    logger.debug("Could not update session_state.json")

        # Hub session registry: a Stop means the turn finished — the session is
        # idle and safe to adopt. Best-effort, never blocks the Stop.
        with run.stage("registry"):
            try:
                from lib import session_registry

                session_registry.record_event(data_dir, hook_input, "stop")
            except Exception:
                logger.warning("Session registry stop-event failed", exc_info=True)

        # Context checkpoint pass — advisory only, never blocks the Stop. This
        # is the stage that can queue real work, so it is the one worth timing:
        # Stop has a 15s ceiling and fires after every single turn.
        system_message: str | None = None
        with run.stage("checkpoint"):
            try:
                system_message = _checkpoint_pass(hook_input, data_dir)
            except Exception:
                logger.exception("Checkpoint pass failed (non-fatal)")

        run.note(message="yes" if system_message else "no")
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
