# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core @ file:///Users/spike/Documents/knowhere/PROJECTS/multiplai-core"]
# ///
"""Stop hook for multiplai plugin.

Lightweight end-of-response checkpoint. Learning/diary extraction is
NOT performed here: it calls the model client, which is too slow for a
Stop hook and would be interrupted. Extraction is deferred — session_end.py
writes a marker that the next session_start.py drains into
extract_learnings.py. This hook only refreshes a liveness timestamp so
health checks can tell the session is active.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.config import read_session_state
from multiplai_core.paths import get_paths
from multiplai_core.log_utils import setup_logging

logger = setup_logging("session_stop")


def main() -> None:
    paths = get_paths()
    data_dir = paths.plugin_data()

    session_state = read_session_state(data_dir) or {}
    session_id = session_state.get("session_id", "unknown")

    # Drain stdin so Claude Code's hook pipe never blocks; the payload is
    # not needed here (extraction is deferred to the SessionEnd path).
    if not sys.stdin.isatty():
        try:
            sys.stdin.read()
        except Exception:
            pass

    if session_state:
        session_state["last_stop"] = datetime.now(timezone.utc).isoformat()
        try:
            (data_dir / "session_state.json").write_text(
                json.dumps(session_state, indent=2)
            )
        except OSError as e:
            logger.debug("Could not update session_state.json: %s", e)

    logger.info("Stop hook completed for session %s", session_id)


if __name__ == "__main__":
    main()
