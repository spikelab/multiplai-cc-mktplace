"""Extract learnings script for multiplai plugin.

Extracts learnings from session interactions and appends to learnings file.
Uses model client for LLM summarization, path resolver for file locations.

Runs as a Stop hook — reads session transcript from stdin JSON and uses
the LLM to identify actionable learnings worth preserving.
"""

import asyncio
import fcntl
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.venv_guard import ensure_venv_python
ensure_venv_python()

from lib.paths import get_paths
from lib.model_client import create_client
from lib.log_utils import setup_logging

logger = setup_logging("extract_learnings")

SYSTEM_PROMPT = """You are a learning extraction assistant. Analyze the session transcript
and identify actionable technical learnings — patterns, gotchas, decisions,
or insights that would be valuable to remember for future sessions.

Return learnings as a markdown list. Each learning should be a concise,
self-contained bullet point. If there are no actionable learnings, return
exactly the text "NO_LEARNINGS".
"""


async def extract() -> None:
    """Extract actionable learnings from the current session transcript."""
    paths = get_paths()
    learnings_file = paths.learnings_file()

    # Read session transcript from stdin (hook input)
    hook_input = sys.stdin.read()
    if not hook_input.strip():
        logger.info("No session data on stdin, skipping extraction")
        return

    transcript_data: dict = {}
    try:
        transcript_data = json.loads(hook_input)
        transcript = transcript_data.get("transcript", hook_input)
    except (json.JSONDecodeError, AttributeError):
        transcript = hook_input
    session_id = transcript_data.get("session_id", "") if isinstance(transcript_data, dict) else ""

    try:
        client = await create_client()
        logger.info("Extract learnings using %s", type(client).__name__)

        messages = [{"role": "user", "content": transcript}]
        response = await client.query(
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        learnings = response.content.strip()
    except Exception:
        logger.exception("LLM call failed during learning extraction")
        return

    # Guard: do not write if no learnings were produced
    if not learnings or "NO_LEARNINGS" in learnings:
        logger.info("No actionable learnings found, nothing to append")
        return

    # Append learnings to the file (never overwrite). Serialize concurrent writes
    # with an advisory flock and skip writing if this session already appended
    # (prevents doubles when the Stop hook fires twice or a session force-stops
    # mid-extraction).
    learnings_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = learnings_file.parent / f".{learnings_file.name}.lock"

    with open(lock_file, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            if session_id and learnings_file.exists():
                existing = learnings_file.read_text()
                if f"[session:{session_id}]" in existing:
                    logger.info(
                        "Session %s already has learnings in %s, skipping",
                        session_id, learnings_file,
                    )
                    return

            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            header_marker = f" [session:{session_id}]" if session_id else ""
            header = f"\n---\n## Session Learnings — {timestamp}{header_marker}\n"
            with open(learnings_file, "a") as f:
                f.write(header)
                f.write(f"{learnings}\n")
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)

    logger.info("Appended learnings to %s", learnings_file)


def main() -> None:
    asyncio.run(extract())


if __name__ == "__main__":
    main()
