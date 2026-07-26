"""Tests for the SDK adapter — tool deny-lists, the repo trust gate, and
failure-path budget accounting."""

from dataclasses import dataclass

import pytest
from unittest.mock import AsyncMock, patch

from build_pipeline import budget as budget_mod
from build_pipeline.sdk import (
    _TOOL_UNIVERSE,
    _deny_list,
    RepoTrustError,
    agent_call,
    llm_call,
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
