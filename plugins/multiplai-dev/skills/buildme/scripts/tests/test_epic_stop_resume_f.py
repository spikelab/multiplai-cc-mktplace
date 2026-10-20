"""Row tests for PR F's "Stop and resume states" table
(plan-buildme-epic-mode-2026-10-12.md, the rows marked `Test: yes`: F.S6, and
F.S11, which the PR's table adds for the review slug kept per child).

F.S6 is B1.S13 and E.S12 for a child in a second repository: the user edits
files in that child's own worktree, which belongs to the second repo. As
B1.S13, the coordinator touches nothing, relaunches with the same argv and
compares with a run that never stopped; as E.S12, 40.1 refuses on the
child's `<W>`, the child parks and `row_helpers.repo_state` is unchanged.

B1.S15 and D2.S4 for a child in the second repository (#342) check that a
stopped child's PR is looked for, and its leftover fixer recognised, in that
repository rather than the epic's.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from build_pipeline.config import EpicSettings
from build_pipeline.epic import coordinator, records, store
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic.split import EpicSplitAnswer

from .epic_row_helpers import (
    CallLog,
    Fakes,
    alive,
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
    pr_number,
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


def test_F_S11_review_spend_of_a_child_in_repo_2_still_counts_after_its_checkout_moved(tmp_path, monkeypatch):
    """F.S11: a (second repo) opened PR 12; the review ledger holds a $4.50
    review of it under r2's slug, `o-r2--pr-12`, and each build spent $1. a's
    checkout then moves: a parks (F.S5), and that review still counts in
    `spent_usd`, as `child_status.a.slug` keeps r2's slug. `spent_usd` is
    that of a run whose checkout never moved."""
    steps = {"a": [{"pr_number": 12, "commit": {"a.txt": "a\n"}, "spend": 1.0}],    # pushed: `gh` reads origin
             "b": [{"spend": 1.0}]}
    stopped, clean = FRoot(tmp_path / "stopped", monkeypatch, steps), FRoot(tmp_path / "clean", monkeypatch, steps)
    for r in (stopped, clean):
        _ledger(r.data / "reviews" / "ledger.jsonl", _finished("o-r2--pr-12", "h1", 4.5, target="/r2/pr-12"))
        assert r.run() == 3 and merge_waiting() == ["https://example.invalid/o/r2/pull/12", pr_url("e-b")]
    epic = json.loads((stopped.dir / "epic.json").read_text())
    assert epic["spent_usd"] == 6.5 and epic["spend_complete"] is True
    assert epic["child_status"]["a"]["slug"] == "o-r2" and epic["child_status"]["b"]["slug"] == "o-r"

    (stopped.root / "q").rename(stopped.root / "q-moved")
    stopped.run()
    clean.run()
    moved, kept = (json.loads((r.dir / "epic.json").read_text()) for r in (stopped, clean))
    assert moved["child_status"]["a"]["status"] == "parked"
    assert moved["child_status"]["a"]["slug"] == "o-r2"
    assert moved["spent_usd"] == kept["spent_usd"] == 6.5 and moved["spend_complete"] is kept["spend_complete"] is True


def test_F_S11_review_spend_of_a_child_approved_on_0_65_still_counts(tmp_path, monkeypatch):
    """F.S11 for an epic approved on 0.65.0: its split prompt asked for
    `repo: owner/name`, but every child was built and reviewed in the epic's
    own repo, so a's PR 12 has its $4.50 review under the epic's slug,
    `o-r--pr-12`, and its `child_status` has no `slug`. On this version
    `o/r2` is read as a path that does not exist: a parks (F.S5), its slug
    falls back to the epic's, and that review still counts in `spent_usd`."""
    steps = {"a": [{"pr_number": 12, "commit": {"a.txt": "a\n"}, "spend": 1.0}], "b": [{"spend": 1.0}]}
    r = FRoot(tmp_path, monkeypatch, steps, answer=EpicSplitAnswer(single=False, reason="One repo.",
                                                                    children=[child("a"), child("b")]))
    _ledger(r.data / "reviews" / "ledger.jsonl", _finished("o-r--pr-12", "h1", 4.5, target="/r/pr-12"))
    assert r.run() == 3
    assert json.loads((r.dir / "epic.json").read_text())["spent_usd"] == 6.5

    epic = store.load_epic(r.dir)                         # as 0.65.0 approved and saved it
    epic.children[0].repo = "o/r2"
    for status in epic.child_status.values():
        status.slug = ""
    store.write_split(r.dir, epic)
    epic.hashes = store.file_hashes(r.dir, epic.children)
    store.save_epic(r.dir, epic)
    assert json.loads((r.dir / "epic.json").read_text())["child_status"]["a"].get("slug", "") == ""

    r.run()
    after = json.loads((r.dir / "epic.json").read_text())
    assert after["child_status"]["a"]["status"] == "parked"
    assert after["child_status"]["a"]["repo_park"] == f"repo {r.project / 'o' / 'r2'} not found"
    assert after["child_status"]["a"]["slug"] == "o-r"
    assert after["spent_usd"] == 6.5 and after["spend_complete"] is True


def test_an_interrupted_child_in_repo_2_has_its_pr_found_in_its_own_repo(tmp_path, monkeypatch):
    """B1.S15 for a child in the second repo (#342): its build opened the PR
    and died before printing `PR:`, and the coordinator was killed. `gh pr
    list --head` runs in the second repo's checkout, the only one whose
    `gh` lists that PR, so a is `built` and is not launched again."""
    r = FRoot(tmp_path, monkeypatch, {"a": [{"block": True, "die_before_pr_line": True}]})
    r.killed(die_at(coordinator, "_tick"))
    r.wait_blocked("a")
    r.fakes.release("a")                                     # opens the PR, dies before PR:
    r.wait_gone("a")
    assert list(r.fakes.prs()) == [url2("e-a")]
    assert r.run(_release_all(r)) == 3 and merge_waiting() == [url2("e-a"), pr_url("e-b")]
    assert r.fakes.attempts("a") == 1
    a = json.loads((r.dir / "epic.json").read_text())["child_status"]["a"]
    assert (a["status"], a["pr"]) == ("built", url2("e-a"))


def test_a_fixer_of_a_child_in_repo_2_left_by_a_killed_run_is_waited_for(tmp_path, monkeypatch):
    """D2.S4 for a child in the second repo (#342): the coordinator dies while
    a's fixer runs in a's worktree, which belongs to the second repo. The next
    run sees that fixer as a's (`fix_pid` against a's own repo root) and
    launches no second one while it lives."""
    n = pr_number("e-a")
    high = {"id": "f1", "severity": "HIGH", "status": "confirmed", "claim": "f1 is wrong",
            "file": "src/app.py", "line_start": 3}
    r = FRoot(tmp_path, monkeypatch, {})
    (r.root / "reviews.json").write_text(json.dumps({str(n): [{"findings": [high]}]}))
    (r.root / "fixes.json").write_text(json.dumps({str(n): [{"block": True, "rounds": [{}]}, {"spend": 0}]}))

    def setup():
        def die_while_fixing(coord):
            c = coord.children["a"]
            if c.rattempt is not None and c.rattempt.kind == "fix":
                os._exit(137)

        coordinator._tick = die_while_fixing

    r.killed(setup)
    pid = None
    for _ in range(500):
        pid = coordinator.fix_pid(n, r.repo2)
        if pid:
            break
        time.sleep(0.02)
    assert pid and alive(pid)                                # the fixer outlived its coordinator
    real_start = records.start

    def start(epic_dir, kind, cid, argv, **kw):
        assert kind != "fix" or not alive(pid), "a second fixer started while the first runs"
        return real_start(epic_dir, kind, cid, argv, **kw)

    monkeypatch.setattr(records, "start", start)
    looks = []

    def tick(coord):
        if alive(pid):
            looks.append(1)
            assert len(r.fakes.fix_launches()) == 1          # never a second fixer
            if len(looks) == 5:
                r.fakes.release_fix("e-a")
                for _ in range(500):
                    if not alive(pid):
                        break
                    time.sleep(0.02)

    try:
        r.run(tick)
    finally:
        r.fakes.release_fix("e-a")
    assert len(looks) == 5
