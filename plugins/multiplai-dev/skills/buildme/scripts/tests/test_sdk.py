"""Tests for the SDK adapter — tool policy on text-only LLM calls."""

import pytest
from unittest.mock import AsyncMock, patch

from build_pipeline.sdk import (
    _TEXT_ONLY_DISALLOWED,
    _text_only_disallowed,
    agent_call,
    llm_call,
    llm_call_structured,
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
