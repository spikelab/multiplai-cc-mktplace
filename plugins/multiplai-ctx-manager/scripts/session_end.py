"""Session end hook for multiplai plugin.

Saves a deferred extraction marker for the next SessionStart hook to
pick up. Narrative diary entries are written by extract_learnings.py
(runs deferred via the pending_extractions queue), not here.

Claude Code kills SessionEnd hooks within a few seconds, so learning
extraction (which calls the model client) can't run here — it would
be interrupted mid-LLM-call.
"""

import json
import os
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


def _save_deferred_marker(
    data_dir: Path,
    session_state: dict,
    hook_input: dict,
) -> None:
    """Persist a marker file describing work the next SessionStart should run.

    Marker schema:
        - session_id:       id of the session that just ended
        - transcript_path:  absolute path to the transcript file (if
                            provided by Claude Code in the hook input)
        - cwd:              working directory of the ended session
        - timestamp:        UTC ISO-8601 timestamp
    """
    pending_dir = data_dir / "pending_extractions"
    pending_dir.mkdir(parents=True, exist_ok=True)

    session_id = session_state.get("session_id") or hook_input.get("session_id") or "unknown"
    marker = {
        "session_id": session_id,
        "transcript_path": hook_input.get("transcript_path", ""),
        "cwd": hook_input.get("cwd", session_state.get("cwd", "")),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    marker_path = pending_dir / f"{session_id}.json"
    tmp = marker_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(marker, indent=2))
    os.replace(str(tmp), str(marker_path))
    logger.info("Wrote deferred extraction marker: %s", marker_path)


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
    session_state = read_session_state(paths.plugin_data()) or {}

    try:
        _save_deferred_marker(paths.plugin_data(), session_state, hook_input)
    except Exception:
        logger.exception("Failed to write deferred extraction marker")


if __name__ == "__main__":
    main()
