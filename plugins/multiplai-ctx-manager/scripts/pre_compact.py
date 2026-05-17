"""PreCompact hook for multiplai plugin.

Conversation context is about to be compacted, so the full transcript
may not survive. Heavy LLM extraction can't run inline in a hook, so
this enqueues a deferred extraction marker (same mechanism as
session_end.py) pointing at the pre-compaction transcript. The next
session_start.py drains it through extract_learnings.py, capturing
learnings/diary before they're lost to compaction.
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
from lib.log_utils import setup_logging, log_event

logger = setup_logging("pre_compact")


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
    data_dir = paths.plugin_data()
    session_state = read_session_state(data_dir) or {}

    transcript_path = hook_input.get("transcript_path", "")
    if not transcript_path:
        logger.info("PreCompact: no transcript_path in payload — nothing to defer")
        return

    session_id = (
        session_state.get("session_id")
        or hook_input.get("session_id")
        or "unknown"
    )
    marker = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": hook_input.get("cwd", session_state.get("cwd", "")),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trigger": "pre_compact",
    }

    pending_dir = data_dir / "pending_extractions"
    pending_dir.mkdir(parents=True, exist_ok=True)
    # Distinct name so a PreCompact marker never overwrites the SessionEnd
    # marker for the same session (and vice versa).
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    marker_path = pending_dir / f"precompact-{session_id}-{stamp}.json"
    tmp = marker_path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(marker, indent=2))
        os.replace(str(tmp), str(marker_path))
        logger.info("PreCompact: wrote deferred extraction marker %s", marker_path)
        log_event(
            "session", "precompact",
            "context compacting — queued deferred extraction to preserve learnings",
            session_id=session_id,
        )
    except OSError:
        logger.exception("PreCompact: failed to write deferred extraction marker")


if __name__ == "__main__":
    main()
