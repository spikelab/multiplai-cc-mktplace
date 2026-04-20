"""Generate catalog script for multiplai plugin.

Unified entry point for catalog generation. Invokes the catalog dispatcher
with support for --force, --dry-run, and --only CLI flags.

Usage:
    python scripts/generate_catalog.py
    python scripts/generate_catalog.py --force
    python scripts/generate_catalog.py --dry-run
    python scripts/generate_catalog.py --only diary
    python scripts/generate_catalog.py --only memory,diary --force
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.venv_guard import ensure_venv_python
ensure_venv_python()

from lib.paths import get_paths
from lib.log_utils import setup_logging
from generators.config import CatalogConfig, load_catalog_config
from generators.dispatcher import generate_catalogs

logger = setup_logging("generate_catalog")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Catalog generation dispatcher")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force regeneration, bypassing state-aware skipping",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be generated without writing files",
    )
    parser.add_argument(
        "--only",
        type=str,
        default=None,
        help="Comma-separated list of generators to run (e.g., memory,diary)",
    )
    args = parser.parse_args()

    paths = get_paths()
    catalogs_dir = paths.catalogs_dir()
    catalogs_dir.mkdir(parents=True, exist_ok=True)

    config = load_catalog_config()

    generator_filter = None
    if args.only:
        generator_filter = [g.strip() for g in args.only.split(",")]

    try:
        results = asyncio.run(
            generate_catalogs(
                config=config,
                generators=generator_filter,
                force=args.force,
                dry_run=args.dry_run,
            )
        )
    except ValueError as e:
        logger.error("Invalid generator filter: %s", e)
        sys.exit(1)

    # Report results
    has_errors = False
    for r in results:
        status = "dry-run" if r.dry_run else "complete"
        logger.info(
            "%s: %s (sources=%d, generated=%d, skipped=%d, pruned=%d, errors=%d)",
            r.generator, status, r.total_sources, r.generated,
            r.skipped, r.pruned, len(r.errors),
        )
        if r.errors:
            has_errors = True
            for err in r.errors:
                logger.error("  %s: %s", r.generator, err)

    logger.info("Catalog generation complete at %s", catalogs_dir)

    if has_errors and not args.dry_run:
        sys.exit(1)


if __name__ == "__main__":
    main()
