# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core @ git+https://github.com/spikelab/multiplai-core@v0.4.0"]
# ///
"""Write memory files from templates during onboarding.

Copies template files to the user's memory directory, skipping any
files that already exist (copy-if-absent logic per D7).

Usage:
    python scripts/setup_write.py [--force]

Output (JSON to stdout):
    {"copied": ["technical-pref.md", "preferences.md"], "skipped": ["me.md"]}
"""

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.config import TEMPLATE_FILENAMES
from multiplai_core.paths import get_paths


def write_memory_files(force: bool = False) -> dict:
    """Copy templates to memory_dir, skipping files that already exist.

    Uses the path resolver for both templates_dir (source) and
    memory_dir (destination). Creates memory_dir if it does not exist.

    Args:
        force: If True, overwrite existing files. Default is False.

    Returns:
        Dict with 'copied' and 'skipped' lists.
    """
    paths = get_paths()
    templates_dir = paths.templates_dir
    memory_dir = paths.memory_dir

    # Create memory directory if it does not exist
    memory_dir.mkdir(parents=True, exist_ok=True)

    copied = []
    skipped = []

    for fname in TEMPLATE_FILENAMES:
        src = templates_dir / fname
        dst = memory_dir / fname

        if dst.exists() and not force:
            skipped.append(fname)
            continue

        if not src.is_file():
            continue

        shutil.copy2(src, dst)
        copied.append(fname)

    return {
        "memory_dir": str(memory_dir),
        "templates_dir": str(templates_dir),
        "copied": copied,
        "skipped": skipped,
    }


def main() -> None:
    force = "--force" in sys.argv
    result = write_memory_files(force=force)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
