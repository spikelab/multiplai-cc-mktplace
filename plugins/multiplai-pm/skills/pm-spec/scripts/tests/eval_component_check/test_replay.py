"""Replay the component check on the DFACCMGMT-1 specs buildme parked on.

`fixtures.yaml` holds the 10 requirements problems (P1-P10) buildme's design,
design-audit and plan-review steps sent back after the handoff, each with the
feature-branch commit its build read. For each distinct commit the eval adds a
temporary detached worktree of the Dolcebot clone, runs
`component_check.run_component_checks` on each fixture's component there, and
asks one no-tools call per fixture whether any finding describes the same
problem. It writes `INBOX/pm-spec-component-check-eval-<date>.md`: per
problem, caught or missed and the finding text; every other finding, for a
person to judge real or false; the cost per component and in total. The raw
findings go beside it as `.json`.

It spends money, so it runs only with PM_SPEC_EVAL=1:

    cd plugins/multiplai-pm/skills/pm-spec/scripts
    PM_SPEC_EVAL=1 uv run --project ../../../../.. --package pm-spec-pipeline \\
        --extra dev python -m pytest tests/eval_component_check -q -s

It stops after the first component's check when that cost more than
EVAL_FIRST_MAX_USD ($8), or when that cost times the number of checks is
above EVAL_TOTAL_MAX_USD ($60). It writes nothing to the Dolcebot repository
but the temporary worktrees, which it removes, and pushes nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures.yaml"
# The report goes to $PM_SPEC_EVAL_OUT, else $WORKSPACE/INBOX; the Dolcebot
# clone is $PM_SPEC_EVAL_REPO, else the `repo` in fixtures.yaml.
INBOX = Path(
    os.environ.get("PM_SPEC_EVAL_OUT")
    or Path(os.environ.get("WORKSPACE", ".")) / "INBOX"
)
EVAL_FIRST_MAX_USD = 8.0
EVAL_TOTAL_MAX_USD = 60.0
PARALLEL = 4

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        os.environ.get("PM_SPEC_EVAL") != "1",
        reason="the replay eval makes real model calls; set PM_SPEC_EVAL=1",
    ),
]


@pytest.fixture(autouse=True)
def model():
    """Replaces the suite's autouse fake model (and its test git identity):
    the eval calls the real one."""
    return


class Match(BaseModel):
    same_problem: bool
    finding: int | None = None
    reason: str = ""


JUDGE_PROMPT = """\
A requirements check reported the findings below on one component's specs.
Separately, a build later raised the requirements problem below on the same
specs. Does any one finding describe the same problem: the same requirement,
and the same conflict or impossibility (it may word it differently, or
propose another fix)? A finding about the same requirement but a different
conflict is not the same problem.

## The problem the build raised
{problem}

## The check's findings
{findings}

Return ONLY JSON: {{"same_problem": true|false, "finding": <the number of the
matching finding, or null>, "reason": "<one sentence>"}}
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def numbered(findings: list[dict]) -> str:
    lines = []
    for n, f in enumerate(findings, 1):
        places = "; ".join(
            f"{p.get('file')} ({p.get('where')})" for p in f.get("places") or []
        )
        lines.append(
            f"{n}. [{f.get('severity')}] ({f.get('category')}) {f.get('description')}"
            f" Places: {places}. Fix: {f.get('suggestion', '')}"
        )
    return "\n".join(lines) or "(no findings)"


async def check_one(wt: Path, change: str, component: str):
    """`run_component_checks` on *component* of the change in worktree *wt*:
    (its findings, its cost)."""
    from pm_spec_pipeline import component_check as CK
    from pm_spec_pipeline.config import SpecConfig
    from pm_spec_pipeline.run import Run
    from pm_spec_pipeline.state import RunState

    run = Run(SpecConfig(wt, change), RunState(change=change))
    checked = await CK.run_component_checks(run, [component])
    result = run.state.component_checks[component]
    return checked.findings, result.cost_usd


async def judge(problem: dict, findings: list[dict], config) -> Match:
    from pm_spec_pipeline import sdk

    return await sdk.text_structured(
        JUDGE_PROMPT.format(problem=problem["text"], findings=numbered(findings)),
        Match,
        config=config,
        step="consistency",
        label=f"eval:judge:{problem['id']}",
    )


def report(fixtures, pairs, results, matches, total, stamp) -> str:
    caught = [p for p in fixtures["problems"] if matches[p["id"]].same_problem]
    out = [
        f"# pm-spec component check: replay on DFACCMGMT-1 ({stamp})",
        "",
        (
            f"**Caught {len(caught)} of {len(fixtures['problems'])}** of P1-P10. "
            f"Total cost ${total:.2f} ({len(pairs)} component checks and "
            f"{len(fixtures['problems'])} judge calls)."
        ),
        "",
        (
            "Each problem is the `spec-issues.md` buildme raised after the handoff; "
            "the check ran on the feature-branch commit that build read "
            f"(`{FIXTURES.name}`). A judge call with no tools decided whether a "
            "finding describes the same problem."
        ),
        "",
        "| # | Component | Commit | Caught | Finding |",
        "|---|---|---|---|---|",
    ]
    for p in fixtures["problems"]:
        m = matches[p["id"]]
        out.append(
            f"| {p['id']} | {p['component']} | `{p['commit']}` | "
            f"{'yes' if m.same_problem else 'no'} | "
            f"{m.finding if m.same_problem else '-'} |"
        )
    out += ["", "## Per problem", ""]
    for p in fixtures["problems"]:
        m = matches[p["id"]]
        findings = results[(p["commit"], p["component"])][0]
        out += [
            f"### {p['id']}: {p['heading']}",
            "",
            f"**{'Caught' if m.same_problem else 'Missed'}.** Judge: {m.reason}",
            "",
        ]
        if m.same_problem and m.finding and 0 < m.finding <= len(findings):
            f = findings[m.finding - 1]
            out += [
                f"> [{f.get('severity')}] ({f.get('category')}) {f.get('description')}",
                ">",
                f"> Fix: {f.get('suggestion', '')}",
                "",
            ]
    out += [
        "## Every other finding",
        "",
        (
            "Findings no problem above matched. Whether each is a real problem or "
            "a false alarm is for a person to judge; this report does not count a "
            "false-positive rate."
        ),
        "",
    ]
    for key in pairs:
        commit, component = key
        findings, cost = results[key]
        used = {
            matches[p["id"]].finding
            for p in fixtures["problems"]
            if (p["commit"], p["component"]) == key and matches[p["id"]].same_problem
        }
        others = [(n, f) for n, f in enumerate(findings, 1) if n not in used]
        out += [f"### {component} at `{commit}` ({len(others)})", ""]
        for n, f in others:
            places = "; ".join(
                f"{p.get('file')} ({p.get('where')})" for p in f.get("places") or []
            )
            out += [
                (
                    f"{n}. [{f.get('severity')}] ({f.get('category')}) "
                    f"{f.get('description')} Places: {places}. Fix: {f.get('suggestion', '')}"
                ),
            ]
        out.append("")
    out += [
        "## Cost",
        "",
        "| Component | Commit | Findings | Cost |",
        "|---|---|---|---|",
    ]
    for key in pairs:
        findings, cost = results[key]
        out.append(f"| {key[1]} | `{key[0]}` | {len(findings)} | ${cost:.2f} |")
    out += ["", f"Total, with the judge calls: ${total:.2f}.", ""]
    return "\n".join(out)


def test_replay_on_the_parked_specs():
    from multiplai_core import pin_session_settings
    from pm_spec_pipeline import sdk
    from pm_spec_pipeline.config import SpecConfig

    pin_session_settings()
    os.environ[sdk.TRUST_ENV] = "1"
    fixtures = yaml.safe_load(FIXTURES.read_text())
    repo = Path(os.environ.get("PM_SPEC_EVAL_REPO") or fixtures["repo"])
    change = fixtures["change"]
    if not (repo / ".git").exists():
        pytest.fail(f"{repo} is not the Dolcebot clone; say where it is")
    pairs = list(
        dict.fromkeys((p["commit"], p["component"]) for p in fixtures["problems"])
    )
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    tmp = Path(tempfile.mkdtemp(prefix="pm-spec-eval-"))
    trees: dict[str, Path] = {}
    sdk.start_budget(EVAL_TOTAL_MAX_USD, None)

    async def main():
        results: dict = {}
        first = pairs[0]
        results[first] = await check_one(trees[first[0]], change, first[1])
        cost = results[first][1]
        projected = cost * len(pairs)
        print(f"EVAL first check {first} cost ${cost:.2f}; projected ${projected:.2f}")
        if cost > EVAL_FIRST_MAX_USD or projected > EVAL_TOTAL_MAX_USD:
            return results, None
        gate = asyncio.Semaphore(PARALLEL)

        async def one(key):
            async with gate:
                results[key] = await check_one(trees[key[0]], change, key[1])
                print(
                    f"EVAL {key} findings={len(results[key][0])} ${results[key][1]:.2f}"
                )

        await asyncio.gather(*(one(k) for k in pairs[1:]))
        config = SpecConfig(trees[first[0]], change)
        verdicts = await asyncio.gather(
            *(
                judge(p, results[(p["commit"], p["component"])][0], config)
                for p in fixtures["problems"]
            )
        )
        return results, {p["id"]: v for p, v in zip(fixtures["problems"], verdicts)}

    try:
        for commit in dict.fromkeys(c for c, _ in pairs):
            trees[commit] = tmp / commit
            git(repo, "worktree", "add", "-q", "--detach", str(trees[commit]), commit)
        results, matches = asyncio.run(main())
    finally:
        for wt in trees.values():
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                check=False,
            )
    total = sdk.ledger().cost_usd
    raw = INBOX / f"pm-spec-component-check-eval-{stamp}.json"
    raw.write_text(
        json.dumps(
            {
                "results": {
                    f"{c}:{comp}": {"findings": f, "cost_usd": cost}
                    for (c, comp), (f, cost) in results.items()
                },
                "matches": {k: v.model_dump() for k, v in (matches or {}).items()},
                "total_usd": total,
            },
            indent=1,
        )
    )
    if matches is None:
        pytest.fail(
            f"stopped after the first check (${results[pairs[0]][1]:.2f}): over "
            f"${EVAL_FIRST_MAX_USD} or projected over ${EVAL_TOTAL_MAX_USD}; see {raw}"
        )
    path = INBOX / f"pm-spec-component-check-eval-{stamp}.md"
    path.write_text(report(fixtures, pairs, results, matches, total, stamp))
    caught = sum(m.same_problem for m in matches.values())
    print(f"EVAL caught {caught} of {len(matches)}; total ${total:.2f}; {path}")
    assert caught >= 8, f"caught {caught} of {len(matches)}; see {path}"
