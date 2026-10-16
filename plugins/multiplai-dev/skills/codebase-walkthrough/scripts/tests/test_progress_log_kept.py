"""A second run on the same target keeps the first run's progress.log under a dated name."""

from __future__ import annotations

import os

from fakes import FakeAgents
from walkthrough_pipeline import orchestrator, sdk
from walkthrough_pipeline.__main__ import main
from test_pipeline import run_args


def test_a_new_run_keeps_the_previous_progress_log(ws, tmp_path, monkeypatch, fake_fetch):
    monkeypatch.setattr(sdk, "agent_call_structured", FakeAgents(cost=0.1))
    assert main(run_args(ws, tmp_path)) == 0
    run_dir = next((tmp_path / "runs").iterdir())
    first = (run_dir / "progress.log").read_text()
    assert "DONE" in first

    assert main(run_args(ws, tmp_path)) == 0

    kept = list(run_dir.glob("progress-*.log"))
    assert [p.read_text() for p in kept] == [first]
    assert (run_dir / "progress.log").read_text().count(" STARTED ") == 1  # a fresh log, not appended


def test_a_taken_dated_name_is_never_overwritten(tmp_path):
    log = tmp_path / "progress.log"
    log.write_text("second run\n")
    orchestrator._keep_previous_progress(tmp_path)
    moved = list(tmp_path.glob("progress-*.log"))
    assert len(moved) == 1 and moved[0].read_text() == "second run\n"

    log.write_text("third run\n")
    st = moved[0].stat()
    os.utime(log, (st.st_atime, st.st_mtime))  # same second: the dated name is taken
    orchestrator._keep_previous_progress(tmp_path)
    assert moved[0].read_text() == "second run\n"
    assert log.read_text() == "third run\n"
