"""SearchRouter — multi-API search with quota tracking, failover, circuit breaker.

Routes queries across Tavily, Exa, Serper, and You.com. Tracks daily/monthly
quotas per API, skips exhausted APIs, trips circuit breakers on consecutive
failures, and provides batch search with strict timeouts.

None of the providers are installed as hard deps of tests — the router can be
used with any subset of providers. Missing provider packages are detected at
init time and gracefully skipped.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from .models import APIQuota, QuotaState, SearchResult

log = logging.getLogger(__name__)

# Providers with genuinely unlimited free usage (flat-rate subscription).
UNLIMITED_FREE_PROVIDERS = frozenset({"claude_agent"})

# Providers that must NEVER be used beyond their free tier, even with
# --allow-paid-fallback. Their paid pricing ($5/1K) is 5x more expensive
# than Serper ($1/1K), so we cap them at free quota and let Serper handle
# paid overflow.
FREE_TIER_ONLY_PROVIDERS = frozenset({"tavily", "exa", "brave"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SearchRouterError(Exception):
    """Base error for SearchRouter."""


class QuotaExhaustedError(SearchRouterError):
    """Raised when all configured APIs have exhausted their quotas."""

    def __init__(self, details: dict[str, str]):
        self.details = details
        super().__init__(f"All search APIs exhausted: {details}")


class ProviderError(SearchRouterError):
    """Raised when a provider call fails (transient or permanent)."""

    def __init__(self, provider: str, message: str, transient: bool = True):
        self.provider = provider
        self.transient = transient
        super().__init__(f"{provider}: {message}")


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


class SearchProvider(Protocol):
    """Protocol every search API provider must satisfy."""

    name: str
    monthly_limit: int | None
    one_time_limit: int | None

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Execute a single search query, return normalized results."""
        ...


# ---------------------------------------------------------------------------
# Router configuration
# ---------------------------------------------------------------------------


@dataclass
class RouterConfig:
    """SearchRouter configuration."""

    quota_file: Path
    # Priority order per strategy
    keyword_priority: list[str]  # e.g. ["tavily", "serper", "you"]
    semantic_priority: list[str]  # e.g. ["exa"]
    # Timeouts — 45s accommodates Claude Agent WebSearch calls which take
    # 28-36s per call (benchmarked). The old 15s killed every SDK call.
    # Tavily/Exa finish in <2s so the higher timeout doesn't affect them.
    per_query_timeout: float = 45.0
    # Circuit breaker
    circuit_failure_threshold: int = 3
    circuit_cooldown_minutes: int = 5

    @classmethod
    def default(
        cls,
        quota_file: Path | None = None,
        prefer_claude_tools: bool = True,
    ) -> "RouterConfig":
        if prefer_claude_tools:
            keyword = ["claude_agent", "brave", "tavily", "serper", "you"]
            semantic = ["claude_agent", "exa", "brave", "tavily"]
        else:
            keyword = ["brave", "tavily", "serper", "you"]
            semantic = ["exa", "brave", "tavily"]
        return cls(
            quota_file=quota_file
            or Path.home() / ".config" / "research-pipeline" / "quotas.json",
            keyword_priority=keyword,
            semantic_priority=semantic,
        )


# ---------------------------------------------------------------------------
# Quota store
# ---------------------------------------------------------------------------


class QuotaStore:
    """Manages per-API quota counters, persisted to JSON.

    Writes are debounced: ``record_success``/``record_failure`` fire inside a
    per-chunk ``asyncio.gather``, so a blocking whole-file write on every call
    stalls the event loop. Instead we mark the state dirty and only flush to
    disk at most once every ``_SAVE_DEBOUNCE_S`` seconds; ``flush()`` forces a
    final write at phase/run boundaries (see ``SearchRouter.aclose``). Up to a
    few seconds of counter increments can be lost on hard crash — acceptable,
    since quota accounting is best-effort and circuits reset each run anyway.
    """

    # Minimum seconds between blocking disk writes during a burst of updates.
    _SAVE_DEBOUNCE_S = 5.0

    def __init__(self, path: Path):
        self.path = path
        self._state: QuotaState = self._load()
        self._dirty = False
        self._last_save = 0.0

    def _load(self) -> QuotaState:
        if not self.path.exists():
            return QuotaState()
        try:
            return QuotaState.model_validate_json(self.path.read_text())
        except Exception:
            log.warning("Quota file corrupt, starting fresh: %s", self.path)
            return QuotaState()

    def _save(self, *, force: bool = False) -> None:
        """Mark state dirty; write to disk only if debounce elapsed or forced."""
        self._dirty = True
        if not force and (time.monotonic() - self._last_save) < self._SAVE_DEBOUNCE_S:
            return
        self.flush()

    def flush(self) -> None:
        """Write pending state to disk if dirty. Safe to call any time."""
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state.updated_at = datetime.now(timezone.utc).isoformat()
        self.path.write_text(self._state.model_dump_json(indent=2))
        self._dirty = False
        self._last_save = time.monotonic()

    def get_or_create(self, provider: SearchProvider) -> APIQuota:
        quota = self._state.quotas.get(provider.name)
        if quota is None:
            quota = APIQuota(
                api_name=provider.name,
                monthly_limit=provider.monthly_limit,
                one_time_limit=provider.one_time_limit,
            )
            self._state.quotas[provider.name] = quota
        return quota

    def _apply_resets(self, quota: APIQuota, now: datetime) -> None:
        """Reset daily/monthly counters if a new day/month has started."""
        today = now.strftime("%Y-%m-%d")
        this_month = now.strftime("%Y-%m")

        if quota.last_reset_day != today:
            quota.daily_count = 0
            quota.last_reset_day = today

        if quota.last_reset_month != this_month:
            # Only reset monthly count if this API has a monthly limit
            # (one-time APIs keep the total_count ticking forever)
            if quota.monthly_limit is not None:
                quota.monthly_count = 0
            quota.last_reset_month = this_month

    def available(self, provider: SearchProvider) -> bool:
        """Does this provider have remaining quota and no open circuit?"""
        quota = self.get_or_create(provider)
        now = datetime.now(timezone.utc)
        self._apply_resets(quota, now)

        # Circuit breaker check
        if quota.circuit_open_until:
            open_until = datetime.fromisoformat(quota.circuit_open_until)
            if now < open_until:
                return False
            # Cooldown elapsed — reset circuit
            quota.circuit_open_until = None
            quota.consecutive_failures = 0

        # Monthly quota
        if quota.monthly_limit is not None and quota.monthly_count >= quota.monthly_limit:
            return False

        # One-time quota
        if quota.one_time_limit is not None and quota.total_count >= quota.one_time_limit:
            return False

        return True

    def remaining(self, provider: SearchProvider) -> str:
        """Human-readable remaining quota for error messages."""
        quota = self.get_or_create(provider)
        parts = []
        if quota.monthly_limit is not None:
            parts.append(f"{quota.monthly_limit - quota.monthly_count}/{quota.monthly_limit}/mo")
        if quota.one_time_limit is not None:
            parts.append(f"{quota.one_time_limit - quota.total_count}/{quota.one_time_limit} total")
        if quota.circuit_open_until:
            parts.append(f"circuit open until {quota.circuit_open_until}")
        return ", ".join(parts) if parts else "no limit"

    def record_success(self, provider: SearchProvider) -> None:
        quota = self.get_or_create(provider)
        now = datetime.now(timezone.utc)
        self._apply_resets(quota, now)
        quota.daily_count += 1
        quota.monthly_count += 1
        quota.total_count += 1
        quota.consecutive_failures = 0
        self._save()

    def reset_circuits(self) -> None:
        """Reset all circuit breakers. Called at pipeline start so stale
        circuit state from a previous run doesn't poison a fresh run."""
        for quota in self._state.quotas.values():
            if quota.circuit_open_until or quota.consecutive_failures > 0:
                log.info(
                    "Resetting circuit breaker for %s (was: %d failures, open_until=%s)",
                    quota.api_name, quota.consecutive_failures, quota.circuit_open_until,
                )
                quota.circuit_open_until = None
                quota.consecutive_failures = 0
        self._save(force=True)

    def record_failure(self, provider: SearchProvider, threshold: int, cooldown_minutes: int) -> None:
        quota = self.get_or_create(provider)
        quota.consecutive_failures += 1
        if quota.consecutive_failures >= threshold:
            cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=cooldown_minutes)
            quota.circuit_open_until = cooldown_until.isoformat()
            log.warning(
                "Circuit breaker opened for %s until %s (%d consecutive failures)",
                provider.name,
                cooldown_until.isoformat(),
                quota.consecutive_failures,
            )
        self._save()


# ---------------------------------------------------------------------------
# SearchRouter
# ---------------------------------------------------------------------------


MAX_TAVILY_FALLBACKS = 10  # hard cap on Tavily content fallbacks per pipeline run


class SearchRouter:
    """Routes queries across multiple providers with quota tracking and failover."""

    def __init__(
        self,
        providers: list[SearchProvider],
        config: RouterConfig | None = None,
        allow_paid_fallback: bool = False,
    ):
        self.config = config or RouterConfig.default()
        self.providers: dict[str, SearchProvider] = {p.name: p for p in providers}
        self.quotas = QuotaStore(self.config.quota_file)
        self.allow_paid_fallback = allow_paid_fallback
        self._tavily_fallback_count = 0
        # Reset circuit breakers on fresh router init. A new pipeline run
        # should not inherit stale circuit state from a previous run —
        # transient failures (rate limits, timeouts) resolve between runs.
        self.quotas.reset_circuits()

    def _free_tier_exhausted(self, provider: SearchProvider) -> bool:
        """Check if a provider's free tier is used up.

        Returns False (= still free) when the provider has remaining quota
        under its monthly or one-time limit. Returns True only when the
        provider would incur cost on the next call.
        """
        quota = self.quotas.get_or_create(provider)
        if provider.monthly_limit is not None and quota.monthly_count >= provider.monthly_limit:
            return True
        if provider.one_time_limit is not None and quota.total_count >= provider.one_time_limit:
            return True
        return False

    def _priority_list(self, strategy: str) -> list[str]:
        if strategy == "semantic":
            return self.config.semantic_priority
        return self.config.keyword_priority

    async def search(
        self,
        query: str,
        max_results: int = 10,
        strategy: str = "keyword",
    ) -> list[SearchResult]:
        """Execute a single query, routing to the first available provider.

        Providers are tried in priority order. Any provider with remaining
        free-tier quota is tried automatically. --allow-paid-fallback is only
        required when a provider's free tier is exhausted (monthly_count >=
        monthly_limit or total_count >= one_time_limit).
        """
        priority = self._priority_list(strategy)
        attempted = {}
        got_empty_success = False

        for name in priority:
            provider = self.providers.get(name)
            if provider is None:
                continue

            if not self.quotas.available(provider):
                attempted[name] = self.quotas.remaining(provider)
                continue

            # Gate providers that have exceeded their free tier.
            if name not in UNLIMITED_FREE_PROVIDERS and self._free_tier_exhausted(provider):
                if name in FREE_TIER_ONLY_PROVIDERS:
                    # Never use beyond free tier (too expensive vs Serper)
                    attempted[name] = f"free tier exhausted, capped ({self.quotas.remaining(provider)})"
                    continue
                if not self.allow_paid_fallback:
                    # Serper/You: allowed with --allow-paid-fallback
                    attempted[name] = f"free tier exhausted ({self.quotas.remaining(provider)})"
                    continue

            try:
                results = await asyncio.wait_for(
                    provider.search(query, max_results=max_results),
                    timeout=self.config.per_query_timeout,
                )
                self.quotas.record_success(provider)
                if results:
                    return results
                # Empty result: the call succeeded but returned nothing (an
                # empty answer, or a parse-failure that surfaced as []). Don't
                # let that starve the query — fall through to the next
                # provider. If no later provider yields anything, we return
                # this clean empty below (a genuine "no results" answer).
                got_empty_success = True
                attempted[name] = "empty result"
            except asyncio.TimeoutError:
                log.warning("Provider %s timed out for query: %s", name, query[:60])
                self.quotas.record_failure(
                    provider,
                    self.config.circuit_failure_threshold,
                    self.config.circuit_cooldown_minutes,
                )
                attempted[name] = "timeout"
            except ProviderError as e:
                if e.transient:
                    self.quotas.record_failure(
                        provider,
                        self.config.circuit_failure_threshold,
                        self.config.circuit_cooldown_minutes,
                    )
                attempted[name] = str(e)
            except Exception as e:  # noqa: BLE001
                log.exception("Unexpected error from provider %s", name)
                self.quotas.record_failure(
                    provider,
                    self.config.circuit_failure_threshold,
                    self.config.circuit_cooldown_minutes,
                )
                attempted[name] = f"unexpected: {e}"

        # Every configured provider was tried. If at least one succeeded but
        # returned no results, that's a legitimate empty answer — return it
        # rather than raising. Only raise when nothing was reachable at all.
        if got_empty_success:
            return []
        raise QuotaExhaustedError(details=attempted)

    async def batch_search(
        self,
        queries: list[str],
        max_results: int = 10,
        strategy: str = "keyword",
        chunk_size: int = 10,
    ) -> list[SearchResult]:
        """Run queries in chunks and return flattened deduplicated results.

        Processes queries in chunks of ``chunk_size`` (default 10) instead of
        firing all at once. This prevents semaphore starvation: when the SDK
        concurrency semaphore has N slots and we fire >N queries, the excess
        queries block on the semaphore while the router's per_query_timeout
        ticks — causing false timeouts and circuit breaker trips.

        By chunking at the router level, each query in a chunk gets the full
        timeout budget for actual execution rather than spending it waiting.
        """
        combined: list[SearchResult] = []
        seen_urls: set[str] = set()
        total_failures = 0

        for i in range(0, len(queries), chunk_size):
            chunk = queries[i : i + chunk_size]
            tasks = [
                self._safe_single_search(q, max_results, strategy) for q in chunk
            ]
            results_per_query = await asyncio.gather(*tasks, return_exceptions=True)

            failures = 0
            for r in results_per_query:
                if isinstance(r, BaseException):
                    failures += 1
                    log.warning("Query failed: %s", r)
                    continue
                # r is list[SearchResult] here
                for result in r:
                    if result.url not in seen_urls:
                        seen_urls.add(result.url)
                        combined.append(result)

            total_failures += failures
            log.info(
                "batch_search chunk %d-%d: %d queries, %d failures",
                i, i + len(chunk), len(chunk), failures,
            )

        log.info(
            "batch_search: %d queries, %d unique results, %d failures",
            len(queries),
            len(combined),
            total_failures,
        )
        return combined

    async def _safe_single_search(
        self, query: str, max_results: int, strategy: str
    ) -> list[SearchResult]:
        """Wrap a single search so exceptions become returned values via gather."""
        return await self.search(query, max_results=max_results, strategy=strategy)

    async def tavily_content_fallback(
        self,
        url: str,
        title: str,
        query: str,
        max_results: int = 3,
    ) -> list[SearchResult] | None:
        """Search Tavily for content from a failed AUTHORITATIVE source.

        Used as last-resort fallback when both SDK and httpx fetchers fail on
        a critical source. Searches for the source's domain + title to find
        alternative pages with the same content.

        Budget: max MAX_TAVILY_FALLBACKS (10) per pipeline run, tracked on the
        router instance. Only called for AUTHORITATIVE reputation sources.
        """
        if self._tavily_fallback_count >= MAX_TAVILY_FALLBACKS:
            log.info(
                "Tavily fallback budget exhausted (%d/%d)",
                self._tavily_fallback_count,
                MAX_TAVILY_FALLBACKS,
            )
            return None

        tavily = self.providers.get("tavily")
        if tavily is None:
            log.info("Tavily fallback unavailable — no Tavily provider configured")
            return None

        if not self.quotas.available(tavily):
            log.info("Tavily fallback unavailable — quota exhausted")
            return None

        # Build a targeted search query using the source's domain + title
        try:
            domain = urlparse(url).netloc
        except Exception:
            domain = ""
        search_query = f"site:{domain} {title or query}" if domain else f"{title or query}"

        try:
            results = await asyncio.wait_for(
                tavily.search(search_query, max_results=max_results),
                timeout=self.config.per_query_timeout,
            )
            self._tavily_fallback_count += 1
            self.quotas.record_success(tavily)
            log.info(
                "Tavily fallback for %s: %d results (%d/%d budget used)",
                url[:60],
                len(results),
                self._tavily_fallback_count,
                MAX_TAVILY_FALLBACKS,
            )
            return results if results else None
        except asyncio.TimeoutError:
            log.warning("Tavily fallback timed out for %s", url[:60])
            self._tavily_fallback_count += 1
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("Tavily fallback failed for %s: %s", url[:60], e)
            self._tavily_fallback_count += 1
            return None

    async def aclose(self) -> None:
        """Close provider HTTP clients and flush pending quota writes.

        Providers holding httpx AsyncClients (Brave/Serper/You/Tavily) leak
        their connection pools if never closed. Called from a ``finally`` in
        ``run_pipeline``; also flushes the debounced quota store so the final
        counter state hits disk.
        """
        for provider in self.providers.values():
            aclose = getattr(provider, "aclose", None)
            if aclose is None:
                continue
            try:
                await aclose()
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "Error closing provider %s: %s",
                    getattr(provider, "name", "?"), e,
                )
        self.quotas.flush()


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class ClaudeAgentSearchProvider:
    """Claude Code SDK-backed search — uses the user's Max subscription, zero external cost.

    Calls claude_agent_sdk.query(allowed_tools=["WebSearch"]) with a focused
    prompt asking Claude to search and return results as JSON. Each call is
    wrapped in asyncio.wait_for by the router (per_query_timeout) for hard kill.
    """

    name = "claude_agent"
    monthly_limit: int | None = None  # unlimited on Max — relies on circuit breaker
    one_time_limit: int | None = None

    SEARCH_PROMPT = """Use the WebSearch tool to search for: {query}

After getting search results, format them as a JSON array. Each result should have:
- "url": the page URL
- "title": the page title
- "snippet": a brief description of the page content

Return up to {max_results} results. Return ONLY the JSON array in a fenced code block, no other text."""

    def __init__(self, *, model: str | None = None, effort: str | None = None):
        self.model = model
        self.effort = effort

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        try:
            from .sdk import extract_json, llm_call

            # max_attempts=1: the router already fails over to the next
            # provider on error — SDK-level retry would double the latency.
            raw = await llm_call(
                self.SEARCH_PROMPT.format(query=query, max_results=max_results),
                model=self.model,
                effort=self.effort,
                allowed_tools=["WebSearch"],
                max_turns=3,
                max_attempts=1,
                label=f"search:{query[:50]}",
            )
        except Exception as e:  # noqa: BLE001
            raise ProviderError(self.name, str(e), transient=True) from e

        try:
            data = extract_json(raw)
            if not isinstance(data, list):
                data = [data] if isinstance(data, dict) else []
        except ValueError as e:
            # A parse failure is transient (the model returned malformed/empty
            # JSON this call) — raise so the router fails over to the next
            # provider instead of silently yielding zero results.
            log.warning("ClaudeAgentSearch: failed to parse JSON from response")
            raise ProviderError(
                self.name, f"JSON parse failure: {e}", transient=True
            ) from e

        results = []
        for item in data[:max_results]:
            if not isinstance(item, dict):
                continue
            results.append(
                SearchResult(
                    url=item.get("url", ""),
                    title=item.get("title", ""),
                    snippet=item.get("snippet", ""),
                    source_api=self.name,
                )
            )
        return results


class TavilyProvider:
    """Tavily search API — 1,000 credits/month free, no CC."""

    name = "tavily"
    monthly_limit = 1000
    one_time_limit: int | None = None

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY")
        if not self.api_key:
            raise ValueError("TAVILY_API_KEY not set")
        # Import lazily so missing package doesn't break import of search_router
        from tavily import AsyncTavilyClient  # type: ignore

        self._client = AsyncTavilyClient(api_key=self.api_key)

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        try:
            response = await self._client.search(
                query=query, max_results=max_results, search_depth="basic"
            )
        except Exception as e:  # noqa: BLE001
            raise ProviderError(self.name, str(e), transient=True) from e

        results = []
        for item in response.get("results", []):
            results.append(
                SearchResult(
                    url=item.get("url", ""),
                    title=item.get("title", ""),
                    snippet=item.get("content", ""),
                    source_api=self.name,
                    score=item.get("score"),
                    published_date=item.get("published_date"),
                )
            )
        return results

    async def aclose(self) -> None:
        aclose = getattr(self._client, "aclose", None)
        if aclose is not None:
            await aclose()


class ExaProvider:
    """Exa search API — 1,000 requests/month free, semantic search."""

    name = "exa"
    monthly_limit = 1000
    one_time_limit: int | None = None

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("EXA_API_KEY")
        if not self.api_key:
            raise ValueError("EXA_API_KEY not set")
        from exa_py import Exa  # type: ignore

        # exa-py provides sync Exa; async via asyncio.to_thread wrapper
        self._client = Exa(api_key=self.api_key)

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        # search_and_contents returns URLs + snippets in a single call.
        # "auto" type is Exa's default — balanced speed/quality, handles both
        # semantic and keyword-ish queries. Highlights (vs full text) are 10x
        # fewer tokens and are the recommended mode for agent workflows per
        # https://exa.ai/docs/reference/search-api-guide-for-coding-agents
        try:
            response = await asyncio.to_thread(
                self._client.search_and_contents,
                query,
                num_results=max_results,
                type="auto",
                highlights={"num_sentences": 3, "highlights_per_url": 1},
            )
        except Exception as e:  # noqa: BLE001
            raise ProviderError(self.name, str(e), transient=True) from e

        results = []
        for item in getattr(response, "results", []):
            # Prefer highlights (most relevant excerpt) over full text for snippet
            highlights = getattr(item, "highlights", None) or []
            snippet = highlights[0] if highlights else (getattr(item, "text", "") or "")
            results.append(
                SearchResult(
                    url=getattr(item, "url", ""),
                    title=getattr(item, "title", "") or "",
                    snippet=snippet,
                    source_api=self.name,
                    score=getattr(item, "score", None),
                    published_date=getattr(item, "published_date", None),
                )
            )
        return results


class BraveProvider:
    """Brave Search API — 1,000 queries/month free ($5 credit, $5/1K queries)."""

    name = "brave"
    monthly_limit = 1000
    one_time_limit: int | None = None

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("BRAVE_API_KEY")
        if not self.api_key:
            raise ValueError("BRAVE_API_KEY not set")
        import httpx  # type: ignore

        self._client = httpx.AsyncClient(
            base_url="https://api.search.brave.com",
            headers={
                "X-Subscription-Token": self.api_key,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
            timeout=15.0,
        )

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        try:
            response = await self._client.get(
                "/res/v1/web/search",
                params={"q": query, "count": min(max_results, 20)},
            )
            response.raise_for_status()
        except Exception as e:  # noqa: BLE001
            raise ProviderError(self.name, str(e), transient=True) from e

        data = response.json()
        results = []
        for item in data.get("web", {}).get("results", [])[:max_results]:
            results.append(
                SearchResult(
                    url=item.get("url", ""),
                    title=item.get("title", ""),
                    snippet=item.get("description", ""),
                    source_api=self.name,
                )
            )
        return results

    async def aclose(self) -> None:
        await self._client.aclose()


class SerperProvider:
    """Serper.dev — 50K one-time free credits ($1/1K paid). Cheapest paid provider."""

    name = "serper"
    monthly_limit: int | None = None
    one_time_limit = 50000

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("SERPER_API_KEY")
        if not self.api_key:
            raise ValueError("SERPER_API_KEY not set")
        import httpx  # type: ignore

        self._client = httpx.AsyncClient(
            base_url="https://google.serper.dev",
            headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            timeout=15.0,
        )

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        try:
            response = await self._client.post(
                "/search", json={"q": query, "num": max_results}
            )
            response.raise_for_status()
        except Exception as e:  # noqa: BLE001
            raise ProviderError(self.name, str(e), transient=True) from e

        data = response.json()
        results = []
        for item in data.get("organic", [])[:max_results]:
            results.append(
                SearchResult(
                    url=item.get("link", ""),
                    title=item.get("title", ""),
                    snippet=item.get("snippet", ""),
                    source_api=self.name,
                    published_date=item.get("date"),
                )
            )
        return results

    async def aclose(self) -> None:
        await self._client.aclose()


class YouProvider:
    """You.com search API — $100 one-time credits (~20K searches)."""

    name = "you"
    monthly_limit: int | None = None
    one_time_limit = 20000  # approximate — $100 / $5/1K = 20K

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("YOU_API_KEY")
        if not self.api_key:
            raise ValueError("YOU_API_KEY not set")
        import httpx  # type: ignore

        self._client = httpx.AsyncClient(
            base_url="https://api.ydc-index.io",
            headers={"X-API-Key": self.api_key},
            timeout=15.0,
        )

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        try:
            response = await self._client.get(
                "/search", params={"query": query, "num_web_results": max_results}
            )
            response.raise_for_status()
        except Exception as e:  # noqa: BLE001
            raise ProviderError(self.name, str(e), transient=True) from e

        data = response.json()
        results = []
        for item in data.get("hits", [])[:max_results]:
            results.append(
                SearchResult(
                    url=item.get("url", ""),
                    title=item.get("title", ""),
                    snippet=item.get("description", "") or item.get("snippets", [""])[0],
                    source_api=self.name,
                )
            )
        return results

    async def aclose(self) -> None:
        await self._client.aclose()


class PaidFallbackError(SearchRouterError):
    """Raised when all free-tier search providers are exhausted or unavailable."""

    def __init__(self) -> None:
        super().__init__(
            "All free-tier search providers exhausted or unavailable. Options:\n"
            "  - Re-run with --allow-paid-fallback to use providers beyond free tier\n"
            "  - Re-run with --no-claude-tools to skip Claude Agent entirely\n"
            "  - Wait for monthly quota reset (Tavily/Exa reset monthly)"
        )


def build_default_router(
    quota_file: Path | None = None,
    prefer_claude_tools: bool = True,
    allow_paid_fallback: bool = False,
    model: str | None = None,
    effort: str | None = None,
) -> SearchRouter:
    """Build a SearchRouter with available providers.

    When prefer_claude_tools=True, includes ClaudeAgentSearchProvider as the
    primary (no API key needed — uses Claude Max subscription). External API
    providers are included if their keys are set, as fallback. ``model``/
    ``effort`` pin the Claude Agent search calls (mechanical formatting work —
    the pipeline passes its parse-tier model).

    When allow_paid_fallback=False (default), the router raises
    PaidFallbackError instead of silently falling back to paid APIs when
    Claude Agent circuit breaker trips.
    """
    providers: list[SearchProvider] = []

    # Claude Agent — primary when preferred (no API key needed)
    if prefer_claude_tools:
        try:
            providers.append(ClaudeAgentSearchProvider(model=model, effort=effort))  # type: ignore[arg-type]
            log.info("Claude Agent search provider enabled (default)")
        except Exception as e:  # noqa: BLE001
            log.warning("Could not init ClaudeAgentSearchProvider: %s", e)

    # External API providers — fallback chain
    for factory, key_var in [
        (BraveProvider, "BRAVE_API_KEY"),
        (TavilyProvider, "TAVILY_API_KEY"),
        (ExaProvider, "EXA_API_KEY"),
        (SerperProvider, "SERPER_API_KEY"),
        (YouProvider, "YOU_API_KEY"),
    ]:
        if os.environ.get(key_var):
            try:
                providers.append(factory())  # type: ignore[arg-type]
            except Exception as e:  # noqa: BLE001
                log.warning("Could not init %s: %s", factory.__name__, e)

    if not providers:
        raise RuntimeError(
            "No search providers configured. Either:\n"
            "  - Ensure claude-agent-sdk is installed (for Claude Agent provider)\n"
            "  - Or set at least one of: TAVILY_API_KEY, EXA_API_KEY, SERPER_API_KEY, YOU_API_KEY"
        )

    config = RouterConfig.default(
        quota_file, prefer_claude_tools=prefer_claude_tools
    )
    return SearchRouter(providers, config, allow_paid_fallback=allow_paid_fallback)
