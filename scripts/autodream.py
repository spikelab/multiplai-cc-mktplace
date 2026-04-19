"""AutoDream consolidation script for multiplai plugin.

Consolidates learnings into memory file updates using LLM synthesis.
Uses path resolver for all file locations, model client for LLM calls.
Processes multiple memory files concurrently via asyncio.gather.
Dream state is persisted as YAML in the plugin data directory.
"""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.venv_guard import ensure_venv_python
ensure_venv_python()

from lib.paths import get_paths
from lib.model_client import create_client
from lib.config import load_yaml, save_yaml
from lib.log_utils import setup_logging

logger = setup_logging("autodream")


def _read_pending_learnings(learnings_file: Path) -> str:
    """Read pending learnings text, returning empty string if none."""
    if not learnings_file.exists():
        return ""
    return learnings_file.read_text().strip()


async def _update_memory_file(
    client, memory_file: Path, learnings: str
) -> str | None:
    """Use LLM to consolidate learnings into a single memory file."""
    if not memory_file.exists():
        return None

    current_content = memory_file.read_text()
    system = (
        "You are a memory consolidation assistant. Given the current memory file "
        "and new learnings, produce an updated version of the memory file that "
        "integrates relevant learnings. Preserve existing structure and content. "
        "Return the full updated file content."
    )
    messages = [
        {
            "role": "user",
            "content": (
                f"## Current memory file ({memory_file.name}):\n{current_content}\n\n"
                f"## New learnings to integrate:\n{learnings}"
            ),
        }
    ]

    try:
        response = await client.query(system=system, messages=messages)
        return response.content
    except Exception:
        logger.exception("Failed to update %s", memory_file.name)
        return None


async def dream() -> None:
    """Consolidate accumulated learnings into memory file updates."""
    paths = get_paths()
    learnings_file = paths.learnings_file()
    memory_dir = paths.memory_dir()
    dream_state_file = paths.dream_state_file()

    # Load dream state to check last run timestamp
    state = load_yaml(dream_state_file)

    learnings = _read_pending_learnings(learnings_file)
    if not learnings:
        logger.info("No pending learnings to consolidate")
        return

    try:
        client = await create_client()
        logger.info("AutoDream using %s", type(client).__name__)

        # Find memory files to update concurrently
        memory_files = list(memory_dir.iterdir()) if memory_dir.exists() else []
        memory_files = [f for f in memory_files if f.suffix == ".md" and f.name != "learnings.md"]

        if not memory_files:
            logger.info("No memory files found to update")
            return

        # Process multiple memory files concurrently via asyncio.gather
        tasks = [
            _update_memory_file(client, mf, learnings)
            for mf in memory_files
        ]
        results = await asyncio.gather(*tasks)

        # Write updated memory files back
        updated_count = 0
        for memory_file, updated_content in zip(memory_files, results):
            if updated_content:
                with open(memory_file, "w") as f:
                    f.write(updated_content)
                updated_count += 1
                logger.info("Updated %s", memory_file.name)

        # Update dream state with current timestamp after successful run
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        state["learnings_processed"] = len(learnings.splitlines())
        state["files_updated"] = updated_count
        save_yaml(dream_state_file, state)

        logger.info(
            "AutoDream complete: %d files updated, dream state saved", updated_count
        )
    except Exception:
        logger.exception("AutoDream consolidation failed")
        raise


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="AutoDream consolidation")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check for pending learnings without running consolidation",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Run the consolidation pipeline",
    )
    args = parser.parse_args()

    if args.check:
        learnings = _read_pending_learnings(get_paths().learnings_file())
        if not learnings:
            print("No pending learnings to consolidate")
            return
        print(f"Pending learnings: {len(learnings.splitlines())} lines ready for consolidation")
        return

    # Default behavior or --run: run consolidation
    asyncio.run(dream())


if __name__ == "__main__":
    main()
