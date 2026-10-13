"""`run` and `resume` print `SPEND:<usd>:<tokens>` last: what this process's
model calls cost, which a resumed run's restored ledger does not include.
buildme adds the line to its own spend when it regenerates a walkthrough."""

from __future__ import annotations

import json

import pytest
from fakes import FakeAgents
from test_pipeline import run_args

from walkthrough_pipeline import budget, sdk
from walkthrough_pipeline.__main__ import main
from walkthrough_pipeline.state import STATE_FILE


def _spend(out: str) -> float:
    last = out.splitlines()[-1]
    assert last.startswith("SPEND:"), last
    return float(last.split(":")[1])


def test_run_and_resume_each_print_their_own_spend(ws, tmp_path, monkeypatch, fake_fetch, capsys):
    monkeypatch.setattr(budget, "_process", budget.ProcessSpend())
    monkeypatch.setattr(sdk, "agent_call_structured", FakeAgents(cost=1.0))
    assert main(run_args(ws, tmp_path, "--max-usd", "1.5")) == 2
    run_dir = next((tmp_path / "runs").iterdir())
    first = json.loads((run_dir / STATE_FILE).read_text())["budget"]["cost_usd"]
    assert _spend(capsys.readouterr().out) == pytest.approx(first)

    monkeypatch.setattr(budget, "_process", budget.ProcessSpend())     # a new process
    assert main(["resume", str(run_dir), "--trust-repo", "--max-usd", "0"]) == 0
    total = json.loads((run_dir / STATE_FILE).read_text())["budget"]["cost_usd"]
    assert total > first
    assert _spend(capsys.readouterr().out) == pytest.approx(total - first)


def test_record_adds_to_the_process_total(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(budget, "_process", budget.ProcessSpend())
    budget.start(None)
    budget.record(SimpleNamespace(input_tokens=10, output_tokens=5, cost_usd=0.5))
    budget.start(None, {"cost_usd": 9.0})       # a resume restores spend it did not make
    budget.record(SimpleNamespace(input_tokens=1, output_tokens=0, cost_usd=0.25))
    assert budget.spend_marker() == "SPEND:0.750000:16"
