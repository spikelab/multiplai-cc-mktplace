# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core @ git+https://github.com/spikelab/multiplai-core@v0.8.1"]
# ///
"""Notification hook for multiplai plugin.

Single, fast job: stamp this session's registry entry with a
``notification`` event (see lib/session_registry.py — the hub input
contract). A Notification fires when Claude Code is waiting for user
input, which is exactly the hub's push-notification trigger: the session
board flips the session to ``waiting_input`` and the phone gets pinged.

Deliberately does nothing else — no LLM calls, no state migration. With
no hub installed the registry file is simply never read.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.paths import get_paths
from multiplai_core.log_utils import setup_logging

logger = setup_logging("session_notification")


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
    setup_logging("session_notification", session_id=hook_input.get("session_id") or "")

    from lib import session_registry

    session_registry.record_event(get_paths().plugin_data(), hook_input, "notification")


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
