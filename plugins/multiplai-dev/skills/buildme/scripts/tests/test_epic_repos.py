"""Epic mode, PR F: children in other repos (plan-buildme-epic-mode-2026-10-12.md,
items 43-44). A child's `repo` names a local checkout, absolute or relative to
the epic's repository root: it becomes the child's `--project-dir`, its PR
is read and reviewed in that repo, and it keys the merge lock. Fakes as in
`test_epic_run`, with a second repository `<root>/q` cloned from
`<root>/o/r2.git`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from build_pipeline.config import EpicSettings
from build_pipeline.epic import coordinator, records, store
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic import split as split_mod
from build_pipeline.epic.split import EpicSplitAnswer

from .epic_row_helpers import (
    CallLog,
    FakeSplit,
    Fakes,
    approve,
    args_for,
    child,
    make_build,
    make_project,
    merge_waiting,
    pr_url,
    run_build,
    run_coordinator,
    use_workspace,
)

REPO2 = "../q"


def in_repo2(c, repo: str = REPO2):
    return c.model_copy(update={"repo": repo})


# b, in the second repo, waits for a's merge in the first (item 44).
CROSS = EpicSplitAnswer(single=False, reason="Two repos.", children=[child("a"), in_repo2(child("b", ("a",)))])
APART = EpicSplitAnswer(single=False, reason="Two repos.", children=[child("a"), in_repo2(child("b"))])


def url2(change: str) -> str:
    return f"https://example.invalid/o/r2/pull/{change}"


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


def git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


class TwoRepos:
    """The epic's repo `<root>/p` (origin `o/r.git`) and a second one,
    `<root>/q` (origin `o/r2.git`); the fakes on PATH; an approved split."""

    def __init__(self, root: Path, monkeypatch, answer=CROSS, steps=None, merge_mode="manual", parallel=2):
        self.root, self.mp = root, monkeypatch
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.repo2 = make_project(root, "r2", "q")
        self.fakes = Fakes(root, monkeypatch, steps)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=parallel, merge=merge_mode)
        approve(self.config, answer, CallLog(root / "split.log"))
        self.dir = self.data / "buildme" / "epics" / "o-r--e"

    def run(self) -> int:
        self.fakes.use(self.mp)
        use_workspace(self.mp, self.root)
        return run_coordinator(self.config)

    def launches(self) -> list[list[str]]:
        return [x["argv"] for x in CallLog(self.root / "calls.log").calls() if x["kind"] == "build"]

    def child_status(self) -> dict:
        return json.loads((self.dir / "epic.json").read_text())["child_status"]

    def status(self) -> dict:
        return {k: v["status"] for k, v in self.child_status().items()}

    def tries(self, cid: str, kind: str) -> list[records.Attempt]:
        return [a for a in records.attempts(self.dir) if a.child == cid and a.kind == kind]


def _opt(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


# --- approval (item 43, F.S1) ----------------------------------------------------

@pytest.mark.parametrize("case", ["missing", "no-origin", "not-a-checkout"])
def test_approval_refuses_a_repo_that_is_not_a_checkout_with_origin(tmp_path, monkeypatch, capsys, case):
    """F.S1: a missing repo, one without `origin`, or a directory git does not
    know exits 1 naming the child and the repo; nothing is approved."""
    use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)
    if case == "no-origin":
        make_project(tmp_path, "r2", "q")
        git(tmp_path / "q", "remote", "remove", "origin")
    elif case == "not-a-checkout":
        (tmp_path / "q").mkdir()
    config = make_build(project)
    with patch.object(split_mod, "epic_split", FakeSplit(CallLog(tmp_path / "split.log"), CROSS)):
        assert run_build(config, args_for(config)) == 3
    epic_lock.release_all()
    with patch.object(coordinator, "run_epic") as run:
        assert run_build(config, args_for(config, approve_split=True)) == 1
    run.assert_not_called()
    err = capsys.readouterr().err
    assert "the split cannot be approved" in err and "child b" in err and "repo ../q" in err
    epic = store.load_epic(tmp_path / "ws" / ".multiplai" / "data" / "buildme" / "epics" / "o-r--e")
    assert epic.state == "proposed" and epic.hashes == {}


def test_approval_takes_an_absolute_repo_and_keeps_it_as_written(tmp_path, monkeypatch):
    """An absolute path is a checkout too; split.md and epic.json keep the
    path as the table gives it."""
    use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)
    make_project(tmp_path, "r2", "q")
    config = make_build(project)
    answer = EpicSplitAnswer(single=False, reason="Two repos.",
                             children=[child("a"), in_repo2(child("b"), str(tmp_path / "q"))])
    approve(config, answer, CallLog(tmp_path / "split.log"))
    epic = store.load_epic(tmp_path / "ws" / ".multiplai" / "data" / "buildme" / "epics" / "o-r--e")
    assert epic.state == "approved" and epic.children[1].repo == str(tmp_path / "q")


# --- a child in another repo (items 43, 44; F.S3) ----------------------------------

def test_a_child_in_another_repo_waits_for_the_merge_in_the_first(tmp_path, monkeypatch):
    """Item 44: b, in the second repo, depends on a in the first. b launches
    only once a's PR has merged, in its own repo (`--project-dir <root>/q`,
    based on r2's origin main), and its PR is reviewed under r2's slug."""
    e = TwoRepos(tmp_path, monkeypatch, steps={"a": [{"commit": {"a.txt": "a\n"}}],
                                               "b": [{"commit": {"b.txt": "b\n"}}]})
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert e.status() == {"a": "built", "b": "waiting"}
    [a_argv] = e.launches()
    assert _opt(a_argv, "--project-dir") == "<root>/p"

    e.fakes.set_pr(pr_url("e-a"), "MERGED")
    assert e.run() == 3 and merge_waiting() == [url2("e-b")]          # F.S3: merged makes b ready
    assert e.status() == {"a": "merged", "b": "built"}
    b_argv = e.launches()[-1]
    assert len(e.launches()) == 2
    assert _opt(b_argv, "--project-dir") == "<root>/q"
    assert _opt(b_argv, "--base-ref") == git(tmp_path / "o" / "r2.git", "rev-parse", "main").strip()
    wt = tmp_path / "ws" / ".worktrees" / "buildme-e-b"
    assert Path(git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()) == tmp_path / "q" / ".git"
    [review] = e.tries("b", "review")
    assert _opt(review.started["argv"], "--repo") == str(wt)
    assert review.result["blocking"] == [] and review.result["review_dir"].endswith("/reviews/o-r2/pr-"
                                                                                     + _opt(review.started["argv"], "--pr"))

    e.fakes.set_pr(url2("e-b"), "MERGED")
    assert e.run() == 0 and e.status() == {"a": "merged", "b": "merged"}
    assert len(e.launches()) == 2


def test_merges_in_two_repos_take_one_lock_each(tmp_path, monkeypatch):
    """F.S4: under `auto`, each child's merge step holds the lock of its own
    repo root, so merges in two repos never wait on each other."""
    e = TwoRepos(tmp_path, monkeypatch, answer=APART, merge_mode="auto",
                 steps={"a": [{"commit": {"a.txt": "a\n"}}], "b": [{"commit": {"b.txt": "b\n"}}]})
    held: list[tuple[str, str, bool]] = []
    real = coordinator.Coordinator._merge_steps

    async def spy(self, c, w):
        key = str(c.root.resolve())
        held.append((c.id, key, key in self.merge_locks and self.merge_locks[key].locked()))
        return await real(self, c, w)

    monkeypatch.setattr(coordinator.Coordinator, "_merge_steps", spy)
    assert e.run() == 3 and sorted(merge_waiting()) == sorted([pr_url("e-a"), url2("e-b")])
    assert sorted(held) == [("a", str((tmp_path / "p").resolve()), True), ("b", str((tmp_path / "q").resolve()), True)]


def test_a_checkout_moved_after_approval_parks_its_child(tmp_path, monkeypatch):
    """F.S5: b's checkout is gone; b parks `repo <path> not found`, a still builds."""
    e = TwoRepos(tmp_path, monkeypatch, answer=APART)
    (tmp_path / "q").rename(tmp_path / "q-moved")
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert e.status() == {"a": "built", "b": "parked"}
    assert e.child_status()["b"]["reason"] == f"repo {tmp_path / 'q'} not found"
    assert [Path(_opt(x, "--plan-file")).stem for x in e.launches()] == ["a"]
    (tmp_path / "q-moved").rename(tmp_path / "q")         # put back: the park stays (repo_park)
    assert e.run() == 3 and e.status() == {"a": "built", "b": "parked"}
    assert e.child_status()["b"]["repo_park"] == f"repo {tmp_path / 'q'} not found"
    assert len(e.launches()) == 1


def test_tracked_changes_in_the_second_repo_are_refused_by_the_child_build(tmp_path, monkeypatch):
    """F.S2: the child's own `preflight` refuses tracked changes in its repo
    (as B1.S12); the coordinator keeps and discards nothing there."""
    e = TwoRepos(tmp_path, monkeypatch, answer=APART)
    (tmp_path / "q" / "README.md").write_text("# a tracked change\n")
    assert e.run() == 1
    assert e.status() == {"a": "built", "b": "failed"}
    assert (tmp_path / "q" / "README.md").read_text() == "# a tracked change\n"
