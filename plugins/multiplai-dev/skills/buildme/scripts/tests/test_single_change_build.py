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


def test_a_refused_rerun_leaves_the_feature_branch_as_it_was(tmp_path):
    """A rerun of a published change without `--force` asks first (exit 3)
    and changes nothing: origin's default branch, moved since, is not merged
    into the feature branch, the head of the open PR."""
    box = Sandbox(tmp_path / "box", proposal=f"# {TITLE}\n\n## Why\nSlow.\n")
    first = box.run()
    assert first.returncode == 0, first.stdout[-1500:] + first.stderr[-1500:]
    origin = tmp_path / "box" / "origin.git"
    feature = f"{TICKET}-feat"
    before = _git(origin, "rev-parse", feature)
    _git(box.repo, "commit", "-q", "--allow-empty", "-m", "main moves")
    _git(box.repo, "push", "-q", "origin", "main")

    again = box.run()

    assert again.returncode == 3, again.stdout[-1500:] + again.stderr[-1500:]
    assert "BUILD:COMPLETE:" in again.stdout
    assert _git(origin, "rev-parse", feature) == before


def test_a_forced_rerun_of_an_archived_change_finds_its_ticket(tmp_path):
    """`--force` after an `--auto` build archived the change: the feature
    branch no longer holds `specs/changes/<name>` and the checkout never did,
    so the ticket comes from where the published build started."""
    box = Sandbox(tmp_path / "box", proposal=f"# {TITLE}\n\n## Why\nSlow.\n")
    first = box.run()
    assert first.returncode == 0, first.stdout[-1500:] + first.stderr[-1500:]

    again = box.run(force=True)

    assert again.returncode == 0, again.stdout[-1500:] + again.stderr[-1500:]
    assert "records no ticket ref" not in again.stdout
    assert f"{TICKET}-feat-2" in _git(box.repo, "branch", "--list", f"{TICKET}-feat-2")
