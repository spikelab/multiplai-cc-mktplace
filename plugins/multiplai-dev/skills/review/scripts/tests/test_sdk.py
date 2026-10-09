from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from multiplai_core.agent_runner import AgentRunError
from review_pipeline import budget, sdk


class Answer(BaseModel):
    value: int


def _result(text: str, cost: float = 0.5):
    return SimpleNamespace(text=text, usage=SimpleNamespace(input_tokens=10, output_tokens=5, cost_usd=cost))


@pytest.fixture
def trusted(monkeypatch):
    monkeypatch.setenv("REVIEW_TRUST_REPO", "1")


@pytest.fixture
def fake_run(monkeypatch):
    calls: list[dict] = []
    replies: list = []

    async def run_agent(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(sdk, "run_agent", run_agent)
    return calls, replies


async def test_untrusted_repo_is_refused_before_any_call(fake_run):
    calls, _ = fake_run
    with pytest.raises(sdk.RepoTrustError):
        await sdk.agent_call_structured("p", Answer, allowed_tools=sdk.FINDER_TOOLS)
    assert calls == []


async def test_read_only_tools_and_their_complement(trusted, fake_run):
    calls, replies = fake_run
    replies.append(_result('{"value": 3}'))
    out = await sdk.agent_call_structured("p", Answer, allowed_tools=sdk.FINDER_TOOLS, cwd="/repo",
                                          budget_label="find:diff-bugs", model="m", effort="high")
    assert out.value == 3
    call = calls[0]
    assert call["allowed_tools"] == ["Read", "Grep", "Glob", "WebFetch", "WebSearch"]
    for tool in ("Bash", "Edit", "Write", "Agent", "Skill"):
        assert tool in call["disallowed_tools"]
    assert not {"Read", "Grep", "Glob", "WebFetch", "WebSearch"} & set(call["disallowed_tools"])
    assert (call["cwd"], call["component"], call["label"], call["model"], call["effort"]) == \
        ("/repo", "review", "find:diff-bugs", "m", "high")


def test_finders_and_verifiers_read_and_browse_the_merger_only_reads():
    assert sdk.FINDER_TOOLS == sdk.VERIFIER_TOOLS == ["Read", "Grep", "Glob", "WebFetch", "WebSearch"]
    assert sdk.MERGER_TOOLS == ["Read", "Grep", "Glob"]
    for tools in (sdk.FINDER_TOOLS, sdk.VERIFIER_TOOLS, sdk.MERGER_TOOLS):
        assert not {"Bash", "Edit", "Write", "Agent"} & set(tools)
        assert "WebFetch" in sdk.deny_list("p", sdk.MERGER_TOOLS)
        assert "WebFetch" not in sdk.deny_list("p", sdk.FINDER_TOOLS)


async def test_an_unparsable_answer_is_reformatted_without_tools_not_rerun(trusted, fake_run):
    calls, replies = fake_run
    replies += [_result('{"value": "not a number"}'), _result('```json\n{"value": 7}\n```')]
    out = await sdk.agent_call_structured("original", Answer, allowed_tools=sdk.VERIFIER_TOOLS,
                                          max_turns=60, budget_label="verify")
    assert out.value == 7
    assert len(calls) == 2
    reformat = calls[1]
    assert not reformat["prompt"].startswith("original") and "original" not in reformat["prompt"]
    assert '<answer>\n{"value": "not a number"}\n</answer>' in reformat["prompt"]
    assert "rejected" in reformat["prompt"] and '"value"' in reformat["prompt"]
    assert reformat["allowed_tools"] == [] and reformat["max_turns"] == sdk.REFORMAT_MAX_TURNS
    assert {"Read", "Grep", "Glob", "Bash"} <= set(reformat["disallowed_tools"])
    assert reformat["label"] == "verify:reformat"


def test_a_prose_answer_is_reported_as_having_no_json():
    prose = "## Tests review\n\n| tier | covered |\n`{tier: 1, name: x}` then [\"merge\"]"
    with pytest.raises(ValueError, match="contains no JSON object with the field.s. value; it starts: '## Tests"):
        sdk.parse_answer(prose, Answer)
    with pytest.raises(ValueError) as caught:  # JSON with the field keeps the parser's own error
        sdk.parse_answer('{"value": "x"}', Answer)
    assert "no JSON object" not in str(caught.value)


async def test_second_failure_raises(trusted, fake_run):
    calls, replies = fake_run
    replies += [_result("no json here"), _result("still none")]
    with pytest.raises(sdk.AgentCallError):
        await sdk.agent_call_structured("p", Answer, allowed_tools=sdk.VERIFIER_TOOLS)
    assert len(calls) == 2


async def test_failed_run_counts_as_a_failure_and_is_reasked(trusted, fake_run):
    calls, replies = fake_run
    err = AgentRunError.__new__(AgentRunError)
    RuntimeError.__init__(err, "boom")
    err.reason, err.stderr_tail, err.partial = "boom", "", None
    replies += [err, _result(json.dumps({"value": 1}))]
    assert (await sdk.agent_call_structured("p", Answer, allowed_tools=sdk.MERGER_TOOLS)).value == 1
    assert len(calls) == 2
    # No answer came back, so there is nothing to reformat: the prompt runs again.
    assert calls[1]["prompt"].startswith("p\n\n---\n") and "boom" in calls[1]["prompt"]
    assert calls[1]["allowed_tools"] == sdk.MERGER_TOOLS


async def test_spend_is_recorded_and_the_breaker_stops_the_next_call(trusted, fake_run):
    calls, replies = fake_run
    ledger = budget.start(1.0)
    replies += [_result('{"value": 1}', cost=1.2)]
    await sdk.agent_call_structured("p", Answer, allowed_tools=sdk.FINDER_TOOLS, budget_label="find:tests")
    assert ledger.cost_usd == pytest.approx(1.2) and ledger.by_label["find:tests"] == pytest.approx(1.2)
    with pytest.raises(budget.BudgetExceededError):
        await sdk.agent_call_structured("p", Answer, allowed_tools=sdk.FINDER_TOOLS)
    assert len(calls) == 1


def test_budget_state_round_trip():
    a = budget.ReviewBudget(max_usd=10)
    a.record(SimpleNamespace(input_tokens=1, output_tokens=2, cost_usd=0.25), label="verify")
    b = budget.ReviewBudget(max_usd=10)
    b.load_state(a.to_state())
    assert (b.cost_usd, b.calls, b.by_label) == (0.25, 1, {"verify": 0.25})


def test_by_stage_holds_calls_tokens_and_cost_per_label_and_sums_to_the_totals():
    a = budget.ReviewBudget(max_usd=10)
    a.record(SimpleNamespace(input_tokens=10, output_tokens=2, cache_read_tokens=100,
                             cache_creation_tokens=5, cost_usd=0.5), label="find:tests")
    a.record(SimpleNamespace(input_tokens=1, output_tokens=1, cost_usd=0.25), label="verify")
    a.record(SimpleNamespace(input_tokens=3, output_tokens=4, cost_usd=0.25), label="verify")
    assert a.by_stage["verify"] == {"calls": 2, "input_tokens": 4, "output_tokens": 5, "cache_read_tokens": 0,
                                    "cache_creation_tokens": 0, "cost_usd": 0.5, "no_usage_calls": 0}
    stages = a.by_stage.values()
    assert sum(s["calls"] for s in stages) == a.calls == 3
    assert sum(s["cost_usd"] for s in stages) == pytest.approx(a.cost_usd)
    for f in budget.TOKEN_FIELDS:
        assert sum(s[f] for s in stages) == getattr(a, f)
    b = budget.ReviewBudget(max_usd=10)
    b.load_state(a.to_state())
    assert b.by_stage == a.by_stage and b.by_label == a.by_label


def test_a_call_with_no_usage_is_counted_with_zero_cost():
    a = budget.ReviewBudget(max_usd=10)
    a.record(SimpleNamespace(), label="merge")
    assert (a.calls, a.cost_usd, a.no_usage_calls) == (1, 0.0, 1)
    assert a.by_stage["merge"]["no_usage_calls"] == 1


def test_an_old_ledger_without_by_stage_loads_and_keeps_counting():
    old = {"calls": 9, "input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 0,
           "cache_creation_tokens": 0, "cost_usd": 1.25, "by_label": {"verify": 1.25}, "max_usd": 50.0}
    a = budget.ReviewBudget(max_usd=50)
    a.load_state(old)
    assert (a.calls, a.cost_usd, a.by_stage, a.no_usage_calls) == (9, 1.25, {}, 0)
    a.record(SimpleNamespace(input_tokens=1, cost_usd=0.5), label="verify")
    assert a.calls == 10 and a.by_stage["verify"]["calls"] == 1
    assert a.by_label["verify"] == pytest.approx(1.75)
