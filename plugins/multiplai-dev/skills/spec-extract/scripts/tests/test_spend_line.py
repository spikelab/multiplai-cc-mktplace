"""`python -m spec_extract_pipeline` prints `SPEND:<usd>:<tokens>` last: what this
process's model calls cost, never a ledger a resume restored. buildme adds the line
to its own `SPEND:` line, as it does a walkthrough run's (PR 4 of the reverse-specs
plan, review round 1 of #345)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from multiplai_core import budget as core_budget
from multiplai_core.agent_runner import AgentRunError
from pydantic import BaseModel

from spec_extract_pipeline import __main__ as cli
from spec_extract_pipeline import sdk

_REAL_CALL = sdk.agent_call_structured  # before the autouse fixture replaces it


class Answer(BaseModel):
    value: int


def _result(text: str, cost: float):
    return SimpleNamespace(text=text, usage=SimpleNamespace(cost_usd=cost, input_tokens=10, output_tokens=2))


@pytest.fixture(autouse=True)
def fresh_process_spend(monkeypatch):
    monkeypatch.setattr(sdk, "_process", sdk.ProcessSpend())


def test_the_process_counts_each_call_but_not_the_restored_ledger(monkeypatch):
    outcomes = [_result('{"value": 1}', 0.5),
                AgentRunError("boom", reason="cli", stderr_tail="", partial=_result("", 0.25))]

    async def run_agent(prompt, **kwargs):
        out = outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(sdk, "run_agent", run_agent)
    earlier = core_budget.start(80.0, None, ledger_cls=sdk.SpecExtractBudget)
    earlier.record(SimpleNamespace(cost_usd=7.0, input_tokens=1000, output_tokens=0), label="explore:old")
    sdk.start_budget(80.0, earlier.to_state())

    async def go():
        return await _REAL_CALL("prompt", Answer, allowed_tools=["Read"], budget_label="explore:unit")

    assert asyncio.run(go()) == Answer(value=1)
    with pytest.raises(sdk.CallFailed):
        asyncio.run(go())
    assert core_budget.get().cost_usd == pytest.approx(7.75)
    assert sdk.spend_marker() == "SPEND:0.750000:24"


def test_the_command_prints_the_spend_line_last_on_every_exit(capsys):
    assert cli.cli(["run", "--tree", "--dir", "x"]) == cli.EXIT_REFUSED
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "SPEND:0.000000:0"


def test_the_module_entry_point_is_the_command_with_its_spend_line():
    """`python -m spec_extract_pipeline` itself, as buildme runs it: a refused run
    still ends with the SPEND line, and exits with the command's code."""
    import subprocess
    import sys
    from pathlib import Path

    res = subprocess.run([sys.executable, "-m", "spec_extract_pipeline", "run", "--tree", "--dir", "x"],
                         cwd=Path(cli.__file__).resolve().parents[1], capture_output=True, text=True,
                         timeout=120, check=False)
    assert res.returncode == cli.EXIT_REFUSED
    assert res.stdout.splitlines()[-1] == "SPEND:0.000000:0"
