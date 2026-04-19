"""Extract learnings script for multiplai plugin.

Extracts learnings from session interactions and appends to learnings file.
Uses model client for LLM summarization, path resolver for file locations.

Runs as a Stop hook — reads session transcript from stdin JSON and uses
the LLM to identify actionable learnings worth preserving.
"""

import asyncio
import json
import sys
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

    try:
        transcript_data = json.loads(hook_input)
        transcript = transcript_data.get("transcript", hook_input)
    except (json.JSONDecodeError, AttributeError):
        transcript = hook_input

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

    # Append learnings to the file (never overwrite)
    learnings_file.parent.mkdir(parents=True, exist_ok=True)
    with open(learnings_file, "a") as f:
        f.write(f"\n{learnings}\n")

    logger.info("Appended learnings to %s", learnings_file)


def main() -> None:
    asyncio.run(extract())


if __name__ == "__main__":
    main()
