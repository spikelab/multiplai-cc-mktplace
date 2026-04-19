"""Session stop hook for multiplai plugin.

Runs extract-learnings when Claude Code finishes a response.
Reads session context and triggers learning extraction.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.venv_guard import ensure_venv_python
ensure_venv_python()

from lib.config import read_session_state
from lib.paths import get_paths
from lib.log_utils import setup_logging

logger = setup_logging("session_stop")


def _read_stdin_context() -> str:
    """Read session context from stdin if available (hook input)."""
    if not sys.stdin.isatty():
        try:
            return sys.stdin.read()
        except Exception as e:
            logger.debug("Could not read stdin context: %s", e)
    return ""


def _extract_learnings(session_state: dict, context: str) -> list[str]:
    """Extract learnings from the session context.

    Returns a list of learning strings. If no actionable learnings
    are found, returns an empty list to avoid mutating the learnings file.
    """
    if not session_state and not context:
        logger.info("No session context available — nothing to extract")
        return []

    # Placeholder: in the full port, this would use ModelClient
    # to identify actionable learnings from the session transcript
    logger.info("Learning extraction triggered for session %s",
                session_state.get("session_id", "unknown"))
    return []


def main() -> None:
    paths = get_paths()
    data_dir = paths.plugin_data()

    # Read session state for context
    session_state = read_session_state(data_dir) or {}

    # Read any stdin context from the hook
    context = _read_stdin_context()

    # Extract learnings from session
    learnings = _extract_learnings(session_state, context)

    # Only write if there are actual learnings
    if learnings:
        learnings_file = paths.learnings_file()
        learnings_file.parent.mkdir(parents=True, exist_ok=True)
        with open(learnings_file, "a") as f:
            for learning in learnings:
                f.write(f"- {learning}\n")
        logger.info("Wrote %d learnings to %s", len(learnings), learnings_file)
    else:
        logger.info("No actionable learnings found — file not modified")

    logger.info("Session stop hook completed")


if __name__ == "__main__":
    main()
