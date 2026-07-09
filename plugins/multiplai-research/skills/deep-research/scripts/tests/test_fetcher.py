"""Tests for the content fetcher — timeouts, retries, batch isolation, extraction."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from research_pipeline import fetcher
from research_pipeline.fetcher import (
    basic_tag_strip,
    extract_content,
    extract_outbound_links,
    fetch_batch,
    fetch_url,
)
from research_pipeline.models import FetchErrorType


# ---------------------------------------------------------------------------
# Test HTML fixtures
# ---------------------------------------------------------------------------


ARTICLE_HTML = """
<!DOCTYPE html>
<html><head><title>Test Article</title></head>
<body>
  <nav>Home | About | Contact</nav>
  <article>
    <h1>The Main Article</h1>
    <p>This is the main body of the article. It contains enough text that
    trafilatura should extract it as the main content. There are multiple
    paragraphs here to ensure we exceed the minimum character threshold.</p>
    <p>Another paragraph with <a href="https://example.com/related">a link</a>
    and more substantive content. Lorem ipsum dolor sit amet, consectetur
    adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore
    magna aliqua.</p>
    <h2>A Subsection</h2>
    <p>More content in the subsection. Ut enim ad minim veniam, quis nostrud
    exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat.</p>
    <ul>
      <li>List item one</li>
      <li>List item two</li>
    </ul>
  </article>
  <footer>Copyright © 2026</footer>
  <a href="/other">Other page</a>
  <a href="https://external.example/page">External link</a>
  <script>var x = 1;</script>
</body></html>
"""

MINIMAL_HTML = "<html><body><p>Too short.</p></body></html>"


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------


class TestExtractContent:
    @pytest.mark.asyncio
    async def test_extracts_article_to_markdown(self) -> None:
        result = await extract_content(ARTICLE_HTML)
        assert len(result) > 200
        assert "Main Article" in result or "main article" in result.lower()
        # Should NOT include navigation boilerplate
        assert "Home | About | Contact" not in result

    @pytest.mark.asyncio
    async def test_falls_back_on_short_content(self) -> None:
        result = await extract_content(MINIMAL_HTML)
        # Should fall back to basic strip when trafilatura returns nothing substantial
        assert isinstance(result, str)
        assert len(result) > 0

    def test_basic_tag_strip(self) -> None:
        html = "<div>Hello <b>world</b></div><script>bad()</script>"
        result = basic_tag_strip(html)
        assert "Hello" in result
        assert "world" in result
        assert "bad()" not in result
        assert "<" not in result

    def test_basic_strip_handles_empty(self) -> None:
        assert basic_tag_strip("") == ""
        assert basic_tag_strip("   ") == ""

    def test_extract_outbound_links(self) -> None:
        links = extract_outbound_links(ARTICLE_HTML, base_url="https://site.example/article")
        assert "https://example.com/related" in links
        assert "https://external.example/page" in links
        assert "https://site.example/other" in links
        # No javascript:/mailto:/fragments
        for link in links:
            assert link.startswith(("http://", "https://"))

    def test_extract_links_ignores_javascript_and_mailto(self) -> None:
        html = '<a href="javascript:void(0)">js</a><a href="mailto:a@b.com">mail</a><a href="https://ok.example">ok</a>'
        links = extract_outbound_links(html, base_url="https://site.example")
        assert links == ["https://ok.example"]


# ---------------------------------------------------------------------------
# Single fetch (using mocked httpx via MockTransport)
# ---------------------------------------------------------------------------


def _make_client_with_handler(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


class TestFetchUrl:
    @pytest.mark.asyncio
    async def test_success_returns_content(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=ARTICLE_HTML)

        async with _make_client_with_handler(handler) as client:
            result = await fetch_url("https://test.example/article", client)

        assert result.success is True
        assert result.content is not None
        assert len(result.content) > 100
        assert result.error is None

    @pytest.mark.asyncio
    async def test_http_404_no_retry(self) -> None:
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(404, text="not found")

        async with _make_client_with_handler(handler) as client:
            result = await fetch_url("https://test.example/missing", client)

        assert result.success is False
        assert result.error is not None
        assert result.error.error_type == FetchErrorType.HTTP_4XX
        assert result.error.status_code == 404
        assert call_count == 1  # no retry on 4xx

    @pytest.mark.asyncio
    async def test_http_500_retries(self) -> None:
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(500, text="server error")

        async with _make_client_with_handler(handler) as client:
            # Use shorter backoff in test
            from research_pipeline import fetcher
            original = fetcher.RETRY_DELAYS
            fetcher.RETRY_DELAYS = [0.01, 0.01]
            try:
                result = await fetch_url("https://test.example/boom", client, max_retries=2)
            finally:
                fetcher.RETRY_DELAYS = original

        assert result.success is False
        assert result.error is not None
        assert result.error.error_type == FetchErrorType.HTTP_5XX
        assert call_count == 3  # original + 2 retries

    @pytest.mark.asyncio
    async def test_503_recovers_on_retry(self) -> None:
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                return httpx.Response(503)
            return httpx.Response(200, text=ARTICLE_HTML)

        async with _make_client_with_handler(handler) as client:
            from research_pipeline import fetcher
            original = fetcher.RETRY_DELAYS
            fetcher.RETRY_DELAYS = [0.01, 0.01]
            try:
                result = await fetch_url("https://test.example/flaky", client)
            finally:
                fetcher.RETRY_DELAYS = original

        assert result.success is True
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_timeout_returns_typed_error(self) -> None:
        async def slow_handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5)  # much longer than timeout
            return httpx.Response(200, text="never")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(slow_handler), timeout=10.0
        ) as client:
            result = await fetch_url(
                "https://test.example/slow",
                client,
                request_timeout=0.1,
                max_retries=0,
            )

        assert result.success is False
        assert result.error is not None
        assert result.error.error_type == FetchErrorType.TIMEOUT


# ---------------------------------------------------------------------------
# Batch fetch
# ---------------------------------------------------------------------------


class TestFetchBatch:
    @pytest.mark.asyncio
    async def test_batch_concurrent_success(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=ARTICLE_HTML)

        async with _make_client_with_handler(handler) as client:
            urls = [f"https://test.example/{i}" for i in range(5)]
            results = await fetch_batch(urls, client=client)

        assert len(results) == 5
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_batch_partial_failure_isolation(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "fail" in str(request.url):
                return httpx.Response(404)
            return httpx.Response(200, text=ARTICLE_HTML)

        urls = [
            "https://test.example/ok1",
            "https://test.example/fail1",
            "https://test.example/ok2",
            "https://test.example/fail2",
        ]
        async with _make_client_with_handler(handler) as client:
            results = await fetch_batch(urls, client=client)

        assert len(results) == 4
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]
        assert len(successes) == 2
        assert len(failures) == 2
        # Failed URLs have typed errors
        assert all(r.error is not None for r in failures)

    @pytest.mark.asyncio
    async def test_batch_never_returns_none(self) -> None:
        """Every URL in the batch produces a typed result — no silent Nones."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        async with _make_client_with_handler(handler) as client:
            from research_pipeline import fetcher
            original = fetcher.RETRY_DELAYS
            fetcher.RETRY_DELAYS = [0.001, 0.001]
            try:
                results = await fetch_batch(
                    ["https://test.example/a", "https://test.example/b"],
                    client=client,
                    batch_timeout=5.0,
                )
            finally:
                fetcher.RETRY_DELAYS = original

        assert len(results) == 2
        for r in results:
            assert r is not None
            assert r.error is not None
            assert r.error.error_type == FetchErrorType.HTTP_5XX


# ---------------------------------------------------------------------------
# Byte-bounded fetch (RES-2) and retry-delay clamping
# ---------------------------------------------------------------------------


class TestBoundedFetch:
    @pytest.mark.asyncio
    async def test_response_body_capped_at_max_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A large body is read only up to MAX_RESPONSE_BYTES (not unbounded)."""
        monkeypatch.setattr(fetcher, "MAX_RESPONSE_BYTES", 1000)
        big_body = "x" * 50_000

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=big_body)

        async with _make_client_with_handler(handler) as client:
            response = await fetcher._get_validated(
                client, "https://big.example/page"
            )

        # Body was truncated at the ceiling rather than materialized whole.
        assert len(response.content) <= 1000

    @pytest.mark.asyncio
    async def test_bounded_fetch_still_returns_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bounded read still yields usable extracted content end-to-end."""
        monkeypatch.setattr(fetcher, "MAX_RESPONSE_BYTES", 5_000_000)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=ARTICLE_HTML)

        async with _make_client_with_handler(handler) as client:
            result = await fetch_url("https://ok.example/article", client)

        assert result.success is True
        assert result.content is not None
        assert len(result.content) > 100


class TestRetryDelayClamp:
    def test_retry_delay_within_table(self) -> None:
        assert fetcher._retry_delay(0) == fetcher.RETRY_DELAYS[0]
        assert fetcher._retry_delay(1) == fetcher.RETRY_DELAYS[1]

    def test_retry_delay_clamps_beyond_table(self) -> None:
        # max_retries > len(RETRY_DELAYS) must not IndexError — reuse last delay.
        assert fetcher._retry_delay(5) == fetcher.RETRY_DELAYS[-1]
