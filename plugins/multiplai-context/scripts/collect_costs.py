# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core @ git+https://github.com/spikelab/multiplai-core@v0.6.0"]
# ///
"""Collect API-call cost records from Claude Code session transcripts.

Incrementally scans ``$CLAUDE_CONFIG_DIR/projects/**/*.jsonl`` and appends
priced records to the monthly cost ledger (``<data_dir>/costs/``). Safe to
run repeatedly — offsets are checkpointed and records dedup against the
ledger, so a full re-run appends nothing new.

Usage::

    python scripts/collect_costs.py [--config-dir PATH] [--dry-run]
    python scripts/collect_costs.py --backfill-branches

First run over a large transcript corpus is a full backfill (minutes);
steady-state passes read only new bytes.

``--backfill-branches`` is a one-time enrichment mode: it re-reads every
transcript from byte 0 (leaving offsets untouched), maps msg_id → git
branch/cwd, and rewrites the monthly ledgers in place to add ``branch``/
``cwd`` to records that lack them. It appends nothing and is idempotent.
"""

import argparse
import fcntl
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.log_utils import setup_logging
from multiplai_core.costing import costs_dir
from lib.costing_collector import default_config_dir, run_backfill_branches, run_collect

logger = setup_logging("costs")

# Single global collector — the ledger and offset state are shared, so two
# concurrent writing passes (e.g. racing SessionStart hooks) could double-append
# records or clobber each other's offsets. A non-blocking exclusive flock makes
# the second launch a no-op. Dry runs don't write, so they skip the lock.
_LOCK_PATH = "/tmp/multiplai-costs-collector.lock"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=None,
                        help="Claude config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan and price, but write neither ledger nor state")
    parser.add_argument("--backfill-branches", action="store_true",
                        help="One-time: add branch/cwd to existing ledger records "
                             "from full transcript re-reads; appends nothing")
    args = parser.parse_args()
    if args.backfill_branches and args.dry_run:
        parser.error("--backfill-branches has no dry-run mode")

    config_dir = args.config_dir or default_config_dir()
    if not (config_dir / "projects").is_dir():
        print(f"No transcripts found: {config_dir}/projects does not exist", file=sys.stderr)
        return 1

    # Serialize writing passes across processes (racing SessionStart hooks).
    # Hold the lock for the whole pass; the fd is released on process exit.
    lock_fd = None
    if not args.dry_run:
        lock_fd = open(_LOCK_PATH, "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("Another cost-collection pass is running; skipping.")
            print("Another cost-collection pass is already running — skipping.")
            return 0

    if args.backfill_branches:
        started = time.monotonic()
        stats = run_backfill_branches(config_dir)
        elapsed = time.monotonic() - started
        logger.info(
            "branch backfill done: %d examined, %d enriched, %d unmatched, %.1fs",
            stats["examined"], stats["enriched"], stats["unmatched"], elapsed,
        )
        print(
            f"Branch backfill: {stats['examined']} records examined, "
            f"{stats['enriched']} enriched, {stats['unmatched']} unmatched "
            f"in {elapsed:.1f}s\nLedger: {costs_dir()}"
        )
        return 0

    state_path = costs_dir() / "collector-state.json"
    started = time.monotonic()
    stats = run_collect(config_dir, state_path, dry_run=args.dry_run)
    elapsed = time.monotonic() - started

    mode = "DRY RUN — " if args.dry_run else ""
    logger.info(
        "collect pass done: %d/%d files read, %d records, $%.2f, %.1fs",
        stats["files_read"], stats["files_seen"], stats["records"],
        stats["cost_usd"], elapsed,
    )
    print(
        f"{mode}{stats['files_read']}/{stats['files_seen']} transcripts read, "
        f"{stats['records']} new records (${stats['cost_usd']:.2f}) in {elapsed:.1f}s\n"
        f"Ledger: {costs_dir()}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
