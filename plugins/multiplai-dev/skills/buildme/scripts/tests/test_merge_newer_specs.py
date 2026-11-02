"""`orchestrator._merge_newer_specs`: the merge of newer ready specs from the
feature branch into a parked build's worktree (plan-buildme-epic-stops rows
S5-S8), and each case where it leaves the worktree as it was."""

from __future__ import annotations

import subprocess
from pathlib import Path

from build_pipeline.models import BlockInfo, BlockStatus
from build_pipeline.orchestrator import _merge_newer_specs
from build_pipeline.state import BuildState, TDDState

REL = "specs/changes/c"
BRANCH = "ENG-1-c"
META = "spec_revision: aaa\nspec_status: ready\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _setup(tmp_path: Path, theirs_specs: str = "revised\n") -> Path:
    """A worktree at the handoff, parked: its `.change.yaml` records
    `spec_issues` and it holds an untracked spec-issues.md. `origin/<BRANCH>`
    holds the revised specs one commit later."""
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q", "-b", "build")
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    change = wt / REL
    change.mkdir(parents=True)
    (change / ".change.yaml").write_text(META)
    (change / "specs.md").write_text("first\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "handoff")
    _git(wt, "checkout", "-q", "-b", "revised")
    (change / "specs.md").write_text(theirs_specs)
    (change / ".change.yaml").write_text("spec_revision: bbb\nspec_status: ready\n")
    _git(wt, "commit", "-q", "-am", "revised")
    _git(wt, "update-ref", f"refs/remotes/origin/{BRANCH}", "HEAD")
    _git(wt, "checkout", "-q", "build")
    _git(wt, "branch", "-q", "-D", "revised")
    (change / ".change.yaml").write_text(META + "spec_issues: spec-issues.md\n")
    (change / "spec-issues.md").write_text("## Design: a problem\n")
    return wt


def _state(*statuses: BlockStatus) -> BuildState:
    blocks = [
        BlockInfo(number=i + 1, name=f"B{i}", description="d", status=s)
        for i, s in enumerate(statuses)
    ]
    return BuildState(
        change_name="c",
        tier="standard",
        tdd=TDDState(blocks=blocks) if blocks else None,
    )


def _tree(wt: Path) -> dict[str, bytes]:
    return {
        p.relative_to(wt).as_posix(): p.read_bytes()
        for p in sorted((wt / REL).rglob("*"))
        if p.is_file()
    }


def test_a_parked_planning_build_merges_and_moves_its_issues_aside(tmp_path):
    wt = _setup(tmp_path)
    assert _merge_newer_specs(_state(), wt, REL, BRANCH, "bbbbbbbbbbbbbbbb") is True
    change = wt / REL
    assert (change / "specs.md").read_text() == "revised\n"
    assert not (change / "spec-issues.md").exists()
    assert (change / "spec-issues.resolved-bbbbbbbbbbbb.md").read_text() == "## Design: a problem\n"
    assert _git(wt, "status", "--porcelain", "--untracked-files=no") == ""


def test_blocks_still_pending_count_as_planning(tmp_path):
    wt = _setup(tmp_path)
    assert _merge_newer_specs(_state(BlockStatus.PENDING), wt, REL, BRANCH, "b" * 16)


def test_a_build_that_started_tdd_is_left_as_it_was(tmp_path):
    wt = _setup(tmp_path)
    before = _tree(wt)
    state = _state(BlockStatus.DONE, BlockStatus.PENDING)
    assert _merge_newer_specs(state, wt, REL, BRANCH, "b" * 16) is False
    assert _tree(wt) == before


def test_S7_another_tracked_edit_is_left_as_it_was(tmp_path):
    wt = _setup(tmp_path)
    (wt / REL / "specs.md").write_text("someone's edit\n")
    before = _tree(wt)
    assert _merge_newer_specs(_state(), wt, REL, BRANCH, "b" * 16) is False
    assert _tree(wt) == before


def test_S7_a_change_yaml_edit_beyond_spec_issues_is_left_as_it_was(tmp_path):
    wt = _setup(tmp_path)
    (wt / REL / ".change.yaml").write_text("spec_revision: zzz\nspec_status: ready\nspec_issues: x\n")
    before = _tree(wt)
    assert _merge_newer_specs(_state(), wt, REL, BRANCH, "b" * 16) is False
    assert _tree(wt) == before


def test_a_conflicting_merge_is_aborted_and_both_files_put_back(tmp_path):
    wt = _setup(tmp_path)
    # A commit of the build's own touching the same line as the revision.
    (wt / REL / ".change.yaml").write_text(META)
    (wt / REL / "specs.md").write_text("the build's own\n")
    _git(wt, "commit", "-q", "-am", "build's commit")
    (wt / REL / ".change.yaml").write_text(META + "spec_issues: spec-issues.md\n")
    before = _tree(wt)
    head = _git(wt, "rev-parse", "HEAD")
    assert _merge_newer_specs(_state(), wt, REL, BRANCH, "b" * 16) is False
    assert _tree(wt) == before
    assert _git(wt, "rev-parse", "HEAD") == head
    assert subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=wt
    ).returncode != 0


def test_S6_a_merge_in_progress_is_not_touched(tmp_path):
    wt = _setup(tmp_path)
    (wt / ".git" / "MERGE_HEAD").write_text(_git(wt, "rev-parse", f"origin/{BRANCH}") + "\n")
    before = _tree(wt)
    assert _merge_newer_specs(_state(), wt, REL, BRANCH, "b" * 16) is False
    assert _tree(wt) == before


def test_S5_a_stop_after_the_rename_merges_on_the_next_resume(tmp_path):
    """Killed after spec-issues.md was moved aside and `.change.yaml` put back,
    before the merge: the next resume finds a clean worktree and merges."""
    wt = _setup(tmp_path)
    change = wt / REL
    (change / "spec-issues.md").rename(change / "spec-issues.resolved-bbbbbbbbbbbb.md")
    _git(wt, "checkout", "HEAD", "--", f"{REL}/.change.yaml")
    assert _merge_newer_specs(_state(), wt, REL, BRANCH, "b" * 16) is True
    assert (change / "specs.md").read_text() == "revised\n"


def test_S8_origin_moving_again_merges_again(tmp_path):
    wt = _setup(tmp_path)
    assert _merge_newer_specs(_state(), wt, REL, BRANCH, "b" * 16)
    _git(wt, "checkout", "-q", "-b", "again", f"origin/{BRANCH}")
    (wt / REL / "specs.md").write_text("revised twice\n")
    _git(wt, "commit", "-q", "-am", "again")
    _git(wt, "update-ref", f"refs/remotes/origin/{BRANCH}", "HEAD")
    _git(wt, "checkout", "-q", "build")
    assert _merge_newer_specs(_state(), wt, REL, BRANCH, "c" * 16)
    assert (wt / REL / "specs.md").read_text() == "revised twice\n"
