"""Row tests for PR A's "Stop and resume states" table
(plan-buildme-epic-mode-2026-10-12.md, rows marked `Test: yes`).

Each case stops the step at the row's point (a forked child exiting 137, as
SIGKILL would) or applies the row's perturbation, resumes, and compares with a
run of the same scenario that never stopped, in a second temporary root: the
epic directory (`epic_snapshot`), the checkpoint's phase and epic directory,
and the launches (`CallLog`). The only differences allowed are the ones the
row's "Resume does" names. """

from __future__ import annotations

import json
import os
from pathlib import Path


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


class Run:
    """One root: a project, its workspace, its call log and its fake."""

    def __init__(self, root: Path, monkeypatch, first, retry=None):
        self.root = root
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.config = make_build(self.project)
        self.log = CallLog(root / "calls.log")
        self.fake = FakeSplit(self.log, first, retry)
        self.epic = self.data / "buildme" / "epics" / "o-r--e"

    def use(self, monkeypatch) -> None:
        """Make this root the current one (env and patched model call)."""
        use_workspace(monkeypatch, self.root)
        monkeypatch.setattr(epic_split_mod, "epic_split", self.fake)

    def build(self, monkeypatch, **flags) -> int:
        self.use(monkeypatch)
        return run_build(self.config, args_for(self.config, **flags))

    def killed(self, monkeypatch, setup=lambda: None, **flags) -> None:
        self.use(monkeypatch)
        run_killed(lambda: run_build(self.config, args_for(self.config, **flags)), setup)

    def result(self) -> dict:
        return {"epic": epic_snapshot(self.epic, self.root),
                "state": state_summary(self.config, self.root)}

    def launches(self) -> list[dict]:
        return self.log.calls()


def _roots(tmp_path: Path, monkeypatch, first, retry=None) -> tuple[Run, Run]:
    return (Run(tmp_path / "stopped", monkeypatch, first, retry),
            Run(tmp_path / "clean", monkeypatch, first, retry))


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

    assert clean.build(monkeypatch) == 3
    (clean.epic / "plans" / "ui.md").write_text(edited)     # the same edit at the same seam
    _assert_same(stopped, clean)


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
    """A.S12: with `--no-epic-split` (or `only`), stopped before or after the
    skip: skips again with no call, or goes on. No epic directory either way."""
    for point, phase in (("before", BuildPhase.CODEBASE_ANALYSIS), ("after", BuildPhase.EPIC_SPLIT)):
        stopped, clean = _roots(tmp_path / point, monkeypatch, SINGLE)
        stopped.killed(monkeypatch, _die_at_board(phase), no_epic_split=True)
        assert load_state(stopped.config).phase == phase
        assert stopped.build(monkeypatch, no_epic_split=True) == 1
        assert clean.build(monkeypatch, no_epic_split=True) == 1
        assert stopped.launches() == [] and clean.launches() == []
        assert stopped.result()["epic"] is None
        _assert_same(stopped, clean)
