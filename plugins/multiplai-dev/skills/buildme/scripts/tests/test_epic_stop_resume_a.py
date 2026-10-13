"""Row tests for PR A's "Stop and resume states" table
(plan-buildme-epic-mode-2026-10-12.md, rows marked `Test: yes`).

Each case stops the step at the row's point (a forked child exiting 137, as
SIGKILL would) or applies the row's perturbation, resumes, and compares with a
run of the same scenario that never stopped, in a second temporary root: the
epic directory (`epic_snapshot`), the checkpoint's phase and epic directory,
and the launches (`CallLog`). The only differences allowed are the ones the
row's "Resume does" names. A refusal row checks the exit, the message and that
nothing was written.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from unittest.mock import AsyncMock

from build_pipeline import orchestrator
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic import split as epic_split_mod
from build_pipeline.epic import store
from build_pipeline.models import BuildPhase

from .epic_row_helpers import (
    CYCLE,
    PLAN,
    SINGLE,
    THREE,
    CallLog,
    FakeSplit,
    args_for,
    die_at,
    epic_snapshot,
    load_state,
    make_build,
    make_project,
    run_build,
    run_killed,
    state_summary,
    use_workspace,
)
from .test_epic_split import hold_in_other_process


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


class Run:
    """One root: a project, its workspace, its call log and its fake."""

    def __init__(self, root: Path, monkeypatch, first, retry=None, mode="scratch"):
        self.root = root
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.config = make_build(self.project, mode=mode)
        self.log = CallLog(root / "calls.log")
        self.fake = FakeSplit(self.log, first, retry)
        self.epic = self.data / "buildme" / "epics" / "o-r--e"

    def use(self, monkeypatch) -> None:
        """Make this root the current one (env and patched model call)."""
        use_workspace(monkeypatch, self.root)
        monkeypatch.setattr(epic_split_mod, "epic_split", self.fake)
        monkeypatch.setattr(orchestrator, "_run_research_check", AsyncMock())   # `only` mode

    def build(self, monkeypatch, spec=None, **flags) -> int:
        self.use(monkeypatch)
        return run_build(self.config, args_for(self.config, **flags), spec)

    def killed(self, monkeypatch, setup=lambda: None, spec=None, **flags) -> None:
        self.use(monkeypatch)
        run_killed(lambda: run_build(self.config, args_for(self.config, **flags), spec), setup)

    def result(self) -> dict:
        return {"epic": epic_snapshot(self.epic, self.root),
                "state": state_summary(self.config, self.root)}

    def launches(self) -> list[dict]:
        return self.log.calls()


def _roots(tmp_path: Path, monkeypatch, first, retry=None, mode="scratch") -> tuple[Run, Run]:
    return (Run(tmp_path / "stopped", monkeypatch, first, retry, mode),
            Run(tmp_path / "clean", monkeypatch, first, retry, mode))


def _assert_same(stopped: Run, clean: Run, extra_calls: list[dict] = ()) -> None:
    """Same epic directory and checkpoint; the stopped run's launches are the
    never-stopped run's plus exactly *extra_calls* (the row's named difference)."""
    assert stopped.result() == clean.result()

    def key(calls):
        return sorted(json.dumps(c, sort_keys=True) for c in calls)

    assert key(stopped.launches()) == key([*clean.launches(), *extra_calls])


SPLIT = {"kind": "epic_split", "retry": False}
RETRY = {"kind": "epic_split", "retry": True}


def test_A_S1_stopped_during_or_after_the_decision_calls_again(tmp_path, monkeypatch):
    """A.S1: resume calls again, one more `epic_split` call; no epic dir (or only `.lock`) before."""
    for point in ("during", "after"):
        stopped, clean = _roots(tmp_path / point, monkeypatch, THREE)
        if point == "during":
            stopped.killed(monkeypatch, lambda: setattr(stopped.fake, "die_on_call", 1))
        else:   # after the answer, before epic.json
            stopped.killed(monkeypatch, die_at(store, "save_epic"))
        assert not (stopped.epic / "epic.json").exists()
        assert load_state(stopped.config).phase == BuildPhase.CODEBASE_ANALYSIS
        assert stopped.build(monkeypatch) == 3
        assert clean.build(monkeypatch) == 3
        _assert_same(stopped, clean, [SPLIT])


def test_A_S2_stopped_around_the_retry_calls_again_from_the_first_answer(tmp_path, monkeypatch):
    """A.S2: at most one retry per invocation, not saved. One more call when
    stopped after the first answer, two more when stopped during the retry."""
    for point, extra in (("after-first", [SPLIT]), ("during-retry", [SPLIT, RETRY])):
        stopped, clean = _roots(tmp_path / point, monkeypatch, CYCLE, retry=THREE)
        if point == "after-first":
            stopped.killed(monkeypatch, die_at(epic_split_mod, "check_answer", after=True))
        else:
            stopped.killed(monkeypatch, lambda: setattr(stopped.fake, "die_on_call", 2))
        assert stopped.build(monkeypatch) == 3
        assert clean.build(monkeypatch) == 3
        _assert_same(stopped, clean, extra)


def test_A_S3_stopped_after_epic_json_renders_again_with_no_call(tmp_path, monkeypatch):
    """A.S3: no call; renders every file again; saves `state.epic_dir`; completes the phase."""
    stopped, clean = _roots(tmp_path, monkeypatch, THREE)
    stopped.killed(monkeypatch, die_at(store, "write_plan"))
    assert (stopped.epic / "epic.json").exists()
    assert not (stopped.epic / "plans" / "api.md").exists()
    assert load_state(stopped.config).epic_dir is None
    assert stopped.build(monkeypatch) == 3
    assert clean.build(monkeypatch) == 3
    _assert_same(stopped, clean)


def test_A_S3_entered_with_files_on_disk_keeps_them(tmp_path, monkeypatch):
    """A.S3 entered with a checkpoint that lost `state.epic_dir` (or a new one)
    and plans already written: no call, a file already on disk is kept (here
    one edited by hand while the split waited, A.S8), only missing ones are rendered."""
    stopped, clean = _roots(tmp_path, monkeypatch, THREE)
    edited = PLAN.format(title="Edited while waiting")
    for run in (stopped, clean):
        assert run.build(monkeypatch) == 3
        (run.epic / "plans" / "ui.md").write_text(edited)
    (stopped.epic / "split.md").unlink()
    fresh = load_state(stopped.config)
    fresh.phase, fresh.epic_dir = BuildPhase.RESEARCH, None
    fresh.checkpoint(stopped.config.state_file_path())
    assert stopped.build(monkeypatch) == 3
    assert (stopped.epic / "plans" / "ui.md").read_text() == edited
    _assert_same(stopped, clean)


def test_A_S4_waiting_repeats_the_line_and_keeps_edits(tmp_path, monkeypatch, capsys):
    """A.S4: without `--approve-split`, same line, exit 3, no call, edits kept."""
    stopped, clean = _roots(tmp_path, monkeypatch, THREE)
    assert stopped.build(monkeypatch) == 3
    edited = PLAN.format(title="Edited while waiting")
    (stopped.epic / "plans" / "ui.md").write_text(edited)
    capsys.readouterr()
    assert stopped.build(monkeypatch) == 3
    assert f"EPIC:SPLIT:{stopped.epic / 'split.md'}" in capsys.readouterr().out
    assert (stopped.epic / "plans" / "ui.md").read_text() == edited
    assert stopped.build(monkeypatch, approve_split=True) == 0

    assert clean.build(monkeypatch) == 3
    (clean.epic / "plans" / "ui.md").write_text(edited)     # the same edit at the same seam
    assert clean.build(monkeypatch, approve_split=True) == 0
    _assert_same(stopped, clean)


def test_A_S8_user_edits_are_used_before_approval_and_refused_after(tmp_path, monkeypatch, capsys):
    """A.S8 (seam: A.S4): before approval, edits are used after item 4's checks;
    after approval, exit 2; an unparsable epic.json exits 1 and is never rewritten."""
    stopped, clean = _roots(tmp_path / "before", monkeypatch, THREE)

    def edit(run: Run) -> None:
        split_md = run.epic / "split.md"
        split_md.write_text(split_md.read_text().replace("| api, ui |", "| ui |"))
        (run.epic / "plans" / "api.md").write_text(PLAN.format(title="API, edited"))

    for run in (stopped, clean):
        assert run.build(monkeypatch) == 3
        edit(run)
        assert run.build(monkeypatch, approve_split=True) == 0
    _assert_same(stopped, clean)
    assert stopped.result()["epic"]["epic.json"]["children"][2]["depends_on"] == ["ui"]

    # After approval: a changed plan exits 2 naming it, and nothing is written.
    before = epic_snapshot(stopped.epic, stopped.root)
    (stopped.epic / "plans" / "ui.md").write_text(PLAN.format(title="Too late"))
    capsys.readouterr()
    assert stopped.build(monkeypatch) == 2
    assert "plans/ui.md" in capsys.readouterr().err
    after = epic_snapshot(stopped.epic, stopped.root)
    assert after["epic.json"] == before["epic.json"]

    # Unparsable epic.json: exit 1, never rewritten.
    broken, _ = _roots(tmp_path / "broken", monkeypatch, THREE)
    assert broken.build(monkeypatch) == 3
    (broken.epic / "epic.json").write_text("{")
    for flags in ({}, {"approve_split": True}):
        assert broken.build(monkeypatch, **flags) == 1
        assert (broken.epic / "epic.json").read_text() == "{"
    assert len(broken.launches()) == 1


def test_A_S11_another_process_holding_the_lock_exits_2_and_writes_nothing(tmp_path, monkeypatch, capsys):
    """A.S11: exit 2 with its pid; writes nothing. Checked at the approval, at
    the re-run of an approved build, and at the split's first write."""
    run = Run(tmp_path / "approve", monkeypatch, THREE)
    assert run.build(monkeypatch) == 3
    before, state_before = epic_snapshot(run.epic, run.root), run.config.state_file_path().read_bytes()
    epic_lock.release_all()
    holder = hold_in_other_process(run.epic / ".lock")
    try:
        capsys.readouterr()
        assert run.build(monkeypatch, approve_split=True) == 2
        assert f"epic e is running now (pid {holder.pid} on " in capsys.readouterr().err
        assert epic_snapshot(run.epic, run.root) == before
        assert run.config.state_file_path().read_bytes() == state_before
    finally:
        holder.communicate("")

    assert run.build(monkeypatch, approve_split=True) == 0
    approved = epic_snapshot(run.epic, run.root)
    epic_lock.release_all()
    holder = hold_in_other_process(run.epic / ".lock")
    try:
        assert run.build(monkeypatch) == 2
        assert epic_snapshot(run.epic, run.root) == approved
    finally:
        holder.communicate("")

    fresh = Run(tmp_path / "split", monkeypatch, THREE)
    holder = hold_in_other_process(fresh.epic / ".lock")
    try:
        assert fresh.build(monkeypatch) == 2
        assert sorted(p.name for p in fresh.epic.iterdir()) == [".lock"]
        assert load_state(fresh.config).epic_dir is None
    finally:
        holder.communicate("")


def _die_at_board(phase: BuildPhase):
    """Exit 137 when the board records *phase*: the checkpoint for that phase is written."""
    from build_pipeline import board

    def setup() -> None:
        real = board.record

        def record(config, state, ph, *a, **k):
            if ph == phase:
                os._exit(137)
            return real(config, state, ph, *a, **k)

        board.record = record

    return setup


def test_A_S12_a_skipped_split_skips_again_or_goes_on(tmp_path, monkeypatch):
    """A.S12: with `--no-epic-split` or in `only` mode, stopped before or after
    the skip: skips again with no call, or goes on. No epic directory either way."""
    for how in ("flag", "only"):
        mode, flag = ("only", False) if how == "only" else ("scratch", True)
        for point, phase in (("before", BuildPhase.CODEBASE_ANALYSIS), ("after", BuildPhase.EPIC_SPLIT)):
            stopped, clean = _roots(tmp_path / how / point, monkeypatch, THREE, mode=mode)
            stopped.killed(monkeypatch, _die_at_board(phase), no_epic_split=flag)
            assert load_state(stopped.config).phase == phase
            rc = clean.build(monkeypatch, no_epic_split=flag)
            assert stopped.build(monkeypatch, no_epic_split=flag) == rc
            assert stopped.launches() == [] and clean.launches() == []
            assert stopped.result()["epic"] is None
            _assert_same(stopped, clean)


async def _killed_spec(*a, **k):
    os._exit(137)


def test_A_S13_a_single_decision_is_not_asked_again(tmp_path, monkeypatch):
    """A.S13: after a `single: true` answer, killed during spec generation
    (pointer at EPIC_SPLIT) or after the build FAILED there (`epic_single`
    saved), resume makes no second call. The fake answers several on any
    second call, so a call would also turn the change into an epic."""
    for point in ("killed", "failed"):
        stopped, clean = _roots(tmp_path / point, monkeypatch, SINGLE, retry=THREE)
        if point == "killed":
            stopped.killed(monkeypatch, spec=_killed_spec)
            assert load_state(stopped.config).phase == BuildPhase.EPIC_SPLIT
        else:
            assert stopped.build(monkeypatch) == 1
            assert load_state(stopped.config).phase == BuildPhase.FAILED
        stopped.fake.first = THREE
        assert stopped.build(monkeypatch) == 1
        assert clean.build(monkeypatch) == 1
        assert stopped.result()["epic"] is None
        _assert_same(stopped, clean)


def test_A_S12_a_skipped_build_that_failed_is_not_asked_without_the_flag(tmp_path, monkeypatch):
    """A.S12: skipped, FAILED, retried without the flag: no call (the fake answers several)."""
    stopped, clean = _roots(tmp_path, monkeypatch, THREE)
    for run in (stopped, clean):
        assert run.build(monkeypatch, no_epic_split=True) == 1
    assert stopped.build(monkeypatch) == clean.build(monkeypatch, no_epic_split=True) == 1
    _assert_same(stopped, clean)


def test_A_S14_a_checkpoint_from_before_epic_mode(tmp_path, monkeypatch):
    """A.S14: a pre-epic checkpoint (no `epic_*` keys) FAILED or past the split, resumed then
    retried, makes no call and ends as a one-change build does; before the split it still decides."""
    for phase in (BuildPhase.FAILED, BuildPhase.SPEC_GENERATION, BuildPhase.CODEBASE_ANALYSIS):
        before = phase == BuildPhase.CODEBASE_ANALYSIS
        stopped, clean = _roots(tmp_path / phase.value, monkeypatch, SINGLE if before else THREE)
        clean.fake.first, path = SINGLE, stopped.config.state_file_path()
        data = {k: v for k, v in json.loads(path.read_text()).items() if not k.startswith("epic_")}
        path.write_text(json.dumps({**data, "phase": phase.value}))
        for run in (stopped, clean, stopped, clean):
            assert run.build(monkeypatch) == 1
        assert stopped.launches() == ([SPLIT] if before else []) and clean.launches() == [SPLIT]
        assert stopped.result() == clean.result()
