# /// script
# requires-python = ">=3.11"
# dependencies = ["multiplai-core @ git+https://github.com/spikelab/multiplai-core@v0.8.1"]
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
    python scripts/costs_report.py --group task --pr-join # cost per completed task
    python scripts/costs_report.py --group build --project-dir <path>
    python scripts/costs_report.py --report cache        # cache hit ratios
    python scripts/costs_report.py --json                 # machine-readable

Per-token cost comparisons across models are misleading — a pricier model that
retries less can cost less per finished thing, and tokenizers differ by ~30%
between model generations. That is what ``--group task`` and ``--group build``
are for: they divide spend by *outcomes* (merged PRs, completed build blocks)
rather than by tokens.

Ad-hoc PR costing still works the old way — resolve the PR's branch via ``gh``
(see the costs SKILL.md recipe), then ``--branch <that-branch>``.
"""

import argparse
import json
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from multiplai_core.costing import costs_dir, iter_ledger


def _no_data(message: str, as_json: bool, **fields) -> int:
    """Report "nothing to show" without breaking the caller's parser.

    `--json` is a promise about the output *shape*, and an empty ledger is a
    normal state (a fresh machine, a narrow window), not a usage error. Printing
    only prose left `--json` consumers parsing an empty stdin — which is how CI
    on a runner with no ledger caught this. Exit stays non-zero so shell callers
    can still branch on it.
    """
    if as_json:
        print(json.dumps({"error": message, "rows": [], **fields}, indent=2))
    else:
        print(message, file=sys.stderr)
    return 1


def _load(args) -> tuple[list[dict], str]:
    """Records for the selected window, plus a human-readable window label.

    Owns all record scoping (months, ``--since``, ``--branch``) so the
    report functions never re-filter.
    """
    months = None
    if args.month:
        months = [args.month]
    elif not args.since and not args.session and not args.branch and not args.all:
        months = [datetime.now(timezone.utc).strftime("%Y-%m")]
    window = f"since {args.since}" if args.since else (months[0] if months else "all months")
    records = list(iter_ledger(months))
    if args.since:
        records = [r for r in records if r.get("ts", "") >= args.since]
    if args.branch:
        records = [r for r in records if _GROUPERS["branch"](r) == args.branch]
    return records, window


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


def _tokens(r: dict) -> dict:
    return r.get("tokens") or {}


def _sum_tokens(records: list[dict]) -> dict[str, int]:
    """Token totals for *records*, by tier. Schemaless reads — an old record
    missing a tier contributes zero rather than breaking the report."""
    out = {"in": 0, "out": 0, "cr": 0, "cw5m": 0, "cw1h": 0}
    for r in records:
        t = _tokens(r)
        for k in out:
            out[k] += int(t.get(k, 0) or 0)
    return out


# --- Cache utilization -----------------------------------------------------

# Cached prefixes are the cheapest tokens in the ledger (a cache read prices at
# a tenth of a fresh input token), so a low ratio is money left on the table
# rather than a correctness problem. Industry telemetry puts typical
# utilization near a third of eligible calls, so 50% is a deliberately
# unambitious floor: rows under it are worth a look, not an alarm.
DEFAULT_CACHE_THRESHOLD = 0.5


def cache_stats(records: list[dict]) -> dict:
    """Cache hit ratio and write share for one bucket of records.

    ``hit_ratio = cr / (in + cr)`` — of the tokens that *could* have come from
    a cache, how many did. Cache writes are excluded from the denominator on
    purpose: a write is the cost of establishing a prefix, and counting it as
    a miss would penalize the very call that makes later hits possible.
    """
    tok = _sum_tokens(records)
    eligible = tok["in"] + tok["cr"]
    writes = tok["cw5m"] + tok["cw1h"]
    total = eligible + writes
    return {
        "calls": len(records),
        "cost_usd": round(sum(r.get("cost_usd", 0.0) for r in records), 4),
        "input_tokens": tok["in"],
        "cache_read_tokens": tok["cr"],
        "cache_write_tokens": writes,
        "hit_ratio": round(tok["cr"] / eligible, 4) if eligible else None,
        "write_share": round(writes / total, 4) if total else None,
    }


def cache_report_rows(records: list[dict], key: str) -> list[tuple[str, dict]]:
    """Per-bucket cache stats, worst hit ratio first.

    Buckets with no cache-eligible tokens sort last: a `None` ratio means "no
    evidence", which must not be presented as the worst offender.
    """
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        bucket = _GROUPERS[key](r)
        if bucket is None:
            continue
        buckets[bucket].append(r)
    rows = [(k, cache_stats(v)) for k, v in buckets.items()]
    return sorted(rows, key=lambda kv: (kv[1]["hit_ratio"] is None,
                                        kv[1]["hit_ratio"] or 0.0))


def _cache_report(records: list[dict], key: str, threshold: float,
                  as_json: bool, window: str) -> int:
    rows = cache_report_rows(records, key)
    overall = cache_stats(records)
    flagged = [k for k, s in rows
               if s["hit_ratio"] is not None and s["hit_ratio"] < threshold]

    if as_json:
        print(json.dumps({
            "window": window, "group": key, "threshold": threshold,
            "overall": overall,
            "rows": {k: s for k, s in rows},
            "flagged": flagged,
        }, indent=2))
        return 0

    print(f"Cache utilization by {key}  [{window}]")
    ratio = overall["hit_ratio"]
    print(f"  overall hit ratio: {'n/a' if ratio is None else f'{ratio:.1%}'} "
          f"({overall['cache_read_tokens']:,} cached / "
          f"{overall['input_tokens'] + overall['cache_read_tokens']:,} eligible tokens)")
    print(f"\n{key:<34} {'hit':>7} {'write':>7} {'cached tok':>12} {'cost':>9} {'calls':>6}")
    print("-" * 79)
    for k, s in rows:
        hit = "  n/a" if s["hit_ratio"] is None else f"{s['hit_ratio']:>6.1%}"
        write = "  n/a" if s["write_share"] is None else f"{s['write_share']:>6.1%}"
        mark = " *" if k in flagged else "  "
        print(f"{k[:32]:<34}{hit} {write} {s['cache_read_tokens']:>12,} "
              f"${s['cost_usd']:>8.2f} {s['calls']:>6}{mark}")
    if flagged:
        print(f"\n* below the {threshold:.0%} threshold — check these call paths for "
              f"missing cache_control breakpoints on stable prefixes.")
    return 0


# --- Task outcomes (branch → PR) -------------------------------------------

_PR_CACHE_TTL_S = 3600  # gh is rate-limited and PR state moves slowly.


def _pr_cache_path(repo_dir: Path) -> Path:
    slug = str(repo_dir).strip("/").replace("/", "-")
    return costs_dir() / "pr-cache" / f"{slug}.json"


def fetch_pr_map(repo_dir: Path, *, ttl_s: int = _PR_CACHE_TTL_S,
                 runner=None) -> dict[str, dict]:
    """``{branch: {number, outcome}}`` for one repo, via ``gh``.

    Degrades to an empty map on any failure — no `gh`, no auth, not a repo,
    bad JSON. A cost report must not die because a PR lookup did; the affected
    rows simply report ``no-pr``.
    """
    cache = _pr_cache_path(repo_dir)
    try:
        if cache.exists() and (time.time() - cache.stat().st_mtime) < ttl_s:
            return json.loads(cache.read_text())
    except (OSError, ValueError):
        pass  # unreadable cache is a miss, not an error

    run = runner or (lambda cmd, cwd: subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=60))
    try:
        proc = run(["gh", "pr", "list", "--state", "all", "--limit", "200",
                    "--json", "number,headRefName,mergedAt,closedAt"], str(repo_dir))
        if proc.returncode != 0:
            print(f"note: gh could not list PRs for {repo_dir} "
                  f"({(proc.stderr or '').strip()[:120]}) — rows will read no-pr",
                  file=sys.stderr)
            return {}
        prs = json.loads(proc.stdout or "[]")
    except Exception as e:
        print(f"note: PR lookup failed for {repo_dir} ({e}) — rows will read no-pr",
              file=sys.stderr)
        return {}

    mapping: dict[str, dict] = {}
    for pr in prs:
        branch = pr.get("headRefName")
        if not branch:
            continue
        if pr.get("mergedAt"):
            outcome = "merged"
        elif pr.get("closedAt"):
            outcome = "closed"
        else:
            outcome = "open"
        # Highest PR number wins when a branch was reused: the latest attempt
        # is the one whose outcome describes the work the ledger just paid for.
        prior = mapping.get(branch)
        if prior is None or pr.get("number", 0) > prior["number"]:
            mapping[branch] = {"number": pr.get("number", 0), "outcome": outcome}
    # Don't cache an empty map: on disk it is indistinguishable from a cached
    # "we couldn't find out", so a repo that genuinely has no PRs yet would
    # suppress the lookup for the whole TTL right when its first PR appears.
    if mapping:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(mapping))
        except OSError:
            pass
    return mapping


def _repo_dirs(records: list[dict]) -> list[Path]:
    """Distinct repo roots seen in the ledger's ``cwd`` stamps."""
    seen: dict[str, Path] = {}
    for r in records:
        cwd = r.get("cwd")
        if not cwd:
            continue
        p = Path(cwd)
        for candidate in [p, *p.parents]:
            if (candidate / ".git").exists():
                seen[str(candidate)] = candidate
                break
    return list(seen.values())


def task_rows(records: list[dict], pr_map: dict[str, dict]) -> list[dict]:
    """One row per branch, with its PR outcome. Costliest first."""
    by_branch: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        branch = r.get("branch")
        if branch:
            by_branch[branch].append(r)

    rows = []
    for branch, recs in by_branch.items():
        pr = pr_map.get(branch) or {}
        rows.append({
            "task": branch,
            "pr": pr.get("number"),
            "outcome": pr.get("outcome", "no-pr"),
            "cost_usd": round(sum(r.get("cost_usd", 0.0) for r in recs), 4),
            "calls": len(recs),
            "tokens": _sum_tokens(recs),
        })
    return sorted(rows, key=lambda r: -r["cost_usd"])


# Standing branches are not tasks. Interactive work on `main` dwarfs every
# feature branch in the ledger, and folding it into the numerator would report
# a "cost per merged task" that is really "cost of everything, ÷ two PRs".
STANDING_BRANCHES = {"main", "master", "live", "develop", "HEAD", "(none)"}


def _p90(sorted_costs: list[float]) -> float:
    """Nearest-rank p90 — the cheapest value at least 90% of tasks are under."""
    idx = min(len(sorted_costs) - 1, max(0, -(-len(sorted_costs) * 9 // 10) - 1))
    return sorted_costs[idx]


def task_summary(rows: list[dict]) -> dict:
    """Cost per *completed* task — the only cross-model-comparable figure here.

    Per-token comparisons mislead because a model that retries less can finish
    the same work for less money at a higher per-token rate. Dividing by
    merged PRs prices the outcome instead.
    """
    tasks = [r for r in rows if r["task"] not in STANDING_BRANCHES]
    standing = round(sum(r["cost_usd"] for r in rows if r["task"] in STANDING_BRANCHES), 4)
    merged = [r for r in tasks if r["outcome"] == "merged"]
    merged_costs = sorted(r["cost_usd"] for r in merged)
    task_total = sum(r["cost_usd"] for r in tasks)
    abandoned = sum(r["cost_usd"] for r in tasks if r["outcome"] == "closed")
    return {
        "tasks": len(tasks),
        "merged_tasks": len(merged),
        "total_usd": round(task_total, 4),
        "merged_usd": round(sum(merged_costs), 4),
        # Branch-attributed spend ÷ merged tasks: abandoned and in-flight work
        # is part of what a finished task costs, so it stays in the numerator.
        "cost_per_merged_task": round(task_total / len(merged), 4) if merged else None,
        "median_merged_task_usd": round(statistics.median(merged_costs), 4) if merged else None,
        "p90_merged_task_usd": round(_p90(merged_costs), 4) if merged_costs else None,
        "abandoned_usd": round(abandoned, 4),
        # Reported, never divided by — see STANDING_BRANCHES.
        "standing_branch_usd": standing,
    }


def _task_report(records: list[dict], pr_join: bool, as_json: bool, window: str) -> int:
    pr_map: dict[str, dict] = {}
    if pr_join:
        for repo in _repo_dirs(records):
            pr_map.update(fetch_pr_map(repo))
    rows = task_rows(records, pr_map)
    if not rows:
        return _no_data("No branch-attributed records in the selected window.",
                        as_json, window=window)
    summary = task_summary(rows)

    if as_json:
        print(json.dumps({"window": window, "summary": summary, "rows": rows}, indent=2))
        return 0

    print(f"Cost per task  [{window}]"
          f"{'' if pr_join else '  (no --pr-join: every row reads no-pr)'}")
    print(f"\n{'task (branch)':<40} {'outcome':>8} {'cost':>9} {'calls':>6}")
    print("-" * 66)
    for r in rows[:30]:
        print(f"{r['task'][:38]:<40} {r['outcome']:>8} ${r['cost_usd']:>8.2f} {r['calls']:>6}")
    if len(rows) > 30:
        print(f"… {len(rows) - 30} more")
    cpm = summary["cost_per_merged_task"]
    print(f"\n  tasks: {summary['tasks']}  merged: {summary['merged_tasks']}")
    print(f"  cost per merged task: {'n/a' if cpm is None else f'${cpm:.2f}'}"
          "   (task-branch spend ÷ merged tasks — abandoned work included)")
    if summary["standing_branch_usd"]:
        print(f"  on standing branches (main/master/…, not tasks): "
              f"${summary['standing_branch_usd']:.2f}")
    if summary["median_merged_task_usd"] is not None:
        print(f"  merged task median: ${summary['median_merged_task_usd']:.2f}   "
              f"p90: ${summary['p90_merged_task_usd']:.2f}")
    if summary["abandoned_usd"]:
        print(f"  spent on closed-unmerged tasks: ${summary['abandoned_usd']:.2f}")
    return 0


# --- Build outcomes (buildme blocks) ---------------------------------------


def read_build_states(project_dir: Path) -> list[dict]:
    """Every buildme change's outcome under *project_dir*.

    buildme emits a far finer completion signal than a merged PR — per-block
    DONE/FAILED in ``.build-state.json``, plus (since buildme 0.5) the build's
    own token/cost ledger. A failed block is a retry loop that spent money and
    produced nothing, which is precisely the spend that per-token comparisons
    hide.
    """
    states = []
    for state_file in sorted(project_dir.glob("specs/changes/*/.build-state.json")):
        try:
            data = json.loads(state_file.read_text())
        except (OSError, ValueError) as e:
            print(f"note: skipping unreadable {state_file} ({e})", file=sys.stderr)
            continue
        blocks = ((data.get("tdd") or {}).get("blocks") or [])
        budget = data.get("budget") or {}
        states.append({
            "change": data.get("change_name") or state_file.parent.name,
            "phase": data.get("phase", "?"),
            "blocks": len(blocks),
            "done": sum(1 for b in blocks if b.get("status") == "done"),
            "failed": sum(1 for b in blocks if b.get("status") == "failed"),
            "cost_usd": round(float(budget.get("cost_usd", 0.0) or 0.0), 4),
            "tokens": int(budget.get("total_tokens", 0) or 0),
            "by_phase": dict(budget.get("by_label") or {}),
        })
    return states


def build_summary(states: list[dict]) -> dict:
    """Cost per completed block, and — the useful one — per failed block."""
    done = sum(s["done"] for s in states)
    failed = sum(s["failed"] for s in states)
    total = sum(s["cost_usd"] for s in states)
    by_phase: dict[str, int] = defaultdict(int)
    for s in states:
        for label, tokens in s["by_phase"].items():
            by_phase[label] += int(tokens or 0)
    return {
        "builds": len(states),
        "blocks_done": done,
        "blocks_failed": failed,
        "total_usd": round(total, 4),
        "cost_per_done_block": round(total / done, 4) if done else None,
        # Not total ÷ failed — that would just restate the line above. This is
        # the share of spend that belongs to builds that failed blocks,
        # divided by those blocks.
        "cost_per_failed_block": (
            round(sum(s["cost_usd"] for s in states if s["failed"]) / failed, 4)
            if failed else None
        ),
        "tokens_by_phase": dict(sorted(by_phase.items(), key=lambda kv: -kv[1])),
    }


def _build_report(project_dir: Path, as_json: bool) -> int:
    states = read_build_states(project_dir)
    if not states:
        return _no_data(f"No buildme state files under {project_dir}/specs/changes/",
                        as_json, project_dir=str(project_dir))
    summary = build_summary(states)
    if as_json:
        print(json.dumps({"project_dir": str(project_dir), "summary": summary,
                          "rows": states}, indent=2))
        return 0

    print(f"Cost per build block  [{project_dir}]")
    print(f"\n{'change':<32} {'phase':>12} {'done':>5} {'failed':>7} {'cost':>9}")
    print("-" * 69)
    for s in states:
        print(f"{s['change'][:30]:<32} {s['phase'][:12]:>12} {s['done']:>5} "
              f"{s['failed']:>7} ${s['cost_usd']:>8.2f}")
    cpd, cpf = summary["cost_per_done_block"], summary["cost_per_failed_block"]
    print(f"\n  blocks done: {summary['blocks_done']}   failed: {summary['blocks_failed']}")
    print(f"  cost per DONE block:   {'n/a' if cpd is None else f'${cpd:.2f}'}")
    print(f"  cost per FAILED block: {'n/a' if cpf is None else f'${cpf:.2f}'}"
          "   (spend on builds that failed blocks ÷ those blocks)")
    if summary["tokens_by_phase"]:
        print("  tokens by phase: " + ", ".join(
            f"{k}={v:,}" for k, v in list(summary["tokens_by_phase"].items())[:6]))
    return 0


def _print_table(rows: list[tuple[str, float, int]], header: str, limit: int = 20) -> None:
    print(f"\n{header:<42} {'cost':>10} {'calls':>7}")
    print("-" * 61)
    for key, cost, count in rows[:limit]:
        print(f"{key:<42} ${cost:>9.2f} {count:>7}")
    if len(rows) > limit:
        rest = sum(c for _, c, _ in rows[limit:])
        print(f"{'… ' + str(len(rows) - limit) + ' more':<42} ${rest:>9.2f}")


def _bill_report(recs: list[dict], kind: str, name: str, as_json: bool,
                 window: str, tables: tuple[str, ...] = ("model",)) -> int:
    """Itemized bill for one entity: totals, main/subagent split, spans,
    plus one grouped table per entry in *tables*."""
    total = sum(r.get("cost_usd", 0.0) for r in recs)
    main = sum(r.get("cost_usd", 0.0) for r in recs if not r.get("sidechain"))
    spans: dict[str, float] = defaultdict(float)
    for r in recs:
        span = r.get("span")
        if span:
            spans[f"{span.get('kind')}:{span.get('name')}"] += r.get("cost_usd", 0.0)
    grouped = {t: _group(recs, _GROUPERS[t]) for t in tables}

    if as_json:
        payload = {
            kind: name, "records": len(recs), "window": window,
            "total_usd": round(total, 4), "main_usd": round(main, 4),
            "subagents_usd": round(total - main, 4),
            "spans": {k: round(v, 4) for k, v in sorted(spans.items(), key=lambda x: -x[1])},
        }
        for t, rows in grouped.items():
            payload[t + "s"] = {k: round(v, 4) for k, v, _ in rows}
        print(json.dumps(payload, indent=2))
        return 0

    print(f"{kind.capitalize()} {name}  [{window}]")
    print(f"  total:      ${total:.2f}  ({len(recs)} API calls)")
    print(f"  main:       ${main:.2f}")
    print(f"  subagents:  ${total - main:.2f}")
    if spans:
        print("  spans (approx for skills):")
        for k, v in sorted(spans.items(), key=lambda x: -x[1]):
            print(f"    {k:<38} ${v:.2f}")
    for t, rows in grouped.items():
        _print_table(rows, t)
    return 0


def _session_report(records: list[dict], prefix: str, as_json: bool, window: str) -> int:
    recs = [r for r in records if r.get("session", "").startswith(prefix)]
    sessions = {r["session"] for r in recs}
    if not sessions:
        return _no_data(f"No ledger records for session prefix {prefix!r}",
                        as_json, window=window)
    if len(sessions) > 1:
        print(f"Ambiguous prefix — matches: {', '.join(s[:12] for s in sorted(sessions))}",
              file=sys.stderr)
        return 1
    return _bill_report(recs, "session", sorted(sessions)[0], as_json, window)


def _branch_report(records: list[dict], branch: str, as_json: bool, window: str) -> int:
    # records arrive pre-filtered to the branch by _load.
    if not records:
        return _no_data(f"No ledger records for branch {branch!r}",
                        as_json, window=window)
    return _bill_report(records, "branch", branch, as_json, window,
                        tables=("model", "session"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", help="YYYY-MM (default: current month)")
    parser.add_argument("--since", help="YYYY-MM-DD lower bound (reads all months)")
    parser.add_argument("--all", action="store_true", help="Read the entire ledger")
    parser.add_argument("--by", choices=sorted(_GROUPERS), help="Group totals by this key")
    parser.add_argument("--group", choices=[*sorted(_GROUPERS), "task", "build"],
                        help="Like --by, plus outcome modes: task (branch→PR), build (buildme blocks)")
    parser.add_argument("--pr-join", action="store_true",
                        help="With --group task: resolve each branch's PR outcome via gh")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd(),
                        help="With --group build: project whose specs/changes/ to read")
    parser.add_argument("--report", choices=["cache"], help="Alternate report mode")
    parser.add_argument("--cache-threshold", type=float, default=DEFAULT_CACHE_THRESHOLD,
                        help=f"Flag cache hit ratios below this (default {DEFAULT_CACHE_THRESHOLD})")
    parser.add_argument("--session", help="Itemized bill for one session (id prefix ok)")
    parser.add_argument("--branch", help='Itemized bill for one git branch ("(none)" for unattributed)')
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    # --group build reads state files, not the ledger — no window applies.
    if args.group == "build":
        return _build_report(args.project_dir, args.json)

    records, window = _load(args)

    if args.report == "cache":
        if not records:
            return _no_data(
                f"No ledger records found under {costs_dir()} for the selected window.",
                args.json, window=window)
        # `--group` is the same axis as `--by` for this report. Read both here:
        # the `args.by = args.group` normalization below runs AFTER this branch,
        # so `--report cache --group session` silently grouped by component.
        if args.group and args.group not in _GROUPERS:
            print(f"--report cache cannot group by {args.group!r}; "
                  f"choose from {', '.join(sorted(_GROUPERS))}.", file=sys.stderr)
            return 2
        return _cache_report(records, args.by or args.group or "component",
                             args.cache_threshold, args.json, window)
    if args.group == "task":
        return _task_report(records, args.pr_join, args.json, window)
    if args.group:
        args.by = args.group
    # --branch + --session: a session that switched branches gets split
    # (records arrive branch-filtered from _load).
    if args.session:
        return _session_report(records, args.session, args.json, window)
    if args.branch:
        return _branch_report(records, args.branch, args.json, window)
    if not records:
        return _no_data(
            f"No ledger records found under {costs_dir()} for the selected window.",
            args.json, window=window)

    total = sum(r.get("cost_usd", 0.0) for r in records)
    if args.by:
        rows = _group(records, _GROUPERS[args.by])
        if args.json:
            print(json.dumps({k: round(v, 4) for k, v, _ in rows}, indent=2))
        else:
            print(f"Total: ${total:.2f} across {len(records)} API calls  [{window}]")
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

    print(f"Total: ${total:.2f} across {len(records)} API calls  [{window}]")
    _print_table(_group(records, _GROUPERS["model"]), "model", limit=10)
    _print_table(_group(records, _GROUPERS["project"]), "project", limit=10)
    _print_table(_group(records, _GROUPERS["session"]), "session (top)", limit=10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
