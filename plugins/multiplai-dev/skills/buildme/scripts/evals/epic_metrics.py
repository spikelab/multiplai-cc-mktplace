"""Per-child metrics of a buildme epic, read from what the run left on disk.

    python evals/epic_metrics.py --epic-dir <data_dir>/buildme/epics/<slug> \\
        --transcripts-root <claude config dir>/projects \\
        [--interventions factory-interventions.csv] \\
        --output metrics.json [--markdown metrics.md]
    python evals/epic_metrics.py --self-check

Nothing here calls a model or the network. Every number comes from a file:

- each child's `.build-state.json` / `.build-state.complete.json` `budget`
  (every state file under the child's worktree `specs/`, counted once per path);
- the epic's launch records (`agents/<NNN>-<kind>-<child>.result.json`): the
  child's worktree (`WORKTREE:`), and the `spend` of its fix and merge steps;
- the change dir's `reviews/block-NN-round-k.json`;
- the review folder of the child's PR (`<data_dir>/reviews/<repo>/pr-<n>/`):
  `review-state.json` `budget` of the folder and of each kept round under
  `rounds/`, `findings.json`, `buildme-decisions.json` and the finders'
  `report-*.md`;
- ledger rows (`<data_dir>/costs/ledger-*.jsonl`) whose `project` is the
  child's worktree, priced with `<data_dir>/costs/pricing.json`;
- the first user message of each agent transcript of the child's worktree;
- `git diff --numstat` of the PR in the child's worktree;
- `TEST_RUN end` lines in `<data_dir>/logs/build-pipeline-*.log`. A child
  with none has its test time reported as missing, never estimated.

Hand interventions come from a CSV with columns date, child and what was
done by hand. Output files are written atomically (a temp file, then a
rename), so a killed run leaves the previous output or none.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PLANNING_LABELS = frozenset(
    {
        "research",
        "codebase_analysis",
        "explainer",
        "plan_review",
        "plan_rewrite",
        "rubric",
        "tasks_audit",
        "design_audit",
    }
)
BLOCKING_SEVERITIES = ("HIGH", "MEDIUM")
IMPLEMENTER_START = "You are an implementation agent"
SPEC_WRITER_START = "You are a technical specification writer"
DESIGN_MARKER = "You are generating a design document"
# A finder report that says its search came back empty. Chosen by reading the
# 10-29 finder reports of PRs #37-#39. A copy of the review skill's
# `round_metrics.EMPTY_SEARCH_RE`, so both count alike; a test pins the two.
EMPTY_SEARCH_RE = re.compile(
    r"\b(?:grep|greps|search|searches|searching|ripgrep|rg)\b[^.\n]{0,120}?"
    r"(?:\b(?:return(?:s|ed|ing)?|came back|comes back|coming back|finds?|found|gives?|gave"
    r"|lists?|listed)\b[^.\n]{0,30}?"
    r"(?:\bnothing\b|\bno (?:match(?:es)?|results?|hits?|files?)\b|\bempty\b|\banything\b)"
    r"|\b(?:is ?n[o']t (?:working|returning|searching|finding)|unable to search"
    r"|could ?n[o']t run|misses files|is skipping files|did ?n[o']t find|does ?n[o']t work"
    r"|did not work|didn't work|is broken|broken here|is failing|failed\b|blocked"
    r"|keeps? (?:returning|coming back)))",
    re.IGNORECASE,
)
# The line buildme logs when a test-command run ends (item 32 of
# plan-factory-merged-2026-10-29.md): `TEST_RUN end ... seconds=<s>`.
TEST_RUN_RE = re.compile(r"TEST_RUN end\b.*?\bseconds=([0-9]+(?:\.[0-9]+)?)")
CHANGE_TAG_RE = re.compile(r"\[change:([^\]]+)\]")
SLOW_TURN_SECONDS = 300
IDLE_GAP_SECONDS = 30 * 60


# ---------------------------------------------------------------- reading


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def transcript_dir_name(path: str) -> str:
    """Claude Code's projects-dir name for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def launch_records(epic_dir: Path) -> list[dict]:
    """Every launch record, oldest first, with its result merged in."""
    out = []
    for started in sorted((epic_dir / "agents").glob("*.started.json")):
        rec = read_json(started) or {}
        result = read_json(
            started.with_name(started.name.replace(".started.json", ".result.json"))
        )
        rec["result"] = result
        rec["name"] = started.name
        out.append(rec)
    return out


def child_worktree(records: list[dict], child: str) -> str | None:
    worktree = None
    for rec in records:
        if rec.get("child") != child or not rec.get("result"):
            continue
        for marker in rec["result"].get("markers") or []:
            if marker.startswith("WORKTREE:"):
                worktree = marker[len("WORKTREE:") :]
    return worktree


def feature_branch(records: list[dict], child: str) -> str | None:
    for rec in reversed(records):
        if rec.get("child") != child:
            continue
        argv = rec.get("argv") or []
        if "--branch" in argv:
            return argv[argv.index("--branch") + 1]
    return None


def build_states(worktree: Path, child: str) -> list[Path]:
    """Every build state file of the child under the worktree's specs/."""
    root = worktree / "specs"
    if not root.is_dir():
        return []
    found = []
    for name in (".build-state.json", ".build-state.complete.json"):
        found += [p for p in root.rglob(name) if p.parent.name == child]
    return sorted(set(found))


def spend_split(states: list[Path]) -> dict:
    planning = total = 0.0
    blocks = 0
    by_label: dict[str, float] = {}
    for path in states:
        data = read_json(path) or {}
        budget = data.get("budget") or {}
        total += float(budget.get("cost_usd") or 0.0)
        for label, usd in (budget.get("usd_by_label") or {}).items():
            by_label[label] = by_label.get(label, 0.0) + float(usd)
            if label.startswith("spec:") or label in PLANNING_LABELS:
                planning += float(usd)
        blocks = max(blocks, len(((data.get("tdd") or {}).get("blocks")) or []))
    return {
        "build_usd": round(total, 6),
        "planning_usd": round(planning, 6),
        "tdd_usd": round(total - planning, 6),
        "blocks": blocks,
        "by_label": {k: round(v, 6) for k, v in sorted(by_label.items())},
        "state_files": [str(p) for p in states],
    }


def step_spend(records: list[dict], child: str) -> dict:
    usd = 0.0
    unknown = []
    acted = 0
    for rec in records:
        if rec.get("child") != child or rec.get("kind") not in ("fix", "merge"):
            continue
        result = rec.get("result") or {}
        if result.get("spend") is None:
            unknown.append(rec["name"])
        else:
            usd += float(result["spend"])
        for marker in result.get("markers") or []:
            match = re.match(r"FINDINGS:DONE:(\d+)", marker)
            if match:
                acted += int(match.group(1))
    return {
        "fix_merge_usd": round(usd, 6),
        "fix_merge_unknown": unknown,
        "fixer_acted_on": acted,
    }


def pr_number(url: str | None) -> tuple[str, int] | None:
    match = re.search(r"github\.com/[^/]+/([^/]+)/pull/(\d+)", url or "")
    return (match.group(1), int(match.group(2))) if match else None


def review_folders(folder: Path) -> list[Path]:
    """The kept rounds (oldest first), then the folder's own latest review."""
    rounds = sorted(p for p in (folder / "rounds").glob("*") if p.is_dir())
    return [*rounds, folder] if (folder / "review-state.json").exists() else rounds


def confirmed_blocking(findings_file: Path) -> list[dict]:
    data = read_json(findings_file) or {}
    return [
        f
        for f in data.get("findings") or []
        if f.get("status") == "confirmed" and f.get("severity") in BLOCKING_SEVERITIES
    ]


def review_metrics(folder: Path) -> dict:
    usd = 0.0
    rounds = []
    for one in review_folders(folder):
        state = read_json(one / "review-state.json") or {}
        cost = float((state.get("budget") or {}).get("cost_usd") or 0.0)
        usd += cost
        # A round that never wrote findings.json (stopped early) is left out
        # of the report counts, as the review's `round-metrics` leaves it out.
        reports = (
            sorted(one.glob("report-*.md")) if (one / "findings.json").exists() else []
        )
        empty = sum(
            1
            for r in reports
            if EMPTY_SEARCH_RE.search(r.read_text(encoding="utf-8", errors="replace"))
        )
        confirmed = confirmed_blocking(one / "findings.json")
        rounds.append(
            {
                "dir": str(one),
                "stage": state.get("stage"),
                "usd": round(cost, 6),
                "confirmed_medium_high": len(confirmed),
                "confirmed_ids": [f.get("id") for f in confirmed],
                "reports": len(reports),
                "empty_search_reports": empty,
            }
        )
    done = [r for r in rounds if r["stage"] == "done"]
    decisions = read_json(folder / "buildme-decisions.json") or {}
    return {
        "review_usd": round(usd, 6),
        "rounds": rounds,
        "first_confirmed": done[0]["confirmed_medium_high"] if done else None,
        "latest_confirmed": done[-1]["confirmed_medium_high"] if done else None,
        "blockers_posted": len(decisions),
        "reports": sum(r["reports"] for r in rounds),
        "empty_search_reports": sum(r["empty_search_reports"] for r in rounds),
    }


def block_rounds(change_dir: Path) -> dict:
    by_block: dict[int, list[tuple[int, bool]]] = {}
    for path in (change_dir / "reviews").glob("block-*-round-*.json"):
        data = read_json(path) or {}
        if not str(data.get("kind", "review")).endswith("review"):
            continue
        match = re.match(r"block-(\d+)-round-(\d+)\.json", path.name)
        if not match:
            continue
        passed = bool((data.get("gate") or {}).get("passed"))
        by_block.setdefault(int(match.group(1)), []).append(
            (int(match.group(2)), passed)
        )
    total = after = 0
    for rounds in by_block.values():
        seen_pass = False
        for _, passed in sorted(rounds):
            total += 1
            if seen_pass:
                after += 1
            seen_pass = seen_pass or passed
    return {"block_review_rounds": total, "rounds_after_pass": after}


def first_prompts(transcripts: Path) -> list[str]:
    out = []
    for path in sorted(transcripts.glob("*.jsonl")):
        try:
            lines = path.open(encoding="utf-8")
        except OSError:
            continue
        with lines:
            for line in lines:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") != "user":
                    continue
                content = (row.get("message") or {}).get("content")
                if isinstance(content, list):
                    content = "".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                out.append(content or "")
                break
    return out


def prompt_metrics(transcripts: Path) -> dict:
    prompts = first_prompts(transcripts)
    implementer = [len(p) for p in prompts if p.startswith(IMPLEMENTER_START)]
    design = [
        len(p)
        for p in prompts
        if p.startswith(SPEC_WRITER_START) and DESIGN_MARKER in p
    ]
    return {
        "agent_transcripts": len(prompts),
        "implementer_prompts": len(implementer),
        "implementer_prompt_chars_median": int(statistics.median(implementer))
        if implementer
        else None,
        "design_prompt_chars_first": design[0] if design else None,
        "design_prompts": design,
    }


def ledger_metrics(ledgers: list[Path], project: str, pricing: dict) -> dict:
    by_type = {"in": 0.0, "out": 0.0, "cw5m": 0.0, "cw1h": 0.0, "cr": 0.0}
    total = 0.0
    rows = 0
    unpriced = set()
    last_ts: dict[str, datetime] = {}
    slow = 0
    for ledger in ledgers:
        try:
            handle = ledger.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("project") != project:
                    continue
                rows += 1
                total += float(row.get("cost_usd") or 0.0)
                rates = (pricing.get("models") or {}).get(row.get("model"))
                if rates is None:
                    unpriced.add(row.get("model"))
                else:
                    for key, count in (row.get("tokens") or {}).items():
                        if key in by_type:
                            by_type[key] += count * float(rates.get(key, 0.0)) / 1e6
                session = row.get("session") or ""
                try:
                    ts = datetime.fromisoformat(row["ts"])
                except (KeyError, ValueError):
                    continue
                if (
                    session in last_ts
                    and (ts - last_ts[session]).total_seconds() > SLOW_TURN_SECONDS
                ):
                    slow += 1
                last_ts[session] = ts
    share = lambda v: round(v / total, 4) if total else None
    return {
        "agent_ledger_rows": rows,
        "agent_usd": round(total, 6),
        "cache_write_1h_usd": round(by_type["cw1h"], 6),
        "cache_write_5m_usd": round(by_type["cw5m"], 6),
        "cache_write_1h_share": share(by_type["cw1h"]),
        "cache_write_5m_share": share(by_type["cw5m"]),
        "turns_after_5min": slow,
        "unpriced_models": sorted(m for m in unpriced if m),
    }


def git(worktree: Path, *args: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(worktree), *args], capture_output=True, text=True, check=False
    )
    return proc.stdout if proc.returncode == 0 else None


def diff_range(worktree: Path, branch: str | None) -> str | None:
    """The PR's diff: the feature branch...HEAD while the PR is open; after a
    merge, the merge commit's first parent...HEAD."""
    if not branch:
        return None
    ref = f"origin/{branch}"
    head = (git(worktree, "rev-parse", "HEAD") or "").strip()
    if not head or git(worktree, "rev-parse", "--verify", "-q", ref) is None:
        return None
    if git(worktree, "merge-base", "--is-ancestor", head, ref) is None:
        return f"{ref}...{head}"
    merges = git(worktree, "log", "--merges", "--format=%H %P", ref) or ""
    for line in merges.splitlines():
        parts = line.split()
        if head in parts[2:]:
            return f"{parts[1]}...{head}"
    return None


def diff_metrics(worktree: Path, branch: str | None) -> dict:
    rng = diff_range(worktree, branch)
    out = git(worktree, "diff", "--numstat", rng) if rng else None
    if out is None:
        return {
            "diff_range": rng,
            "added_lines": None,
            "spec_lines": None,
            "planning_file_lines": None,
        }
    added = spec = planning = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        count, path = int(parts[0]), parts[2]
        added += count
        if path.startswith("specs/"):
            spec += count
            if Path(path).name in ("facts.json", "codebase-analysis.md"):
                planning += count
    return {
        "diff_range": rng,
        "added_lines": added,
        "spec_lines": spec,
        "planning_file_lines": planning,
    }


def test_minutes(logs: list[Path], change_ref: str) -> float | None:
    seconds = 0.0
    found = False
    for log in logs:
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            tag = CHANGE_TAG_RE.search(line)
            match = TEST_RUN_RE.search(line)
            if tag and match and tag.group(1) == change_ref:
                seconds += float(match.group(1))
                found = True
    return round(seconds / 60, 2) if found else None


def interventions(path: Path | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not path or not path.exists():
        return counts
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            child = (row.get("child") or "").strip()
            counts[child] = counts.get(child, 0) + 1
    return counts


LOG_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\]")


def log_activity(logs: list[Path], change: str) -> list[tuple[datetime, datetime]]:
    """One zero-length span per build-pipeline log line of the change or one of
    its children, so builds run by hand outside the epic count as activity."""
    spans = []
    for log in logs:
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            tag = CHANGE_TAG_RE.search(line)
            stamp = LOG_TS_RE.match(line)
            if not (tag and stamp):
                continue
            ref = tag.group(1)
            if ref == change or ref.startswith((f"{change}/", f"{change}-")):
                ts = datetime.fromisoformat(stamp.group(1) + "+00:00")
                spans.append((ts, ts))
    return spans


def idle_gaps(
    records: list[dict], extra: list[tuple[datetime, datetime]] = ()
) -> list[dict]:
    """Gaps longer than IDLE_GAP_SECONDS with no epic launch running and no
    build-pipeline log line for the change. Whether work could have started in
    a gap is a judgment the reader makes; every gap is listed with its times."""
    spans = list(extra)
    for rec in records:
        try:
            start = datetime.fromisoformat(rec["at"])
            end = datetime.fromisoformat((rec.get("result") or {})["at"])
        except (KeyError, TypeError, ValueError):
            continue
        spans.append((start, end))
    spans.sort()
    gaps = []
    if not spans:
        return gaps
    cover_end = spans[0][1]
    for start, end in spans[1:]:
        if (start - cover_end).total_seconds() > IDLE_GAP_SECONDS:
            gaps.append(
                {
                    "from": cover_end.isoformat(),
                    "to": start.isoformat(),
                    "hours": round((start - cover_end).total_seconds() / 3600, 2),
                }
            )
        cover_end = max(cover_end, end)
    return gaps


# ---------------------------------------------------------------- metrics


def compute(
    epic_dir: Path,
    transcripts_root: Path,
    interventions_csv: Path | None = None,
    data_dir: Path | None = None,
) -> dict:
    data_dir = data_dir or epic_dir.parents[2]
    epic = read_json(epic_dir / "epic.json") or {}
    change = epic.get("name", "")
    records = launch_records(epic_dir)
    pricing = read_json(data_dir / "costs" / "pricing.json") or {}
    ledgers = sorted((data_dir / "costs").glob("ledger-*.jsonl"))
    logs = sorted((data_dir / "logs").glob("build-pipeline-*.log"))
    hand = interventions(interventions_csv)
    children = []
    for child in epic.get("children") or []:
        cid = child["id"]
        status = (epic.get("child_status") or {}).get(cid) or {}
        worktree = child_worktree(records, cid)
        if not worktree and not status.get("pr"):
            continue
        row = {
            "child": cid,
            "status": status.get("status", "pending"),
            "pr": status.get("pr"),
        }
        row["accepted"] = row["status"] == "merged"
        row["worktree"] = worktree
        wt = Path(worktree) if worktree else None
        states = build_states(wt, cid) if wt else []
        row.update(spend_split(states))
        row.update(step_spend(records, cid))
        pr = pr_number(status.get("pr"))
        review = (
            review_metrics(data_dir / "reviews" / pr[0] / f"pr-{pr[1]}")
            if pr
            else review_metrics(Path("/nonexistent"))
        )
        row.update(review)
        row["total_usd"] = round(
            row["build_usd"] + row["fix_merge_usd"] + row["review_usd"], 6
        )
        row["tdd_usd_per_block"] = (
            round(row["tdd_usd"] / row["blocks"], 4) if row["blocks"] else None
        )
        row["merged_with_open_confirmed"] = bool(
            row["accepted"] and row["latest_confirmed"]
        )
        change_dir = states[0].parent if states else None
        row.update(
            block_rounds(change_dir)
            if change_dir
            else {"block_review_rounds": 0, "rounds_after_pass": 0}
        )
        if wt:
            row.update(prompt_metrics(transcripts_root / transcript_dir_name(str(wt))))
            row.update(ledger_metrics(ledgers, transcript_dir_name(str(wt)), pricing))
            row.update(diff_metrics(wt, feature_branch(records, cid)))
        minutes = test_minutes(logs, f"{change}/{cid}")
        row["test_minutes"] = minutes
        row["test_minutes_per_block"] = (
            round(minutes / row["blocks"], 2)
            if minutes is not None and row["blocks"]
            else None
        )
        row["test_minutes_missing"] = minutes is None
        row["hand_interventions"] = hand.get(cid, 0)
        row["found_before_pr"] = None
        children.append(row)
    activity = log_activity(logs, change)
    return {
        "epic": change,
        "epic_dir": str(epic_dir),
        "children": children,
        "totals": totals(children, records, activity),
    }


def totals(children: list[dict], records: list[dict], activity=()) -> dict:
    def total(key):
        return round(sum(c.get(key) or 0 for c in children), 6)

    accepted = sum(1 for c in children if c["accepted"])
    blocks = total("blocks")
    reviewed = [c for c in children if c["rounds"]]
    reports = total("reports")
    gaps = idle_gaps(records, activity)
    return {
        "children": len(children),
        "accepted": accepted,
        "total_usd": total("total_usd"),
        "usd_per_accepted": round(total("total_usd") / accepted, 2)
        if accepted
        else None,
        "usd_per_attempted": round(total("total_usd") / len(children), 2)
        if children
        else None,
        "planning_usd": total("planning_usd"),
        "tdd_usd": total("tdd_usd"),
        "tdd_usd_per_block": round(total("tdd_usd") / blocks, 4) if blocks else None,
        "review_usd": total("review_usd"),
        "build_usd": total("build_usd"),
        "fix_merge_usd": total("fix_merge_usd"),
        "agent_usd": total("agent_usd"),
        "cache_write_1h_usd": total("cache_write_1h_usd"),
        "cache_write_5m_usd": total("cache_write_5m_usd"),
        "review_usd_per_pr": round(total("review_usd") / len(reviewed), 2)
        if reviewed
        else None,
        "fixer_acted_on": total("fixer_acted_on"),
        "blockers_posted": total("blockers_posted"),
        "merged_with_open_confirmed": sum(
            1 for c in children if c["merged_with_open_confirmed"]
        ),
        "first_confirmed": total("first_confirmed"),
        "empty_search_reports": total("empty_search_reports"),
        "reports": reports,
        "block_review_rounds": total("block_review_rounds"),
        "rounds_after_pass": total("rounds_after_pass"),
        "hand_interventions": total("hand_interventions"),
        "idle_gaps": gaps,
        "idle_gap_hours": round(sum(g["hours"] for g in gaps), 2),
    }


# ---------------------------------------------------------------- output

COLUMNS = (
    ("child", "child"),
    ("status", "status"),
    ("total_usd", "$ total"),
    ("planning_usd", "planning $"),
    ("tdd_usd_per_block", "TDD $/block"),
    ("review_usd", "review $"),
    ("fixer_acted_on", "fixer acted"),
    ("blockers_posted", "blockers"),
    ("first_confirmed", "confirmed (first review)"),
    ("empty_search_reports", "empty searches"),
    ("reports", "reports"),
    ("rounds_after_pass", "rounds after pass"),
    ("block_review_rounds", "block rounds"),
    ("test_minutes_per_block", "test min/block"),
    ("implementer_prompt_chars_median", "implementer chars"),
    ("design_prompt_chars_first", "design chars"),
    ("cache_write_1h_share", "1h cache share"),
    ("cache_write_5m_share", "5m cache share"),
    ("spec_lines", "specs/ lines"),
    ("added_lines", "added lines"),
    ("hand_interventions", "hand"),
)


def cell(value) -> str:
    if value is None:
        return "missing"
    if isinstance(value, float):
        return f"{value:,.2f}" if value >= 1 or value == 0 else f"{value:.4f}"
    return (
        f"{value:,}"
        if isinstance(value, int) and not isinstance(value, bool)
        else str(value)
    )


def markdown(result: dict) -> str:
    head = "| " + " | ".join(title for _, title in COLUMNS) + " |"
    rule = "|" + "---|" * len(COLUMNS)
    rows = [
        "| " + " | ".join(cell(c.get(key)) for key, _ in COLUMNS) + " |"
        for c in result["children"]
    ]
    t = result["totals"]
    lines = [head, rule, *rows, ""]
    lines.append(
        f"Totals: ${t['total_usd']:,.2f} over {t['children']} children, {t['accepted']} accepted; "
        f"planning ${t['planning_usd']:,.2f}, TDD ${t['tdd_usd']:,.2f}, review ${t['review_usd']:,.2f}, "
        f"build-agent 1-hour cache writes ${t['cache_write_1h_usd']:,.2f} of ${t['agent_usd']:,.2f}."
    )
    return "\n".join(lines) + "\n"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- self-check


def _run(argv, cwd):
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True)


def build_fixture(root: Path) -> dict:
    """A one-child epic with every source the script reads, and its answers."""
    data = root / "data"
    epic_dir = data / "buildme" / "epics" / "o-repo--chg"
    (epic_dir / "agents").mkdir(parents=True)
    wt = root / "wt"
    origin = root / "origin.git"
    _run(["git", "init", "-q", "--bare", str(origin)], root)
    _run(["git", "init", "-q", "-b", "feat", str(wt)], root)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        _run(["git", "config", key, value], wt)
    (wt / "a.py").write_text("x = 1\n")
    _run(["git", "add", "."], wt)
    _run(["git", "commit", "-q", "-m", "base"], wt)
    _run(["git", "remote", "add", "origin", str(origin)], wt)
    _run(["git", "push", "-q", "origin", "feat"], wt)
    _run(["git", "checkout", "-q", "-b", "child"], wt)
    change_dir = wt / "specs" / "changes" / "chg" / "components" / "c1"
    (change_dir / "reviews").mkdir(parents=True)
    (wt / "b.py").write_text("y = 2\nz = 3\n")
    (change_dir / "facts.json").write_text("1\n2\n3\n")
    _run(["git", "add", "b.py", "specs/changes/chg/components/c1/facts.json"], wt)
    _run(["git", "commit", "-q", "-m", "child"], wt)
    _run(["git", "fetch", "-q", "origin"], wt)
    state = {
        "budget": {
            "cost_usd": 10.0,
            "usd_by_label": {
                "codebase_analysis": 2.0,
                "spec:design": 1.0,
                "implementer": 5.0,
                "review": 2.0,
            },
        },
        "tdd": {"blocks": [{"number": 1}, {"number": 2}]},
    }
    (change_dir / ".build-state.complete.json").write_text(json.dumps(state))
    for block, rnd, passed in ((1, 1, False), (1, 2, True), (1, 3, True), (2, 1, True)):
        (change_dir / "reviews" / f"block-{block:02d}-round-{rnd}.json").write_text(
            json.dumps({"kind": "review", "gate": {"passed": passed}})
        )
    epic = {
        "name": "chg",
        "children": [
            {"id": "c1", "depends_on": []},
            {"id": "c2", "depends_on": ["c1"]},
        ],
        "child_status": {
            "c1": {"status": "merged", "pr": "https://github.com/o/repo/pull/7"}
        },
    }
    (epic_dir / "epic.json").write_text(json.dumps(epic))

    def record(n, kind, at, end, markers, spend):
        base = epic_dir / "agents" / f"{n:03d}-{kind}-c1"
        argv = ["python", "-m", "build_pipeline", "build", "--branch", "feat"]
        Path(f"{base}.started.json").write_text(
            json.dumps({"kind": kind, "child": "c1", "argv": argv, "at": at})
        )
        Path(f"{base}.result.json").write_text(
            json.dumps({"markers": markers, "spend": spend, "at": end})
        )

    record(
        1,
        "build",
        "2026-10-28T01:00:00+00:00",
        "2026-10-28T02:00:00+00:00",
        [f"WORKTREE:{wt}"],
        10.0,
    )
    record(
        2,
        "fix",
        "2026-10-28T04:00:00+00:00",
        "2026-10-28T04:10:00+00:00",
        ["FINDINGS:DONE:1"],
        1.5,
    )
    review = data / "reviews" / "repo" / "pr-7"
    (review / "rounds" / "01-abc").mkdir(parents=True)
    (review / "rounds" / "01-abc" / "review-state.json").write_text(
        json.dumps({"stage": "paths", "budget": {"cost_usd": 3.0}})
    )
    (review / "review-state.json").write_text(
        json.dumps({"stage": "done", "budget": {"cost_usd": 4.0}})
    )
    findings = [
        {"id": "f1", "status": "confirmed", "severity": "MEDIUM"},
        {"id": "f2", "status": "unverified", "severity": "LOW"},
    ]
    (review / "findings.json").write_text(json.dumps({"findings": findings}))
    (review / "buildme-decisions.json").write_text(
        json.dumps({"f1": {"decision": "accept"}})
    )
    (review / "report-callers-1.md").write_text(
        "The grep for `foo` returned nothing.\n"
    )
    (review / "report-callers-2.md").write_text("Found two callers.\n")
    (data / "costs").mkdir(parents=True)
    (data / "costs" / "pricing.json").write_text(
        json.dumps(
            {"models": {"m": {"in": 1, "out": 1, "cw5m": 1, "cw1h": 2, "cr": 0}}}
        )
    )
    project = transcript_dir_name(str(wt))
    rows = [
        {
            "ts": "2026-10-28T01:00:00Z",
            "session": "s",
            "project": project,
            "model": "m",
            "cost_usd": 4.0,
            "tokens": {"in": 0, "out": 0, "cw5m": 0, "cw1h": 1_000_000, "cr": 0},
        },
        {
            "ts": "2026-10-28T01:10:00Z",
            "session": "s",
            "project": project,
            "model": "m",
            "cost_usd": 1.0,
            "tokens": {"in": 0, "out": 1_000_000, "cw5m": 0, "cw1h": 0, "cr": 0},
        },
        {
            "ts": "2026-10-28T01:10:00Z",
            "session": "x",
            "project": "other",
            "model": "m",
            "cost_usd": 9.0,
            "tokens": {},
        },
    ]
    (data / "costs" / "ledger-2026-10.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows)
    )
    tdir = root / "projects" / project
    tdir.mkdir(parents=True)
    for name, text in (
        ("a", IMPLEMENTER_START + " x" * 10),
        ("b", IMPLEMENTER_START + " y" * 20),
        ("c", SPEC_WRITER_START + "\n" + DESIGN_MARKER + " z"),
    ):
        user = {"type": "user", "message": {"content": text}}
        (tdir / f"{name}.jsonl").write_text(
            json.dumps({"type": "queue-operation"}) + "\n" + json.dumps(user) + "\n"
        )
    (data / "logs").mkdir(parents=True)
    (data / "logs" / "build-pipeline-2026-10-28.log").write_text(
        "[t] INFO: [change:chg/c1] TEST_RUN end dir=. exit=0 seconds=120.0\n"
        "[t] INFO: [change:chg/c1] TEST_RUN end dir=. exit=1 seconds=60\n"
        "[t] INFO: [change:chg/other] TEST_RUN end dir=. exit=0 seconds=999\n"
    )
    csv_path = root / "interventions.csv"
    csv_path.write_text(
        "date,child,what\n2026-10-28,c1,edited tasks.md\n2026-10-28,c1,relaunched\n"
    )
    expected = {
        "total_usd": 18.5,
        "planning_usd": 3.0,
        "tdd_usd_per_block": 3.5,
        "review_usd": 7.0,
        "fixer_acted_on": 1,
        "blockers_posted": 1,
        "first_confirmed": 1,
        "empty_search_reports": 1,
        "reports": 2,
        "block_review_rounds": 4,
        "rounds_after_pass": 1,
        "test_minutes": 3.0,
        "implementer_prompt_chars_median": (len(IMPLEMENTER_START) * 2 + 20 + 40) // 2,
        "design_prompt_chars_first": len(SPEC_WRITER_START)
        + 1
        + len(DESIGN_MARKER)
        + 2,
        "cache_write_1h_share": 0.4,
        "turns_after_5min": 1,
        "added_lines": 5,
        "spec_lines": 3,
        "planning_file_lines": 3,
        "hand_interventions": 2,
        "merged_with_open_confirmed": True,
    }
    return {
        "epic_dir": epic_dir,
        "projects": root / "projects",
        "csv": csv_path,
        "wt": wt,
        "expected": expected,
    }


def self_check() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = build_fixture(Path(tmp))
        wt = fixture["wt"]
        # The PR is merged: the merge commit's first parent is the base.
        _run(["git", "checkout", "-q", "feat"], wt)
        _run(["git", "merge", "-q", "--no-ff", "-m", "merge child", "child"], wt)
        _run(["git", "push", "-q", "origin", "feat"], wt)
        _run(["git", "checkout", "-q", "child"], wt)
        _run(["git", "fetch", "-q", "origin"], wt)
        result = compute(fixture["epic_dir"], fixture["projects"], fixture["csv"])
    rows = result["children"]
    wrong = {}
    if len(rows) != 1:
        wrong["children"] = len(rows)
    else:
        for key, want in fixture["expected"].items():
            if rows[0].get(key) != want:
                wrong[key] = {"want": want, "got": rows[0].get(key)}
    if result["totals"]["usd_per_accepted"] != 18.5:
        wrong["usd_per_accepted"] = result["totals"]["usd_per_accepted"]
    return {"ok": not wrong, "wrong": wrong}


# ---------------------------------------------------------------- CLI


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--epic-dir", type=Path)
    parser.add_argument(
        "--transcripts-root", type=Path, help="Claude Code's projects directory"
    )
    parser.add_argument(
        "--data-dir", type=Path, help="default: three levels above --epic-dir"
    )
    parser.add_argument(
        "--interventions", type=Path, help="CSV with columns date, child, what"
    )
    parser.add_argument("--output", type=Path, help="JSON output; stdout when omitted")
    parser.add_argument(
        "--markdown", type=Path, help="also write the markdown table here"
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="check the counting on a fixture; no paid call",
    )
    args = parser.parse_args(argv)
    if args.self_check:
        result = self_check()
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    if not args.epic_dir or not args.transcripts_root:
        parser.error("--epic-dir and --transcripts-root are required")
    if not (args.epic_dir / "epic.json").exists():
        parser.error(f"no epic.json in {args.epic_dir}")
    result = compute(
        args.epic_dir, args.transcripts_root, args.interventions, args.data_dir
    )
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        write_atomic(args.output, text)
    else:
        sys.stdout.write(text)
    if args.markdown:
        write_atomic(args.markdown, markdown(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
