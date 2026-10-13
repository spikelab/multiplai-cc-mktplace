"""Row tests for PR B2's "Stop and resume states" table
(plan-buildme-epic-mode-2026-10-12.md, rows marked `Test: yes`: B2.S3 only).

The case stops where the row says, resumes, and compares with the same
scenario run without the stop in a second temporary root: the epic directory,
the records per child as (kind, argv, exit_code, markers, interrupted), the
last summary and the launches. B2.S3 names no difference: the resume launches
nothing and writes the same files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from build_pipeline import budget
from build_pipeline.config import EpicSettings
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic.split import EpicSplitAnswer

from .epic_row_helpers import (
    CallLog,
    Fakes,
    approve,
    child,
    commit_names,
    epic_snapshot,
    last_summary,
    make_build,
    make_project,
    name_commits,
    records_snapshot,
    run_coordinator,
    use_workspace,
)

THREE = EpicSplitAnswer(single=False, reason="Three parts.", children=[child("a"), child("b"), child("c")])
EACH = [{"spend": 4.0}]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(budget, "_process", budget.ProcessSpend())
    yield
    epic_lock.release_all()


class Root:
    def __init__(self, root: Path, monkeypatch):
        self.root, self.mp = root, monkeypatch
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.fakes = Fakes(root, monkeypatch, {"a": EACH, "b": EACH, "c": EACH})
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=1, max_usd=5.0)
        approve(self.config, THREE, CallLog(root / "split.log"))
        self.dir = self.data / "buildme" / "epics" / "o-r--e"

    def run(self) -> int:
        use_workspace(self.mp, self.root)
        self.fakes.use(self.mp)
        return run_coordinator(self.config)

    def snapshot(self) -> dict:
        names = commit_names([self.root / "o" / "r.git", self.project])
        return name_commits({"epic": epic_snapshot(self.dir, self.root),
                             "records": records_snapshot(self.dir, self.root),
                             "summary": last_summary(self.dir, self.root)}, names)

    def launches(self) -> list[dict]:
        return CallLog(self.root / "calls.log").calls()


def test_b2_s3_still_over_the_ceiling_exits_3_and_launches_nothing(tmp_path, monkeypatch, capsys):
    stopped = Root(tmp_path / "stopped", monkeypatch)
    clean = Root(tmp_path / "clean", monkeypatch)

    assert clean.run() == 3
    assert stopped.run() == 3                   # the stop: EPIC:BUDGET, exit 3
    first = capsys.readouterr().out
    assert first.splitlines().count("EPIC:BUDGET:8.00") == 2

    assert stopped.run() == 3                   # the resume
    out = capsys.readouterr().out.splitlines()
    assert "EPIC:BUDGET:8.00" in out
    assert len(stopped.launches()) == 2         # a and b; c never launched

    assert stopped.snapshot() == clean.snapshot()
    assert stopped.launches() == clean.launches()
    saved = json.loads((stopped.dir / "epic.json").read_text())
    assert saved["spent_usd"] == pytest.approx(8.0) and saved["child_status"]["c"]["status"] == "pending"
