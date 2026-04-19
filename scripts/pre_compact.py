"""Pre-compact hook for multiplai plugin.

Runs context preservation before Claude Code compacts conversation context.
Extracts and saves pending learnings so they survive compaction.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.venv_guard import ensure_venv_python
ensure_venv_python()

from lib.paths import get_paths
from lib.log_utils import setup_logging

logger = setup_logging("pre_compact")


def main() -> None:
    """Extract and preserve learnings before context compaction."""
    paths = get_paths()

    logger.info("Pre-compact hook fired — preserving context before compaction")

    # Trigger extract_learnings to flush any pending learnings to disk
    extract_script = paths.scripts_dir() / "extract_learnings.py"
    if extract_script.exists():
        try:
            subprocess.run(
                [sys.executable, str(extract_script)],
                timeout=10,
                capture_output=True,
                stdin=subprocess.DEVNULL,
            )
            logger.info("Learning extraction triggered before compaction")
        except subprocess.TimeoutExpired:
            logger.warning("Learning extraction timed out during pre-compact")
        except Exception:
            logger.warning("Could not run learning extraction during pre-compact")

    # Write a preservation marker so post-compact can detect what was saved
    diary_dir = paths.diary_dir()
    diary_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Context preservation complete")


if __name__ == "__main__":
    main()
