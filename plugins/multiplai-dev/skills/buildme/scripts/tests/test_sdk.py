"""Tests for the SDK adapter — tool policy and failure-path budget accounting."""

from dataclasses import dataclass

import pytest
from unittest.mock import AsyncMock, patch

from build_pipeline import budget as budget_mod
from build_pipeline.sdk import (
    _TEXT_ONLY_DISALLOWED,
    _text_only_disallowed,
    llm_call,
    LLMCallError,
    LLMCallTimeoutError,
)
from multiplai_core.agent_runner import (
    MAX_PROMPT_BYTES,
    AgentRunError,
    AgentRunTimeout,
)


class TestTextOnlyDisallowed:
    """A no-tools call must actively deny tools: run_agent's allow-list is
    advisory under bypassPermissions, so a model that reaches for Bash burns
    its single turn and the call dies with "Reached maximum number of turns"."""

    def test_small_prompt_denies_everything(self):
        assert _text_only_disallowed("short prompt") == _TEXT_ONLY_DISALLOWED

    def test_bash_is_denied(self):
        assert "Bash" in _text_only_disallowed("short prompt")

    def test_oversized_prompt_keeps_read_available(self):
        """run_agent spills an oversized prompt to a temp file and directs the
        agent to Read it — denying Read would break that fallback."""
        big = "x" * (MAX_PROMPT_BYTES + 1)
        denied = _text_only_disallowed(big)
        assert "Read" not in denied
        assert "Bash" in denied


class TestLlmCallToolPolicy:
    @pytest.mark.asyncio
    async def test_no_tools_requested_passes_deny_list(self):
        result = AsyncMock()
        result.text = "answer"
        with patch("build_pipeline.sdk.run_agent", new_callable=AsyncMock) as run:
            run.return_value.text = "answer"
            await llm_call("hello")

        denied = run.call_args.kwargs["disallowed_tools"]
        assert "Bash" in denied

    @pytest.mark.asyncio
    async def test_explicit_tools_are_not_overridden(self):
        """Callers that genuinely want tools (e.g. codebase analysis) keep them."""
        with patch("build_pipeline.sdk.run_agent", new_callable=AsyncMock) as run:
            run.return_value.text = "answer"
            await llm_call("hello", allowed_tools=["Read", "Grep"])

        assert run.call_args.kwargs["disallowed_tools"] is None
        assert run.call_args.kwargs["allowed_tools"] == ["Read", "Grep"]


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
