"""T4 of plan-spec-feature-branch-2026-10-22.md: a single change, run from a
checkout that lacks its change dir, builds on the feature branch pm-spec
pushed and opens one PR from it to origin's default branch through the pr
skill, titled `[<default>] <TICKET> <title>`.

Each build is one real `build` process (`stop_resume_driver.py`), with the
model-calling phases replaced by fakes and the pr skill by a recorder."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.spec_fixtures import TICKET
from tests.test_stop_resume_logging import Sandbox

TITLE = "Exports time out after 5 min"


def _git(cwd: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_T4_a_single_change_builds_on_its_feature_branch_and_opens_one_pr(tmp_path):
    box = Sandbox(tmp_path / "box", proposal=f"# {TITLE}\n\n## Why\nSlow.\n")
    feature = f"{TICKET}-feat"
    handed_off = _git(box.repo, "rev-parse", f"origin/{feature}")
    assert not (box.repo / "specs" / "changes" / "feat").exists()

    log = tmp_path / "pr.jsonl"
    proc = box.run(pr_log=str(log))
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]

    # Built on the feature branch itself: no second branch, here or on origin.
    worktree = box.worktree()
    assert _git(worktree, "branch", "--show-current") == feature
    origin = tmp_path / "box" / "origin.git"
    heads = _git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    assert sorted(heads.split()) == sorted(["main", feature])
    pushed = _git(origin, "rev-parse", feature)
    assert pushed == _git(worktree, "rev-parse", "HEAD") != handed_off
    _git(origin, "merge-base", "--is-ancestor", handed_off, pushed)

    # One PR, through the pr skill, from the feature branch to main.
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 1, calls
    (argv,) = calls
    assert argv[4:8] == ["python", "-m", "pr_pipeline", "open"]
    assert argv[argv.index("--head") + 1] == feature
    assert argv[argv.index("--base") + 1] == "main"
    assert argv[argv.index("--title") + 1] == f"[main] {TICKET} {TITLE}"
