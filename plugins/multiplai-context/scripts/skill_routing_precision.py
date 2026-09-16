"""Report skill-routing precision: suggested skills vs. skills actually invoked.

Joins the ``ROUTING ... skills=[...]`` lines the context manager already
writes against the Skill tool calls and slash commands in each session's
transcript. See ``lib/skill_precision.py`` for the join rules.

    # human table, for /multiplai-context:memory-health-audit
    uv run --project scripts scripts/skill_routing_precision.py

    # last 7 days, machine-readable
    uv run --project scripts scripts/skill_routing_precision.py --days 7 --json

    # every log on disk
    uv run --project scripts scripts/skill_routing_precision.py --days 0

Reads only. Logs come from ``paths.logs_dir()`` (``$CLAUDE_PLUGIN_DATA/logs``)
and transcripts from ``$CLAUDE_CONFIG_DIR/projects/``; both can be overridden.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.paths import get_paths

from lib.costing_collector import default_config_dir
from lib.skill_precision import render, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=30,
                        help="window in days (default 30; 0 = all logs)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    parser.add_argument("--logs-dir", type=Path, default=None,
                        help="override the context_manager log directory")
    parser.add_argument("--config-dir", type=Path, default=None,
                        help="override CLAUDE_CONFIG_DIR (transcripts live under projects/)")
    parser.add_argument("--limit", type=int, default=25, help="per-skill rows to print")
    args = parser.parse_args(argv)

    logs_dir = args.logs_dir or get_paths().logs_dir()
    config_dir = args.config_dir or default_config_dir()
    days = args.days if args.days > 0 else None

    report = run(logs_dir, config_dir, days=days)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render(report, limit=args.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
