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
    move_origin,
    name_commits,
    alive,
    pr_url,
    records_snapshot,
    run_build,
    run_coordinator,
    run_killed,
    run_signalled,
    state_summary,
    use_workspace,
)

TWO = EpicSplitAnswer(single=False, reason="Two parts.", children=[child("a"), child("b")])
CHAIN = EpicSplitAnswer(single=False, reason="Two parts.", children=[child("a"), child("b", ("a",))])


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


class Root:
    """One root: project, workspace, fakes, an approved two-child epic."""

    def __init__(self, root: Path, monkeypatch, steps: dict, parallel: int = 1, answer=TWO):
        self.root, self.mp = root, monkeypatch
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.fakes = Fakes(root, monkeypatch, steps)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=parallel)
        approve(self.config, answer, CallLog(root / "split.log"))
        self.dir = self.data / "buildme" / "epics" / "o-r--e"

    def use(self) -> None:
        use_workspace(self.mp, self.root)
        self.fakes.use(self.mp)

    def run(self, tick=None, *, ps_fails: bool = False) -> int:
        self.use()
        if ps_fails:
            self.fakes.ps_fails(self.mp)
        self.mp.setattr(coordinator, "_tick", tick or (lambda coord: None))
        return run_coordinator(self.config)

    def status(self) -> dict:
        return {k: v["status"] for k, v in json.loads((self.dir / "epic.json").read_text())["child_status"].items()}

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


def _roots(tmp_path, monkeypatch, steps, parallel=1, answer=TWO) -> tuple[Root, Root]:
    return (Root(tmp_path / "stopped", monkeypatch, steps, parallel, answer),
            Root(tmp_path / "clean", monkeypatch, steps, parallel, answer))


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
    assert not epic_lock.held(stopped.dir)                           # the resume takes it itself
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


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
def test_B1_S6_a_signal_stops_every_childs_group_and_resume_relaunches_each(tmp_path, monkeypatch, sig):
    """B1.S6: the signal sent to the coordinator reaches the whole group of each
    child, the one it launched (b) and the one it adopted from a killed run (a):
    exit 128 + the signal, results `interrupted: true`. The next run relaunches
    each: one more launch and pair each."""
    first = {"block": True, "grandchild": True}
    stopped, clean = _roots(tmp_path, monkeypatch, {"a": [first, {}], "b": [first, {}]}, parallel=2)
    try:
        stopped.killed(die_at(coordinator, "_spawn", after=True))     # a runs on
        stopped.use()
        monkeypatch.setattr(coordinator, "STOP_WAIT", 3000.0)
        rc = run_signalled(lambda: run_coordinator(stopped.config),
                           lambda: len(stopped.fakes.grandchildren()) == 2, sig)
        assert rc == 128 + sig
        for _ in range(250):
            if not any(alive(p) for p in stopped.fakes.grandchildren()):
                break
            time.sleep(0.02)
        assert not any(alive(p) for p in stopped.fakes.grandchildren())
        s = records_snapshot(stopped.dir, stopped.root)
        assert s["a"][0][2] is None and s["b"][0][2] not in (0, None)
        assert all(s[cid][0][4] is True for cid in ("a", "b"))
        assert stopped.run(_release_all(stopped)) == 0
        assert clean.run(_release_all(clean)) == 0
        _assert_same(stopped, clean, {
            cid: [("build", "<argv>", s[cid][0][2], _built(cid)[:2], True),
                  ("build", "<argv>", 0, [_built(cid)[0], _built(cid)[2]], False)]
            for cid in ("a", "b")}, ["a", "b"])
    finally:
        for pid in stopped.fakes.grandchildren() + clean.fakes.grandchildren():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


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
    """B1.S13: the coordinator touches nothing; the relaunch has the same argv,
    though origin moved at the same seam (B1.S14: b branches from the new sha);
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
    moved = move_origin(stopped.root)

    def clean_tick(coord):
        if not (wt(clean) / "notes.txt").exists():
            (wt(clean) / "notes.txt").write_bytes(edit)
            (wt(clean) / "README.md").write_text("# r, edited\n")
            move_origin(clean.root)
        _release_all(clean)(coord)

    assert stopped.run(_release_all(stopped)) == 0
    assert clean.run(clean_tick) == 0
    for root in (stopped, clean):
        assert (wt(root) / "notes.txt").read_bytes() == edit
        assert (wt(root) / "README.md").read_text() == "# r, edited\n"
    argvs = [a.started["argv"] for a in records.attempts(stopped.dir) if a.child == "a"]
    assert len(argvs) == 2 and argvs[0] == argvs[1]
    assert moved not in argvs[1] and (stopped.root / "bases" / "b").read_text() == moved
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


def test_B1_S20_a_failed_pr_lookup_launches_nothing(tmp_path, monkeypatch):
    """B1.S20: `gh` fails while looking for a stopped child's PR: no launch for
    it, status `unknown`, exit 1. The next run looks again: as B1.S5 (no PR)."""
    stopped, clean = _roots(tmp_path, monkeypatch, {"a": [{"block": True}, {}]})
    stopped.killed(die_at(coordinator, "_tick"))
    stopped.kill_child("a")
    (stopped.root / "gh-fails").write_text("")
    assert stopped.run() == 1
    assert stopped.fakes.attempts("a") == 1 and stopped.status()["a"] == "unknown"
    (stopped.root / "gh-fails").unlink()
    assert stopped.run(_release_all(stopped)) == 0
    assert clean.run(_release_all(clean)) == 0
    _assert_same(stopped, clean, {"a": [
        ("build", "<argv>", None, _built("a")[:2], True),
        ("build", "<argv>", 0, [_built("a")[0], _built("a")[2]], False)]}, ["a"])


def test_B1_S21_a_failing_ps_never_counts_a_live_child_as_gone(tmp_path, monkeypatch):
    """B1.S21: `ps` fails while a stopped child still runs: no result and no
    launch for it, status `unknown`, exit 1. The next run checks again: as B1.S4."""
    stopped, clean = _roots(tmp_path, monkeypatch, {"a": [{"block": True}, {}]})
    stopped.killed(die_at(coordinator, "_tick"))
    assert stopped.run(ps_fails=True) == 1
    assert stopped.fakes.attempts("a") == 1 and stopped.status()["a"] == "unknown"
    assert records_snapshot(stopped.dir, stopped.root)["a"][0][2] == "no result"
    assert stopped.run(_release_all(stopped)) == 0
    assert clean.run(_release_all(clean)) == 0
    _assert_same(stopped, clean, {"a": [("build", "<argv>", None, _built("a"), True)]})


def test_B1_S21_a_failing_ps_while_polling_an_adopted_child(tmp_path, monkeypatch):
    """B1.S21, in the run loop: the resumed run adopts the live child, then `ps`
    fails as `_poll` checks it. No result and no launch for it, status
    `unknown`, exit 1. The next run checks again: as B1.S4."""
    stopped, clean = _roots(tmp_path, monkeypatch, {"a": [{"block": True}, {}]})
    stopped.killed(die_at(coordinator, "_tick"))
    assert stopped.run(lambda coord: stopped.fakes.ps_fails(monkeypatch)) == 1
    assert stopped.fakes.attempts("a") == 1 and stopped.status()["a"] == "unknown"
    reason = json.loads((stopped.dir / "epic.json").read_text())["child_status"]["a"]["reason"]
    assert reason.startswith("cannot tell whether its build is still running")
    assert records_snapshot(stopped.dir, stopped.root)["a"][0][2] == "no result"
    assert stopped.run(_release_all(stopped)) == 0
    assert clean.run(_release_all(clean)) == 0
    _assert_same(stopped, clean, {"a": [("build", "<argv>", None, _built("a"), True)]})


def test_B1_S24_a_failed_pr_read_is_not_an_open_pr(tmp_path, monkeypatch, capsys):
    """B1.S24: `gh pr view` fails for the PR b waits on: a is `unknown`, no
    EPIC:MERGE_WAITING for a PR that may have merged, nothing launched, exit 1.
    Once `gh` answers: as B1.S17, with the records of a run that never failed."""
    stopped, clean = _roots(tmp_path, monkeypatch, {}, answer=CHAIN)
    assert stopped.run() == 3 and clean.run() == 3
    capsys.readouterr()
    (stopped.root / "gh-fails").write_text("")
    assert stopped.run() == 1
    out = capsys.readouterr()
    assert "EPIC:MERGE_WAITING" not in out.out and "child a unknown" in out.err
    assert stopped.status() == {"a": "unknown", "b": "pending"}
    (stopped.root / "gh-fails").unlink()
    for r in (stopped, clean):
        r.fakes.set_pr(pr_url("e-a"), "MERGED")
        assert r.run() == 0
    _assert_same(stopped, clean)


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
