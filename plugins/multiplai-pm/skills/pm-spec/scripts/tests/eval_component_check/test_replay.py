"""Replay the component check on the DFACCMGMT-1 specs buildme parked on.

`fixtures.yaml` holds the 10 requirements problems (P1-P10) buildme's design,
design-audit and plan-review steps sent back after the handoff, each with the
feature-branch commit its build read. `labels.yaml` holds findings earlier
checks raised on the same commits, each labelled real or false.
`heldout.yaml` holds problems from other changes, reported apart and never
used to edit the prompt (README.md).

For each distinct commit the eval adds a temporary detached worktree of the
fixture's clone and runs `component_check.run_component_checks` on each
component there. Then, with no tools:
- one judge call per problem says whether any finding describes it;
- one judge call per replay check maps each finding to a labelled finding
  of the same commit and component, or to none (`eval_score`).

It writes `INBOX/pm-spec-component-check-eval-<date>[-<tag>].md`, starting with
a table of P1-P10 caught, labelled-real findings kept, labelled-false
findings raised, unlabelled findings and the cost; then each problem, the
held-out problems, each unlabelled finding with a blank verdict, and the cost
per check. The raw findings and the mapping go beside it as `.json`.

It spends money, so it runs only with PM_SPEC_EVAL=1:

    cd plugins/multiplai-pm/skills/pm-spec/scripts
    PM_SPEC_EVAL=1 PM_SPEC_EVAL_TAG=baseline uv run --project ../../../../.. \\
        --package pm-spec-pipeline --extra dev python -m pytest tests/eval_component_check -q -s

After labelling a run's unlabelled findings in `labels.yaml` (each with `run`
set to the run's name and `n` to its number), recount that run with no model
call:

    uv run --project ../../../../.. --package pm-spec-pipeline \\
        python tests/eval_component_check/test_replay.py --rescore <the run's .json>

It stops after the first check when that cost more than EVAL_FIRST_MAX_USD
($8), or when that cost times the number of checks is above
EVAL_TOTAL_MAX_USD ($60). It writes nothing to the repositories but the
temporary worktrees, which it removes, and pushes nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import eval_score as S

FIXTURES = HERE / "fixtures.yaml"
LABELS = HERE / "labels.yaml"
HELDOUT = HERE / "heldout.yaml"
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


def key_of(p: dict, repo: Path, change: str) -> tuple:
    """A check's key: (repo, change, commit, component)."""
    return (
        str(p.get("repo") or repo),
        p.get("change") or change,
        p["commit"],
        p["component"],
    )


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


async def map_labels(key: tuple, findings: list[dict], labels: list[dict], config):
    from pm_spec_pipeline import sdk

    mine = S.of_check(labels, key[2], key[3])
    if not findings or not mine:
        return S.LabelMap()
    return await sdk.text_structured(
        S.label_prompt(mine, numbered(findings)),
        S.LabelMap,
        config=config,
        step="consistency",
        label=f"eval:labels:{key[2]}:{key[3]}",
    )


def assigned_of(data: dict, labels: list[dict], problems: list[dict]) -> dict:
    """(commit, component) -> a label id or None per finding of each replay check."""
    out = {}
    for name, r in data["results"].items():
        if r.get("heldout"):
            continue
        commit, comp = r["commit"], r["component"]
        hits = {
            data["matches"][p["id"]]["finding"]: p["id"]
            for p in problems
            if (p["commit"], p["component"]) == (commit, comp)
            and data["matches"].get(p["id"], {}).get("same_problem")
        }
        answer = data["label_maps"].get(name)
        out[(commit, comp)] = S.assign(
            len(r["findings"]),
            S.LabelMap(**answer) if answer else None,
            S.of_check(labels, commit, comp),
            data["run"],
            hits,
        )
    return out


def finding_line(n: int, f: dict) -> str:
    places = "; ".join(
        f"{p.get('file')} ({p.get('where')})" for p in f.get("places") or []
    )
    return (
        f"{n}. [{f.get('severity')}] ({f.get('category')}) "
        f"{f.get('description')} Places: {places}. Fix: {f.get('suggestion', '')}"
    )


def report(data: dict, fixtures: dict, heldout: dict, labels: list[dict]) -> str:
    problems = fixtures["problems"]
    matches = data["matches"]
    assigned = assigned_of(data, labels, problems)
    s = S.summarize(labels, assigned)
    caught = [p for p in problems if matches[p["id"]]["same_problem"]]
    held = heldout.get("problems") or []
    out = [
        f"# pm-spec component check: replay on DFACCMGMT-1 ({data['run']})",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| P1-P10 caught | {len(caught)} of {len(problems)} |",
        f"| Real kept (labelled real, matched) | {s['real_kept']} of {s['real_labelled']} |",
        f"| Real kept, of the triage set | {s['triage_real_kept']} of {s['triage_real']} |",
        f"| False raised (labelled false, matched) | {s['false_raised']} of {s['false_labelled']} |",
        f"| False raised, of the triage set | {s['triage_false_raised']} of 18 |",
        f"| Unlabelled findings | {s['unlabelled']} |",
        f"| Findings in all | {s['findings']} |",
        f"| Held-out caught | {sum(matches[h['id']]['same_problem'] for h in held if h['id'] in matches)} of {len(held)} |",
        f"| Cost | ${data['total_usd']:.2f} |",
        "",
        (
            "Each problem is the `spec-issues.md` buildme raised after the handoff; "
            "the check ran on the feature-branch commit that build read "
            f"(`{FIXTURES.name}`). Judge calls with no tools decided whether a "
            "finding describes a problem, and which labelled finding "
            f"(`{LABELS.name}`) each finding repeats. A labelled finding counts "
            "once however many findings repeat it; an unlabelled one counts as "
            "neither real nor false."
        ),
        "",
        "| # | Component | Commit | Caught | Finding |",
        "|---|---|---|---|---|",
    ]
    for p in problems:
        m = matches[p["id"]]
        out.append(
            f"| {p['id']} | {p['component']} | `{p['commit']}` | "
            f"{'yes' if m['same_problem'] else 'no'} | "
            f"{m['finding'] if m['same_problem'] else '-'} |"
        )

    def per_problem(p, findings):
        m = matches[p["id"]]
        lines = [
            f"### {p['id']}: {p['heading']}",
            "",
            f"**{'Caught' if m['same_problem'] else 'Missed'}.** Judge: {m['reason']}",
            "",
        ]
        n = m["finding"]
        if m["same_problem"] and n and 0 < n <= len(findings):
            f = findings[n - 1]
            lines += [
                f"> [{f.get('severity')}] ({f.get('category')}) {f.get('description')}",
                ">",
                f"> Fix: {f.get('suggestion', '')}",
                "",
            ]
        return lines

    by_check = {(r["commit"], r["component"]): r for r in data["results"].values()}
    out += ["", "## Per problem", ""]
    for p in problems:
        out += per_problem(p, by_check[(p["commit"], p["component"])]["findings"])
    out += [
        "## Held-out problems",
        "",
        (
            f"From `{HELDOUT.name}`: problems of other changes, never used to edit "
            "the prompt. Their checks' other findings are not labelled or counted."
        ),
        "",
    ]
    for h in held:
        if h["id"] not in matches:
            out += [f"### {h['id']}: not run", ""]
            continue
        hr = next(r for r in data["results"].values() if r.get("heldout") == h["id"])
        out += per_problem(h, hr["findings"])
        out += [
            f"Its check raised {len(hr['findings'])} findings at `{h['commit']}`.",
            "",
        ]
    out += [
        "## Unlabelled findings",
        "",
        (
            "Findings that repeat no labelled finding. Each needs a verdict read "
            "from the code and specs at its commit before it goes into "
            f"`{LABELS.name}` with `run: {data['run']}` and its `n` (README.md)."
        ),
        "",
    ]
    for (commit, comp), ids in assigned.items():
        none = [n for n, i in enumerate(ids, 1) if i is None]
        if not none:
            continue
        findings = by_check[(commit, comp)]["findings"]
        out += [f"### {comp} at `{commit}` ({len(none)})", ""]
        for n in none:
            out += [finding_line(n, findings[n - 1]), "", "Verdict: ", ""]
    out += [
        "## Labels per check",
        "",
        "| Component | Commit | Findings | Real kept | False raised | Unlabelled | Cost |",
        "|---|---|---|---|---|---|---|",
    ]
    by_id = {lab["id"]: lab for lab in labels}
    for r in data["results"].values():
        key = (r["commit"], r["component"])
        ids = assigned.get(key)
        if ids is None:
            out.append(
                f"| {key[1]} (held out) | `{key[0]}` | {len(r['findings'])} | - | - | - | ${r['cost_usd']:.2f} |"
            )
            continue
        hit = {i for i in ids if i}
        real = sum(
            by_id.get(i, {}).get("verdict") == "real" or i.startswith("problem:")
            for i in hit
        )
        false = sum(by_id.get(i, {}).get("verdict") == "false" for i in hit)
        out.append(
            f"| {key[1]} | `{key[0]}` | {len(ids)} | {real} | {false} | "
            f"{sum(i is None for i in ids)} | ${r['cost_usd']:.2f} |"
        )
    missed = [
        lab
        for lab in labels
        if (lab["commit"], lab["component"]) in assigned
        and lab["id"] not in {i for ids in assigned.values() for i in ids if i}
    ]
    out += [
        "",
        "## Labelled findings not raised this run",
        "",
        *[
            f"- `{lab['id']}` ({lab['verdict']}): {lab['description'][:200]}"
            for lab in missed
        ],
        "",
        f"Total, with the judge calls: ${data['total_usd']:.2f}.",
        "",
    ]
    return "\n".join(out)


def write_report(raw: Path, data: dict) -> Path:
    fixtures = yaml.safe_load(FIXTURES.read_text())
    heldout = yaml.safe_load(HELDOUT.read_text()) if HELDOUT.exists() else {}
    path = raw.with_suffix(".md")
    path.write_text(report(data, fixtures, heldout, S.load_labels(LABELS)))
    return path


def test_replay_on_the_parked_specs():
    from multiplai_core import pin_session_settings
    from pm_spec_pipeline import sdk
    from pm_spec_pipeline.config import SpecConfig

    pin_session_settings()
    os.environ[sdk.TRUST_ENV] = "1"
    fixtures = yaml.safe_load(FIXTURES.read_text())
    heldout = yaml.safe_load(HELDOUT.read_text()) if HELDOUT.exists() else {}
    labels = S.load_labels(LABELS)
    repo = Path(os.environ.get("PM_SPEC_EVAL_REPO") or fixtures["repo"])
    change = fixtures["change"]
    if not (repo / ".git").exists():
        pytest.fail(f"{repo} is not the Dolcebot clone; say where it is")
    problems = fixtures["problems"]
    held = heldout.get("problems") or []
    keys = list(dict.fromkeys(key_of(p, repo, change) for p in problems))
    held_keys = {h["id"]: key_of(h, repo, change) for h in held}
    checks = keys + [k for k in dict.fromkeys(held_keys.values()) if k not in keys]
    tag = os.environ.get("PM_SPEC_EVAL_TAG", "").strip()
    run_name = datetime.now(UTC).strftime("%Y-%m-%d") + (f"-{tag}" if tag else "")
    tmp = Path(tempfile.mkdtemp(prefix="pm-spec-eval-"))
    trees: dict[tuple[str, str], Path] = {}
    sdk.start_budget(EVAL_TOTAL_MAX_USD, None)

    async def main():
        results: dict = {}
        first = checks[0]
        results[first] = await check_one(trees[first[0], first[2]], first[1], first[3])
        cost = results[first][1]
        projected = cost * len(checks)
        print(
            f"EVAL first check {first[2:]} cost ${cost:.2f}; projected ${projected:.2f}"
        )
        if cost > EVAL_FIRST_MAX_USD or projected > EVAL_TOTAL_MAX_USD:
            return results, None, None
        gate = asyncio.Semaphore(PARALLEL)

        async def one(key):
            async with gate:
                results[key] = await check_one(trees[key[0], key[2]], key[1], key[3])
                print(
                    f"EVAL {key[2:]} findings={len(results[key][0])} ${results[key][1]:.2f}"
                )

        await asyncio.gather(*(one(k) for k in checks[1:]))
        config = SpecConfig(trees[first[0], first[2]], change)
        judged = [(p, key_of(p, repo, change)) for p in [*problems, *held]]
        verdicts = await asyncio.gather(
            *(judge(p, results[k][0], config) for p, k in judged)
        )
        maps = await asyncio.gather(
            *(map_labels(k, results[k][0], labels, config) for k in keys)
        )
        return (
            results,
            {p["id"]: v for (p, _), v in zip(judged, verdicts)},
            dict(zip(keys, maps)),
        )

    try:
        for r in dict.fromkeys(Path(k[0]) for k in checks):
            git(r, "worktree", "prune")
        for k in checks:
            if (k[0], k[2]) in trees:
                continue
            wt = trees[k[0], k[2]] = tmp / f"{Path(k[0]).name}-{k[2]}"
            git(Path(k[0]), "worktree", "add", "-q", "--detach", str(wt), k[2])
        results, matches, maps = asyncio.run(main())
    finally:
        for (r, _), wt in trees.items():
            subprocess.run(
                ["git", "-C", r, "worktree", "remove", "--force", str(wt)],
                check=False,
            )
    total = sdk.ledger().cost_usd
    held_of = {v: hid for hid, v in held_keys.items()}

    def name(k):
        return f"{k[2]}:{k[3]}"

    data = {
        "run": run_name,
        "results": {
            name(k): {
                "repo": k[0],
                "change": k[1],
                "commit": k[2],
                "component": k[3],
                "heldout": held_of.get(k) if k not in keys else None,
                "findings": f,
                "cost_usd": cost,
            }
            for k, (f, cost) in results.items()
        },
        "matches": {k: v.model_dump() for k, v in (matches or {}).items()},
        "label_maps": {name(k): v.model_dump() for k, v in (maps or {}).items()},
        "total_usd": total,
    }
    raw = INBOX / f"pm-spec-component-check-eval-{run_name}.json"
    raw.write_text(json.dumps(data, indent=1))
    if matches is None:
        pytest.fail(
            f"stopped after the first check (${results[checks[0]][1]:.2f}): over "
            f"${EVAL_FIRST_MAX_USD} or projected over ${EVAL_TOTAL_MAX_USD}; see {raw}"
        )
    path = write_report(raw, data)
    caught = sum(matches[p["id"]].same_problem for p in problems)
    print(f"EVAL caught {caught} of {len(problems)}; total ${total:.2f}; {path}")
    assert caught >= 8, f"caught {caught} of {len(problems)}; see {path}"


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--rescore":
        sys.exit("usage: test_replay.py --rescore <the run's .json>")
    raw = Path(sys.argv[2])
    print(write_report(raw, json.loads(raw.read_text())))
