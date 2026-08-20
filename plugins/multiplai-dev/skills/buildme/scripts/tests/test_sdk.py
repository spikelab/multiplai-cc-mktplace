"""Tests for the SDK adapter — tool deny-lists, the repo trust gate, and
failure-path budget accounting."""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from unittest.mock import AsyncMock, patch

from build_pipeline import budget as budget_mod
from build_pipeline.sdk import (
    _TOOL_UNIVERSE,
    _deny_list,
    RepoTrustError,
    agent_call,
    agent_call_structured,
    llm_call,
    llm_call_structured,
    LLMCallError,
    LLMCallTimeoutError,
)
from multiplai_core.agent_runner import (
    MAX_PROMPT_BYTES,
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
    """Every call must actively deny the tools it does not allow: run_agent's
    allow-list is advisory under bypassPermissions, so only disallowed_tools
    actually removes a tool."""

    def test_no_tools_denies_everything(self):
        assert _deny_list("short prompt", None) == _TOOL_UNIVERSE

    def test_universe_names_the_capabilities_that_carry_the_risk(self):
        """Pin names, not the list against itself.

        `_deny_list(...) == _TOOL_UNIVERSE` passes no matter what the universe
        forgot — which is how Artifact, SendMessage, REPL and the Task/Cron
        family sat outside it while every deny-list test stayed green.
        """
        for tool in (
            "Bash", "REPL",                             # execution
            "WebFetch", "Artifact", "SendMessage",      # egress
            "TaskCreate", "CronCreate", "Workflow",     # deferred execution
            "Read", "Write", "Edit",                    # local files
            "ToolSearch", "Skill",                      # loading tools back in
        ):
            assert tool in _TOOL_UNIVERSE, tool

    def test_bash_is_denied(self):
        assert "Bash" in _deny_list("short prompt", None)

    def test_allowlist_gets_the_complement(self):
        """The explainer's allow-list must still deny Bash/Write/Edit — a
        web-ingesting agent is one prompt injection away from using them."""
        denied = _deny_list("p", ["WebSearch", "WebFetch", "Read", "Glob", "Grep"])
        for tool in ("Bash", "Write", "Edit", "NotebookEdit", "Task"):
            assert tool in denied
        for tool in ("WebSearch", "WebFetch", "Read", "Glob", "Grep"):
            assert tool not in denied

    def test_prototype_allowlist_keeps_write_denies_bash(self):
        """Bash writes would bypass the files_changed-based write-boundary
        check (only Write/Edit ToolUseBlocks are collected)."""
        denied = _deny_list("p", ["Read", "Write", "Glob", "Grep"])
        assert "Bash" in denied
        assert "Write" not in denied

    def test_oversized_prompt_keeps_read_available(self):
        """run_agent spills an oversized prompt to a temp file and directs the
        agent to Read it — denying Read would break that fallback."""
        big = "x" * (MAX_PROMPT_BYTES + 1)
        denied = _deny_list(big, None)
        assert "Read" not in denied
        assert "Bash" in denied


class TestLlmCallToolPolicy:
    @pytest.mark.asyncio
    async def test_no_tools_requested_passes_deny_list(self):
        with patch("build_pipeline.sdk.run_agent", _mock_run_agent()) as run:
            await llm_call("hello")

        denied = run.call_args.kwargs["disallowed_tools"]
        assert "Bash" in denied

    @pytest.mark.asyncio
    async def test_text_only_needs_no_trust(self, monkeypatch):
        """Pure text calls have no tools to abuse — they must keep working
        without --trust-repo."""
        monkeypatch.delenv("BUILDME_TRUST_REPO", raising=False)
        with patch("build_pipeline.sdk.run_agent", _mock_run_agent()):
            assert await llm_call("hello") == "answer"

    @pytest.mark.asyncio
    async def test_explicit_tools_get_complement_deny_list(self, monkeypatch):
        """Callers that genuinely want tools keep them — but everything else
        is actively denied, Bash first."""
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        with patch("build_pipeline.sdk.run_agent", _mock_run_agent()) as run:
            await llm_call("hello", allowed_tools=["Read", "Grep"])

        assert run.call_args.kwargs["allowed_tools"] == ["Read", "Grep"]
        denied = run.call_args.kwargs["disallowed_tools"]
        assert "Bash" in denied
        assert "Write" in denied
        assert "Read" not in denied

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
    async def test_deny_list_reaches_run_agent(self, monkeypatch):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        with patch("build_pipeline.sdk.run_agent", _mock_run_agent()) as run:
            result = await agent_call(
                "build it", allowed_tools=["Read", "Write", "Glob", "Grep"],
            )

        assert result.success
        denied = run.call_args.kwargs["disallowed_tools"]
        assert "Bash" in denied
        assert "Write" not in denied

    @pytest.mark.asyncio
    async def test_untrusted_repo_refused(self, monkeypatch):
        monkeypatch.delenv("BUILDME_TRUST_REPO", raising=False)
        with patch("build_pipeline.sdk.run_agent", _mock_run_agent()) as run:
            with pytest.raises(RepoTrustError):
                await agent_call("build it", allowed_tools=["Read"])
        run.assert_not_awaited()


class TestExplainerPath:
    """run_explainer spawns a web-ingesting, tool-bearing agent — it must
    inherit both the complement deny-list and the trust gate via llm_call."""

    def _dep_and_config(self):
        class Dep:
            name = "somelib"
            mentioned_in = ["design.md § Decisions"]
            evidence = "not declared in pyproject.toml"

        class Config:
            model = None
            project_description = "test project"

        return Dep(), Config()

    @pytest.mark.asyncio
    async def test_deny_list_includes_bash_and_write(self, monkeypatch):
        from build_pipeline.llm_steps.spec_steps import run_explainer

        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        dep, config = self._dep_and_config()
        with patch("build_pipeline.sdk.run_agent", _mock_run_agent()) as run:
            await run_explainer(dep, config, usage_context="parses TOML configs")

        assert "WebSearch" in run.call_args.kwargs["allowed_tools"]
        denied = run.call_args.kwargs["disallowed_tools"]
        for tool in ("Bash", "Write", "Edit"):
            assert tool in denied

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
        denied = run.call_args.kwargs["disallowed_tools"]
        assert "Bash" in denied
        assert "Write" in denied
        assert "Read" not in denied

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
