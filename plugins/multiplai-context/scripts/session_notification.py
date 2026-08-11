"""Notification hook for multiplai plugin.

Single, fast job: stamp this session's registry entry with a
``notification`` event (see lib/session_registry.py — the hub input
contract). A Notification fires when Claude Code is waiting for user
input, which is exactly the hub's push-notification trigger: the session
board flips the session to ``waiting_input`` and the phone gets pinged.

Deliberately does nothing else — no LLM calls, no state migration. With
no hub installed the registry file is simply never read.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.paths import get_paths
from multiplai_core.log_utils import hook_run, setup_logging
from lib.hook_input import read_hook_input

logger = setup_logging("session_notification")


def main() -> None:
    hook_input = read_hook_input()

    # A subagent / nested hook session must not stamp the user's session
    # registry — its "waiting for input" is not the user's (M7).
    from lib.checkpoint import is_child_session

    if is_child_session(hook_input.get("transcript_path") or ""):
        return

    session_id = hook_input.get("session_id") or ""
    setup_logging("session_notification", session_id=session_id)

    with hook_run("session_notification", logger, session_id=session_id) as run:
        from lib import session_registry

        with run.stage("record"):
            session_registry.record_event(
                get_paths().plugin_data(), hook_input, "notification"
            )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook must never crash the user's session — log and exit cleanly.
        try:
            logger.exception("session_notification hook failed; exiting cleanly")
        except Exception:
            pass
        sys.exit(0)
