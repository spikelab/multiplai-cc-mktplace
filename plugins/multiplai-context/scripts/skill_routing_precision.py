"""Report skill-routing precision: suggested skills vs. skills actually invoked.

Reads each main-session transcript under ``$CLAUDE_CONFIG_DIR/projects/``:
the ``UserPromptSubmit`` hook attachment on every prompt says which skills the
context hook suggested, and the Skill tool calls and slash commands that
follow say which were used. See ``lib/skill_precision.py`` for the join rules.

    # human table, for /multiplai-context:memory-health-audit
    uv run --project scripts scripts/skill_routing_precision.py

    # last 7 days, and keep the machine-readable copy from the same run
    uv run --project scripts scripts/skill_routing_precision.py --days 7 --json-out out.json

    # every transcript on disk
    uv run --project scripts scripts/skill_routing_precision.py --days 0

Reads only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.costing_collector import default_config_dir
from lib.skill_precision import render, run


def _days(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("--days must be 0 (all transcripts) or a positive number")
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report skill-routing precision: suggested skills vs. skills actually invoked."
    )
    parser.add_argument("--days", type=_days, default=30,
                        help="window in days (default 30; 0 = all transcripts)")
    parser.add_argument("--json", action="store_true", help="emit JSON on stdout instead of markdown")
    parser.add_argument("--json-out", type=Path, default=None,
                        help="also write the JSON report to this file (one run, both outputs)")
    parser.add_argument("--config-dir", type=Path, default=None,
                        help="override CLAUDE_CONFIG_DIR (transcripts live under projects/)")
    parser.add_argument("--limit", type=int, default=25, help="per-skill rows to print")
    args = parser.parse_args(argv)

    config_dir = args.config_dir or default_config_dir()
    report = run(config_dir, days=args.days)
    payload = report.to_dict()
    if args.json_out is not None:
        args.json_out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render(report, limit=args.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
