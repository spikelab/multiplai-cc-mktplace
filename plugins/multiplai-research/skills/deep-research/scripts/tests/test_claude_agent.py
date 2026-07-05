"""Tests for ClaudeAgentSearchProvider, ClaudeAgentFetcher, FetcherProtocol,
and the prefer_claude_tools / allow_paid_fallback config wiring.

All tests use stubs — no real SDK or API calls.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from research_pipeline.claude_agent_fetcher import (
    ClaudeAgentFetcher,
    FetcherProtocol,
    HttpxFetcher,
)
from research_pipeline.config import ResearchConfig
from research_pipeline.models import FetchErrorType, SearchResult
from research_pipeline.search_router import (
    ClaudeAgentSearchProvider,
    PaidFallbackError,
    ProviderError,
    QuotaExhaustedError,
    RouterConfig,
    SearchRouter,
    build_default_router,
)


# ---------------------------------------------------------------------------
# SDK allowed_tools
# ---------------------------------------------------------------------------


class TestSDKAllowedTools:
    @pytest.mark.asyncio
    async def test_default_allowed_tools_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {}

        async def fake_llm_call(prompt, **kwargs):  # type: ignore
            calls["allowed_tools"] = kwargs.get("allowed_tools")
            return '{"sub_questions": ["q1"]}'

        import research_pipeline.sdk as sdk_mod
        monkeypatch.setattr(sdk_mod, "llm_call", fake_llm_call)

        from research_pipeline.sdk import llm_call_structured
        from research_pipeline.nodes.plan import PlanResponse

        await llm_call_structured("test", PlanResponse)
        # default: no tools
        assert calls.get("allowed_tools") is None or calls["allowed_tools"] == []

    @pytest.mark.asyncio
    async def test_allowed_tools_passed_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {}

        async def fake_llm_call(prompt, **kwargs):  # type: ignore
            calls["allowed_tools"] = kwargs.get("allowed_tools")
            return "result"

        import research_pipeline.sdk as sdk_mod
        monkeypatch.setattr(sdk_mod, "llm_call", fake_llm_call)

        await sdk_mod.llm_call("test", allowed_tools=["WebSearch"])
        assert calls["allowed_tools"] == ["WebSearch"]


# ---------------------------------------------------------------------------
# ClaudeAgentSearchProvider
# ---------------------------------------------------------------------------


class TestClaudeAgentSearchProvider:
    @pytest.mark.asyncio
    async def test_parses_json_results(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_llm_call(prompt, **kwargs):  # type: ignore
            return """```json
[
  {"url": "https://a.example", "title": "A", "snippet": "desc a"},
  {"url": "https://b.example", "title": "B", "snippet": "desc b"}
]
```"""

        import research_pipeline.sdk as sdk_mod
        monkeypatch.setattr(sdk_mod, "llm_call", fake_llm_call)

        provider = ClaudeAgentSearchProvider()
        results = await provider.search("test query", max_results=5)
        assert len(results) == 2
        assert results[0].url == "https://a.example"
        assert results[0].source_api == "claude_agent"

    @pytest.mark.asyncio
    async def test_raises_provider_error_on_sdk_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def failing_llm_call(prompt, **kwargs):  # type: ignore
            raise Exception("SDK unavailable")

        from research_pipeline import sdk
        monkeypatch.setattr(sdk, "llm_call", failing_llm_call)

        provider = ClaudeAgentSearchProvider()
        with pytest.raises(ProviderError):
            await provider.search("test")

    def test_unlimited_quota(self) -> None:
        provider = ClaudeAgentSearchProvider()
        assert provider.monthly_limit is None
        assert provider.one_time_limit is None


# ---------------------------------------------------------------------------
# Router with Claude Agent
# ---------------------------------------------------------------------------


class StubClaudeAgent:
    name = "claude_agent"
    monthly_limit: int | None = None
    one_time_limit: int | None = None

    def __init__(self, fail_count: int = 0):
        self.fail_count = fail_count
        self.calls = 0

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        self.calls += 1
        if self.fail_count > 0:
            self.fail_count -= 1
            raise ProviderError(self.name, "rate limited", transient=True)
        return [
            SearchResult(
                url=f"https://claude/{query}",
                title=f"Claude: {query}",
                snippet="from claude agent",
                source_api=self.name,
            )
        ]


class StubExternal:
    name = "tavily"
    monthly_limit = 1000
    one_time_limit: int | None = None

    def __init__(self):
        self.calls = 0

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        self.calls += 1
        return [
            SearchResult(
                url=f"https://tavily/{query}",
                title=f"Tavily: {query}",
                snippet="from tavily",
                source_api=self.name,
            )
        ]


@pytest.fixture
def claude_first_config(tmp_path: Path) -> RouterConfig:
    return RouterConfig(
        quota_file=tmp_path / "quotas.json",
        keyword_priority=["claude_agent", "tavily"],
        semantic_priority=["claude_agent", "tavily"],
        per_query_timeout=5.0,
        circuit_failure_threshold=2,
        circuit_cooldown_minutes=5,
    )


class TestRouterWithClaudeAgent:
    @pytest.mark.asyncio
    async def test_claude_agent_used_first(
        self, claude_first_config: RouterConfig
    ) -> None:
        claude = StubClaudeAgent()
        tavily = StubExternal()
        router = SearchRouter([claude, tavily], claude_first_config)

        results = await router.search("test")
        assert claude.calls == 1
        assert tavily.calls == 0
        assert results[0].source_api == "claude_agent"

    @pytest.mark.asyncio
    async def test_falls_back_when_claude_fails_and_fallback_allowed(
        self, claude_first_config: RouterConfig
    ) -> None:
        claude = StubClaudeAgent(fail_count=10)
        tavily = StubExternal()
        router = SearchRouter([claude, tavily], claude_first_config, allow_paid_fallback=True)

        # Claude fails, falls back to tavily (allowed because flag is set)
        results = await router.search("test")
        assert results[0].source_api == "tavily"

    @pytest.mark.asyncio
    async def test_circuit_breaker_trips_for_claude_agent(
        self, claude_first_config: RouterConfig
    ) -> None:
        claude = StubClaudeAgent(fail_count=10)
        tavily = StubExternal()
        router = SearchRouter([claude, tavily], claude_first_config, allow_paid_fallback=True)

        # After 2 failures (threshold), claude_agent circuit opens
        await router.search("q1")  # claude fails → tavily (allowed)
        await router.search("q2")  # claude fails → tavily (circuit opens)
        await router.search("q3")  # claude skipped → tavily directly

        assert claude.calls == 2  # stopped after threshold
        assert tavily.calls == 3


class StubExternalExhausted:
    """Tavily-like provider whose free tier is exhausted."""
    name = "tavily_exhausted"
    monthly_limit = 1000
    one_time_limit: int | None = None

    def __init__(self):
        self.calls = 0

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        self.calls += 1
        return [
            SearchResult(
                url=f"https://tavily/{query}",
                title=f"Tavily: {query}",
                snippet="from tavily",
                source_api=self.name,
            )
        ]


class TestPaidFallbackEnforcement:
    @pytest.mark.asyncio
    async def test_free_tier_provider_used_when_claude_fails(
        self, claude_first_config: RouterConfig
    ) -> None:
        """Claude Agent fails → Tavily with free quota remaining should be tried automatically."""
        claude = StubClaudeAgent(fail_count=10)
        tavily = StubExternal()
        router = SearchRouter([claude, tavily], claude_first_config, allow_paid_fallback=False)

        results = await router.search("test")
        assert results[0].source_api == "tavily"
        assert tavily.calls == 1

    @pytest.mark.asyncio
    async def test_exhausted_free_tier_blocked_without_flag(
        self, claude_first_config: RouterConfig
    ) -> None:
        """Providers with exhausted free tier are skipped without --allow-paid-fallback."""
        claude = StubClaudeAgent(fail_count=10)
        tavily = StubExternalExhausted()
        config = RouterConfig(
            quota_file=claude_first_config.quota_file,
            keyword_priority=["claude_agent", "tavily_exhausted"],
            semantic_priority=["claude_agent", "tavily_exhausted"],
            per_query_timeout=5.0,
            circuit_failure_threshold=2,
            circuit_cooldown_minutes=5,
        )
        router = SearchRouter([claude, tavily], config, allow_paid_fallback=False)
        # Pre-exhaust the free tier
        quota = router.quotas.get_or_create(tavily)
        quota.monthly_count = 1000  # at limit
        from datetime import datetime, timezone
        quota.last_reset_month = datetime.now(timezone.utc).strftime("%Y-%m")
        quota.last_reset_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        router.quotas._save()

        with pytest.raises(QuotaExhaustedError):
            await router.search("test")
        assert tavily.calls == 0

    @pytest.mark.asyncio
    async def test_paid_fallback_allowed_when_flag_set(
        self, claude_first_config: RouterConfig
    ) -> None:
        """With allow_paid_fallback=True, falls back normally even past free tier."""
        claude = StubClaudeAgent(fail_count=10)
        tavily = StubExternal()
        router = SearchRouter([claude, tavily], claude_first_config, allow_paid_fallback=True)

        results = await router.search("test")
        assert results[0].source_api == "tavily"

    @pytest.mark.asyncio
    async def test_no_free_provider_skips_gate(
        self, claude_first_config: RouterConfig
    ) -> None:
        """When no free providers exist (--no-claude-tools), paid providers work without gate."""
        config = RouterConfig(
            quota_file=claude_first_config.quota_file,
            keyword_priority=["tavily"],
            semantic_priority=["tavily"],
            per_query_timeout=5.0,
            circuit_failure_threshold=2,
            circuit_cooldown_minutes=5,
        )
        tavily = StubExternal()
        router = SearchRouter([tavily], config, allow_paid_fallback=False)

        results = await router.search("test")
        assert results[0].source_api == "tavily"

    @pytest.mark.asyncio
    async def test_batch_search_falls_back_to_free_tier(
        self, claude_first_config: RouterConfig
    ) -> None:
        """Batch search: Claude fails, Tavily with free quota succeeds."""
        claude = StubClaudeAgent(fail_count=100)
        tavily = StubExternal()
        router = SearchRouter([claude, tavily], claude_first_config, allow_paid_fallback=False)

        results = await router.batch_search(["q1", "q2", "q3"])
        assert len(results) == 3  # all succeeded via Tavily fallback


class TestNoClaudeToolsConfig:
    def test_no_claude_tools_excludes_from_priority(self) -> None:
        config = RouterConfig.default(prefer_claude_tools=False)
        assert "claude_agent" not in config.keyword_priority
        assert "claude_agent" not in config.semantic_priority

    def test_claude_tools_default_includes(self) -> None:
        config = RouterConfig.default(prefer_claude_tools=True)
        assert config.keyword_priority[0] == "claude_agent"
        assert config.semantic_priority[0] == "claude_agent"


# ---------------------------------------------------------------------------
# ClaudeAgentFetcher
# ---------------------------------------------------------------------------


class TestClaudeAgentFetcher:
    @pytest.mark.asyncio
    async def test_successful_fetch_returns_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_llm_call(prompt, **kwargs):  # type: ignore
            return """```json
{
  "content_markdown": "# Article\\nSome content about trafilatura.",
  "findings": [
    {"fact": "trafilatura has F1 0.958", "quote": null, "confidence": "high"}
  ],
  "links": ["https://example.com/more"]
}
```"""

        import research_pipeline.sdk as sdk_mod
        monkeypatch.setattr(sdk_mod, "llm_call", fake_llm_call)

        fetcher = ClaudeAgentFetcher(request_timeout=30)
        result = await fetcher.fetch_url("https://test.example", query="test")

        assert result.success is True
        assert "trafilatura" in (result.content or "")
        assert len(result.extracted_links) == 1
        # Pre-extracted findings attached
        assert hasattr(result, "_pre_extracted_findings")
        findings = result._pre_extracted_findings  # type: ignore
        assert len(findings) == 1
        assert "F1 0.958" in findings[0]["fact"]

    @pytest.mark.asyncio
    async def test_timeout_returns_fetch_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The per-request timeout is now enforced INSIDE llm_call (via
        # sdk.hard_timeout, covered by test_hard_timeout.py). When it fires,
        # llm_call raises LLMCallTimeoutError. The fetcher's responsibility —
        # tested here — is to translate that into a TIMEOUT FetchResult rather
        # than letting it propagate.
        from research_pipeline import sdk

        async def timing_out_llm_call(prompt, *, call_timeout=None, **kwargs):  # type: ignore
            raise sdk.LLMCallTimeoutError(
                f"LLM call exceeded {call_timeout}s timeout"
            )

        monkeypatch.setattr(sdk, "llm_call", timing_out_llm_call)

        fetcher = ClaudeAgentFetcher(request_timeout=0.1)
        result = await fetcher.fetch_url("https://slow.example")

        assert result.success is False
        assert result.error is not None
        assert result.error.error_type == FetchErrorType.TIMEOUT

    @pytest.mark.asyncio
    async def test_batch_isolation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_count = {"n": 0}

        async def mixed_llm_call(prompt, **kwargs):  # type: ignore
            call_count["n"] += 1
            if "fail" in prompt:
                raise Exception("boom")
            return '{"content_markdown": "ok", "findings": [], "links": []}'

        from research_pipeline import sdk
        monkeypatch.setattr(sdk, "llm_call", mixed_llm_call)

        fetcher = ClaudeAgentFetcher(request_timeout=5, batch_timeout=10)
        results = await fetcher.fetch_batch(
            ["https://ok.example", "https://fail.example", "https://ok2.example"]
        )

        assert len(results) == 3
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]
        assert len(successes) == 2
        assert len(failures) == 1


# ---------------------------------------------------------------------------
# FetcherProtocol conformance
# ---------------------------------------------------------------------------


class TestFetcherProtocol:
    def test_claude_agent_fetcher_is_fetcher_protocol(self) -> None:
        """Verify structural subtyping — ClaudeAgentFetcher has the right methods."""
        fetcher = ClaudeAgentFetcher()
        assert hasattr(fetcher, "fetch_url")
        assert hasattr(fetcher, "fetch_batch")
        assert asyncio.iscoroutinefunction(fetcher.fetch_url)
        assert asyncio.iscoroutinefunction(fetcher.fetch_batch)

    def test_httpx_fetcher_is_fetcher_protocol(self) -> None:
        fetcher = HttpxFetcher()
        assert hasattr(fetcher, "fetch_url")
        assert hasattr(fetcher, "fetch_batch")
        assert asyncio.iscoroutinefunction(fetcher.fetch_url)
        assert asyncio.iscoroutinefunction(fetcher.fetch_batch)


# ---------------------------------------------------------------------------
# Config + CLI flags
# ---------------------------------------------------------------------------


class TestConfigFlags:
    def test_default_prefers_claude_tools(self) -> None:
        import argparse

        args = argparse.Namespace(
            query="test",
            output=Path("/tmp"),
            preset="quick",
            auto=True,
            parallel=False,
            agents=None,
            deep=False,
            challenge=False,
            no_challenge=False,
            no_memory=False,
            date="2026-04-06",
            research_type="general",
            personal_context="",
            prior_knowledge="",
            plan_only=False,
            approved_plan=None,
            no_claude_tools=False,
            allow_paid_fallback=False,
        )
        config = ResearchConfig.from_cli_args(args)
        assert config.prefer_claude_tools is True
        assert config.allow_paid_fallback is False

    def test_no_claude_tools_flag(self) -> None:
        import argparse

        args = argparse.Namespace(
            query="test",
            output=Path("/tmp"),
            preset="quick",
            auto=True,
            parallel=False,
            agents=None,
            deep=False,
            challenge=False,
            no_challenge=False,
            no_memory=False,
            date="2026-04-06",
            research_type="general",
            personal_context="",
            prior_knowledge="",
            plan_only=False,
            approved_plan=None,
            no_claude_tools=True,
            allow_paid_fallback=False,
        )
        config = ResearchConfig.from_cli_args(args)
        assert config.prefer_claude_tools is False

    def test_allow_paid_fallback_flag(self) -> None:
        import argparse

        args = argparse.Namespace(
            query="test",
            output=Path("/tmp"),
            preset="quick",
            auto=True,
            parallel=False,
            agents=None,
            deep=False,
            challenge=False,
            no_challenge=False,
            no_memory=False,
            date="2026-04-06",
            research_type="general",
            personal_context="",
            prior_knowledge="",
            plan_only=False,
            approved_plan=None,
            no_claude_tools=False,
            allow_paid_fallback=True,
        )
        config = ResearchConfig.from_cli_args(args)
        assert config.allow_paid_fallback is True
