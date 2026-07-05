"""Tests for the SDK wrapper — JSON extraction, structured output retry, timeouts."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from research_pipeline.sdk import (
    LLMCallTimeoutError,
    LLMCallUsage,
    _record_usage,
    extract_json,
    get_accumulated_usage,
    llm_call,
    llm_call_structured,
    reset_accumulated_usage,
)


class Thing(BaseModel):
    name: str
    count: int


class TestExtractJson:
    def test_plain_object(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_code_block(self) -> None:
        text = 'Here is the result:\n```json\n{"name": "foo"}\n```\nDone.'
        assert extract_json(text) == {"name": "foo"}

    def test_fenced_without_language(self) -> None:
        text = '```\n{"x": 2}\n```'
        assert extract_json(text) == {"x": 2}

    def test_object_with_surrounding_prose(self) -> None:
        text = 'The answer is: {"result": 42} end of response.'
        assert extract_json(text) == {"result": 42}

    def test_nested_objects(self) -> None:
        text = '{"outer": {"inner": {"deep": [1, 2, 3]}}}'
        assert extract_json(text) == {"outer": {"inner": {"deep": [1, 2, 3]}}}

    def test_array_at_top_level(self) -> None:
        text = "[1, 2, 3]"
        assert extract_json(text) == [1, 2, 3]

    def test_string_with_braces_inside_ignored(self) -> None:
        # Braces inside strings shouldn't confuse the bracket balancer
        text = '{"msg": "this has } inside"}'
        assert extract_json(text) == {"msg": "this has } inside"}

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            extract_json("")

    def test_no_json_raises(self) -> None:
        with pytest.raises(ValueError):
            extract_json("just plain text, no json here")


class TestLLMCallStructured:
    @pytest.mark.asyncio
    async def test_parses_valid_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_llm_call(prompt, **kwargs):  # type: ignore[no-untyped-def]
            return '```json\n{"name": "widget", "count": 5}\n```'

        from research_pipeline import sdk
        monkeypatch.setattr(sdk, "llm_call", fake_llm_call)

        result = await llm_call_structured("ignored", Thing)
        assert result.name == "widget"
        assert result.count == 5

    @pytest.mark.asyncio
    async def test_retries_on_validation_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        async def fake_llm_call(prompt, **kwargs):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 1:
                return '{"name": "widget"}'  # missing count
            return '{"name": "widget", "count": 3}'

        from research_pipeline import sdk
        monkeypatch.setattr(sdk, "llm_call", fake_llm_call)

        result = await llm_call_structured("ignored", Thing)
        assert result.count == 3
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_fails_after_retries_exhausted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_llm_call(prompt, **kwargs):  # type: ignore[no-untyped-def]
            return "not even json"

        from research_pipeline import sdk
        from research_pipeline.sdk import LLMCallError
        monkeypatch.setattr(sdk, "llm_call", fake_llm_call)

        with pytest.raises(LLMCallError):
            await llm_call_structured("ignored", Thing)

    @pytest.mark.asyncio
    async def test_timeout_propagates_through_structured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LLMCallTimeoutError from llm_call propagates through llm_call_structured."""

        async def fake_llm_call(prompt, **kwargs):  # type: ignore[no-untyped-def]
            raise LLMCallTimeoutError("timed out after 1s")

        from research_pipeline import sdk
        monkeypatch.setattr(sdk, "llm_call", fake_llm_call)

        with pytest.raises(LLMCallTimeoutError):
            await llm_call_structured("ignored", Thing)


class TestLLMCallTimeout:
    """Tests for the asyncio.wait_for timeout wrapper in llm_call()."""

    @pytest.mark.asyncio
    async def test_timeout_raises_llm_call_timeout_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hanging SDK call should raise LLMCallTimeoutError after call_timeout."""
        import sys
        import types

        fake_claude_sdk = types.ModuleType("claude_agent_sdk")

        async def hanging_query(prompt, options):  # type: ignore[no-untyped-def]
            await asyncio.sleep(9999)  # never returns
            yield  # make it an async generator (unreachable)  # type: ignore[misc]

        fake_claude_sdk.query = hanging_query  # type: ignore[attr-defined]

        # ClaudeAgentOptions needs to be a callable that returns something
        class FakeOptions:
            def __init__(self, **kwargs: object) -> None:
                pass

        class FakeAssistantMessage:
            content: list = []

        class FakeTextBlock:
            text = ""

        fake_claude_sdk.ClaudeAgentOptions = FakeOptions  # type: ignore[attr-defined]
        fake_claude_sdk.AssistantMessage = FakeAssistantMessage  # type: ignore[attr-defined]
        fake_claude_sdk.TextBlock = FakeTextBlock  # type: ignore[attr-defined]

        monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_claude_sdk)

        with pytest.raises(LLMCallTimeoutError, match="exceeded"):
            await llm_call("test prompt", call_timeout=0.05)  # 50ms timeout

    @pytest.mark.asyncio
    async def test_default_timeout_is_ten_minutes(self) -> None:
        """Verify the default timeout constant is 600s (10 minutes)."""
        from research_pipeline.sdk import DEFAULT_LLM_CALL_TIMEOUT_S
        assert DEFAULT_LLM_CALL_TIMEOUT_S == 600.0

    @pytest.mark.asyncio
    async def test_call_timeout_kwarg_accepted_by_structured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """llm_call_structured accepts and forwards call_timeout."""
        received_timeout: list[float] = []

        async def fake_llm_call(prompt, **kwargs):  # type: ignore[no-untyped-def]
            received_timeout.append(kwargs.get("call_timeout", -1.0))
            return '{"name": "x", "count": 1}'

        from research_pipeline import sdk
        monkeypatch.setattr(sdk, "llm_call", fake_llm_call)

        await llm_call_structured("ignored", Thing, call_timeout=120.0)
        assert received_timeout[0] == 120.0


class TestEffortParameter:
    """Tests that the effort parameter is correctly threaded through to ClaudeAgentOptions."""

    @pytest.mark.asyncio
    async def test_effort_not_passed_when_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When effort=None (default), the 'effort' key must NOT appear in opts_kwargs."""
        import sys
        import types

        fake_claude_sdk = types.ModuleType("claude_agent_sdk")

        captured_kwargs: dict = {}

        class FakeOptions:
            def __init__(self, **kwargs: object) -> None:
                captured_kwargs.update(kwargs)

        async def fake_query(prompt, options):  # type: ignore[no-untyped-def]
            # Yield one AssistantMessage with a TextBlock
            msg = FakeAssistantMessage()
            msg.content = [FakeTextBlock()]
            yield msg

        class FakeAssistantMessage:
            content: list = []

        class FakeTextBlock:
            text = "hello"

        fake_claude_sdk.ClaudeAgentOptions = FakeOptions  # type: ignore[attr-defined]
        fake_claude_sdk.query = fake_query  # type: ignore[attr-defined]
        fake_claude_sdk.AssistantMessage = FakeAssistantMessage  # type: ignore[attr-defined]
        fake_claude_sdk.TextBlock = FakeTextBlock  # type: ignore[attr-defined]

        monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_claude_sdk)

        await llm_call("test prompt")  # effort defaults to None
        assert "effort" not in captured_kwargs

    @pytest.mark.asyncio
    async def test_effort_passed_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When effort='low', the 'effort' key must appear in ClaudeAgentOptions kwargs."""
        import sys
        import types

        fake_claude_sdk = types.ModuleType("claude_agent_sdk")

        captured_kwargs: dict = {}

        class FakeOptions:
            def __init__(self, **kwargs: object) -> None:
                captured_kwargs.update(kwargs)

        async def fake_query(prompt, options):  # type: ignore[no-untyped-def]
            msg = FakeAssistantMessage()
            msg.content = [FakeTextBlock()]
            yield msg

        class FakeAssistantMessage:
            content: list = []

        class FakeTextBlock:
            text = "hello"

        fake_claude_sdk.ClaudeAgentOptions = FakeOptions  # type: ignore[attr-defined]
        fake_claude_sdk.query = fake_query  # type: ignore[attr-defined]
        fake_claude_sdk.AssistantMessage = FakeAssistantMessage  # type: ignore[attr-defined]
        fake_claude_sdk.TextBlock = FakeTextBlock  # type: ignore[attr-defined]

        monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_claude_sdk)

        await llm_call("test prompt", effort="low")
        assert captured_kwargs["effort"] == "low"


class TestUsageAccumulator:
    """Tests for the LLMCallUsage tracking functions."""

    def test_reset_zeroes_all_fields(self) -> None:
        """reset_accumulated_usage() produces zeroed counters."""
        # Dirty the accumulator first
        _record_usage(LLMCallUsage(input_tokens=100, num_calls=1))
        reset_accumulated_usage()
        usage = get_accumulated_usage()
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.cache_creation_tokens == 0
        assert usage.cache_read_tokens == 0
        assert usage.cost_usd == 0.0
        assert usage.num_calls == 0

    def test_record_usage_single(self) -> None:
        """A single _record_usage call is reflected in get_accumulated_usage."""
        reset_accumulated_usage()
        _record_usage(
            LLMCallUsage(
                input_tokens=500,
                output_tokens=200,
                cache_creation_tokens=10,
                cache_read_tokens=20,
                cost_usd=0.05,
                num_calls=1,
            )
        )
        usage = get_accumulated_usage()
        assert usage.input_tokens == 500
        assert usage.output_tokens == 200
        assert usage.cache_creation_tokens == 10
        assert usage.cache_read_tokens == 20
        assert usage.cost_usd == pytest.approx(0.05)
        assert usage.num_calls == 1

    def test_record_usage_accumulates(self) -> None:
        """Multiple _record_usage calls sum all fields."""
        reset_accumulated_usage()
        _record_usage(
            LLMCallUsage(
                input_tokens=100,
                output_tokens=50,
                cache_creation_tokens=5,
                cache_read_tokens=10,
                cost_usd=0.01,
                num_calls=1,
            )
        )
        _record_usage(
            LLMCallUsage(
                input_tokens=200,
                output_tokens=80,
                cache_creation_tokens=3,
                cache_read_tokens=7,
                cost_usd=0.02,
                num_calls=1,
            )
        )
        usage = get_accumulated_usage()
        assert usage.input_tokens == 300
        assert usage.output_tokens == 130
        assert usage.cache_creation_tokens == 8
        assert usage.cache_read_tokens == 17
        assert usage.cost_usd == pytest.approx(0.03)
        assert usage.num_calls == 2
