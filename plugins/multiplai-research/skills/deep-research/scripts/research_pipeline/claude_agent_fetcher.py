"""ClaudeAgentFetcher — fetch + extract in one SDK call (Strategy C).

Uses claude_agent_sdk.query(allowed_tools=["WebFetch"]) to fetch a URL and
extract findings in a single subprocess. PoC-validated: 33% fewer tokens,
16% faster, quality parity vs two-call strategies.

Every call is wrapped in asyncio.wait_for for hard cancellation — the exact
fix for the hung-WebFetch failure mode that plagued the old prompt-driven skill.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from .models import FetchError, FetchErrorType, FetchResult
from . import sdk

log = logging.getLogger(__name__)

# PoC benchmarked Strategy C at 16.9s on a fast docs page. Real-world pages
# (gov sites, PDFs, forums) consistently take 30-50s via SDK subprocess +
# WebFetch + extraction. 60s per request accommodates heavy pages.
DEFAULT_FETCH_TIMEOUT_S = 60.0
DEFAULT_BATCH_TIMEOUT_S = 180.0


# ---------------------------------------------------------------------------
# FetcherProtocol — implemented by both HttpxFetcher and ClaudeAgentFetcher
# ---------------------------------------------------------------------------


class FetcherProtocol(Protocol):
    """Swappable fetch interface. Both HttpxFetcher and ClaudeAgentFetcher
    implement this so the pipeline can swap backends via config."""

    async def fetch_url(self, url: str, *, query: str = "", sub_questions: list[str] | None = None) -> FetchResult:
        """Fetch a single URL and return content + optionally pre-extracted findings."""
        ...

    async def fetch_batch(self, urls: list[str], *, query: str = "", sub_questions: list[str] | None = None) -> list[FetchResult]:
        """Fetch multiple URLs concurrently with per-request isolation."""
        ...


# ---------------------------------------------------------------------------
# ClaudeAgentFetcher — Strategy C (combined fetch + extract)
# ---------------------------------------------------------------------------


FETCH_EXTRACT_PROMPT = """Use the WebFetch tool on this URL: {url}

Pass WebFetch a prompt telling it to return the article content relevant to this query.

QUERY: {query}

After WebFetch returns the content, extract key findings from it.

Return a JSON object with this shape:
{{
  "content_markdown": "the main article content as clean markdown (max 5000 chars)",
  "findings": [
    {{"fact": "one-sentence claim", "quote": "direct quote or null", "confidence": "high|medium|low"}},
    ...
  ],
  "links": ["url1", "url2"]
}}

Rules:
- Limit findings to the 7 most relevant.
- Include direct quotes only when exact wording matters.
- Links should be URLs from the page worth following for deeper data.
- Return ONLY valid JSON in a fenced code block, no prose.
"""


class ClaudeAgentFetcher:
    """Fetch + extract via Claude Code SDK. One call per source."""

    def __init__(
        self,
        *,
        model: str | None = None,
        effort: str | None = None,
        request_timeout: float = DEFAULT_FETCH_TIMEOUT_S,
        batch_timeout: float = DEFAULT_BATCH_TIMEOUT_S,
    ):
        self.model = model
        self.effort = effort
        self.request_timeout = request_timeout
        self.batch_timeout = batch_timeout

    async def fetch_url(
        self,
        url: str,
        *,
        query: str = "",
        sub_questions: list[str] | None = None,
    ) -> FetchResult:
        """Strategy C: one SDK call with WebFetch + extraction."""
        start = time.monotonic()

        prompt = FETCH_EXTRACT_PROMPT.format(
            url=url,
            query=query or "Extract the main content and key facts.",
        )
        if sub_questions:
            prompt += "\n\nSub-questions to focus on:\n" + "\n".join(
                f"- {q}" for q in sub_questions
            )

        # Extract domain for labeling
        try:
            from urllib.parse import urlparse
            _domain = urlparse(url).netloc or url[:40]
        except Exception:
            _domain = url[:40]

        try:
            # Pass the request timeout INTO llm_call (which now uses
            # hard_timeout internally) rather than wrapping in an outer
            # asyncio.wait_for. An outer wait_for would re-introduce the
            # cancel-and-await hang: cancelling a wedged llm_call could block
            # the outer wait_for forever. One robust timeout, enforced in sdk.
            raw = await sdk.llm_call(
                prompt,
                model=self.model,
                effort=self.effort,
                allowed_tools=["WebFetch"],
                max_turns=3,
                call_timeout=self.request_timeout,
                label=f"fetch:{_domain}",
            )
        except sdk.LLMCallTimeoutError:
            elapsed = time.monotonic() - start
            log.warning("ClaudeAgentFetcher timeout for %s after %.1fs", url, elapsed)
            return FetchResult(
                url=url,
                success=False,
                error=FetchError(
                    url=url,
                    error_type=FetchErrorType.TIMEOUT,
                    message=f"SDK call exceeded {self.request_timeout}s",
                    elapsed_seconds=elapsed,
                ),
                elapsed_seconds=elapsed,
            )
        except sdk.LLMCallError as e:
            elapsed = time.monotonic() - start
            log.warning("ClaudeAgentFetcher error for %s: %s", url, e)
            return FetchResult(
                url=url,
                success=False,
                error=FetchError(
                    url=url,
                    error_type=FetchErrorType.UNKNOWN,
                    message=str(e),
                    elapsed_seconds=elapsed,
                ),
                elapsed_seconds=elapsed,
            )

        elapsed = time.monotonic() - start

        # Parse the response — expect JSON with content_markdown, findings, links
        try:
            data = sdk.extract_json(raw)
            if not isinstance(data, dict):
                data = {}
        except (ValueError, Exception):  # noqa: BLE001
            # If JSON parse fails, treat the raw text as content
            data = {"content_markdown": raw, "findings": [], "links": []}

        content = data.get("content_markdown", raw) or raw
        links = data.get("links", []) or []
        # findings are carried in the FetchResult for read.py to pick up
        # without needing a separate LLM call
        findings_raw = data.get("findings", []) or []

        result = FetchResult(
            url=url,
            success=True,
            content=content if isinstance(content, str) else str(content),
            elapsed_seconds=elapsed,
            extracted_links=links[:10] if isinstance(links, list) else [],
        )
        # Attach pre-extracted findings as extra data — read.py checks for this
        result._pre_extracted_findings = findings_raw  # type: ignore[attr-defined]
        return result

    async def fetch_batch(
        self,
        urls: list[str],
        *,
        query: str = "",
        sub_questions: list[str] | None = None,
    ) -> list[FetchResult]:
        """Concurrent fetch with per-request isolation and batch timeout."""
        tasks = [
            asyncio.create_task(
                self.fetch_url(url, query=query, sub_questions=sub_questions)
            )
            for url in urls
        ]

        # asyncio.wait (NOT wait_for): it returns (done, pending) when the
        # timeout elapses and does NOT await cancellation of pending tasks. A
        # wedged subprocess can therefore leak in the background but can never
        # block the batch from returning. Each fetch_url already enforces its
        # own per-request timeout via sdk.hard_timeout; this is the backstop.
        done, pending = await asyncio.wait(tasks, timeout=self.batch_timeout)
        if pending:
            log.warning(
                "ClaudeAgentFetcher batch timeout at %.1fs (%d/%d still pending)",
                self.batch_timeout, len(pending), len(tasks),
            )
        raw_results = []
        for t in tasks:
            if t in done:
                try:
                    raw_results.append(t.result())
                except Exception as e:  # noqa: BLE001
                    raw_results.append(e)
            else:
                t.cancel()  # fire-and-forget; do NOT await
                t.add_done_callback(sdk._swallow_task_result)
                raw_results.append(
                    FetchResult(
                        url="<cancelled>",
                        success=False,
                        error=FetchError(
                            url="<cancelled>",
                            error_type=FetchErrorType.TIMEOUT,
                            message="batch timeout",
                            elapsed_seconds=self.batch_timeout,
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
                    )
                )
            else:
                results.append(r)

        return results


# ---------------------------------------------------------------------------
# HttpxFetcher — wraps existing free functions
# ---------------------------------------------------------------------------


class HttpxFetcher:
    """Wraps the existing fetcher.py free functions as a FetcherProtocol impl."""

    async def fetch_url(
        self,
        url: str,
        *,
        query: str = "",
        sub_questions: list[str] | None = None,
    ) -> FetchResult:
        from . import fetcher
        import httpx

        async with httpx.AsyncClient(
            headers={"User-Agent": fetcher.USER_AGENT},
            timeout=httpx.Timeout(fetcher.DEFAULT_REQUEST_TIMEOUT_S),
        ) as client:
            return await fetcher.fetch_url(url, client)

    async def fetch_batch(
        self,
        urls: list[str],
        *,
        query: str = "",
        sub_questions: list[str] | None = None,
    ) -> list[FetchResult]:
        from . import fetcher

        return await fetcher.fetch_batch(urls)
