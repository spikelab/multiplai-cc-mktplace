from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from pydantic import BaseModel

from multiplai_core import deny_list
from multiplai_core.agent_runner import AgentRunError, UsageLimitError
from review_pipeline import budget, sdk
from review_pipeline.config import STAGE_MODELS, load_config
from review_pipeline.models import VerifierAnswer


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


async def test_the_allow_list_reaches_run_agent_and_core_builds_the_deny_list(trusted, fake_run):
    calls, replies = fake_run
    replies.append(_result('{"value": 3}'))
    out = await sdk.agent_call_structured("p", Answer, allowed_tools=sdk.FINDER_TOOLS, cwd="/repo",
                                          budget_label="find:diff-bugs", model="m", effort="high")
    assert out.value == 3
    call = calls[0]
    assert call["allowed_tools"] == ["Read", "Grep", "Glob", "WebFetch", "WebSearch"]
    assert "disallowed_tools" not in call
    assert (call["cwd"], call["component"], call["label"], call["model"], call["effort"]) == \
        ("/repo", "review", "find:diff-bugs", "m", "high")


async def test_every_call_limits_reads_to_its_cwd_including_the_reformat(trusted, fake_run, tmp_path):
    """read_roots=[cwd] reaches run_agent on the first call and on the no-tools reformat."""
    calls, replies = fake_run
    replies.extend([_result("not json"), _result('{"value": 1}')])
    await sdk.agent_call_structured("p", Answer, allowed_tools=sdk.FINDER_TOOLS, cwd=str(tmp_path))
    assert [c["read_roots"] for c in calls] == [[str(tmp_path)], [str(tmp_path)]]


async def test_a_call_with_no_cwd_may_read_nothing(trusted, fake_run):
    calls, replies = fake_run
    replies.append(_result('{"value": 1}'))
    await sdk.agent_call_structured("p", Answer, allowed_tools=sdk.FINDER_TOOLS)
    assert calls[0]["read_roots"] == []


def test_finders_and_verifiers_read_and_browse_the_merger_only_reads():
    assert sdk.FINDER_TOOLS == sdk.VERIFIER_TOOLS == ["Read", "Grep", "Glob", "WebFetch", "WebSearch"]
    assert sdk.MERGER_TOOLS == ["Read", "Grep", "Glob"]
    for tools in (sdk.FINDER_TOOLS, sdk.VERIFIER_TOOLS, sdk.MERGER_TOOLS):
        assert not {"Bash", "Edit", "Write", "Agent"} & set(tools)


def test_core_denies_what_each_stage_does_not_allow():
    """run_agent's default deny-list is core's `deny_list(allowed)`; call the
    real one with each stage's allow-list."""
    for tools in (sdk.FINDER_TOOLS, sdk.VERIFIER_TOOLS, sdk.MERGER_TOOLS):
        denied = deny_list(tools)
        for tool in ("Bash", "Edit", "Write", "Agent", "Skill"):
            assert tool in denied, tool
        assert not set(tools) & set(denied)
    assert {"WebFetch", "WebSearch"} <= set(deny_list(sdk.MERGER_TOOLS))
    assert not {"WebFetch", "WebSearch"} & set(deny_list(sdk.FINDER_TOOLS))
    assert not {"WebFetch", "WebSearch"} & set(deny_list(sdk.VERIFIER_TOOLS))


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
    assert "disallowed_tools" not in reformat
    assert {"Read", "Grep", "Glob", "Bash"} <= set(deny_list(reformat["allowed_tools"]))
    assert reformat["label"] == "verify:reformat"


async def test_a_verifier_answer_with_an_unknown_topic_is_asked_again(trusted, fake_run):
    calls, replies = fake_run
    answer = {"status": "confirmed", "reason": "r", "impact": "breaks-users"}
    replies += [_result(json.dumps({**answer, "topic": "test"})), _result(json.dumps({**answer, "topic": "tests"}))]
    out = await sdk.agent_call_structured("p", VerifierAnswer, allowed_tools=sdk.VERIFIER_TOOLS, budget_label="verify")
    assert out.topic == "tests" and len(calls) == 2
    assert "topic is required" in calls[1]["prompt"]


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


def _limit_error(partial=None) -> UsageLimitError:
    """What multiplai-core raises when the CLI reports a hit usage limit."""
    return UsageLimitError(
        "run_agent [verify] failed: Claude Code returned an error result: You've hit your session limit "
        "\u00b7 resets 9:40am (UTC) (exit code: 1) after 1 attempt(s)",
        limit="session", resets="9:40am (UTC)", reason="failed: You've hit your session limit", partial=partial)


@pytest.mark.parametrize("call", ["structured", "text"])
async def test_a_usage_limit_stops_at_once_with_no_reask(trusted, fake_run, call, monkeypatch):
    import paths_stub

    monkeypatch.setattr(sdk, "agent_call_text", paths_stub.REAL_AGENT_CALL_TEXT)  # conftest stubs it
    calls, replies = fake_run
    ledger = budget.start(10.0)
    partial = SimpleNamespace(usage=SimpleNamespace(input_tokens=1, output_tokens=0, cost_usd=0.25), tool_calls=(),
                              turns=0)
    replies += [_limit_error(partial), _result('{"value": 1}')]
    with pytest.raises(sdk.UsageLimitError) as exc:
        if call == "structured":
            await sdk.agent_call_structured("p", Answer, allowed_tools=sdk.FINDER_TOOLS, budget_label="verify")
        else:
            await sdk.agent_call_text("p", allowed_tools=sdk.READ_ONLY_TOOLS, budget_label="find:piece")
    assert len(calls) == 1
    assert isinstance(exc.value, sdk.AgentCallError)
    assert exc.value.limit == "session" and exc.value.resets == "9:40am (UTC)"
    assert "usage limit" in str(exc.value) and "9:40am (UTC)" in str(exc.value)
    assert ledger.cost_usd == pytest.approx(0.25)  # the failed call was still paid for


async def test_a_usage_limit_on_the_reask_also_stops(trusted, fake_run):
    calls, replies = fake_run
    replies += [_result("not json"), _limit_error()]
    with pytest.raises(sdk.UsageLimitError):
        await sdk.agent_call_structured("p", Answer, allowed_tools=sdk.FINDER_TOOLS)
    assert len(calls) == 2


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


def test_a_call_whose_usage_is_none_is_counted_as_a_no_usage_call():
    a = budget.ReviewBudget(max_usd=10)
    a.record(None, label="find")
    assert (a.calls, a.cost_usd, a.no_usage_calls, a.total_tokens) == (1, 0.0, 1, 0)
    assert a.by_stage["find"]["no_usage_calls"] == 1


def test_an_old_ledger_without_by_stage_loads_and_keeps_counting():
    old = {"calls": 9, "input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 0,
           "cache_creation_tokens": 0, "cost_usd": 1.25, "by_label": {"verify": 1.25}, "max_usd": 50.0}
    a = budget.ReviewBudget(max_usd=50)
    a.load_state(old)
    assert (a.calls, a.cost_usd, a.by_stage, a.no_usage_calls) == (9, 1.25, {}, 0)
    a.record(SimpleNamespace(input_tokens=1, cost_usd=0.5), label="verify")
    assert a.calls == 10 and a.by_stage["verify"]["calls"] == 1
    assert a.by_label["verify"] == pytest.approx(1.75)


def _fake_sdk(monkeypatch) -> list[dict]:
    """Replace `claude_agent_sdk` under multiplai-core's real `run_agent`; return the
    `ClaudeAgentOptions` keyword arguments of every call, i.e. what the CLI would be given."""
    options: list[dict] = []

    class TextBlock:
        text = '{"value": 1}'

    class AssistantMessage:
        content = [TextBlock()]

    async def query(prompt, options):
        yield AssistantMessage()

    fake = ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = lambda **kwargs: options.append(kwargs) or SimpleNamespace(**kwargs)
    fake.query = query
    fake.AssistantMessage = AssistantMessage
    fake.TextBlock = TextBlock
    fake.HookMatcher = lambda **kwargs: SimpleNamespace(**kwargs)  # run_agent needs it for read_roots
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    return options


@pytest.mark.parametrize("env, review_yaml, sent", [
    ({}, "", ("claude-test-session", "low")),
    ({"MULTIPLAI_MODEL": "claude-test-default", "MULTIPLAI_EFFORT": "high"}, "",
     ("claude-test-default", "high")),
    ({"MULTIPLAI_MODEL": "claude-test-default", "MULTIPLAI_EFFORT": "high"},
     "finder_model: claude-test-explicit\nverifier_model: claude-test-explicit\neffort: medium\n",
     ("claude-test-explicit", "medium")),
], ids=["session", "multiplai-defaults", "review-yaml-wins"])
async def test_every_stage_call_gets_the_configured_then_multiplai_then_the_session_model(
        trusted, monkeypatch, tmp_path, env, review_yaml, sent):
    """SESSION_DEFAULT (no model or effort configured) passes None, and run_agent's own fallback
    (multiplai-core 0.21) picks MULTIPLAI_MODEL / MULTIPLAI_EFFORT, else the session's."""
    # conftest's _no_session_defaults has cleared MULTIPLAI_* and CLAUDE_CODE_SESSION_ID, so
    # no session transcript is read.
    monkeypatch.setenv("MULTIPLAI_SESSION_MODEL", "claude-test-session")
    monkeypatch.setenv("MULTIPLAI_SESSION_EFFORT", "low")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "review.yaml").write_text(review_yaml, encoding="utf-8")
    cfg = load_config(tmp_path)
    options = _fake_sdk(monkeypatch)

    for stage, attr in STAGE_MODELS.items():
        out = await sdk.agent_call_structured("p", Answer, allowed_tools=sdk.FINDER_TOOLS,
                                              model=getattr(cfg, attr), effort=cfg.effort,
                                              budget_label=stage)
        assert out.value == 1
    assert [(o["model"], o.get("effort")) for o in options] == [sent] * len(STAGE_MODELS)
