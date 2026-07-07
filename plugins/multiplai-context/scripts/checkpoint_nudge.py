# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core @ git+https://github.com/spikelab/multiplai-core@v0.5.0"]
# ///
"""Checkpoint handoff nudge (UserPromptSubmit hook).

The Stop hook's ``systemMessage`` tells the *user* a handoff is due; this
hook tells *Claude*. When the session is at/above the handoff threshold, it
emits a one-line additionalContext so the model can finish the current
piece of work cleanly and suggest ``/clear`` at a natural boundary instead
of sailing past the budget.

Deliberately tiny and fast: one transcript-tail read, no LLM, no state
mutation beyond its own cooldown file. Emits nothing in the common case
(below threshold), for child sessions, or when checkpointing is disabled.
Never blocks a prompt.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.paths import get_paths
from multiplai_core.log_utils import setup_logging
from lib import checkpoint as cp

logger = setup_logging("checkpoint_nudge")


def _cooldown_ok(data_dir: Path, session_id: str, tokens: int, step: int) -> bool:
    """At most one nudge per *step* tokens of growth (own bookkeeping file)."""
    cfile = cp.session_dir(data_dir, session_id) / "claude_nudge.json"
    try:
        last = int(json.loads(cfile.read_text()).get("tokens") or 0)
    except (OSError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
        last = 0
    if last and tokens - last < step:
        return False
    try:
        cfile.parent.mkdir(parents=True, exist_ok=True)
        cfile.write_text(json.dumps({"tokens": tokens}))
    except OSError:
        pass
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
