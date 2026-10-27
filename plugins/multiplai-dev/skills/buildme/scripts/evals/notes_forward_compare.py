"""Paired benchmark: buildme's TDD step with and without `carry_block_notes`.

Each run copies a fixture's starting repo and frozen change directory into a
fresh git repository, runs `python -m build_pipeline tdd` on it with the flag
off (`current`) or on (`notes`), then runs the fixture's held-out acceptance
tests from outside the checkout. Planning is not run, so both arms build the
same blocks. See plan-buildme-carry-block-notes-2026-10-27.md.

    python evals/notes_forward_compare.py --self-check
    uv run --project . python evals/notes_forward_compare.py \\
        --model <exact model id> --effort <effort> --window-tokens <n> \\
        --per-run-usd 15 --max-total-usd 300 --output /tmp/notes-forward.json

`--control-run` runs one `current` run of the first planted fixture and
reports its cost, to size the limits before a sweep.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
FIXTURES = HERE / "fixtures" / "multiblock"
CHANGE = "bench"
ARMS = ("current", "notes")
IMPLEMENTER_PROMPT_START = "You are an implementation agent"
SPEND_RE = re.compile(r"^SPEND:([0-9.]+):(\d+)", re.MULTILINE)

# Which fixtures hide a fact an early block must find and a later block must
# respect, and which blocks are the later ones (the decision rule's turns).
MANIFEST = {
    "string-ids": {"planted": True, "dependent_blocks": [3, 4]},
    "suffixed-prices": {"planted": True, "dependent_blocks": [4]},
    "keyword-events": {"planted": True, "dependent_blocks": [3, 4]},
    "day-first-dates": {"planted": True, "dependent_blocks": [3, 4]},
    "control-text": {"planted": False, "dependent_blocks": [3, 4]},
}


def run(argv, cwd, timeout=120, env=None):
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=True)


def test_command() -> str:
    return f"{sys.executable} -m pytest -q -p no:cacheprovider"


# --- Acceptance --------------------------------------------------------------


def acceptance(fixture: str, project: Path) -> bool:
    """The held-out tests, copied to a directory outside the checkout so no
    agent ever saw them, run against the checkout's code."""
    with tempfile.TemporaryDirectory() as tmp:
        test = Path(tmp) / "test_acceptance.py"
        shutil.copy(FIXTURES / fixture / "acceptance" / "test_acceptance.py", test)
        env = {**os.environ, "PYTHONPATH": str(project)}
        try:
            result = run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    str(test),
                ],
                project,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return False
    return result.returncode == 0


# --- Self-check (offline) ----------------------------------------------------


def self_check() -> dict:
    sys.path.insert(0, str(SCRIPTS))
    from build_pipeline.spec_generator import (
        scan_no_behaviour_blocks,
        scan_placeholders,
    )
    from build_pipeline.tdd_engine import parse_blocks

    rows = []
    for fixture in sorted(MANIFEST):
        tasks = FIXTURES / fixture / "change" / "tasks.md"
        problems = []
        if len(parse_blocks(tasks)) != 4:
            problems.append("tasks.md does not parse into 4 blocks")
        if scan_placeholders(tasks.read_text()):
            problems.append("tasks.md has placeholders")
        if scan_no_behaviour_blocks(tasks):
            problems.append("tasks.md has a block that adds no behaviour")
        with tempfile.TemporaryDirectory() as tmp:
            start = Path(tmp) / "start"
            copy_tree(FIXTURES / fixture / "repo", start)
            baseline = run(test_command().split(), start)
            if baseline.returncode != 0:
                problems.append("the starting repo's own tests fail")
            fails_on_start = not acceptance(fixture, start)
            solved = Path(tmp) / "solved"
            copy_tree(FIXTURES / fixture / "repo", solved)
            copy_tree(FIXTURES / fixture / "solution", solved)
            passes_on_solution = acceptance(fixture, solved)
        if not fails_on_start:
            problems.append("acceptance passes on the starting repo")
        if not passes_on_solution:
            problems.append("acceptance fails on the reference solution")
        rows.append(
            {
                "fixture": fixture,
                "fails_on_start": fails_on_start,
                "passes_on_solution": passes_on_solution,
                "problems": problems,
            }
        )
    return {"ok": not any(r["problems"] for r in rows), "fixtures": rows}


# --- One live run ------------------------------------------------------------


def prepare(fixture: str, arm: str, checkout: Path, args) -> None:
    copy_tree(FIXTURES / fixture / "repo", checkout)
    change = checkout / "specs" / "changes" / CHANGE
    copy_tree(FIXTURES / fixture / "change", change)
    config = {
        "tdd": {"test_command": test_command()},
        "mutation": {"enabled": False},
        "budget": {"max_usd": args.per_run_usd},
        "models": {"default": args.model},
        "efforts": {"default": args.effort},
        "carry_block_notes": arm == "notes",
    }
    (checkout / "specs" / "config.yaml").write_text(json.dumps(config, indent=2) + "\n")
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        [
            "git",
            "-c",
            "user.name=bench",
            "-c",
            "user.email=bench@example.invalid",
            "add",
            "-A",
        ],
        [
            "git",
            "-c",
            "user.name=bench",
            "-c",
            "user.email=bench@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        result = run(argv, checkout)
        if result.returncode:
            raise RuntimeError(result.stderr)


def build_argv(checkout: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "build_pipeline",
        "tdd",
        "--change",
        CHANGE,
        "--project-dir",
        str(checkout),
        "--trust-repo",
    ]


def run_build(
    argv: list[str], log: Path, timeout: float
) -> tuple[int | None, str, bool]:
    """Run the build in its own process group; a timeout kills the group and
    counts as a failed run."""
    with log.open("w") as out:
        proc = subprocess.Popen(
            argv,
            cwd=SCRIPTS,
            stdout=subprocess.PIPE,
            stderr=out,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, _ = proc.communicate()
            out.write(stdout or "")
            return None, stdout or "", True
        out.write(stdout or "")
    return proc.returncode, stdout or "", False


def saved_state(checkout: Path) -> dict:
    change = checkout / "specs" / "changes" / CHANGE
    for name in (".build-state.complete.json", ".build-state.json"):
        path = change / name
        if path.exists():
            return json.loads(path.read_text())
    return {}


def transcripts_for(cwd: Path, config_dir: Path) -> list[list[dict]]:
    """Every Claude Code transcript whose session ran in *cwd*. SDK runs keep
    them under `<config dir>/projects/`, one `.jsonl` per session."""
    found = []
    projects = config_dir / "projects"
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    for path in sorted((projects / slug).glob("*.jsonl")):
        rows = []
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if any(r.get("cwd") == str(cwd) for r in rows):
            found.append(rows)
    return found


def implementer_calls(sessions: list[list[dict]]) -> dict:
    """Per block name: the implementer's turns and end-of-call context, then
    any later implementer-prompt calls (review fixes) in order. Also every
    model that served a message."""
    by_block: dict[str, list[dict]] = {}
    models: set[str] = set()
    for rows in sessions:
        assistants = [r for r in rows if r.get("type") == "assistant"]
        models |= {
            a["message"].get("model") for a in assistants if a["message"].get("model")
        }
        users = [r for r in rows if r.get("type") == "user"]
        if not users or not assistants:
            continue
        first = users[0]["message"].get("content")
        text = first if isinstance(first, str) else json.dumps(first)
        if IMPLEMENTER_PROMPT_START not in text:
            continue
        match = re.search(r"## Block: (.+)", text.replace("\\n", "\n"))
        if not match:
            continue
        usage = assistants[-1]["message"].get("usage") or {}
        by_block.setdefault(match.group(1).strip(), []).append(
            {
                "started": users[0].get("timestamp", ""),
                "turns": len({a["message"].get("id") for a in assistants}),
                "end_context_tokens": usage.get("input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0),
            }
        )
    for calls in by_block.values():
        calls.sort(key=lambda c: c["started"])
    return {"blocks": by_block, "models": sorted(models)}


def measure(fixture: str, arm: str, repeat: int, root: Path, args) -> dict:
    checkout = root / f"{fixture}-{arm}-{repeat}"
    prepare(fixture, arm, checkout, args)
    log = root / f"{checkout.name}.log"
    start = time.monotonic()
    code, stdout, timed_out = run_build(build_argv(checkout), log, args.timeout)
    elapsed = time.monotonic() - start
    spend = SPEND_RE.findall(stdout)
    state = saved_state(checkout)
    blocks = (state.get("tdd") or {}).get("blocks", [])
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    calls = implementer_calls(transcripts_for(checkout, config_dir))
    per_block = []
    for b in blocks:
        mine = calls["blocks"].get(b["name"], [])
        per_block.append(
            {
                "number": b["number"],
                "status": b.get("status"),
                "review_iterations": b.get("review_iterations", 0),
                "handoff_chars": sum(
                    len(n["text"]) for n in b.get("handoff_notes", [])
                ),
                "implementer_turns": mine[0]["turns"] if mine else None,
                "implementer_end_context_tokens": mine[0]["end_context_tokens"]
                if mine
                else None,
                "fix_calls": len(mine[1:]),
            }
        )
    passed = acceptance(fixture, checkout)
    claimed = code == 0
    return {
        "fixture": fixture,
        "arm": arm,
        "repeat": repeat,
        "acceptance": passed,
        "claimed_success": claimed,
        "false_success": claimed and not passed,
        "timeout": timed_out,
        "exit_code": code,
        "elapsed_s": elapsed,
        "cost_usd": float(spend[-1][0]) if spend else None,
        "tokens": int(spend[-1][1]) if spend else None,
        "calls": (state.get("budget") or {}).get("calls"),
        "served_models": calls["models"],
        "blocks": per_block,
        "checkout": str(checkout),
    }


# --- Report ------------------------------------------------------------------


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _dependent_turns(row: dict) -> list[int]:
    wanted = MANIFEST[row["fixture"]]["dependent_blocks"]
    return [
        b["implementer_turns"]
        for b in row["blocks"]
        if b["number"] in wanted and b["implementer_turns"] is not None
    ]


def comparable(rows: list[dict], repetitions: int) -> tuple[bool, list[str]]:
    reasons = []
    for fixture in {r["fixture"] for r in rows}:
        for arm in ARMS:
            n = sum(1 for r in rows if r["fixture"] == fixture and r["arm"] == arm)
            if n != repetitions:
                reasons.append(f"{fixture}/{arm}: {n} of {repetitions} runs")
    for r in rows:
        if r["cost_usd"] is None and not r["timeout"]:
            reasons.append(f"{r['fixture']}/{r['arm']}/{r['repeat']}: no SPEND line")
    served = {arm: set() for arm in ARMS}
    for r in rows:
        served[r["arm"]] |= set(r["served_models"])
    if served["current"] != served["notes"]:
        reasons.append(
            f"served models differ: {sorted(served['current'])} vs {sorted(served['notes'])}"
        )
    return not reasons, reasons


def verdict(rows: list[dict], window_tokens: int | None) -> dict:
    """The decision rules of the plan, written before any live run."""
    fixtures = sorted({r["fixture"] for r in rows})
    planted = [f for f in fixtures if MANIFEST[f]["planted"]]

    def arm_rows(fixture, arm):
        return [r for r in rows if r["fixture"] == fixture and r["arm"] == arm]

    per_fixture = {}
    for f in fixtures:
        cur, notes = arm_rows(f, "current"), arm_rows(f, "notes")
        cur_cost, notes_cost = (
            _median([r["cost_usd"] for r in cur]),
            _median([r["cost_usd"] for r in notes]),
        )
        per_fixture[f] = {
            "acceptance": {
                "current": sum(r["acceptance"] for r in cur),
                "notes": sum(r["acceptance"] for r in notes),
            },
            "false_success": sum(r["false_success"] for r in cur + notes),
            "median_cost": {"current": cur_cost, "notes": notes_cost},
            "cost_ratio": (notes_cost / cur_cost)
            if cur_cost and notes_cost is not None
            else None,
            "median_dependent_turns": {
                "current": _median([t for r in cur for t in _dependent_turns(r)]),
                "notes": _median([t for r in notes for t in _dependent_turns(r)]),
            },
        }
    no_loss = all(
        p["acceptance"]["notes"] >= p["acceptance"]["current"]
        for p in per_fixture.values()
    )
    no_false = all(p["false_success"] == 0 for p in per_fixture.values())
    gain = sum(
        per_fixture[f]["acceptance"]["notes"] - per_fixture[f]["acceptance"]["current"]
        for f in planted
    )
    cur_turns = _median(
        [
            t
            for r in rows
            if r["arm"] == "current" and r["fixture"] in planted
            for t in _dependent_turns(r)
        ]
    )
    notes_turns = _median(
        [
            t
            for r in rows
            if r["arm"] == "notes" and r["fixture"] in planted
            for t in _dependent_turns(r)
        ]
    )
    turn_drop = (
        (1 - notes_turns / cur_turns) if cur_turns and notes_turns is not None else None
    )
    cost_ok = all(
        p["cost_ratio"] is not None and p["cost_ratio"] <= 1.10
        for p in per_fixture.values()
    )
    better = gain >= 2 or (turn_drop is not None and turn_drop >= 0.15)
    default_on = no_loss and no_false and better and cost_ok

    fits = None
    if window_tokens:
        fits = {}
        for f in planted:
            sums = [
                sum(b["implementer_end_context_tokens"] or 0 for b in r["blocks"])
                for r in arm_rows(f, "current")
            ]
            fits[f] = sum(s < 0.6 * window_tokens for s in sums)
        propose_resume = (not default_on) and all(n >= 2 for n in fits.values())
    else:
        propose_resume = None
    return {
        "per_fixture": per_fixture,
        "no_acceptance_loss": no_loss,
        "no_false_success": no_false,
        "planted_acceptance_gain": gain,
        "planted_dependent_turn_drop": turn_drop,
        "cost_within_10pct": cost_ok,
        "default_on": default_on,
        "runs_fitting_60pct_window": fits,
        "propose_session_resume": propose_resume,
    }


def markdown(report: dict) -> str:
    v = report["verdict"]
    lines = [
        "COMPARABLE" if report["comparable"] else "NOT COMPARABLE",
        "",
        f"# Notes-forward benchmark ({report['model']}, effort {report['effort']})",
        "",
    ]
    if report["not_comparable_reasons"]:
        lines += (
            ["## Why not comparable", ""]
            + [f"- {r}" for r in report["not_comparable_reasons"]]
            + [""]
        )
    lines += [
        "| Fixture | Acceptance (current / notes) | False successes | Median cost $ (current / notes) | Cost ratio | Median dependent-block implementer turns (current / notes) |",
        "|---|---|---|---|---|---|",
    ]
    for f, p in v["per_fixture"].items():
        lines.append(
            f"| {f} | {p['acceptance']['current']} / {p['acceptance']['notes']} | {p['false_success']} "
            f"| {p['median_cost']['current']} / {p['median_cost']['notes']} | {p['cost_ratio']} "
            f"| {p['median_dependent_turns']['current']} / {p['median_dependent_turns']['notes']} |"
        )
    lines += [
        "",
        (
            f"This is a pilot of {report['repetitions']} repetitions per arm and fixture; "
            "about 40 tasks x 3 trials detect only a 17-21 point gap."
        ),
        "",
        (
            f"Default: {'on' if v['default_on'] else 'off'} (acceptance gain on planted fixtures "
            f"{v['planted_acceptance_gain']}, dependent-block turn drop {v['planted_dependent_turn_drop']}, "
            f"no acceptance loss {v['no_acceptance_loss']}, no false success {v['no_false_success']}, "
            f"cost within 10% {v['cost_within_10pct']})."
        ),
        (
            f"Session resume: {'propose' if v['propose_session_resume'] else 'do not propose' if v['propose_session_resume'] is False else 'undecided (no --window-tokens)'} "
            f"(runs per planted fixture under 60% of the window: {v['runs_fitting_60pct_window']})."
        ),
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--control-run", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--window-tokens", type=int)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--per-run-usd", type=float)
    parser.add_argument("--max-total-usd", type=float)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--fixtures", nargs="*", default=sorted(MANIFEST))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.self_check:
        result = self_check()
        text = json.dumps(result, indent=2)
        if args.output:
            args.output.write_text(text + "\n")
        print(text)
        return 0 if result["ok"] else 1

    missing = [
        k
        for k in ("model", "effort", "per_run_usd", "output")
        if getattr(args, k) is None
    ]
    if not args.control_run and args.max_total_usd is None:
        missing.append("max_total_usd")
    if missing:
        parser.error(
            "a live run needs " + ", ".join("--" + m.replace("_", "-") for m in missing)
        )

    root = args.output.with_suffix(".runs")
    root.mkdir(parents=True, exist_ok=True)
    if args.control_run:
        fixture = next(f for f in args.fixtures if MANIFEST[f]["planted"])
        row = measure(fixture, "current", 0, root, args)
        args.output.write_text(json.dumps(row, indent=2) + "\n")
        print(f"CONTROL:{fixture}:{row['cost_usd']}")
        return 0

    rows, spent, stopped = [], 0.0, False
    for repeat in range(args.repetitions):
        for fixture in args.fixtures:
            for arm in ARMS:
                if spent >= args.max_total_usd:
                    stopped = True
                    break
                row = measure(fixture, arm, repeat, root, args)
                rows.append(row)
                spent += row["cost_usd"] or 0.0
                args.output.with_suffix(".rows.json").write_text(
                    json.dumps(rows, indent=2) + "\n"
                )
    ok, reasons = comparable(rows, args.repetitions)
    if stopped:
        ok, reasons = (
            False,
            [f"stopped at ${spent:.2f}, the --max-total-usd limit", *reasons],
        )
    report = {
        "comparable": ok,
        "not_comparable_reasons": reasons,
        "model": args.model,
        "effort": args.effort,
        "repetitions": args.repetitions,
        "spent_usd": spent,
        "verdict": verdict(rows, args.window_tokens),
        "rows": rows,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    args.output.with_suffix(".md").write_text(markdown(report))
    print(markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
