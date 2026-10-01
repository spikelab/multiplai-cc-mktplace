"""A budget stop exits 2 with a checkpoint that keeps paid answers; resume finishes without re-asking them."""

from __future__ import annotations

import json

from fakes import FakeAgents
from walkthrough_pipeline import sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.state import STATE_FILE
from test_pipeline import run_args


def test_resume_after_a_budget_stop(ws, tmp_path, monkeypatch, fake_fetch, capsys):
    agents = FakeAgents(cost=1.0)
    monkeypatch.setattr(sdk, "agent_call_structured", agents)
    assert main(run_args(ws, tmp_path, "--max-usd", "1.5")) == 2
    assert "STOPPED: circuit breaker" in capsys.readouterr().err
    run_dir = next((tmp_path / "runs").iterdir())
    state = json.loads((run_dir / STATE_FILE).read_text())
    assert state["stage"] == "partition"                 # explore did not finish
    paid = dict(state["explore_answers"])
    assert paid, "answers paid for before the stop are in the checkpoint"
    assert state["budget"]["cost_usd"] >= 1.5
    assert "FAILED circuit breaker" in (run_dir / "progress.log").read_text()

    paid_prompts = {c["prompt"] for c in agents.calls if c["schema"] == "ExploreOutput"}
    asked_before = len(agents.calls)
    assert main(["resume", str(run_dir), "--trust-repo", "--max-usd", "0"]) == 0
    resumed = {c["prompt"] for c in agents.calls[asked_before:] if c["schema"] == "ExploreOutput"}
    assert not paid_prompts & resumed, "resume re-asked an explore task it had already paid for"
    state = json.loads((run_dir / STATE_FILE).read_text())
    assert state["stage"] == "done"
    assert (tmp_path / "out" / "bookings-walkthrough.md").is_file()
