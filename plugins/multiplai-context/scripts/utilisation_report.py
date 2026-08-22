"""Render the memory utilisation table from `data/utilisation.jsonl`.

Every number here is an **estimate**, from one of two model-based estimators
with opposite biases, and the renderer says so on every surface. That is a
contract, not a stylistic choice: see the master plan's decision 9 and
`lib/utilisation.py`.

    # human table, for /multiplai-context:memory-health-audit
    uv run --project scripts scripts/utilisation_report.py

    # the same data as JSON — this is what P4/P5 consume
    uv run --project scripts scripts/utilisation_report.py --json

Nothing here prunes, edits, or proposes. The table is evidence; a human
disposes.

**The judge column is not the whole judge history, on purpose.** On
2026-08-16 the judge's extended thinking switched off as a side effect of an
unrelated change, and per-section credit moved from 2.8% to 14.5% on a fixed
subset with the prompt held constant. Verdicts either side of that are readings
from two instruments, and no stored record from before the change says which
one produced it — so only verdicts carrying an instrument stamp count toward
`judge`, and the rest are reported beside it as `legacy_judge`, never merged.
`--json` consumers MUST NOT sum or average the two; the human table prints a
warning naming how many sessions are held aside. See
`lib/utilisation.JUDGE_INSTRUMENT_CHANGED_AT`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.paths import get_paths

from lib.utilisation import (
    DISAGREEMENT_MARGIN,
    MIN_OBSERVATIONS,
    build_table,
    catalog_keys,
    read_records,
    render_table,
    utilisation_path,
)


def _load_catalog(catalogs_dir: Path) -> dict:
    path = Path(catalogs_dir) / "memory.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def build(args: argparse.Namespace) -> dict:
    paths = get_paths()
    data_dir = Path(args.data_dir) if args.data_dir else paths.data_dir()
    catalogs_dir = Path(args.catalogs_dir) if args.catalogs_dir else paths.catalogs_dir()
    records = read_records(utilisation_path(data_dir))
    return build_table(
        records,
        known_keys=catalog_keys(_load_catalog(catalogs_dir)),
        min_observations=args.min_observations,
        margin=args.margin,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the ESTIMATED memory utilisation table "
                    "(retrieved vs estimated-used, from two independent estimators)",
    )
    parser.add_argument("--json", action="store_true",
                        help="emit the table as JSON (the machine contract) "
                             "instead of the human table")
    parser.add_argument("--limit", type=int, default=25,
                        help="rows to show per section in the human table (default 25)")
    parser.add_argument("--min-observations", type=int, default=MIN_OBSERVATIONS,
                        help=f"estimator observations below which a row is not "
                             f"ranked (default {MIN_OBSERVATIONS})")
    parser.add_argument("--margin", type=float, default=DISAGREEMENT_MARGIN,
                        help=f"use-rate gap past which the two estimators are "
                             f"marked as disagreeing (default {DISAGREEMENT_MARGIN})")
    parser.add_argument("--data-dir", default="",
                        help="override the runtime data directory")
    parser.add_argument("--catalogs-dir", default="",
                        help="override the catalogs directory (for the "
                             "never-retrieved list)")
    args = parser.parse_args(argv)

    table = build(args)
    if args.json:
        print(json.dumps(table, indent=2))
    else:
        print(render_table(table, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
