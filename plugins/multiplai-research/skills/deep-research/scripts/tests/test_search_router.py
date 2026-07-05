"""Tests for SearchRouter — routing priority, quota tracking, circuit breaker, batch.

Uses stub providers instead of real API clients. This lets us exercise the
router logic deterministically without network calls.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from research_pipeline.models import SearchResult
from research_pipeline.search_router import (
    ProviderError,
    QuotaExhaustedError,
    RouterConfig,
    SearchRouter,
)


# ---------------------------------------------------------------------------
# Stub providers
# ---------------------------------------------------------------------------


class StubProvider:
    def __init__(
        self,
        name: str,
        monthly_limit: int | None = None,
        one_time_limit: int | None = None,
        fail_transient: bool = False,
        fail_count: int = 0,
        hang: bool = False,
        results_per_query: int = 3,
    ):
        self.name = name
        self.monthly_limit = monthly_limit
        self.one_time_limit = one_time_limit
        self.fail_transient = fail_transient
        self.fail_count = fail_count
        self.hang = hang
        self.results_per_query = results_per_query
        self.calls = 0

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        self.calls += 1
        if self.hang:
            await asyncio.sleep(60)  # would hang longer than any test timeout
        if self.fail_count > 0:
            self.fail_count -= 1
            raise ProviderError(self.name, "synthetic failure", transient=self.fail_transient)
        return [
            SearchResult(
                url=f"https://{self.name}.example/{query}/{i}",
                title=f"{self.name} result {i} for {query}",
                snippet=f"snippet {i}",
                source_api=self.name,
            )
            for i in range(self.results_per_query)
        ]


@pytest.fixture
def tmp_quota_file(tmp_path: Path) -> Path:
    return tmp_path / "quotas.json"


@pytest.fixture
def fast_config(tmp_quota_file: Path) -> RouterConfig:
    return RouterConfig(
        quota_file=tmp_quota_file,
        keyword_priority=["primary", "secondary", "overflow"],
        semantic_priority=["neural"],
        per_query_timeout=0.5,
        circuit_failure_threshold=2,
        circuit_cooldown_minutes=5,
    )


# ---------------------------------------------------------------------------
# Routing priority
# ---------------------------------------------------------------------------


class TestRouting:
    @pytest.mark.asyncio
    async def test_primary_used_first(self, fast_config: RouterConfig) -> None:
        primary = StubProvider("primary", monthly_limit=100)
        secondary = StubProvider("secondary", monthly_limit=100)
        router = SearchRouter([primary, secondary], fast_config)

        results = await router.search("test")

        assert primary.calls == 1
        assert secondary.calls == 0
        assert len(results) == 3
        assert all(r.source_api == "primary" for r in results)

    @pytest.mark.asyncio
    async def test_falls_back_when_primary_exhausted(
        self, fast_config: RouterConfig
    ) -> None:
        primary = StubProvider("primary", monthly_limit=1)
        secondary = StubProvider("secondary", monthly_limit=100)
        router = SearchRouter([primary, secondary], fast_config)

        await router.search("first")  # uses primary (1/1)
        result = await router.search("second")  # primary full, falls to secondary

        assert primary.calls == 1
        assert secondary.calls == 1
        assert result[0].source_api == "secondary"

    @pytest.mark.asyncio
    async def test_semantic_strategy_uses_semantic_priority(
        self, fast_config: RouterConfig
    ) -> None:
        primary = StubProvider("primary", monthly_limit=100)
        neural = StubProvider("neural", monthly_limit=100)
        router = SearchRouter([primary, neural], fast_config)

        results = await router.search("test", strategy="semantic")

        assert neural.calls == 1
        assert primary.calls == 0
        assert results[0].source_api == "neural"

    @pytest.mark.asyncio
    async def test_all_exhausted_raises(self, fast_config: RouterConfig) -> None:
        primary = StubProvider("primary", monthly_limit=0)  # nothing left
        router = SearchRouter([primary], fast_config)

        with pytest.raises(QuotaExhaustedError):
            await router.search("test")


# ---------------------------------------------------------------------------
# Quota tracking
# ---------------------------------------------------------------------------


class TestQuotaTracking:
    @pytest.mark.asyncio
    async def test_monthly_count_increments(
        self, fast_config: RouterConfig, tmp_quota_file: Path
    ) -> None:
        primary = StubProvider("primary", monthly_limit=100)
        router = SearchRouter([primary], fast_config)

        for _ in range(5):
            await router.search("test")

        # Reload quota file
        from research_pipeline.models import QuotaState
        state = QuotaState.model_validate_json(tmp_quota_file.read_text())
        assert state.quotas["primary"].monthly_count == 5
        assert state.quotas["primary"].total_count == 5

    @pytest.mark.asyncio
    async def test_one_time_limit_enforced(self, fast_config: RouterConfig) -> None:
        overflow = StubProvider("overflow", one_time_limit=2)
        router = SearchRouter([overflow], fast_config)

        await router.search("a")
        await router.search("b")
        with pytest.raises(QuotaExhaustedError):
            await router.search("c")

    @pytest.mark.asyncio
    async def test_monthly_count_resets_on_new_month(
        self, fast_config: RouterConfig, tmp_quota_file: Path
    ) -> None:
        """Simulate month rollover by mutating the persisted last_reset_month."""
        primary = StubProvider("primary", monthly_limit=3)
        router = SearchRouter([primary], fast_config)

        # Exhaust monthly limit
        for _ in range(3):
            await router.search("test")
        with pytest.raises(QuotaExhaustedError):
            await router.search("test")

        # Backdate last_reset_month
        router.quotas._state.quotas["primary"].last_reset_month = "2020-01"
        router.quotas._save()
        router.quotas._state = router.quotas._load()

        # Should work again after reset
        await router.search("test")
        assert primary.calls == 4


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_trips_after_threshold_failures(
        self, fast_config: RouterConfig
    ) -> None:
        primary = StubProvider(
            "primary", monthly_limit=100, fail_transient=True, fail_count=10
        )
        secondary = StubProvider("secondary", monthly_limit=100)
        router = SearchRouter([primary, secondary], fast_config)

        # Threshold = 2. After 2 failures, primary is skipped.
        await router.search("q1")  # primary fails -> secondary succeeds
        await router.search("q2")  # primary fails -> secondary succeeds (circuit opens)
        await router.search("q3")  # primary skipped -> secondary succeeds

        assert primary.calls == 2  # stopped after threshold
        assert secondary.calls == 3

    @pytest.mark.asyncio
    async def test_circuit_resets_after_cooldown(
        self, fast_config: RouterConfig
    ) -> None:
        primary = StubProvider(
            "primary", monthly_limit=100, fail_transient=True, fail_count=2
        )
        secondary = StubProvider("secondary", monthly_limit=100)
        router = SearchRouter([primary, secondary], fast_config)

        await router.search("q1")
        await router.search("q2")  # circuit opens

        # Manually rewind circuit_open_until to the past
        router.quotas._state.quotas["primary"].circuit_open_until = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        router.quotas._save()

        # Now primary's fail_count is 0, so next call should succeed through primary
        await router.search("q3")
        assert primary.calls == 3


# ---------------------------------------------------------------------------
# Batch + timeout
# ---------------------------------------------------------------------------


class TestBatchAndTimeout:
    @pytest.mark.asyncio
    async def test_batch_search_concurrent(self, fast_config: RouterConfig) -> None:
        primary = StubProvider("primary", monthly_limit=1000, results_per_query=2)
        router = SearchRouter([primary], fast_config)

        queries = [f"q{i}" for i in range(5)]
        results = await router.batch_search(queries)

        assert primary.calls == 5
        assert len(results) == 10  # 5 queries × 2 results

    @pytest.mark.asyncio
    async def test_batch_deduplicates_urls(self, fast_config: RouterConfig) -> None:
        """Same URLs across queries should be deduplicated."""
        class SameUrlProvider:
            name = "primary"  # must match fast_config.keyword_priority
            monthly_limit = 1000
            one_time_limit: int | None = None

            async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
                return [
                    SearchResult(url="https://same.example/a", title="A", snippet="", source_api=self.name),
                    SearchResult(url="https://same.example/b", title="B", snippet="", source_api=self.name),
                ]

        router = SearchRouter([SameUrlProvider()], fast_config)  # type: ignore[list-item]
        results = await router.batch_search(["q1", "q2", "q3"])
        assert len(results) == 2  # deduplicated

    @pytest.mark.asyncio
    async def test_per_query_timeout_kills_hung_provider(
        self, fast_config: RouterConfig
    ) -> None:
        hanging = StubProvider("primary", monthly_limit=100, hang=True)
        fallback = StubProvider("secondary", monthly_limit=100)
        router = SearchRouter([hanging, fallback], fast_config)

        # With per_query_timeout=0.5, the hanging provider is killed and we fall back
        result = await router.search("test")

        assert result[0].source_api == "secondary"

    @pytest.mark.asyncio
    async def test_batch_partial_failure_isolation(
        self, fast_config: RouterConfig
    ) -> None:
        # Use a single provider that fails on specific queries
        class SelectivelyFailingProvider:
            name = "primary"
            monthly_limit = 1000
            one_time_limit: int | None = None

            def __init__(self):
                self.calls = 0

            async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
                self.calls += 1
                if "fail" in query:
                    raise ProviderError(self.name, "boom", transient=False)
                return [
                    SearchResult(url=f"https://example/{query}", title=query, snippet="", source_api=self.name)
                ]

        provider = SelectivelyFailingProvider()
        router = SearchRouter([provider], fast_config)  # type: ignore[list-item]

        results = await router.batch_search(["ok1", "fail1", "ok2", "fail2", "ok3"])

        # 3 successful queries, each with 1 result
        assert len(results) == 3
