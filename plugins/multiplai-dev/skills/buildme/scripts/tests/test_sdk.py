"""Tests for the SDK adapter — tool policy on text-only LLM calls."""

import pytest
from unittest.mock import AsyncMock, patch

from build_pipeline.sdk import (
    _TEXT_ONLY_DISALLOWED,
    _text_only_disallowed,
    llm_call,
)
from multiplai_core.agent_runner import MAX_PROMPT_BYTES


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
