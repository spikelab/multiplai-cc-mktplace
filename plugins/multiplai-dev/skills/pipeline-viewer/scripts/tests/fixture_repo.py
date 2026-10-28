"""A synthetic repo with one change at each stop, for the tests and the
manual check (`python tests/fixture_repo.py <dir>`). All content is invented.

    <root>/origin.git        bare origin
    <root>/repo              the main checkout, on main
    <root>/wt/pm-spec-…      pm-spec's detached worktrees (stops 1-3)
    <root>/wt/buildme-…      build worktrees (stop 4 waiting, mid-TDD, stop 5)
    <root>/box               an empty box for the server

| change | where | stop |
|---|---|---|
| export-csv | pm-spec worktree | 1 waiting |
| search-box | pm-spec worktree | 2 waiting |
| dark-mode | pm-spec worktree | 3 waiting |
| rate-limit | build worktree, `plan-approval.md`, buildme's waiting note | 4 waiting |
| audit-log | build worktree, TDD block 2 of 3 (buildme at work: writes refused) | 4 passed, building |
| help-page | build worktree, `.build-state.complete.json` with a PR | 5 waiting |
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PEOPLE = {
    "pm": ("Pat Planner", "pat@example.org", "U0PAT"),
    "engineer": ("Eli Engineer", "eli@example.org", "U0ELI"),
}
CONFIG = """\
pipeline_viewer:
  reviewers:
    pm: [Pat Planner]
    engineer: [Eli Engineer]
  ack_timeout_minutes: 60
  decide_timeout_minutes: 240
  slack: {channel: C0TEST}
"""
STARTED = "2026-01-05T09:00:00+00:00"


def git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd}: {res.stderr}")
    return res.stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _pm_state(change_dir: Path, steps: list[str], split: bool) -> None:
    run = change_dir / ".pm-spec"
    write(run / ".gitignore", "*\n")
    state = {
        "change": change_dir.name,
        "steps": {
            s: {"outcome": "passed", "reason": "Pushed for review"} for s in steps
        },
        "split": {"inputs": "x", "single": False, "children": []} if split else None,
    }
    write(run / "state.json", json.dumps(state))
    write(run / "progress.md", "STAGE started\nReview stop reached\n")


def _docs(d: Path, stop: int) -> None:
    write(
        d / "proposal.md",
        "# Proposal\n\n## Why\n\nPeople want this.\n\n## What changes\n\nA small thing.\n",
    )
    if stop >= 2:
        write(d / "use-cases.md", "# Use cases\n\n## UC1\n\nSomeone uses it.\n")
        write(
            d / "components.md",
            "# Components\n\n| component | depends on |\n|---|---|\n| api | - |\n",
        )
        write(
            d / "components/api/plan.md", "# Plan: api\n\n## Scope\n\nThe endpoint.\n"
        )
    if stop >= 3:
        write(
            d / "components/api/specs.md",
            "# Capability: thing-api\n\n## ADDED Requirements\n",
        )
        write(d / "tickets.json", '{"components": []}\n')
        write(d / "prototype/NOTES.md", "# Prototype notes\n")


def _pm_spec_change(repo: Path, wt_root: Path, name: str, stop: int) -> Path:
    branch = f"TCK-{stop}-{name}"
    git(repo, "checkout", "-q", "-b", branch, "main")
    d = repo / "specs" / "changes" / name
    write(d / ".change.yaml", f"branch: {branch}\nspec_status: draft\n")
    _docs(d, stop)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"specs({name}): stage {stop} for review")
    git(repo, "push", "-q", "-u", "origin", branch)
    git(repo, "checkout", "-q", "main")
    wt = wt_root / f"pm-spec-{branch}"
    git(repo, "worktree", "add", "-q", "--detach", str(wt), f"origin/{branch}")
    steps = ["proposal", "components", "prototype", "tickets", "specs"][
        : {1: 1, 2: 2, 3: 5}[stop]
    ]
    _pm_state(wt / "specs" / "changes" / name, steps, split=stop >= 2)
    return wt


def _build_change(
    repo: Path,
    wt_root: Path,
    name: str,
    state: dict,
    files: dict[str, str],
    done: bool = False,
) -> Path:
    branch = f"TCK-9-{name}"
    git(repo, "branch", branch, "main")
    git(repo, "push", "-q", "-u", "origin", branch)
    wt = wt_root / f"buildme-{name}"
    git(repo, "worktree", "add", "-q", str(wt), branch)
    git(wt, "branch", "-q", "--set-upstream-to", f"origin/{branch}")
    d = wt / "specs" / "changes" / name
    write(d / ".change.yaml", f"branch: {branch}\nspec_status: ready\n")
    _docs(d, 3)
    for rel, text in files.items():
        write(d / rel, text)
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", f"build({name}): plan")
    git(wt, "push", "-q")
    base = git(wt, "rev-parse", "HEAD")
    state = {
        "change_name": name,
        "tier": "standard",
        "branch": branch,
        "base_sha": base,
        "started_at": STARTED,
        "updated_at": "2026-01-05T11:00:00+00:00",
        "budget": {"cost_usd": 4.5, "max_usd": 20.0},
        **state,
    }
    write(
        d / (".build-state.complete.json" if done else ".build-state.json"),
        json.dumps(state),
    )
    write(d / ".board.json", '{"columns": []}\n')
    write(wt / "build-progress.md", "# Build progress\n\nPhase started\n")
    return wt


def build(root: Path) -> dict:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    origin = root / "origin.git"
    git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = root / "repo"
    git(root, "clone", "-q", str(origin), str(repo))
    git(repo, "checkout", "-q", "-b", "main")
    git(repo, "config", "user.name", "Fixture Runner")
    git(repo, "config", "user.email", "runner@example.org")
    git(repo, "config", "commit.gpgsign", "false")
    write(repo / "README.md", "# Sample service\n")
    write(repo / ".gitignore", ".build-state*.json\n.board.json\nbuild-progress.md\n")
    write(repo / "specs" / "config.yaml", CONFIG)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    git(repo, "push", "-q", "-u", "origin", "main")
    wt_root = root / "wt"
    wt_root.mkdir()
    out = {
        "root": root,
        "origin": origin,
        "repo": repo,
        "wt": wt_root,
        "box": root / "box",
    }
    for stop, name in ((1, "export-csv"), (2, "search-box"), (3, "dark-mode")):
        out[name] = _pm_spec_change(repo, wt_root, name, stop)

    plan_files = {
        "implementation-plan.md": "# Implementation plan\n\n## Approach\n\nAdd a token bucket.\n",
        "tasks.md": "# Tasks\n\n## Block 1: bucket\n",
        "plan-approval.md": "# Approve the implementation plan: rate-limit\n\nPlan revision: `planrev4`\n",
    }
    out["rate-limit"] = _build_change(
        repo, wt_root, "rate-limit",
        {"phase": "plan_review", "spec_gen": {"revision": "planrev4"}},
        plan_files,
    )  # fmt: skip
    # buildme's last note while it waits at stop 4 (so writes may go here).
    write(
        out["rate-limit"] / "build-progress.md",
        "# Build progress\n\n## [2026-01-05 11:00] PLAN_APPROVAL\n"
        "Waiting for an engineer to approve implementation-plan.md at revision planrev4; "
        "request in plan-approval.md.\n",
    )

    wt = _build_change(
        repo, wt_root, "audit-log",
        {"phase": "tdd_build", "spec_gen": {"revision": "planrev5"}},
        {"design.md": "# Design\n\n## Approach\n\nAn append-only table.\n", "tasks.md": "# Tasks\n"},
    )  # fmt: skip
    write(wt / "src" / "audit.py", "def record(event):\n    return event\n")
    git(wt, "add", "src/audit.py")
    git(wt, "commit", "-q", "-m", "test+impl: block 1")
    block1 = git(wt, "rev-parse", "HEAD")
    state_path = wt / "specs" / "changes" / "audit-log" / ".build-state.json"
    state = json.loads(state_path.read_text())
    state["plan_approval"] = {
        "revision": "planrev5",
        "by": "Eli Engineer",
        "commit": block1,
    }
    state["tdd"] = {
        "blocks": [
            {"number": 1, "name": "Table", "description": "", "status": "done",
             "review_iterations": 2, "impl_commit": block1, "red_evidence": "1 failed",
             "green_evidence": "1 passed"},
            {"number": 2, "name": "Writer", "description": "", "status": "implementing",
             "timed_out": True, "fix_pass": {"checkpoint": "abc"}},
            {"number": 3, "name": "Reader", "description": "", "status": "pending"},
        ]
    }  # fmt: skip
    state_path.write_text(json.dumps(state))
    write(state_path.parent / "reviews" / "block-01-round-1.json", '{"score": 4}\n')
    out["audit-log"] = wt
    out["audit-block1"] = block1

    out["help-page"] = _build_change(
        repo, wt_root, "help-page",
        {"phase": "complete", "spec_gen": {"revision": "planrev6"},
         "plan_approval": {"revision": "planrev6", "by": "Eli Engineer", "commit": "0" * 40},
         "pr_url": "https://example.org/pulls/7"},
        {"implementation-plan.md": "# Plan\n", "tasks.md": "# Tasks\n"},
        done=True,
    )  # fmt: skip
    return out


if __name__ == "__main__":
    paths = build(Path(sys.argv[1]))
    print(json.dumps({k: str(v) for k, v in paths.items()}, indent=2))
