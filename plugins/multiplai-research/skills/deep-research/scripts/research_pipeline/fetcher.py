"""Async web content fetcher with strict timeouts and retry.

Key properties:
- Hard 15s per-request timeout via asyncio.wait_for (never hangs)
- Retry with exponential backoff (2x, 1s and 3s) for transient errors only
- Never returns None — always typed FetchResult or FetchError
- Batch fetching with per-request failure isolation and 30s batch timeout
- trafilatura for markdown extraction with fallback chain
- Optional Playwright fallback for JS-rendered pages (only if installed)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Iterable

import httpx

from .models import FetchError, FetchErrorType, FetchResult

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


DEFAULT_REQUEST_TIMEOUT_S = 15.0
DEFAULT_BATCH_TIMEOUT_S = 30.0
DEFAULT_MAX_RETRIES = 2
RETRY_DELAYS = [1.0, 3.0]  # exponential backoff
MIN_EXTRACTED_CONTENT_CHARS = 200

USER_AGENT = (
    "Mozilla/5.0 (compatible; DeepResearchPipeline/0.1; "
    "+https://github.com/claude-code-multiplai)"
)


# ---------------------------------------------------------------------------
# Single fetch with retry
# ---------------------------------------------------------------------------


async def fetch_url(
    url: str,
    client: httpx.AsyncClient,
    *,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT_S,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> FetchResult:
    """Fetch a single URL with hard timeout and retry.

    Returns FetchResult with either content or a typed FetchError — never None.
    """
    start = time.monotonic()
    last_error: FetchError | None = None

    for attempt in range(max_retries + 1):
        try:
            response = await asyncio.wait_for(
                client.get(url, follow_redirects=True),
                timeout=request_timeout,
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            last_error = FetchError(
                url=url,
                error_type=FetchErrorType.TIMEOUT,
                message=f"Request exceeded {request_timeout}s",
                elapsed_seconds=elapsed,
                retry_count=attempt,
            )
            log.warning("Timeout fetching %s (attempt %d)", url, attempt + 1)
            if attempt < max_retries:
                await asyncio.sleep(RETRY_DELAYS[attempt])
                continue
            return FetchResult(url=url, success=False, error=last_error, elapsed_seconds=elapsed)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError) as e:
            elapsed = time.monotonic() - start
            last_error = FetchError(
                url=url,
                error_type=FetchErrorType.CONNECTION,
                message=str(e),
                elapsed_seconds=elapsed,
                retry_count=attempt,
            )
            log.warning("Connection error %s (attempt %d): %s", url, attempt + 1, e)
            if attempt < max_retries:
                await asyncio.sleep(RETRY_DELAYS[attempt])
                continue
            return FetchResult(url=url, success=False, error=last_error, elapsed_seconds=elapsed)
        except Exception as e:  # noqa: BLE001
            elapsed = time.monotonic() - start
            return FetchResult(
                url=url,
                success=False,
                error=FetchError(
                    url=url,
                    error_type=FetchErrorType.UNKNOWN,
                    message=str(e),
                    elapsed_seconds=elapsed,
                    retry_count=attempt,
                ),
                elapsed_seconds=elapsed,
            )

        # Got a response — check status
        elapsed = time.monotonic() - start
        if 200 <= response.status_code < 300:
            content = await extract_content(response.text)
            links = extract_outbound_links(response.text, base_url=str(response.url))
            return FetchResult(
                url=str(response.url),
                success=True,
                content=content,
                elapsed_seconds=elapsed,
                extracted_links=links,
            )
        if 500 <= response.status_code < 600:
            # Retry on 5xx
            last_error = FetchError(
                url=url,
                error_type=FetchErrorType.HTTP_5XX,
                message=f"HTTP {response.status_code}",
                elapsed_seconds=elapsed,
                retry_count=attempt,
                status_code=response.status_code,
            )
            if attempt < max_retries:
                await asyncio.sleep(RETRY_DELAYS[attempt])
                continue
            return FetchResult(url=url, success=False, error=last_error, elapsed_seconds=elapsed)
        # 4xx — no retry
        return FetchResult(
            url=url,
            success=False,
            error=FetchError(
                url=url,
                error_type=FetchErrorType.HTTP_4XX,
                message=f"HTTP {response.status_code}",
                elapsed_seconds=elapsed,
                retry_count=attempt,
                status_code=response.status_code,
            ),
            elapsed_seconds=elapsed,
        )

    # Exhausted retries without succeeding (shouldn't reach here given returns above)
    return FetchResult(
        url=url,
        success=False,
        error=last_error
        or FetchError(
            url=url,
            error_type=FetchErrorType.UNKNOWN,
            message="retries exhausted",
            elapsed_seconds=time.monotonic() - start,
            retry_count=max_retries,
        ),
        elapsed_seconds=time.monotonic() - start,
    )


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------


async def extract_content(html: str) -> str:
    """Extract main content from HTML as markdown.

    Chain: trafilatura (markdown) → readability fallback (trafilatura handles
    internally) → basic tag stripping. Never returns None — always a string.
    """
    # Run trafilatura in a thread (CPU-bound, releases GIL via lxml)
    try:
        import trafilatura

        markdown = await asyncio.to_thread(
            trafilatura.extract,
            html,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            include_comments=False,
            include_formatting=True,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("trafilatura failed: %s", e)
        markdown = None

    if markdown and len(markdown) >= MIN_EXTRACTED_CONTENT_CHARS:
        return markdown

    # Fallback: basic tag stripping
    log.info("Falling back to basic tag stripping (trafilatura returned %d chars)",
             len(markdown) if markdown else 0)
    return basic_tag_strip(html)


def basic_tag_strip(html: str) -> str:
    """Strip HTML tags as a last-resort fallback.

    Not ideal — includes boilerplate — but always returns something.
    """
    # Remove script/style blocks entirely
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Replace tags with spaces
    text = re.sub(r"<[^>]+>", " ", html)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_outbound_links(html: str, base_url: str) -> list[str]:
    """Extract outbound links from HTML for link-following.

    Returns absolute URLs only, deduplicated.
    """
    try:
        from urllib.parse import urljoin, urlparse

        href_pattern = re.compile(r'<a[^>]+href=["\']([^"\']+)["\']', re.IGNORECASE)
        raw_links = href_pattern.findall(html)
        base_host = urlparse(base_url).netloc

        abs_links: list[str] = []
        seen: set[str] = set()
        for href in raw_links:
            if href.startswith("#") or href.startswith("javascript:") or href.startswith("mailto:"):
                continue
            absolute = urljoin(base_url, href)
            parsed = urlparse(absolute)
            if parsed.scheme not in ("http", "https"):
                continue
            # Skip same-page fragments
            if parsed.netloc == base_host and not parsed.path.strip("/"):
                continue
            if absolute not in seen:
                seen.add(absolute)
                abs_links.append(absolute)
        return abs_links[:50]  # cap per-page
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Playwright fallback (optional)
# ---------------------------------------------------------------------------


async def fetch_with_playwright(url: str) -> FetchResult:
    """Render a JS-heavy page with Playwright and extract via trafilatura.

    Only used as a fallback when httpx+trafilatura returns insufficient content
    AND Playwright is installed. Never raises — returns FetchResult with error
    if Playwright isn't available.
    """
    start = time.monotonic()
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        log.warning("Playwright not installed, skipping JS rendering for %s", url)
        return FetchResult(
            url=url,
            success=False,
            error=FetchError(
                url=url,
                error_type=FetchErrorType.EXTRACTION,
                message="Playwright not installed",
                elapsed_seconds=time.monotonic() - start,
            ),
            elapsed_seconds=time.monotonic() - start,
        )

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await asyncio.wait_for(
                page.goto(url, wait_until="networkidle"), timeout=30.0
            )
            html = await page.content()
            await browser.close()

        content = await extract_content(html)
        elapsed = time.monotonic() - start
        return FetchResult(
            url=url,
            success=True,
            content=content,
            elapsed_seconds=elapsed,
            extracted_links=extract_outbound_links(html, url),
        )
    except Exception as e:  # noqa: BLE001
        elapsed = time.monotonic() - start
        return FetchResult(
            url=url,
            success=False,
            error=FetchError(
                url=url,
                error_type=FetchErrorType.UNKNOWN,
                message=f"Playwright failure: {e}",
                elapsed_seconds=elapsed,
            ),
            elapsed_seconds=elapsed,
        )


# ---------------------------------------------------------------------------
# Batch fetch
# ---------------------------------------------------------------------------


async def fetch_batch(
    urls: Iterable[str],
    *,
    client: httpx.AsyncClient | None = None,
    batch_timeout: float = DEFAULT_BATCH_TIMEOUT_S,
    js_fallback: bool = False,
) -> list[FetchResult]:
    """Fetch multiple URLs concurrently with per-request isolation.

    - gather(return_exceptions=True) ensures one failure doesn't kill the batch
    - Outer asyncio.wait_for enforces batch-level timeout
    - Each URL gets its own FetchResult (success or typed error)
    - Failed sources don't block successful ones
    """
    urls = list(urls)
    owned_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(DEFAULT_REQUEST_TIMEOUT_S),
        )

    try:
        tasks = [asyncio.create_task(fetch_url(url, client)) for url in urls]
        try:
            raw_results: list = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=batch_timeout,
            )
        except asyncio.TimeoutError:
            # Batch exceeded overall timeout — gather what completed, cancel rest
            log.warning("Batch timeout at %.1fs — cancelling remaining", batch_timeout)
            raw_results = []
            for t in tasks:
                if t.done() and not t.cancelled():
                    try:
                        raw_results.append(t.result())
                    except Exception as e:  # noqa: BLE001
                        raw_results.append(e)
                else:
                    t.cancel()
                    raw_results.append(
                        FetchResult(
                            url="<cancelled>",
                            success=False,
                            error=FetchError(
                                url="<cancelled>",
                                error_type=FetchErrorType.TIMEOUT,
                                message="batch timeout",
                                elapsed_seconds=batch_timeout,
                            ),
                        )
                    )

        results: list[FetchResult] = []
        for url, r in zip(urls, raw_results):
            if isinstance(r, BaseException):
                results.append(
                    FetchResult(
                        url=url,
                        success=False,
                        error=FetchError(
                            url=url,
                            error_type=FetchErrorType.UNKNOWN,
                            message=str(r),
                            elapsed_seconds=0.0,
                        ),
                        elapsed_seconds=0.0,
                    )
                )
            else:
                results.append(r)

        # Optional: JS fallback for pages where extraction yielded empty content
        if js_fallback:
            for i, r in enumerate(results):
                if r.success and r.content and len(r.content) < MIN_EXTRACTED_CONTENT_CHARS:
                    log.info("Retrying %s with Playwright (extraction too short)", r.url)
                    results[i] = await fetch_with_playwright(r.url)

        return results
    finally:
        if owned_client:
            await client.aclose()
