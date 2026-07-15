# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core @ git+https://github.com/spikelab/multiplai-core@v0.8.1"]
# ///
"""Check for existing memory files before onboarding.

Reports which memory files already exist in the configured memory directory
so the setup skill can decide whether to skip, warn, or proceed.

Usage:
    python scripts/setup_check.py

Output (JSON to stdout):
    {"existing": ["me.md"], "missing": ["technical-pref.md", "preferences.md"]}
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.config import TEMPLATE_FILENAMES
from multiplai_core.paths import get_paths


def check_memory_files() -> dict:
    """Check which memory files exist and which are missing.

    Returns a dict with 'existing' and 'missing' lists, plus
    'memory_dir' for informational purposes.
    """
    paths = get_paths()
    memory_dir = paths.memory_dir

    existing = []
    missing = []

    for fname in TEMPLATE_FILENAMES:
        fpath = memory_dir / fname
        if fpath.is_file():
            existing.append(fname)
        else:
            missing.append(fname)

    return {
        "memory_dir": str(memory_dir),
        "existing": existing,
        "missing": missing,
        "all_present": len(missing) == 0,
    }


def main() -> None:
    result = check_memory_files()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
