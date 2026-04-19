"""Synthesize now script for multiplai plugin.

Reads diary entries, learnings, and memory files to produce a synthesis.
Uses path resolver for file locations, model client for LLM calls.
This is the manual dream trigger invoked by /multiplai:dream.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.venv_guard import ensure_venv_python
ensure_venv_python()

from lib.paths import get_paths
from lib.model_client import create_client
from lib.config import read_memory_files
from lib.log_utils import setup_logging

logger = setup_logging("synthesize_now")


def _read_diary_entries(diary_dir: Path) -> str:
    """Read all diary entries from the diary directory."""
    if not diary_dir.exists():
        logger.info("Diary directory does not exist: %s", diary_dir)
        return ""

    entries = []
    for entry_file in sorted(diary_dir.glob("*.md")):
        try:
            entries.append(entry_file.read_text())
        except OSError:
            logger.warning("Could not read diary entry: %s", entry_file)
    return "\n\n---\n\n".join(entries)


async def synthesize() -> None:
    """Read diary entries, learnings, and memory files to produce a synthesis."""
    paths = get_paths()
    diary_dir = paths.diary_dir()
    memory_dir = paths.memory_dir()
    learnings_file = paths.learnings_file()

    # Read all input sources
    diary_content = _read_diary_entries(diary_dir)
    memory_files = read_memory_files(memory_dir, exclude={"learnings.md"})

    learnings_content = ""
    if learnings_file.exists():
        learnings_content = learnings_file.read_text()

    if not diary_content and not learnings_content:
        logger.info("No diary entries or learnings to synthesize")
        return

    try:
        client = await create_client()
        logger.info("Synthesize now using %s", type(client).__name__)

        # Build synthesis prompt with all inputs
        memory_section = "\n\n".join(
            f"### {name}\n{content}" for name, content in memory_files.items()
        )
        messages = [
            {
                "role": "user",
                "content": (
                    f"## Current Memory Files:\n{memory_section}\n\n"
                    f"## Recent Diary Entries:\n{diary_content}\n\n"
                    f"## Accumulated Learnings:\n{learnings_content}\n\n"
                    "Synthesize these inputs into updated memory file contents. "
                    "Return the updated content for each memory file."
                ),
            }
        ]

        response = await client.query(
            system="You are a memory synthesis assistant. Consolidate diary entries "
                   "and learnings into updated memory files, preserving structure.",
            messages=messages,
        )

        # Write synthesis output to memory directory
        output_file = memory_dir / "synthesis.md"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w") as f:
            f.write(response.content)

        logger.info("Synthesis written to %s", output_file)
    except Exception:
        logger.exception("Synthesis failed")
        raise


def main() -> None:
    asyncio.run(synthesize())


if __name__ == "__main__":
    main()
