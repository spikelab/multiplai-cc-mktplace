# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core @ git+https://github.com/spikelab/multiplai-core@v0.6.0"]
# ///
"""Report on the multiplai cost ledger.

Reads the monthly JSONL ledgers written by ``collect_costs.py`` (transcripts)
and the agent_runner SDK tap, and prints aggregate or per-session views.

Usage::

    python scripts/costs_report.py                       # month-to-date summary
    python scripts/costs_report.py --month 2026-06
    python scripts/costs_report.py --since 2026-06-15
    python scripts/costs_report.py --by session|project|model|day|skill|component|branch
    python scripts/costs_report.py --session <id-prefix>  # itemized chat bill
    python scripts/costs_report.py --branch <name>        # one branch's bill
    python scripts/costs_report.py --json                 # machine-readable

PR costing stays out of this script by design — resolve the PR's branch via
``gh`` (see the costs SKILL.md recipe), then ``--branch <that-branch>``.
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.costing import costs_dir, iter_ledger


def _load(args) -> list[dict]:
    months = None
    if args.month:
        months = [args.month]
    elif not args.since and not args.session and not args.branch and not args.all:
        months = [datetime.now(timezone.utc).strftime("%Y-%m")]
    records = list(iter_ledger(months))
    if args.since:
        records = [r for r in records if r.get("ts", "") >= args.since]
    return records


def _group(records: list[dict], key_fn) -> list[tuple[str, float, int]]:
    totals: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for r in records:
        key = key_fn(r)
        if key is None:
            continue
        totals[key] += r.get("cost_usd", 0.0)
        counts[key] += 1
    return sorted(((k, v, counts[k]) for k, v in totals.items()), key=lambda x: -x[1])


_GROUPERS = {
    "session": lambda r: r.get("session", "")[:8],
    "project": lambda r: r.get("project") or "(none)",
    "model": lambda r: r.get("model", "?"),
    "day": lambda r: str(r.get("ts", ""))[:10],
    "component": lambda r: r.get("component") or ("interactive" if r.get("source") == "transcript" else "(sdk)"),
    "skill": lambda r: (r.get("span") or {}).get("name") if (r.get("span") or {}).get("kind") == "skill" else None,
    "branch": lambda r: r.get("branch") or "(none)",
}


def _print_table(rows: list[tuple[str, float, int]], header: str, limit: int = 20) -> None:
    print(f"\n{header:<42} {'cost':>10} {'calls':>7}")
    print("-" * 61)
    for key, cost, count in rows[:limit]:
        print(f"{key:<42} ${cost:>9.2f} {count:>7}")
    if len(rows) > limit:
        rest = sum(c for _, c, _ in rows[limit:])
        print(f"{'… ' + str(len(rows) - limit) + ' more':<42} ${rest:>9.2f}")


def _session_report(records: list[dict], prefix: str, as_json: bool) -> int:
    recs = [r for r in records if r.get("session", "").startswith(prefix)]
    sessions = {r["session"] for r in recs}
    if not sessions:
        print(f"No ledger records for session prefix {prefix!r}", file=sys.stderr)
        return 1
    if len(sessions) > 1:
        print(f"Ambiguous prefix — matches: {', '.join(s[:12] for s in sorted(sessions))}",
              file=sys.stderr)
        return 1

    total = sum(r["cost_usd"] for r in recs)
    main = sum(r["cost_usd"] for r in recs if not r.get("sidechain"))
    side = total - main
    spans: dict[str, float] = defaultdict(float)
    for r in recs:
        span = r.get("span")
        if span:
            spans[f"{span.get('kind')}:{span.get('name')}"] += r["cost_usd"]
    models = _group(recs, _GROUPERS["model"])

    if as_json:
        print(json.dumps({
            "session": sorted(sessions)[0], "records": len(recs),
            "total_usd": round(total, 4), "main_usd": round(main, 4),
            "subagents_usd": round(side, 4),
            "spans": {k: round(v, 4) for k, v in sorted(spans.items(), key=lambda x: -x[1])},
            "models": {k: round(v, 4) for k, v, _ in models},
        }, indent=2))
        return 0

    print(f"Session {sorted(sessions)[0]}")
    print(f"  total:      ${total:.2f}  ({len(recs)} API calls)")
    print(f"  main:       ${main:.2f}")
    print(f"  subagents:  ${side:.2f}")
    if spans:
        print("  spans (approx for skills):")
        for k, v in sorted(spans.items(), key=lambda x: -x[1]):
            print(f"    {k:<38} ${v:.2f}")
    _print_table(models, "model")
    return 0


def _branch_report(records: list[dict], branch: str, as_json: bool) -> int:
    recs = [r for r in records if _GROUPERS["branch"](r) == branch]
    if not recs:
        print(f"No ledger records for branch {branch!r}", file=sys.stderr)
        return 1

    total = sum(r.get("cost_usd", 0.0) for r in recs)
    main_cost = sum(r.get("cost_usd", 0.0) for r in recs if not r.get("sidechain"))
    side = total - main_cost
    spans: dict[str, float] = defaultdict(float)
    for r in recs:
        span = r.get("span")
        if span:
            spans[f"{span.get('kind')}:{span.get('name')}"] += r.get("cost_usd", 0.0)
    models = _group(recs, _GROUPERS["model"])
    sessions = _group(recs, _GROUPERS["session"])

    if as_json:
        print(json.dumps({
            "branch": branch, "records": len(recs),
            "total_usd": round(total, 4), "main_usd": round(main_cost, 4),
            "subagents_usd": round(side, 4),
            "spans": {k: round(v, 4) for k, v in sorted(spans.items(), key=lambda x: -x[1])},
            "models": {k: round(v, 4) for k, v, _ in models},
            "sessions": {k: round(v, 4) for k, v, _ in sessions},
        }, indent=2))
        return 0

    print(f"Branch {branch}")
    print(f"  total:      ${total:.2f}  ({len(recs)} API calls)")
    print(f"  main:       ${main_cost:.2f}")
    print(f"  subagents:  ${side:.2f}")
    if spans:
        print("  spans (approx for skills):")
        for k, v in sorted(spans.items(), key=lambda x: -x[1]):
            print(f"    {k:<38} ${v:.2f}")
    _print_table(models, "model")
    _print_table(sessions, "session")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", help="YYYY-MM (default: current month)")
    parser.add_argument("--since", help="YYYY-MM-DD lower bound (reads all months)")
    parser.add_argument("--all", action="store_true", help="Read the entire ledger")
    parser.add_argument("--by", choices=sorted(_GROUPERS), help="Group totals by this key")
    parser.add_argument("--session", help="Itemized bill for one session (id prefix ok)")
    parser.add_argument("--branch", help='Itemized bill for one git branch ("(none)" for unattributed)')
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    records = _load(args)
    if args.branch:
        records = [r for r in records if _GROUPERS["branch"](r) == args.branch]
        # --branch + --session: a session that switched branches gets split.
        if args.session:
            return _session_report(records, args.session, args.json)
        return _branch_report(records, args.branch, args.json)
    if args.session:
        return _session_report(records, args.session, args.json)
    if not records:
        print(f"No ledger records found under {costs_dir()} for the selected window.",
              file=sys.stderr)
        return 1

    total = sum(r.get("cost_usd", 0.0) for r in records)
    if args.by:
        rows = _group(records, _GROUPERS[args.by])
        if args.json:
            print(json.dumps({k: round(v, 4) for k, v, _ in rows}, indent=2))
        else:
            print(f"Total: ${total:.2f} across {len(records)} API calls")
            _print_table(rows, args.by)
        return 0

    if args.json:
        print(json.dumps({
            "total_usd": round(total, 4), "records": len(records),
            "by_model": {k: round(v, 4) for k, v, _ in _group(records, _GROUPERS["model"])},
            "by_project": {k: round(v, 4) for k, v, _ in _group(records, _GROUPERS["project"])},
            "top_sessions": {k: round(v, 4) for k, v, _ in _group(records, _GROUPERS["session"])[:10]},
        }, indent=2))
        return 0

    print(f"Total: ${total:.2f} across {len(records)} API calls")
    _print_table(_group(records, _GROUPERS["model"]), "model", limit=10)
    _print_table(_group(records, _GROUPERS["project"]), "project", limit=10)
    _print_table(_group(records, _GROUPERS["session"]), "session (top)", limit=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
