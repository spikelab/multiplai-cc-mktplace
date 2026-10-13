"""The model-call wrapper, its trust gate and retry policy, `--plan-only`, and the model key."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from multiplai_core import budget as core_budget
from multiplai_core.agent_runner import AgentRunError, UnknownAgentBackendError
from multiplai_core.budget import BudgetExceededError
from pydantic import BaseModel

from conftest import make_repo, make_units_repo, read_state, run
from spec_extract_pipeline import sdk

# These tests are about resolve, scope and explore: the run ends after explore, as it did
# before the group and write steps existed.
pytestmark = pytest.mark.usefixtures("stops_after_explore")

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


def test_a_failed_run_raises_call_failed_without_a_rerun(monkeypatch):
    # The dispatch rule asks the unit once more (plan, item 7); the wrapper does not re-run it
    # too, or each attempt could cost two agent runs.
    err = AgentRunError("boom", reason="cli", stderr_tail="tail", partial=_result("", cost=0.25))
    fake, go = _call(monkeypatch, err, _result('{"value": 1}'))
    with pytest.raises(sdk.CallFailed):
        asyncio.run(go())
    assert len(fake.calls) == 1
    assert core_budget.get().cost_usd == 0.25  # the failed run's partial spend is kept


def test_a_reformat_that_does_not_parse_raises_call_failed(monkeypatch):
    fake, go = _call(monkeypatch, _result("not json"), _result("still not json"))
    with pytest.raises(sdk.CallFailed):
        asyncio.run(go())
    assert len(fake.calls) == 2 and fake.calls[1]["allowed_tools"] == []


# The pi backend's failures that no call can get past (multiplai_core/pi/backend.py).
PI_SETUP_ERRORS = [
    AgentRunError("pi binary 'pi' not found", reason="pi binary not found"),
    AgentRunError("pi failed to start (PermissionError)", reason="pi failed to start: PermissionError"),
    AgentRunError("bad spec", reason="malformed pi model spec"),
    AgentRunError("PI_GATE_EXT='/x' is not a file", reason="pi gating extension not found"),
]
PI_SETUP_IDS = ["pi-missing", "pi-spawn-failed", "pi-spec-malformed", "pi-gate-missing"]


@pytest.mark.parametrize("err", [
    UnknownAgentBackendError("no agent backend for provider 'pi'", reason="unknown agent backend 'pi'"),
    AgentRunError("claude-agent-sdk is not available", reason="claude-agent-sdk not installed"),
    AgentRunError("rejected", reason="ClaudeAgentOptions rejected: unexpected keyword"),
    *PI_SETUP_ERRORS,
], ids=["unknown-backend", "sdk-missing", "options-rejected", *PI_SETUP_IDS])
def test_a_backend_that_cannot_run_is_not_a_call_failure(monkeypatch, err):
    fake, go = _call(monkeypatch, err, _result('{"value": 1}'))
    with pytest.raises(sdk.BackendUnavailable) as e:
        asyncio.run(go())
    assert not isinstance(e.value, sdk.CallFailed)
    assert len(fake.calls) == 1


def test_a_budget_stop_at_the_reformat_is_a_call_failure(monkeypatch):
    # The first run is paid and recorded. Letting BudgetExceededError through would take its
    # attempt back, and a resume would pay for the whole run again (plan, item 7; row R31).
    fake, go = _call(monkeypatch, _result("not json", cost=1.0), _result('{"value": 1}'), max_usd=1.0)
    with pytest.raises(sdk.CallFailed) as e:
        asyncio.run(go())
    assert isinstance(e.value.__cause__, BudgetExceededError)
    assert len(fake.calls) == 1
    assert core_budget.get().cost_usd == 1.0


def test_a_backend_failure_at_the_reformat_is_a_call_failure(monkeypatch):
    # Same rule for a backend that fails to start only at the reformat: the first run was paid.
    fake, go = _call(monkeypatch, _result("not json"), PI_SETUP_ERRORS[1])
    with pytest.raises(sdk.CallFailed) as e:
        asyncio.run(go())
    assert isinstance(e.value.__cause__, sdk.BackendUnavailable)
    assert len(fake.calls) == 2 and core_budget.get().cost_usd == 0.5


@pytest.mark.parametrize("at", ["first", "reformat"])
def test_any_other_exception_from_run_agent_is_a_call_failure(monkeypatch, at):
    # A bare exception escaping explore's task would cancel every other unit in flight
    # (code_facts.bounded's TaskGroup). The pi backend can raise one: usage_from_events
    # parses usage with int() outside its try (multiplai_core/pi/backend.py).
    err = ValueError("invalid literal for int() with base 10: 'n/a'")
    outcomes = [err] if at == "first" else [_result("not json"), err]
    fake, go = _call(monkeypatch, *outcomes)
    with pytest.raises(sdk.CallFailed) as e:
        asyncio.run(go())
    causes, c = [], e.value
    while c is not None:
        causes.append(c)
        c = c.__cause__
    assert err in causes and "ValueError" in str(e.value)
    assert len(fake.calls) == len(outcomes)


def test_budget_reached_is_not_a_call_failure(monkeypatch):
    fake, go = _call(monkeypatch, _result('{"value": 1}'), max_usd=0.0001)
    core_budget.get().cost_usd = 1.0
    with pytest.raises(BudgetExceededError):
        asyncio.run(go())
    assert fake.calls == []


def test_model_key_from_multiplai_conf(monkeypatch):
    monkeypatch.setattr(sdk, "load_multiplai_conf", lambda: {"spec_extract_explore_model": "pi:some/model"})
    assert _REAL_MODEL_FOR("explore") == "pi:some/model"
    monkeypatch.setattr(sdk, "load_multiplai_conf", lambda: {"spec_extract_explore_model": "anthropic:claude-sonnet-5"})
    assert _REAL_MODEL_FOR("explore") == "claude-sonnet-5"  # an Anthropic model goes bare
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


def _real_call_with(monkeypatch, run_agent) -> None:
    monkeypatch.setattr(sdk, "agent_call_structured", _REAL_CALL)
    monkeypatch.setattr(sdk, "run_agent", run_agent)


def test_a_unit_whose_runs_fail_makes_at_most_two_agent_runs(tmp_path, monkeypatch):
    calls = []

    async def failing(prompt, **kwargs):
        calls.append(prompt)
        raise AgentRunError("boom", reason="cli")

    _real_call_with(monkeypatch, failing)
    repo = make_repo(tmp_path / "widgets")
    root = tmp_path / "out"
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    (folder,) = (root / "widgets").glob("tree@*")
    assert len(calls) == 2  # one per attempt, as plan.txt says
    assert "at most 2 agent runs" in (folder / "plan.txt").read_text()
    assert read_state(folder)["explore_attempts"] == {"unit:u1": 2}


def test_an_unexpected_exception_fails_one_unit_and_spares_the_others(tmp_path, monkeypatch):
    # Not an AgentRunError (the pi backend's int() on a malformed usage field): the unit's
    # call fails as any other does, and the units in flight beside it are not cancelled.
    calls: list[str] = []

    async def run_agent(prompt, **kwargs):
        unit = "d0" if "d0/mod.py" in prompt else "other"
        calls.append(unit)
        await asyncio.sleep(0)  # the other units are in flight now
        if unit == "d0":
            raise ValueError("invalid literal for int() with base 10: 'n/a'")
        return _result('{"facts": [], "files": []}')

    _real_call_with(monkeypatch, run_agent)
    repo = make_units_repo(tmp_path / "widgets", 3)
    root = tmp_path / "out"
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    (folder,) = (root / "widgets").glob("tree@*")
    state = read_state(folder)
    assert state["explore_attempts"] == {"unit:u1": 2, "unit:u2": 1, "unit:u3": 1}
    assert "ValueError" in state["explore_answers"]["unit:u1"]["error"]
    assert "facts" in state["explore_answers"]["unit:u2"] and "facts" in state["explore_answers"]["unit:u3"]
    assert calls.count("d0") == 2 and calls.count("other") == 2


@pytest.mark.parametrize("err", [
    UnknownAgentBackendError("no agent backend for provider 'pi'", reason="unknown agent backend 'pi'"),
    PI_SETUP_ERRORS[0],
], ids=["unknown-backend", "pi-missing"])
def test_a_backend_that_cannot_run_counts_no_attempt(tmp_path, monkeypatch, capsys, err):
    calls = []

    async def no_backend(prompt, **kwargs):
        calls.append(prompt)
        raise err

    _real_call_with(monkeypatch, no_backend)
    repo = make_repo(tmp_path / "widgets")
    root = tmp_path / "out"
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 1
    assert err.reason in capsys.readouterr().err
    (folder,) = (root / "widgets").glob("tree@*")
    state = read_state(folder)
    assert len(calls) == 1
    assert state["explore_attempts"] == {} and state["explore_answers"] == {}
    assert "explore" not in state["steps_done"]

    # Once the setting is fixed, the same command asks the unit, as a run that never failed does.
    async def ok(prompt, **kwargs):
        return _result('{"facts": [], "files": []}')

    monkeypatch.setattr(sdk, "run_agent", ok)
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    state = read_state(folder)
    assert state["explore_attempts"] == {"unit:u1": 1} and "facts" in state["explore_answers"]["unit:u1"]
