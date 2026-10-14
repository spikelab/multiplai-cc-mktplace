"""Row tests for PR F's "Stop and resume states" table
(plan-buildme-epic-mode-2026-10-12.md, the rows marked `Test: yes`: F.S6, and
F.S10, which the PR's table adds for the review slug kept per child).

F.S6 is B1.S13 and E.S12 for a child in a second repository: the user edits
files in that child's own worktree, which belongs to the second repo. As
B1.S13, the coordinator touches nothing, relaunches with the same argv and
compares with a run that never stopped; as E.S12, 40.1 refuses on the
child's `<W>`, the child parks and `row_helpers.repo_state` is unchanged.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from build_pipeline.config import EpicSettings
from build_pipeline.epic import coordinator, records
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic.split import EpicSplitAnswer

from .epic_row_helpers import (
    CallLog,
    Fakes,
    approve,
    child,
    commit_names,
    die_at,
    epic_snapshot,
    last_summary,
    make_build,
    make_project,
    merge_waiting,
    name_commits,
    pr_url,
    records_snapshot,
    use_workspace,
)
from .row_helpers import assert_same_as_unstopped, repo_state
from .test_epic_spend import _finished, _ledger
from .test_epic_stop_resume_b1 import Root, _assert_same, _release_all

R2 = "../q"
# a, in the second repo; b in the epic's own.
APART = EpicSplitAnswer(single=False, reason="Two repos.",
                        children=[child("a").model_copy(update={"repo": R2}), child("b")])
CHAIN = EpicSplitAnswer(single=False, reason="Two repos.",
                        children=[child("a").model_copy(update={"repo": R2}), child("b", ("a",))])


def url2(change: str) -> str:
    return f"https://example.invalid/o/r2/pull/{change}"


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


class FRoot(Root):
    """B1's root with a second repository, `<root>/q` (origin `o/r2.git`),
    made before the split is approved, as approval checks it (item 43)."""

    def __init__(self, root: Path, monkeypatch, steps: dict, answer=APART, merge_mode: str = "manual"):
        self.root, self.mp = root, monkeypatch
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.repo2 = make_project(root, "r2", "q")
        self.fakes = Fakes(root, monkeypatch, steps)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=1, merge=merge_mode)
        approve(self.config, answer, CallLog(root / "split.log"))
        self.dir = self.data / "buildme" / "epics" / "o-r--e"
        self.w = root / "ws" / ".worktrees" / "buildme-e-a"

    def snapshot(self) -> dict:
        names = commit_names([self.root / "o" / "r.git", self.project, self.root / "o" / "r2.git", self.repo2])
        return name_commits({"epic": epic_snapshot(self.dir, self.root),
                             "records": records_snapshot(self.dir, self.root),
                             "summary": last_summary(self.dir, self.root)}, names)

    def wait_blocked(self, cid: str) -> None:
        """Until *cid*'s first build waits at its block, past its commit and push."""
        for _ in range(500):
            if (self.root / "blocked" / f"{cid}.1").exists():
                return
            time.sleep(0.02)
        raise AssertionError(f"{cid} never reached its block")

    def step_pushes(self) -> list[dict]:
        """`git push` calls but the fake build's own."""
        return [x for x in CallLog(self.root / "pushes.log").calls() if not x["argv"].startswith("push -q -u")]


def _built_a(*, branch: bool = True, pr: bool = True) -> list[str]:
    return ["WORKTREE:<root>/ws/.worktrees/buildme-e-a", *(["BRANCH:buildme/e-a"] if branch else []),
            *([f"PR:{url2('e-a')}"] if pr else [])]


def test_F_S6_user_edit_in_a_stopped_child_s_worktree_in_repo_2_is_left_alone(tmp_path, monkeypatch):
    """F.S6 as B1.S13: a's worktree belongs to the second repo. After the
    kill the user edits it; the coordinator touches nothing, relaunches a
    with the same argv (`--project-dir <root>/q`), and the edited files are
    byte-identical. One more launch and pair than the never-stopped run."""
    steps = {"a": [{"block": True, "commit": {"a.txt": "a\n"}}, {}]}
    stopped, clean = FRoot(tmp_path / "stopped", monkeypatch, steps), FRoot(tmp_path / "clean", monkeypatch, steps)
    edit = b"my own notes\n\x00binary-ish\n"

    stopped.killed(die_at(coordinator, "_tick"))
    stopped.wait_blocked("a")
    stopped.kill_child("a")
    (stopped.w / "notes.txt").write_bytes(edit)                      # at the kill seam
    (stopped.w / "README.md").write_text("# r2, edited\n")

    def clean_tick(coord):
        if not (clean.w / "notes.txt").exists():
            (clean.w / "notes.txt").write_bytes(edit)
            (clean.w / "README.md").write_text("# r2, edited\n")
        _release_all(clean)(coord)

    assert stopped.run(_release_all(stopped)) == 3 and merge_waiting() == [url2("e-a"), pr_url("e-b")]
    assert clean.run(clean_tick) == 3 and merge_waiting() == [url2("e-a"), pr_url("e-b")]
    for r in (stopped, clean):
        assert (r.w / "notes.txt").read_bytes() == edit
        assert (r.w / "README.md").read_text() == "# r2, edited\n"
    argvs = [a.started["argv"] for a in records.attempts(stopped.dir) if a.child == "a" and a.kind == "build"]
    assert len(argvs) == 2 and argvs[0] == argvs[1]
    assert argvs[0][argvs[0].index("--project-dir") + 1] == str(tmp_path / "stopped" / "q")
    _assert_same(stopped, clean, {"a": [
        ("build", "<argv>", None, _built_a(pr=False), True),
        ("build", "<argv>", 0, _built_a(branch=False), False)]}, ["a"])
    assert_same_as_unstopped(stopped.w, clean.w)


@pytest.mark.parametrize("where", ["README.md", "notes.txt"], ids=["tracked", "untracked"])
def test_F_S6_user_edit_in_repo_2_s_worktree_parks_the_merge_and_is_left_alone(tmp_path, monkeypatch, where):
    """F.S6 as E.S12: under `auto`, a (second repo) stops at the merge step's
    start; the user edits its worktree. 40.1 refuses on that `<W>`, a parks
    naming the file (b, which waits on it, is skipped), nothing is pushed,
    and the worktree is as the user left it."""
    r = FRoot(tmp_path, monkeypatch, {"a": [{"commit": {"a.txt": "a\n"}}]}, answer=CHAIN, merge_mode="auto")

    def die_at_start():
        def dying(coord, c, point):
            if point == "start":
                os._exit(137)
        coordinator._merge_point = dying

    r.killed(die_at_start)
    assert r.w.is_dir() and r.step_pushes() == []
    (r.w / where).write_text("the user's edit\n")
    before = repo_state(r.w)
    assert r.run() == 0
    assert repo_state(r.w) == before
    assert r.status() == {"a": "parked", "b": "skipped"}
    a = json.loads((r.dir / "epic.json").read_text())["child_status"]["a"]
    assert a["merge_park"] == a["reason"] and where in a["reason"]
    assert r.step_pushes() == []


def test_F_S10_review_spend_of_a_child_in_repo_2_still_counts_after_its_checkout_moved(tmp_path, monkeypatch):
    """F.S10: a (second repo) opened PR 12; the review ledger holds a $4.50
    review of it under r2's slug, `o-r2--pr-12`. a's checkout then moves: a
    parks (F.S5), and that review still counts in `spent_usd`, as
    `child_status.a.slug` keeps r2's slug. `spent_usd` is that of a run whose
    checkout never moved."""
    steps = {"a": [{"pr_number": 12}]}
    stopped, clean = FRoot(tmp_path / "stopped", monkeypatch, steps), FRoot(tmp_path / "clean", monkeypatch, steps)
    for r in (stopped, clean):
        _ledger(r.data / "reviews" / "ledger.jsonl", _finished("o-r2--pr-12", "h1", 4.5, target="/r2/pr-12"))
        assert r.run() == 3 and merge_waiting() == ["https://example.invalid/o/r2/pull/12", pr_url("e-b")]
    epic = json.loads((stopped.dir / "epic.json").read_text())
    assert epic["spent_usd"] == 4.5
    assert epic["child_status"]["a"]["slug"] == "o-r2" and epic["child_status"]["b"]["slug"] == "o-r"

    (stopped.root / "q").rename(stopped.root / "q-moved")
    stopped.run()
    clean.run()
    moved, kept = (json.loads((r.dir / "epic.json").read_text()) for r in (stopped, clean))
    assert moved["child_status"]["a"]["status"] == "parked"
    assert moved["child_status"]["a"]["slug"] == "o-r2"
    assert moved["spent_usd"] == kept["spent_usd"] == 4.5 and moved["spend_complete"] is True
