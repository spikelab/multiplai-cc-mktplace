"""Row tests for PR B1's "Stop and resume states" table
(plan-buildme-epic-mode-2026-10-12.md, rows marked `Test: yes`).

Each case stops the coordinator at the row's point (a forked child exiting
137, as SIGKILL would; Ctrl-C as `KeyboardInterrupt` at a seam) or applies
the row's perturbation, resumes, and compares with the same scenario run
without a stop in a second temporary root: the epic directory, the records
per child as (kind, argv, exit_code, markers, interrupted), the last summary
and the launches. The only differences allowed are the ones the row's
"Resume does" names.
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import pytest

from build_pipeline.config import EpicSettings
from build_pipeline.epic import coordinator, records
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic import split as epic_split_mod
from build_pipeline.epic.split import EpicSplitAnswer
from build_pipeline.models import BuildPhase
from build_pipeline.state import BuildState

from .epic_row_helpers import (
    CallLog,
    FakeSplit,
    Fakes,
    SINGLE,
    approve,
    args_for,
    child,
    commit_names,
    die_at,
    epic_snapshot,
    last_summary,
    make_build,
    make_project,
    name_commits,
    records_snapshot,
    run_build,
    run_coordinator,
    run_killed,
    state_summary,
    use_workspace,
)

TWO = EpicSplitAnswer(single=False, reason="Two parts.", children=[child("a"), child("b")])


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


class Root:
    """One root: project, workspace, fakes, an approved two-child epic."""

    def __init__(self, root: Path, monkeypatch, steps: dict, parallel: int = 1):
        self.root, self.mp = root, monkeypatch
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.fakes = Fakes(root, monkeypatch, steps)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=parallel)
        approve(self.config, TWO, CallLog(root / "split.log"))
        self.dir = self.data / "buildme" / "epics" / "o-r--e"

    def use(self) -> None:
        use_workspace(self.mp, self.root)
        self.fakes.use(self.mp)

    def run(self, tick=None) -> int:
        self.use()
        self.mp.setattr(coordinator, "_tick", tick or (lambda coord: None))
        return run_coordinator(self.config)

    def killed(self, setup, tick=None) -> None:
        self.use()
        self.mp.setattr(coordinator, "_tick", tick or (lambda coord: None))
        run_killed(lambda: run_coordinator(self.config), setup)

    def plan(self, cid: str) -> Path:
        return self.dir / "plans" / f"{cid}.md"

    def kill_child(self, cid: str) -> None:
        pid = coordinator.build_pid(self.plan(cid))
        assert pid, f"{cid} is not running"
        os.killpg(pid, signal.SIGKILL)
        self.wait_gone(cid)

    def wait_gone(self, cid: str) -> None:
        for _ in range(500):
            if coordinator.build_pid(self.plan(cid)) is None:
                return
            time.sleep(0.02)
        raise AssertionError(f"{cid} never exited")

    def snapshot(self) -> dict:
        names = commit_names([self.root / "o" / "r.git", self.project])
        return name_commits({"epic": epic_snapshot(self.dir, self.root),
                             "records": records_snapshot(self.dir, self.root),
                             "summary": last_summary(self.dir, self.root)}, names)

    def launches(self) -> list[dict]:
        return CallLog(self.root / "calls.log").calls()


def _roots(tmp_path, monkeypatch, steps, parallel=1) -> tuple[Root, Root]:
    return (Root(tmp_path / "stopped", monkeypatch, steps, parallel),
            Root(tmp_path / "clean", monkeypatch, steps, parallel))


def _release_all(root: Root):
    def tick(coord):
        for cid in ("a", "b"):
            root.fakes.release(cid)
    return tick


def _assert_same(stopped: Root, clean: Root, records: dict | None = None,
                 extra_launches: list[str] = ()) -> None:
    """Same epic files and summary; records equal except the children named in
    *records* (stopped-run records given whole, with "<argv>" for the child's
    one argv); launches equal plus one per child id in *extra_launches*."""
    s, c = stopped.snapshot(), clean.snapshot()
    assert s["epic"] == c["epic"]
    assert s["summary"] == c["summary"]
    for cid, got in s["records"].items():
        if records and cid in records:
            argv = c["records"][cid][0][1]
            want = [(k, argv if a == "<argv>" else a, e, m, i) for k, a, e, m, i in records[cid]]
            assert got == want, cid
        else:
            assert got == c["records"][cid], cid

    def key(calls):
        return sorted(json.dumps(x, sort_keys=True) for x in calls)

    by_child = {}
    for call in clean.launches():
        cid = Path(call["argv"][call["argv"].index("--plan-file") + 1]).stem
        by_child[cid] = call
    assert key(stopped.launches()) == key([*clean.launches(), *(by_child[c] for c in extra_launches)])


def _built(cid: str, *, branch: bool = True) -> list[str]:
    wt = f"WORKTREE:<root>/ws/.worktrees/buildme-e-{cid}"
    return [wt, *([f"BRANCH:buildme/e-{cid}"] if branch else []),
            f"PR:https://example.invalid/o/r/pull/e-{cid}"]


def test_B1_S4_coordinator_killed_with_the_child_alive_adopts_it(tmp_path, monkeypatch):
    """B1.S4: no launch for the live child; once it is gone, B1.S5 (its result
    is written from the log: built, `exit_code: null`, `interrupted: true`)."""
    steps = {"a": [{"block": True}]}
    stopped, clean = _roots(tmp_path, monkeypatch, steps)
    stopped.killed(die_at(coordinator, "_tick"))
    assert coordinator.build_pid(stopped.plan("a")) is not None      # the child outlived it
    assert stopped.run(_release_all(stopped)) == 0
    assert clean.run(_release_all(clean)) == 0
    _assert_same(stopped, clean, {"a": [("build", "<argv>", None, _built("a"), True)]})


@pytest.mark.parametrize("pr", [True, False], ids=["pr-found", "no-pr"])
def test_B1_S5_child_gone_without_a_result(tmp_path, monkeypatch, pr):
    """B1.S5: the result comes from the log. PR found: built, `exit_code: null`,
    `interrupted: true`. None: relaunch with the same argv, one more launch and pair."""
    steps = {"a": [{"block": True}, {}]}
    stopped, clean = _roots(tmp_path, monkeypatch, steps)
    stopped.killed(die_at(coordinator, "_tick"))
    if pr:
        stopped.fakes.release("a")
        stopped.wait_gone("a")
        want = [("build", "<argv>", None, _built("a"), True)]
    else:
        stopped.kill_child("a")
        want = [("build", "<argv>", None, _built("a")[:2], True),
                ("build", "<argv>", 0, [_built("a")[0], _built("a")[2]], False)]
    assert stopped.run(_release_all(stopped)) == 0
    assert clean.run(_release_all(clean)) == 0
    _assert_same(stopped, clean, {"a": want}, [] if pr else ["a"])


def test_B1_S6_ctrl_c_interrupts_every_child_and_resume_relaunches_each(tmp_path, monkeypatch):
    """B1.S6: SIGINT reaches each child's group; results `interrupted: true`;
    the next run relaunches each, one more launch and pair each."""
    steps = {"a": [{"block": True}, {}], "b": [{"block": True}, {}]}
    stopped, clean = _roots(tmp_path, monkeypatch, steps, parallel=2)

    def ctrl_c(coord):
        raise KeyboardInterrupt

    assert stopped.run(ctrl_c) == 130
    for cid in ("a", "b"):
        first = records_snapshot(stopped.dir, stopped.root)[cid][0]
        assert first[2] not in (0, None) and first[4] is True     # interrupted, by a signal
    assert stopped.run(_release_all(stopped)) == 0
    assert clean.run(_release_all(clean)) == 0
    s = records_snapshot(stopped.dir, stopped.root)
    _assert_same(stopped, clean, {
        cid: [("build", "<argv>", s[cid][0][2], _built(cid)[:2], True),
              ("build", "<argv>", 0, [_built(cid)[0], _built(cid)[2]], False)]
        for cid in ("a", "b")}, ["a", "b"])


def test_B1_S7_killed_after_the_result_before_epic_json(tmp_path, monkeypatch):
    """B1.S7: status is rebuilt from the records; the built child is not relaunched."""
    stopped, clean = _roots(tmp_path, monkeypatch, {})
    stopped.killed(die_at(records, "finish", after=True))
    assert json.loads((stopped.dir / "epic.json").read_text())["child_status"]["a"]["status"] == "running"
    assert stopped.run() == 0
    assert clean.run() == 0
    _assert_same(stopped, clean)


def test_B1_S10_failed_then_killed_before_the_relaunch(tmp_path, monkeypatch):
    """B1.S10: the next run relaunches it once, as the never-stopped run did: equal."""
    steps = {"a": [{"exit": 1}, {}]}
    stopped, clean = _roots(tmp_path, monkeypatch, steps)

    def die_on_relaunch():
        real = records.start

        def start(epic_dir, kind, child, argv, **kw):
            if child == "a" and any(a.child == "a" for a in records.attempts(epic_dir)):
                os._exit(137)
            return real(epic_dir, kind, child, argv, **kw)

        records.start = start

    stopped.killed(die_on_relaunch)
    assert stopped.fakes.attempts("a") == 1
    assert stopped.run() == 0
    assert clean.run() == 0
    _assert_same(stopped, clean)


def test_B1_S13_user_edit_in_a_stopped_childs_worktree_is_left_alone(tmp_path, monkeypatch):
    """B1.S13: the coordinator touches nothing; the relaunch has the same argv;
    the edited file is byte-identical. Otherwise as B1.S5 (no PR found)."""
    steps = {"a": [{"block": True}, {}]}
    stopped, clean = _roots(tmp_path, monkeypatch, steps)
    edit = b"my own notes\n\x00binary-ish\n"

    def wt(root: Root) -> Path:
        return root.root / "ws" / ".worktrees" / "buildme-e-a"

    stopped.killed(die_at(coordinator, "_tick"))
    stopped.kill_child("a")
    (wt(stopped) / "notes.txt").write_bytes(edit)                     # at the kill seam
    (wt(stopped) / "README.md").write_text("# r, edited\n")

    def clean_tick(coord):
        if not (wt(clean) / "notes.txt").exists():
            (wt(clean) / "notes.txt").write_bytes(edit)
            (wt(clean) / "README.md").write_text("# r, edited\n")
        _release_all(clean)(coord)

    assert stopped.run(_release_all(stopped)) == 0
    assert clean.run(clean_tick) == 0
    for root in (stopped, clean):
        assert (wt(root) / "notes.txt").read_bytes() == edit
        assert (wt(root) / "README.md").read_text() == "# r, edited\n"
    argvs = [a.started["argv"] for a in records.attempts(stopped.dir) if a.child == "a"]
    assert len(argvs) == 2 and argvs[0] == argvs[1]
    _assert_same(stopped, clean, {"a": [
        ("build", "<argv>", None, _built("a")[:2], True),
        ("build", "<argv>", 0, [_built("a")[0], _built("a")[2]], False)]}, ["a"])


def test_B1_S15_stopped_twice_the_relaunch_opened_the_pr(tmp_path, monkeypatch):
    """B1.S15: the PR is found through the first log's BRANCH: and `gh pr list
    --head`; no third launch, no second PR; both results null and interrupted."""
    steps = {"a": [{"block": True}, {"block": True, "die_before_pr_line": True}]}
    stopped, clean = _roots(tmp_path, monkeypatch, steps)
    stopped.killed(die_at(coordinator, "_tick"))
    stopped.kill_child("a")                                         # stopped once
    stopped.killed(die_at(coordinator, "_tick"))                   # the relaunch runs ...
    stopped.fakes.release("a")                                      # ... opens the PR, dies before PR:
    stopped.wait_gone("a")
    assert list(stopped.fakes.prs()) == ["https://example.invalid/o/r/pull/e-a"]
    assert stopped.run(_release_all(stopped)) == 0
    assert clean.run(_release_all(clean)) == 0
    assert stopped.fakes.attempts("a") == 2 and len(stopped.fakes.prs()) == 2
    _assert_same(stopped, clean, {"a": [
        ("build", "<argv>", None, _built("a")[:2], True),
        ("build", "<argv>", None, _built("a")[:1], True)]}, ["a"])


def _die_at_epic_split_skip() -> None:
    """A setup for `run_killed`: exit 137 as the skip saves EPIC_SPLIT."""
    real = BuildState.advance_to

    def advance_to(self, phase, path=None):
        if phase == BuildPhase.EPIC_SPLIT:
            os._exit(137)
        return real(self, phase, path)

    BuildState.advance_to = advance_to


def test_B1_S18_a_child_stopped_around_epic_split_never_calls_it(tmp_path, monkeypatch):
    """B1.S18: the relaunch argv carries --no-epic-split, and a child build
    stopped at its EPIC_SPLIT skip makes no `epic_split` call on resume."""
    from build_pipeline.__main__ import build_parser

    stopped = Root(tmp_path / "coord", monkeypatch, {"a": [{"exit": 1}, {}]})
    assert stopped.run() == 0
    argvs = [a.started["argv"] for a in records.attempts(stopped.dir) if a.child == "a"]
    assert argvs[0] == argvs[1]
    args = build_parser().parse_args(argvs[1][3:])
    assert args.no_epic_split

    results = []
    for name, stop in (("stopped", True), ("clean", False)):
        root = tmp_path / name
        use_workspace(monkeypatch, root)
        config = make_build(make_project(root), change="e-a", phase=BuildPhase.CODEBASE_ANALYSIS)
        log = CallLog(root / "calls.log")
        monkeypatch.setattr(epic_split_mod, "epic_split", FakeSplit(log, SINGLE))
        child_args = args_for(config, no_epic_split=args.no_epic_split)
        if stop:
            run_killed(lambda: run_build(config, child_args), _die_at_epic_split_skip)
        run_build(config, child_args)
        assert log.calls() == []
        results.append(state_summary(config, root))
    assert results[0] == results[1]
