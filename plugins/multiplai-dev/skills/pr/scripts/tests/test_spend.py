"""`open` and `edit` print `SPEND:<usd>:<tokens>` last: what this process's
model calls cost. buildme adds the line to its own spend, so an epic's
`epic.max_usd` counts the PR body's model calls."""

from __future__ import annotations

import asyncio

import multiplai_core
import pytest
from multiplai_core.agent_runner import AgentRunError, AgentRunResult, AgentUsage

from pr_pipeline import __main__ as cli
from pr_pipeline import commands, fill

# The real call, kept before conftest's guard replaces it; these tests stub
# `run_agent` underneath it instead.
_RUN_MODEL = fill._run_model


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch):
    monkeypatch.setattr(fill, "_spent", fill.ProcessSpend())


def _result(usd: float, tokens: int) -> AgentRunResult:
    return AgentRunResult(text="answer", turns=1, files_changed=[], stderr_tail="",
                          usage=AgentUsage(input_tokens=tokens, output_tokens=0, cost_usd=usd))


def test_each_model_call_adds_to_the_process_spend(monkeypatch):
    async def run_agent(prompt, **kw):
        return _result(0.25, 100)

    monkeypatch.setattr(multiplai_core, "run_agent", run_agent)
    assert asyncio.run(_RUN_MODEL("p")) == "answer"
    asyncio.run(_RUN_MODEL("p"))
    assert fill.spend_marker() == "SPEND:0.500000:200"


def test_a_failed_call_counts_what_it_spent(monkeypatch):
    async def run_agent(prompt, **kw):
        raise AgentRunError("timed out", partial=_result(0.75, 30))

    monkeypatch.setattr(multiplai_core, "run_agent", run_agent)
    with pytest.raises(AgentRunError):
        asyncio.run(_RUN_MODEL("p"))
    assert fill.spend_marker() == "SPEND:0.750000:30"


@pytest.mark.parametrize("argv", [["open"], ["edit", "7"]])
@pytest.mark.parametrize("code", [0, 1, 2])
def test_open_and_edit_print_spend_last(tmp_path, monkeypatch, capsys, argv, code):
    def run(args):
        fill._spent.add(0.4, 40)
        print("PR:https://example.invalid/pull/1", flush=True)
        return code

    monkeypatch.setattr(commands, "run", run)
    assert cli.main([*argv, "--repo", str(tmp_path), "--change-dir", str(tmp_path)]) == code
    assert capsys.readouterr().out.splitlines()[-1] == "SPEND:0.400000:40"


def test_open_prints_spend_when_it_cannot_start(tmp_path, monkeypatch, capsys):
    import multiplai_core.env

    def boom(*a, **k):
        raise PermissionError("cannot read .env")

    monkeypatch.setattr(multiplai_core.env, "load_env", boom)
    assert cli.main(["open", "--repo", str(tmp_path), "--change-dir", str(tmp_path)]) == 2
    assert capsys.readouterr().out.splitlines()[-1] == "SPEND:0.000000:0"


def test_render_prints_no_spend(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(commands, "run", lambda args: 0)
    assert cli.main(["render", "--repo", str(tmp_path), "--change-dir", str(tmp_path)]) == 0
    assert "SPEND:" not in capsys.readouterr().out
