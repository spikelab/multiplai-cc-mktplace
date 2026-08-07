"""Checkpoint handoff nudge (UserPromptSubmit hook).

The Stop hook's ``systemMessage`` tells the *user* a handoff is due; this
hook tells *Claude*. When the session is at/above the handoff threshold, it
emits a one-line additionalContext so the model can finish the current
piece of work cleanly and suggest ``/clear`` at a natural boundary instead
of sailing past the budget.

Deliberately tiny and fast: one transcript-tail read, no LLM, no state
mutation beyond its own cooldown file. Emits nothing in the common case
(below threshold), for child sessions, or when checkpointing is disabled.

**Advisory by default, enforcing on request.** With
``checkpoint_hard_stop_tokens`` set, a session past that threshold stops
accepting new prompts (``decision: block``) until the user hands off. The
option exists because advice is not a guardrail: with native auto-compaction
disabled, nothing else sits between the handoff threshold and the model's
real context ceiling, and a session that sails past it degrades in exactly
the way the checkpoint system exists to prevent.

Three carve-outs keep the block from being a trap — a hook that can refuse
every prompt must leave a way out that does not require editing config from
inside the blocked session:

* slash commands pass through, so ``/clear`` and ``/compact`` — the two
  things the block is asking for — always work;
* ``!keepgoing`` anywhere in a prompt overrides for one refresh band of
  growth, for the case where finishing the thought matters more;
* any failure in this hook falls through to "do not block".
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.paths import get_paths
from multiplai_core.log_utils import setup_logging, log_event
from lib import checkpoint as cp

logger = setup_logging("checkpoint_nudge")


def _cooldown_ok(data_dir: Path, session_id: str, tokens: int, step: int) -> bool:
    """At most one nudge per *step* tokens of growth (own bookkeeping file).

    Merges rather than overwrites: ``claude_nudge.json`` also carries the
    hard-stop override watermark, and a nudge on the same prompt that set it
    would otherwise erase it.
    """
    cfile = cp.session_dir(data_dir, session_id) / "claude_nudge.json"
    payload = _read_state(cfile)
    try:
        last = int(payload.get("tokens") or 0)
    except (ValueError, TypeError):
        last = 0
    if last and tokens - last < step:
        return False
    payload["tokens"] = tokens
    _write_state(cfile, payload)
    return True


def _read_state(cfile: Path) -> dict:
    """Best-effort read of the nudge bookkeeping file; garbage reads as {}."""
    try:
        payload = json.loads(cfile.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(cfile: Path, payload: dict) -> None:
    try:
        cfile.parent.mkdir(parents=True, exist_ok=True)
        cfile.write_text(json.dumps(payload))
    except OSError:
        pass


_OVERRIDE_TOKEN = "!keepgoing"


def _override_ok(data_dir: Path, session_id: str, tokens: int, step: int) -> bool:
    """True when a recent ``!keepgoing`` still covers this prompt.

    Scoped to one refresh band of growth rather than the rest of the
    session: an override that never expires is a disabled feature with extra
    steps, and re-typing it every single prompt is the other failure mode.
    """
    cfile = cp.session_dir(data_dir, session_id) / "claude_nudge.json"
    try:
        at = int(_read_state(cfile).get("override_tokens") or 0)
    except (ValueError, TypeError):
        return False
    return bool(at) and tokens - at < step


def _record_override(data_dir: Path, session_id: str, tokens: int) -> None:
    """Stamp the override watermark, preserving the nudge cooldown."""
    cfile = cp.session_dir(data_dir, session_id) / "claude_nudge.json"
    payload = _read_state(cfile)
    payload["override_tokens"] = tokens
    _write_state(cfile, payload)


def _hard_stop(
    data_dir: Path, session_id: str, prompt: str, tokens: int, cfg
) -> bool:
    """Block this prompt? Emits the block JSON as a side effect when True.

    Only reached above ``handoff_tokens``; returns False whenever the hard
    stop is unset, carved out, or overridden.
    """
    if not cfg.hard_stop_tokens or tokens < cfg.hard_stop_tokens:
        return False
    # /clear and /compact are the way out of the block — never block a slash
    # command, or the wall has no door.
    if prompt.lstrip().startswith("/"):
        return False
    if _OVERRIDE_TOKEN in prompt:
        _record_override(data_dir, session_id, tokens)
        logger.info("Hard stop overridden at %d tokens for %s", tokens, session_id)
        return False
    if _override_ok(data_dir, session_id, tokens, cfg.refresh_tokens):
        return False

    has_checkpoint = cp.checkpoint_file(data_dir, session_id).exists()
    state = (
        "This session's work state is checkpointed and will be restored "
        "automatically in the next one"
        if has_checkpoint
        else "A checkpoint is being written now and will be restored in the "
        "next session"
    )
    print(json.dumps({
        "decision": "block",
        "reason": (
            f"Context hard stop: {tokens:,} tokens (limit "
            f"{cfg.hard_stop_tokens:,}). {state} — run /clear to hand off, or "
            f"/compact to stay in this session. To continue here anyway, "
            f"include {_OVERRIDE_TOKEN} in your prompt."
        ),
    }))
    logger.info("Hard stop blocked a prompt at %d tokens for %s", tokens, session_id)
    log_event(
        "checkpoint", "hard_stop",
        f"prompt blocked at {tokens:,} tokens (limit {cfg.hard_stop_tokens:,})",
        session_id=session_id, tokens=tokens,
    )
    return True


def main() -> None:
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except Exception:
        hook_input = {}
    if not isinstance(hook_input, dict):
        return

    cfg = cp.load_config()
    if not cfg.enabled:
        return

    session_id = hook_input.get("session_id") or ""
    setup_logging("checkpoint_nudge", session_id=session_id)
    transcript_path = hook_input.get("transcript_path") or ""
    if not session_id or not transcript_path:
        return
    if cp.is_child_session(transcript_path):
        return

    data_dir = get_paths().plugin_data()
    state = cp.load_state(data_dir, session_id)
    tokens = cp.read_context_tokens(transcript_path, after_ts=state.get("rebuild_ts"))
    if tokens < cfg.handoff_tokens:
        return

    # Enforcement before advice: a blocked prompt is never also nudged, and
    # the block outranks auto mode (if compaction were going to save this
    # session it would have fired by now).
    if _hard_stop(data_dir, session_id, hook_input.get("prompt") or "", tokens, cfg):
        return

    # Auto mode: steered auto-compaction + SessionStart(compact) re-injection
    # handles the rebuild with no action from Claude or the user. Stay silent
    # unless compaction is overdue (misconfigured/disabled).
    auto_trigger = cp.autocompact_trigger_tokens()
    if auto_trigger is not None and tokens < auto_trigger + cfg.refresh_tokens:
        return

    if not _cooldown_ok(data_dir, session_id, tokens, cfg.refresh_tokens):
        return

    has_checkpoint = cp.checkpoint_file(data_dir, session_id).exists()
    state = (
        "A checkpoint of this session's state is saved and refreshes automatically"
        if has_checkpoint
        else "A checkpoint of this session's state is being written"
    )
    if auto_trigger is not None:
        advice = (
            "Auto-compaction should have rebuilt this context by now but has "
            "not fired. Finish the current piece of work cleanly, then run "
            "/compact (the checkpoint re-injects automatically afterwards) "
            "and mention the auto-compact env vars may be misconfigured."
        )
    else:
        advice = (
            "After /clear or /compact this project's context is re-seeded "
            "from it. Finish the current piece of work cleanly, then suggest "
            "the user run /clear at the next natural stopping point."
        )
    print(
        f"--- CONTEXT BUDGET ---\n"
        f"This session is at {tokens:,} context tokens (handoff threshold: "
        f"{cfg.handoff_tokens:,}). {state}. {advice} Do not abandon or rush "
        f"in-flight work because of this notice."
    )
    logger.info("Handoff nudge emitted at %d tokens for %s", tokens, session_id)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            logger.exception("checkpoint_nudge failed; exiting cleanly")
        except Exception:
            pass
        sys.exit(0)
