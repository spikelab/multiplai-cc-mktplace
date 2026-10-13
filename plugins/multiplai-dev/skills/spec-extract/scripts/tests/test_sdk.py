"""The model-call wrapper, its trust gate and retry policy, `--plan-only`, and the model key."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from multiplai_core import budget as core_budget
from multiplai_core.agent_runner import AgentRunError
from multiplai_core.budget import BudgetExceededError
from pydantic import BaseModel

from conftest import make_repo, read_state, run
from spec_extract_pipeline import sdk

_REAL_CALL = sdk.agent_call_structured  # before the autouse fixture replaces it
_REAL_MODEL_FOR = sdk.model_for


class Answer(BaseModel):
    value: int


def _result(text: str, cost: float = 0.5):
    return SimpleNamespace(text=text, usage=SimpleNamespace(cost_usd=cost, input_tokens=10, output_tokens=1))


class FakeRunAgent:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def __call__(self, prompt, **kwargs):
        self.calls.append(dict(kwargs, prompt=prompt))
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


def _call(monkeypatch, *outcomes, max_usd=80.0):
    fake = FakeRunAgent(*outcomes)
    monkeypatch.setattr(sdk, "run_agent", fake)
    core_budget.start(max_usd, None, ledger_cls=sdk.SpecExtractBudget)

    async def go():
        return await _REAL_CALL("prompt", Answer, allowed_tools=["Read"], budget_label="explore:unit")
    return fake, go


def test_untrusted_repo_is_refused_before_any_call(monkeypatch):
    monkeypatch.delenv(sdk.TRUST_ENV)
    fake, go = _call(monkeypatch, _result('{"value": 1}'))
    with pytest.raises(sdk.RepoTrustError):
        asyncio.run(go())
    assert fake.calls == []
    assert not issubclass(sdk.RepoTrustError, sdk.CallFailed)


def test_answer_is_parsed_and_spend_recorded(monkeypatch):
    fake, go = _call(monkeypatch, _result('{"value": 7}'))
    assert asyncio.run(go()) == Answer(value=7)
    assert core_budget.get().cost_usd == 0.5
    assert fake.calls[0]["allowed_tools"] == ["Read"] and fake.calls[0]["component"] == "spec-extract"


def test_unparsable_answer_is_reformatted_once_without_tools(monkeypatch):
    fake, go = _call(monkeypatch, _result("not json"), _result('{"value": 3}'))
    assert asyncio.run(go()) == Answer(value=3)
    assert fake.calls[1]["allowed_tools"] == [] and fake.calls[1]["max_turns"] == 1
    assert core_budget.get().cost_usd == 1.0


def test_two_failures_raise_call_failed(monkeypatch):
    err = AgentRunError("boom", reason="cli", stderr_tail="tail", partial=_result("", cost=0.25))
    fake, go = _call(monkeypatch, err, err)
    with pytest.raises(sdk.CallFailed):
        asyncio.run(go())
    assert len(fake.calls) == 2
    assert "rejected by a program" in fake.calls[1]["prompt"]
    assert core_budget.get().cost_usd == 0.5  # each failed run's partial spend is kept


def test_budget_reached_is_not_a_call_failure(monkeypatch):
    fake, go = _call(monkeypatch, _result('{"value": 1}'), max_usd=0.0001)
    core_budget.get().cost_usd = 1.0
    with pytest.raises(BudgetExceededError):
        asyncio.run(go())
    assert fake.calls == []


def test_model_key_from_multiplai_conf(monkeypatch):
    monkeypatch.setattr(sdk, "load_multiplai_conf", lambda: {"spec_extract_explore_model": "pi:some/model"})
    assert _REAL_MODEL_FOR("explore") == "pi:some/model"
    monkeypatch.setattr(sdk, "load_multiplai_conf", lambda: {})
    assert _REAL_MODEL_FOR("explore") is None


# --- the command line ---------------------------------------------------------------------

def test_exit_4_without_trust_and_no_call(tmp_path, monkeypatch, capsys, fake_call):
    monkeypatch.delenv(sdk.TRUST_ENV)
    repo = make_repo(tmp_path / "widgets")
    root = tmp_path / "out"
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 4
    assert "--trust-repo" in capsys.readouterr().err
    assert not fake_call.calls
    (folder,) = (root / "widgets").glob("tree@*")
    assert read_state(folder)["explore_attempts"] == {}
    # --trust-repo is the same as the variable.
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root, "--trust-repo") == 0


def test_plan_only_needs_no_trust_and_makes_no_call(tmp_path, monkeypatch, capsys, fake_call):
    monkeypatch.delenv(sdk.TRUST_ENV)
    repo = make_repo(tmp_path / "widgets")
    root = tmp_path / "out"
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root, "--plan-only") == 0
    out = capsys.readouterr().out
    (folder,) = (root / "widgets").glob("tree@*")
    assert f"SPEC_EXTRACT:PLAN {folder / 'plan.txt'}" in out
    assert "Explore: 1 unit(s)" in out and "- u1: 3 file(s)" in out
    assert "Public symbols to specify: 3." in out and "at least 1 write call(s)" in out
    assert (folder / "plan.txt").read_text() in out
    assert not fake_call.calls
    state = read_state(folder)
    assert state["steps_done"] == ["resolve", "scope"] and state["budget"] is None
