"""SSRF guard — refuse URLs that resolve to a non-public destination.

Lives in its own module because two fetch paths need it: the httpx path
(`fetcher.py`, which also revalidates every redirect hop) and the Claude Agent
path (`claude_agent_fetcher.py`, which reaches the network through WebFetch).
A guard that only covers one of them is not a guard.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------
#
# URLs handled here originate from search results and links scraped off fetched
# pages — i.e. attacker-influenceable. Without a guard, a crafted result/link
# (or an HTTP redirect) can point the fetcher at cloud metadata
# (169.254.169.254), loopback, or RFC1918 hosts → credential theft / internal
# SSRF (CWE-918). We therefore, before *every* request and on *every* redirect
# hop: restrict the scheme to http/https, and resolve the host and reject it if
# any resolved address is loopback / link-local / private / reserved / ULA, or
# the host is a known metadata name.
#
# Residual risk: DNS rebinding (host resolves to a public IP at check time and
# an internal IP at connect time) is not fully closed here — that needs pinning
# the connection to the validated IP. Documented, out of scope for this guard.

_BLOCKED_HOSTNAMES = {"metadata.google.internal", "metadata"}


class UnsafeURLError(Exception):
    """A URL targets a disallowed scheme or a non-public host (SSRF guard)."""


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    # IPv4-mapped / -compatible IPv6 (e.g. ::ffff:169.254.169.254) must be
    # unwrapped and re-checked, else they slip past the flags above.
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None and _ip_is_blocked(mapped):
            return True
    return False


async def _assert_safe_url(url: str) -> None:
    """Raise UnsafeURLError if `url` is not an http(s) URL to a public host.

    Fails *open* on DNS resolution failure: a host that does not resolve cannot
    be used to reach an internal target, and blocking it would break offline /
    mocked callers. Any host that *does* resolve is checked.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError(f"scheme not allowed: {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise UnsafeURLError("URL has no host")
    if host.lower() in _BLOCKED_HOSTNAMES:
        raise UnsafeURLError(f"blocked host: {host}")

    try:
        candidates = [ipaddress.ip_address(host)]
    except ValueError:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM
            )
        except socket.gaierror:
            log.debug("SSRF guard: %s did not resolve — allowing", host)
            return
        candidates = [ipaddress.ip_address(info[4][0]) for info in infos]

    for ip in candidates:
        if _ip_is_blocked(ip):
            raise UnsafeURLError(f"host {host} resolves to non-public address {ip}")



