"""Fetch vendor doc pages: httpx, the SSRF guard on every redirect hop, a size cap.

Trimmed copy of research_pipeline/fetcher.py's `_get_validated` and
`fetch_url`. Pages listed in an `llms.txt` are Markdown already, so
trafilatura's `extract_content` is not needed; an HTML answer falls back to
`basic_tag_strip`, copied from the same module.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from .netguard import UnsafeURLError, _assert_safe_url

log = logging.getLogger(__name__)

MAX_REDIRECTS = 5
_REDIRECT_STATUS = (301, 302, 303, 307, 308)
DEFAULT_REQUEST_TIMEOUT_S = 15.0
RETRY_DELAYS = [1.0, 3.0]
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (compatible; WalkthroughPipeline/0.1; +https://github.com/spikelab/multiplai-cc-mktplace)"


@dataclass
class Fetched:
    url: str
    status: int = 0
    text: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.error


async def _get_validated(client: httpx.AsyncClient, url: str, allowed_host: str | None) -> httpx.Response:
    """GET *url*, following redirects by hand and checking every hop (scheme, public IP, host)."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        await _assert_safe_url(current)
        if allowed_host and httpx.URL(current).host != allowed_host:
            raise UnsafeURLError(f"host {httpx.URL(current).host} is not the docs host {allowed_host}")
        async with client.stream("GET", current, follow_redirects=False) as response:
            if response.status_code in _REDIRECT_STATUS and "location" in response.headers:
                current = urljoin(current, response.headers["location"])
                continue
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) >= MAX_RESPONSE_BYTES:
                    break
            response._content = bytes(body[:MAX_RESPONSE_BYTES])
            return response
    raise UnsafeURLError(f"too many redirects (> {MAX_REDIRECTS})")


def basic_tag_strip(html: str) -> str:
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


async def fetch_url(url: str, client: httpx.AsyncClient, *, allowed_host: str | None = None,
                    request_timeout: float = DEFAULT_REQUEST_TIMEOUT_S, max_retries: int = 2) -> Fetched:
    """Fetch one URL; retry timeouts, connection errors and 5xx. Never raises."""
    last = Fetched(url=url, error="not fetched")
    for attempt in range(max_retries + 1):
        try:
            resp = await asyncio.wait_for(_get_validated(client, url, allowed_host), timeout=request_timeout)
        except UnsafeURLError as e:
            return Fetched(url=url, error=f"blocked: {e}")
        except (asyncio.TimeoutError, httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError) as e:
            last = Fetched(url=url, error=f"{type(e).__name__}")
            if attempt < max_retries:
                await asyncio.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                continue
            return last
        except Exception as e:  # noqa: BLE001
            return Fetched(url=url, error=f"{type(e).__name__}: {str(e)[:200]}")
        if 500 <= resp.status_code < 600 and attempt < max_retries:
            await asyncio.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
            last = Fetched(url=url, status=resp.status_code, error=f"HTTP {resp.status_code}")
            continue
        text = resp.text
        if "html" in resp.headers.get("content-type", "") and "<html" in text[:2000].lower():
            text = basic_tag_strip(text)
        return Fetched(url=url, status=resp.status_code, text=text,
                       error="" if 200 <= resp.status_code < 300 else f"HTTP {resp.status_code}")
    return last


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_REQUEST_TIMEOUT_S)
