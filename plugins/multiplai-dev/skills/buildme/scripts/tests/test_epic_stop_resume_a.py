"""Row tests for PR A's "Stop and resume states" table
(plan-buildme-epic-mode-2026-10-12.md, rows marked `Test: yes`) that still
apply once pm-spec writes the split (plan-pm-spec-split-2026-10-21.md,
Decision 11): the split and approval rows went with buildme's own split.
A refusal row checks the exit, the message and that nothing was written.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from build_pipeline.epic import lock as epic_lock

from .epic_row_helpers import (
    THREE,
    approve,
    args_for,
    epic_snapshot,
    make_build,
    make_project,
    run_build,
    use_workspace,
)
from .test_epic_store import hold_in_other_process


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


class Run:
    """One root: a project with an epic pm-spec split, and its workspace."""

    def __init__(self, root: Path, monkeypatch):
        self.root = root
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.config = make_build(self.project)
        approve(self.config, THREE)
        self.epic = self.data / "buildme" / "epics" / "o-r--e"

    def build(self, monkeypatch, **flags) -> int:
        use_workspace(monkeypatch, self.root)
        return run_build(self.config, args_for(self.config, **flags))


def test_A_S11_another_process_holding_the_lock_exits_2_and_writes_nothing(tmp_path, monkeypatch, capsys):
    """A.S11: exit 2 with its pid; writes nothing. Checked at a first run,
    before any epic.json, and at a re-run of an epic that has one."""
    run = Run(tmp_path / "first", monkeypatch)
    holder = hold_in_other_process(run.epic / ".lock")
    try:
        capsys.readouterr()
        assert run.build(monkeypatch) == 2
        assert f"epic e is running now (pid {holder.pid} on " in capsys.readouterr().err
        assert sorted(p.name for p in run.epic.iterdir()) == [".lock"]
    finally:
        holder.communicate("")
    assert not (run.config.change_dir / ".build-state.json").exists()

    again = Run(tmp_path / "again", monkeypatch)
    again.epic.mkdir(parents=True)
    (again.epic / "epic.json").write_text('{"name": "e", "children": []}')
    before = epic_snapshot(again.epic, again.root)
    holder = hold_in_other_process(again.epic / ".lock")
    try:
        assert again.build(monkeypatch) == 2
        assert epic_snapshot(again.epic, again.root) == before
    finally:
        holder.communicate("")
