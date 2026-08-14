"""Tests for the SDK wrapper — JSON extraction, structured output retry, timeouts."""

from __future__ import annotations

import asyncio
import logging

import pytest
from pydantic import BaseModel

from research_pipeline import sdk as sdk_module
from research_pipeline.sdk import (
    MAX_PROMPT_BYTES,
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


class TestMaxAttempts:
    """Retry policy: SDK-level retry by default, disabled where callers
    already have their own failover (router chain, per-source fetch handling)."""

    def _patch_run_agent(self, monkeypatch: pytest.MonkeyPatch, text: str = "[]") -> dict:
        from types import SimpleNamespace

        from research_pipeline import sdk

        captured: dict = {}

        async def fake_run_agent(prompt, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                text=text,
                usage=SimpleNamespace(
                    input_tokens=1, output_tokens=1,
                    cache_creation_tokens=0, cache_read_tokens=0, cost_usd=0.0,
                ),
            )

        monkeypatch.setattr(sdk, "run_agent", fake_run_agent)
        return captured

    @pytest.mark.asyncio
    async def test_llm_call_defaults_to_two_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._patch_run_agent(monkeypatch)
        await llm_call("p")
        assert captured["max_attempts"] == 2

    @pytest.mark.asyncio
    async def test_llm_call_max_attempts_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._patch_run_agent(monkeypatch)
        await llm_call("p", max_attempts=1)
        assert captured["max_attempts"] == 1

    @pytest.mark.asyncio
    async def test_fetcher_disables_sdk_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ClaudeAgentFetcher has per-source failover — one attempt only."""
        from research_pipeline.claude_agent_fetcher import ClaudeAgentFetcher

        captured = self._patch_run_agent(monkeypatch, text='{"content_markdown": "c"}')
        result = await ClaudeAgentFetcher().fetch_url("https://a.example")
        assert result.success
        assert captured["max_attempts"] == 1

    @pytest.mark.asyncio
    async def test_search_provider_disables_sdk_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The router fails over to the next provider — one attempt only."""
        from research_pipeline.search_router import ClaudeAgentSearchProvider

        captured = self._patch_run_agent(monkeypatch, text="[]")
        results = await ClaudeAgentSearchProvider().search("q")
        assert results == []
        assert captured["max_attempts"] == 1


class TestToolDenyList:
    """Every prompt here carries fetched web text, so an allow-list alone is
    not a boundary — under bypassPermissions only disallowed_tools removes a
    tool. These assert the deny-list actually reaches run_agent."""

    def _patch_run_agent(self, monkeypatch: pytest.MonkeyPatch, text: str = "[]") -> dict:
        return TestMaxAttempts()._patch_run_agent(monkeypatch, text=text)

    @pytest.mark.asyncio
    async def test_text_only_call_denies_the_whole_universe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._patch_run_agent(monkeypatch)
        await llm_call("p")
        assert captured["disallowed_tools"] == sdk_module._TOOL_UNIVERSE

    def test_universe_names_the_capabilities_that_carry_the_risk(self) -> None:
        """Pin names, not the list against itself.

        `disallowed_tools == _TOOL_UNIVERSE` passes no matter what the universe
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
            assert tool in sdk_module._TOOL_UNIVERSE, tool

    @pytest.mark.asyncio
    async def test_allow_list_denies_the_complement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._patch_run_agent(monkeypatch)
        await llm_call("p", allowed_tools=["WebFetch"])
        denied = captured["disallowed_tools"]
        assert "WebFetch" not in denied
        for tool in ("Bash", "Read", "Write", "WebSearch"):
            assert tool in denied

    @pytest.mark.asyncio
    async def test_structured_call_carries_the_deny_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """llm_call_structured routes through llm_call — pin it, so a future
        refactor that gives it its own run_agent call cannot lose the list."""
        self._patch_run_agent(monkeypatch, text='{"content_markdown": "c"}')
        captured = self._patch_run_agent(monkeypatch, text='{"value": 1}')

        class _Schema(BaseModel):
            value: int

        await llm_call_structured("p", _Schema)
        assert "Bash" in captured["disallowed_tools"]

    @pytest.mark.asyncio
    async def test_fetcher_opens_only_webfetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from research_pipeline.claude_agent_fetcher import ClaudeAgentFetcher

        captured = self._patch_run_agent(monkeypatch, text='{"content_markdown": "c"}')
        await ClaudeAgentFetcher().fetch_url("https://a.example")
        assert captured["allowed_tools"] == ["WebFetch"]
        assert "Bash" in captured["disallowed_tools"]
        assert "WebFetch" not in captured["disallowed_tools"]


    @pytest.mark.asyncio
    async def test_search_provider_opens_only_websearch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from research_pipeline.search_router import ClaudeAgentSearchProvider

        captured = self._patch_run_agent(monkeypatch, text="[]")
        await ClaudeAgentSearchProvider().search("q")
        assert captured["allowed_tools"] == ["WebSearch"]
        assert "Bash" in captured["disallowed_tools"]
        assert "WebSearch" not in captured["disallowed_tools"]

    @pytest.mark.asyncio
    async def test_oversized_prompt_keeps_read_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_agent's E2BIG fallback writes the prompt to a file and tells the
        agent to Read it — denying Read would break every large synthesis."""
        captured = self._patch_run_agent(monkeypatch)
        await llm_call("x" * (MAX_PROMPT_BYTES + 1))
        assert "Read" not in captured["disallowed_tools"]
        assert "Bash" in captured["disallowed_tools"]


class TestOversizedPromptCarveOut:
    """`_deny_list` re-opens Read above the E2BIG threshold, because run_agent
    then hands the prompt over as a temp file. That is a real widening on the
    one code path whose input is entirely attacker-authored, so what it costs
    is pinned here rather than left to the comment."""

    # Comfortably over MAX_PROMPT_BYTES without depending on its exact value.
    _BIG = "x" * (sdk_module.MAX_PROMPT_BYTES + 1024)

    def test_read_is_denied_on_an_ordinary_prompt(self) -> None:
        assert "Read" in sdk_module._deny_list("short prompt", None)

    def test_threshold_is_measured_in_bytes_not_characters(self) -> None:
        """A prompt of multi-byte characters can be under the character count
        and over the byte limit — run_agent's own check is on bytes, so this
        one must be too or the two disagree about which mode is in force."""
        just_under = "€" * ((sdk_module.MAX_PROMPT_BYTES // 3) - 16)
        assert len(just_under) < sdk_module.MAX_PROMPT_BYTES
        assert "Read" in sdk_module._deny_list(just_under, None)

    def test_the_carve_out_opens_read_and_nothing_else(self) -> None:
        ordinary = set(sdk_module._deny_list("short prompt", None))
        oversized = set(sdk_module._deny_list(self._BIG, None))
        assert ordinary - oversized == {"Read"}

    def test_every_egress_tool_stays_denied_in_the_carve_out(self) -> None:
        """The half that matters. Read alone is disclosure to a subprocess that
        has no way to speak; it becomes exfiltration only if something can also
        carry the bytes out. Nothing that can may be opened here."""
        denied = set(sdk_module._deny_list(self._BIG, None))
        for tool in (
            "Bash", "BashOutput", "REPL", "Task", "Agent",       # execution
            "Write", "Edit", "MultiEdit", "NotebookEdit",        # local writes
            "WebFetch", "WebSearch",                             # network
            "Artifact", "SendMessage", "PushNotification",       # publish / message
            "RemoteTrigger", "SendFeedback",
            "TaskCreate", "Workflow", "CronCreate",              # deferred execution
            "ToolSearch", "Skill", "SlashCommand",               # loading tools back in
        ):
            assert tool in denied, tool

    def test_the_carve_out_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A silent widening is one nobody audits."""
        with caplog.at_level(logging.WARNING, logger="research_pipeline.sdk"):
            sdk_module._deny_list(self._BIG, None)
        assert any("Read left open" in r.getMessage() for r in caplog.records)


class TestToolUniverseDrift:
    """`_TOOL_UNIVERSE` is deliberately a copy of core's, not an import — it has
    to hold on an installed plugin that resolved a core release predating core's
    fail-closed default (see the comment on the constant). A copy drifts, and a
    tool missing from it is a tool left open, so pin the two together: when core
    learns about a new tool, this list must learn about it in the same bump."""

    def test_covers_everything_core_knows_about(self) -> None:
        core = __import__(
            "multiplai_core.agent_runner", fromlist=["TOOL_UNIVERSE"]
        ).TOOL_UNIVERSE
        missing = sorted(set(core) - set(sdk_module._TOOL_UNIVERSE))
        assert not missing, (
            f"multiplai-core knows about tools this deny-list does not: {missing}. "
            "Add them to _TOOL_UNIVERSE — each one is a tool left pre-approved "
            "under bypassPermissions."
        )

    def test_has_no_duplicates(self) -> None:
        dupes = {t for t in sdk_module._TOOL_UNIVERSE
                 if sdk_module._TOOL_UNIVERSE.count(t) > 1}
        assert not dupes, dupes


class TestThinkingParameter:
    """thinking= reaches run_agent only when set, and is dropped (with one
    warning) when the resolved core's run_agent cannot accept it."""

    def _patch_run_agent(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        from types import SimpleNamespace

        captured: dict = {}

        async def fake_run_agent(prompt, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                text="[]",
                usage=SimpleNamespace(
                    input_tokens=1, output_tokens=1,
                    cache_creation_tokens=0, cache_read_tokens=0, cost_usd=0.0,
                ),
            )

        monkeypatch.setattr(sdk_module, "run_agent", fake_run_agent)
        # The support probe caches its answer against whatever run_agent it
        # first saw — reset so each test probes its own fake.
        monkeypatch.setattr(sdk_module, "_core_thinking_support", None)
        return captured

    @pytest.mark.asyncio
    async def test_not_forwarded_when_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """thinking=None (the default) must not put the key in the kwargs at
        all — an old core must never even see it."""
        captured = self._patch_run_agent(monkeypatch)
        await llm_call("p")
        assert "thinking" not in captured

    @pytest.mark.asyncio
    async def test_forwarded_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._patch_run_agent(monkeypatch)
        await llm_call("p", thinking={"type": "disabled"})
        assert captured["thinking"] == {"type": "disabled"}

    @pytest.mark.asyncio
    async def test_structured_forwards_to_llm_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}

        async def fake_llm_call(prompt, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return '{"name": "x", "count": 1}'

        monkeypatch.setattr(sdk_module, "llm_call", fake_llm_call)
        await llm_call_structured("p", Thing, thinking={"type": "disabled"})
        assert captured["thinking"] == {"type": "disabled"}

    @pytest.mark.asyncio
    async def test_dropped_on_old_core_with_one_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An old core (no thinking=, no **kwargs) gets the kwarg dropped —
        a single warning naming the fix, never a TypeError."""
        from types import SimpleNamespace

        captured: dict = {}

        async def old_core_run_agent(
            prompt, *, system_prompt=None, allowed_tools=None,
            disallowed_tools=None, max_turns=1, max_attempts=2, model=None,
            effort=None, timeout_s=600.0, label="", component="",
        ):
            captured.update(
                system_prompt=system_prompt, max_turns=max_turns, label=label
            )
            return SimpleNamespace(
                text="ok",
                usage=SimpleNamespace(
                    input_tokens=1, output_tokens=1,
                    cache_creation_tokens=0, cache_read_tokens=0, cost_usd=0.0,
                ),
            )

        monkeypatch.setattr(sdk_module, "run_agent", old_core_run_agent)
        monkeypatch.setattr(sdk_module, "_core_thinking_support", None)

        with caplog.at_level(logging.WARNING, logger="research_pipeline.sdk"):
            assert await llm_call("p", thinking={"type": "disabled"}) == "ok"
            assert await llm_call("p", thinking={"type": "disabled"}) == "ok"

        warnings = [
            r for r in caplog.records
            if "uv lock --upgrade-package multiplai-core" in r.getMessage()
        ]
        assert len(warnings) == 1  # probe is cached: warn once, not per call
