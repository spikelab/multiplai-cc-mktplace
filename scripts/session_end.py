"""Session end hook for multiplai plugin.

Handles session cleanup and learning consolidation when a session ends.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.venv_guard import ensure_venv_python
ensure_venv_python()

from lib.config import read_session_state
from lib.paths import get_paths
from lib.log_utils import setup_logging

logger = setup_logging("session_end")


def _write_diary_entry(diary_dir: Path, session_state: dict) -> None:
    """Write a session summary to the diary directory."""
    diary_dir.mkdir(parents=True, exist_ok=True)

    entry = {
        **session_state,
        "end_time": datetime.now(timezone.utc).isoformat(),
    }
    session_id = entry.get("session_id", "unknown")
    summary_file = diary_dir / f"session-{session_id}.json"
    summary_file.write_text(json.dumps(entry, indent=2))

    logger.info("Session ended, summary written to %s", summary_file)


def main() -> None:
    paths = get_paths()
    session_state = read_session_state(paths.plugin_data())
    if session_state is not None:
        _write_diary_entry(paths.diary_dir(), session_state)
    else:
        logger.warning("No session_state.json found — skipping diary entry")


if __name__ == "__main__":
    main()
