#!/usr/bin/env python3
"""Report the rules memory already held that a session learned again.

Read-only. Reads ``.multiplai/data/rejections.jsonl`` (what the judge refused)
and ``.multiplai/data/utilisation.jsonl`` (what the router injected and whether
it was used), and prints the ``## Rules Re-learned`` section. Writes nothing,
touches no memory file, and is safe to run at any time.

The reasoning — why a redundant drop is a retention signal rather than
housekeeping, and what separates the three fixes — is in ``lib.relearns``.

Usage::

    relearn_report.py                     # rules only, top 20
    relearn_report.py --limit 0           # every group
    relearn_report.py --all-kinds         # facts and decisions too
    relearn_report.py --json              # machine-readable
    relearn_report.py --since 2026-08-01  # ignore older drops
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from multiplai_core.paths import get_paths  # noqa: E402

from lib import rejections, relearns  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rules memory already held that a session derived again.",
    )
    parser.add_argument(
        "--limit", type=int, default=20,
        help="rows to print; 0 for all (default: 20)",
    )
    parser.add_argument(
        "--all-kinds", action="store_true",
        help="include FACT and DECISION drops, not just RULE",
    )
    parser.add_argument(
        "--since", metavar="YYYY-MM-DD", default="",
        help="ignore drops logged before this date",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="emit JSON instead of the markdown section",
    )
    parser.add_argument(
        "--rejections", type=Path, default=None,
        help="override the rejection log path (testing)",
    )
    parser.add_argument(
        "--utilisation", type=Path, default=None,
        help="override the utilisation log path (testing)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    paths = get_paths()
    data_dir = paths.data_dir()
    rejections_path = args.rejections or rejections.default_path(data_dir)
    utilisation_path = args.utilisation or (data_dir / "utilisation.jsonl")

    records = rejections.read(rejections_path)
    if args.since:
        records = [r for r in records if str(r.get("ts", "")) >= args.since]
    rows = relearns.read_utilisation(utilisation_path)

    groups = relearns.analyse(
        records, rows, kinds=None if args.all_kinds else ("RULE",)
    )

    if args.as_json:
        print(json.dumps([
            {
                "count": g.count,
                "target": g.target,
                "title": g.title,
                "verdict": g.verdict,
                "evidence": g.evidence,
                "dates": g.dates,
                "occurrences": [
                    {"title": o.title, "source": o.source, "ts": o.ts,
                     "kind": o.kind, "judge_reason": o.judge_reason}
                    for o in g.occurrences
                ],
            }
            for g in groups
        ], indent=2, ensure_ascii=False))
        return 0

    limit = len(groups) if args.limit == 0 else args.limit
    print(relearns.render_section(groups, limit=limit), end="")
    if not rows:
        print(
            "\n_No utilisation telemetry was readable, so every verdict is "
            "inconclusive by construction._"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
