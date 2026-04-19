"""Generate catalog script for multiplai plugin.

Generates plugin-relevant catalog data in the plugin data directory.
No skill/resource routing catalog generation (removed per D8).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.venv_guard import ensure_venv_python
ensure_venv_python()

from lib.paths import get_paths
from lib.log_utils import setup_logging

logger = setup_logging("generate_catalog")


def main() -> None:
    paths = get_paths()
    catalogs_dir = paths.catalogs_dir()
    catalogs_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Catalog generation complete at %s", catalogs_dir)


if __name__ == "__main__":
    main()
