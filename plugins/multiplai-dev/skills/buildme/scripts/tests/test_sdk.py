"""Tests for the SDK adapter — tool allow-lists and core's deny-list, the repo trust gate, and
failure-path budget accounting."""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from unittest.mock import AsyncMock, patch

from build_pipeline import budget as budget_mod
from build_pipeline.sdk import (
    RepoTrustError,
    agent_call,
    agent_call_structured,
    llm_call,
    llm_call_structured,
    LLMCallError,
    LLMCallTimeoutError,
)
from multiplai_core import deny_list
from multiplai_core.agent_runner import (
    AgentRunError,
    AgentRunTimeout,
)


def _mock_run_agent():
    run = AsyncMock()
    run.return_value.text = "answer"
    run.return_value.turns = 1
    run.return_value.files_changed = []
    return run


class TestDenyList:
    """buildme passes only allow-lists; run_agent builds the deny-list from
    them with core's `deny_list`. These call the real core function with
    buildme's own allow-lists, so a core change that opened a dangerous tool
    to one of them fails here."""

    def test_read_only_agents_are_denied_execution_writes_and_egress(self):
        from build_pipeline.llm_steps.review_steps import REVIEWER_TOOLS

        denied = deny_list(REVIEWER_TOOLS)
        for tool in ("Bash", "Edit", "Write", "WebFetch", "WebSearch"):
            assert tool in denied, tool
        for tool in REVIEWER_TOOLS:
            assert tool not in denied, tool

    def test_text_only_call_is_denied_everything(self):
        denied = deny_list(None)
        for tool in ("Bash", "Edit", "Write", "WebFetch", "WebSearch", "Read"):
            assert tool in denied, tool

    def test_explainer_keeps_the_web_and_loses_bash_and_writes(self):
        """The explainer's allow-list must still deny Bash/Write/Edit — a
        web-ingesting agent is one prompt injection away from using them."""
        denied = deny_list(["WebSearch", "WebFetch", "Read", "Glob", "Grep"])
        for tool in ("Bash", "Write", "Edit", "NotebookEdit", "Task"):
            assert tool in denied, tool
        for tool in ("WebSearch", "WebFetch"):
            assert tool not in denied, tool

    def test_prototype_allowlist_keeps_write_denies_bash(self):
        """Bash writes would bypass the files_changed-based write-boundary
        check (only Write/Edit ToolUseBlocks are collected)."""
        from build_pipeline.llm_steps.prototype_steps import PROTOTYPE_TOOLS

        denied = deny_list(PROTOTYPE_TOOLS)
        assert "Bash" in denied
        assert "Write" not in denied


class TestLlmCallToolPolicy:
    @pytest.mark.asyncio
    async def test_no_tools_requested_leaves_the_deny_list_to_core(self):
        """No allow-list and no deny-list: run_agent then denies every tool."""
        with patch("build_pipeline.sdk.run_agent", _mock_run_agent()) as run:
            await llm_call("hello")

        assert run.call_args.kwargs["allowed_tools"] is None
        assert "disallowed_tools" not in run.call_args.kwargs

    @pytest.mark.asyncio
    async def test_text_only_needs_no_trust(self, monkeypatch):
        """Pure text calls have no tools to abuse — they must keep working
        without --trust-repo."""
        monkeypatch.delenv("BUILDME_TRUST_REPO", raising=False)
        with patch("build_pipeline.sdk.run_agent", _mock_run_agent()):
            assert await llm_call("hello") == "answer"

    @pytest.mark.asyncio
    async def test_explicit_tools_reach_run_agent_without_a_deny_list(self, monkeypatch):
        """Callers that genuinely want tools keep them; run_agent denies the
        complement (see TestDenyList)."""
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        with patch("build_pipeline.sdk.run_agent", _mock_run_agent()) as run:
            await llm_call("hello", allowed_tools=["Read", "Grep"])

        assert run.call_args.kwargs["allowed_tools"] == ["Read", "Grep"]
        assert "disallowed_tools" not in run.call_args.kwargs

    @pytest.mark.asyncio
    async def test_tool_bearing_call_requires_trust(self, monkeypatch):
        """A tool-using llm_call is an agent in all but name — same gate."""
        monkeypatch.delenv("BUILDME_TRUST_REPO", raising=False)
        with patch("build_pipeline.sdk.run_agent", _mock_run_agent()) as run:
            with pytest.raises(RepoTrustError):
                await llm_call("hello", allowed_tools=["Read", "Grep"])
        run.assert_not_awaited()


class TestFailedCallBudgetAccounting:
    """A call that dies after burning a 150k-char prompt still cost money.

    `agent_call` already charged partial usage on failure; `llm_call` raised
    without recording, so exactly the spend the breaker exists to catch — a
    review that times out on a huge diff — was invisible to it.
    """

    @dataclass
    class _Usage:
        input_tokens: int = 0
        output_tokens: int = 0
        cache_read_tokens: int = 0
        cache_creation_tokens: int = 0
        cost_usd: float = 0.0

    class _Partial:
        def __init__(self, usage):
            self.usage = usage
            self.text = "partial"
            self.turns = 1
            self.files_changed = []

    @pytest.fixture(autouse=True)
    def _clean_budget(self):
        budget_mod.reset()
        yield
        budget_mod.reset()

    def _error(self, cls):
        return cls(
            "boom", reason="boom", stderr_tail="",
            partial=self._Partial(self._Usage(input_tokens=40_000, output_tokens=100)),
        )

    @pytest.mark.asyncio
    async def test_llm_call_error_charges_partial_usage(self):
        with patch("build_pipeline.sdk.run_agent", new_callable=AsyncMock) as run:
            run.side_effect = self._error(AgentRunError)
            with pytest.raises(LLMCallError):
                await llm_call("hello", budget_label="review")
        assert budget_mod.get_budget().total_tokens == 40_100

    @pytest.mark.asyncio
    async def test_llm_call_timeout_charges_partial_usage(self):
        with patch("build_pipeline.sdk.run_agent", new_callable=AsyncMock) as run:
            run.side_effect = self._error(AgentRunTimeout)
            with pytest.raises(LLMCallTimeoutError):
                await llm_call("hello", budget_label="review")
        assert budget_mod.get_budget().total_tokens == 40_100

    @pytest.mark.asyncio
    async def test_a_failure_with_no_partial_is_not_fatal(self):
        """No usage recorded is fine; accounting must never mask the error."""
        with patch("build_pipeline.sdk.run_agent", new_callable=AsyncMock) as run:
            run.side_effect = AgentRunError("boom", reason="boom", stderr_tail="")
            with pytest.raises(LLMCallError):
                await llm_call("hello")
        assert budget_mod.get_budget().total_tokens == 0


class TestAgentCallToolPolicy:
    @pytest.mark.asyncio
    async def test_allow_list_reaches_run_agent_without_a_deny_list(self, monkeypatch):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        with patch("build_pipeline.sdk.run_agent", _mock_run_agent()) as run:
            result = await agent_call(
                "build it", allowed_tools=["Read", "Write", "Glob", "Grep"],
            )

        assert result.success
        assert run.call_args.kwargs["allowed_tools"] == ["Read", "Write", "Glob", "Grep"]
        assert "disallowed_tools" not in run.call_args.kwargs

    @pytest.mark.asyncio
    async def test_untrusted_repo_refused(self, monkeypatch):
        monkeypatch.delenv("BUILDME_TRUST_REPO", raising=False)
        with patch("build_pipeline.sdk.run_agent", _mock_run_agent()) as run:
            with pytest.raises(RepoTrustError):
                await agent_call("build it", allowed_tools=["Read"])
        run.assert_not_awaited()


class TestExplainerPath:
    """run_explainer spawns a web-ingesting, tool-bearing agent — it must
    reach run_agent with its allow-list only (so core denies the complement)
    and inherit the trust gate via llm_call."""

    def _dep_and_config(self):
        class Dep:
            name = "somelib"
            mentioned_in = ["design.md § Decisions"]
            evidence = "not declared in pyproject.toml"

        class Config:
            model = None
            project_description = "test project"
            model_for = staticmethod(lambda step: None)
            effort_for = staticmethod(lambda step: None)

        return Dep(), Config()

    @pytest.mark.asyncio
    async def test_allow_list_reaches_run_agent_without_a_deny_list(self, monkeypatch):
        from build_pipeline.llm_steps.spec_steps import run_explainer

        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        dep, config = self._dep_and_config()
        with patch("build_pipeline.sdk.run_agent", _mock_run_agent()) as run:
            await run_explainer(dep, config, usage_context="parses TOML configs")

        allowed = run.call_args.kwargs["allowed_tools"]
        assert "WebSearch" in allowed
        assert not {"Bash", "Write", "Edit"} & set(allowed)
        assert "disallowed_tools" not in run.call_args.kwargs

    @pytest.mark.asyncio
    async def test_respects_trust_gate(self, monkeypatch):
        from build_pipeline.llm_steps.spec_steps import run_explainer

        monkeypatch.delenv("BUILDME_TRUST_REPO", raising=False)
        dep, config = self._dep_and_config()
        with patch("build_pipeline.sdk.run_agent", _mock_run_agent()) as run:
            with pytest.raises(RepoTrustError):
                await run_explainer(dep, config)
        run.assert_not_awaited()


class TestEffortReachesTheSdk:
    """The conf-configured effort is worthless if it stops at the config
    object — these assert it lands in the actual run_agent call."""

    @pytest.mark.asyncio
    async def test_llm_call_forwards_effort(self):
        with patch("build_pipeline.sdk.run_agent", new_callable=AsyncMock) as run:
            run.return_value.text = "answer"
            await llm_call("hello", effort="low")

        assert run.call_args.kwargs["effort"] == "low"

    @pytest.mark.asyncio
    async def test_llm_call_defaults_to_no_effort(self):
        """Unset stays unset — run_agent only forwards it to the SDK when set,
        so the default behaviour is untouched."""
        with patch("build_pipeline.sdk.run_agent", new_callable=AsyncMock) as run:
            run.return_value.text = "answer"
            await llm_call("hello")

        assert run.call_args.kwargs["effort"] is None

    @pytest.mark.asyncio
    async def test_structured_call_forwards_effort(self):
        from pydantic import BaseModel

        class Answer(BaseModel):
            value: int

        with patch("build_pipeline.sdk.run_agent", new_callable=AsyncMock) as run:
            run.return_value.text = '{"value": 1}'
            await llm_call_structured("hello", Answer, effort="medium")

        assert run.call_args.kwargs["effort"] == "medium"

    @pytest.mark.asyncio
    async def test_agent_call_forwards_effort(self, monkeypatch):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        with patch("build_pipeline.sdk.run_agent", new_callable=AsyncMock) as run:
            run.return_value.text = "done"
            run.return_value.turns = 1
            run.return_value.files_changed = []
            await agent_call("hello", allowed_tools=["Read"], effort="high")

        assert run.call_args.kwargs["effort"] == "high"


class TestAgentCallStructured:
    """Same caller contract as `llm_call_structured` — a validated model or an
    `LLMCallError` — but backed by a tool-using agent, whose failures arrive as
    `AgentResult(success=False)` rather than as exceptions."""

    class Answer(BaseModel):
        value: int

    @staticmethod
    def _run_result(text: str):
        result = SimpleNamespace()
        result.text = text
        result.turns = 1
        result.files_changed = []
        result.usage = SimpleNamespace()
        return result

    @pytest.fixture(autouse=True)
    def _trusted(self, monkeypatch):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")

    @pytest.mark.asyncio
    async def test_happy_path_returns_validated_model(self):
        with patch("build_pipeline.sdk.run_agent", new_callable=AsyncMock) as run:
            run.return_value = self._run_result('{"value": 1}')
            answer = await agent_call_structured(
                "review it", self.Answer, allowed_tools=["Read", "Grep"],
            )

        assert answer.value == 1
        assert run.call_args.kwargs["allowed_tools"] == ["Read", "Grep"]
        assert "disallowed_tools" not in run.call_args.kwargs

    @pytest.mark.asyncio
    async def test_one_retry_then_success(self):
        """The repair prompt echoes the schema, exactly as llm_call_structured."""
        with patch("build_pipeline.sdk.run_agent", new_callable=AsyncMock) as run:
            run.side_effect = [
                self._run_result("sorry, no JSON here"),
                self._run_result('{"value": 7}'),
            ]
            answer = await agent_call_structured(
                "review it", self.Answer, allowed_tools=["Read"],
            )

        assert answer.value == 7
        assert run.await_count == 2
        repair_prompt = run.await_args_list[1].args[0]
        assert "Return ONLY valid JSON matching this schema" in repair_prompt
        assert '"value"' in repair_prompt

    @pytest.mark.asyncio
    async def test_failed_agent_run_is_treated_as_a_validation_failure(self):
        """agent_call degrades to success=False instead of raising — that must
        consume a retry rather than being validated as if it were output."""
        with patch("build_pipeline.sdk.run_agent", new_callable=AsyncMock) as run:
            run.side_effect = [
                AgentRunError("boom", reason="boom", stderr_tail=""),
                self._run_result('{"value": 3}'),
            ]
            answer = await agent_call_structured(
                "review it", self.Answer, allowed_tools=["Read"],
            )

        assert answer.value == 3
        assert run.await_count == 2

    @pytest.mark.asyncio
    async def test_exhausted_retries_raises_llm_call_error(self):
        """run_code_review gathers with return_exceptions=True, so exhaustion
        must arrive as an exception, not as a half-built model."""
        with patch("build_pipeline.sdk.run_agent", new_callable=AsyncMock) as run:
            run.side_effect = [
                self._run_result("nope"),
                self._run_result("still nope"),
            ]
            with pytest.raises(LLMCallError):
                await agent_call_structured(
                    "review it", self.Answer, allowed_tools=["Read"],
                )

        assert run.await_count == 2

    @pytest.mark.asyncio
    async def test_untrusted_repo_refused(self, monkeypatch):
        monkeypatch.delenv("BUILDME_TRUST_REPO", raising=False)
        with patch("build_pipeline.sdk.run_agent", new_callable=AsyncMock) as run:
            with pytest.raises(RepoTrustError):
                await agent_call_structured(
                    "review it", self.Answer, allowed_tools=["Read"],
                )
        run.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_calls_and_parse_retries_obey_budget(monkeypatch):
    import asyncio
    from build_pipeline import sdk
    budget_mod.reset()
    budget_mod.configure(max_usd=1)
    monkeypatch.setattr(sdk, "_sdk_semaphore", asyncio.Semaphore(1))
    async def provider(*args, **kwargs):
        await asyncio.sleep(0)
        return SimpleNamespace(text="invalid json", usage=SimpleNamespace(cost_usd=2), turns=1, files_changed=[])
    run = AsyncMock(side_effect=provider)
    monkeypatch.setattr(sdk, "run_agent", run)
    try:
        outcomes = await asyncio.gather(llm_call("one"), llm_call("two"), return_exceptions=True)
        assert run.await_count == 1
        assert sum(isinstance(x, budget_mod.BudgetExceededError) for x in outcomes) == 1
        with pytest.raises(budget_mod.BudgetExceededError):
            await llm_call_structured("retry", BaseModel)
        assert run.await_count == 1
        assert budget_mod.get_budget().cost_usd == 2
    finally:
        budget_mod.reset()

@pytest.mark.asyncio
async def test_real_core_cancellation_without_usage_stays_unknown_on_resume(monkeypatch):
    import asyncio
    import claude_agent_sdk as provider
    from build_pipeline import sdk
    from multiplai_core import agent_runner
    entered = asyncio.Event()
    async def stream(**_kwargs):
        entered.set()
        await asyncio.Event().wait()
        yield  # async generator; cancellation supplies no final usage
    monkeypatch.setattr(provider, 'query', stream)
    monkeypatch.setenv('MULTIPLAI_SDK_DEBUG_LOG', 'off')
    monkeypatch.setenv('MULTIPLAI_AGENT_HEARTBEAT_S', '0')
    monkeypatch.setattr(sdk, 'run_agent', agent_runner.run_agent)
    budget_mod.reset()
    budget_mod.configure(max_usd=1)
    task = asyncio.create_task(sdk.llm_call('offline cancellation', model='anthropic:claude-test', budget_label='planning'))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = budget_mod.get_budget().to_state()
    assert saved['unaccounted_calls'] == ['planning']
    assert saved['cost_usd'] == 0  # unknown is not fabricated into a charge
    budget_mod.reset().load_state(saved)
    budget_mod.configure(max_usd=10)
    with pytest.raises(budget_mod.BudgetExceededError, match='unknown'):
        await sdk.llm_call('must not start another request', model='anthropic:claude-test')
    budget_mod.reset()
