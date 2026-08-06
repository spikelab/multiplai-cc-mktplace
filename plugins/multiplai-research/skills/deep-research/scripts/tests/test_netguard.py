"""SSRF guard: the policy itself, and that *both* fetch paths apply it.

The guard existed before these tests, but only on the httpx path and with no
test naming a blocked address — so the gap that mattered (the Claude Agent
fetcher reaching the network through WebFetch, unguarded) was invisible.
"""

from __future__ import annotations

import ipaddress
import socket

import pytest

from research_pipeline.models import FetchErrorType
from research_pipeline.netguard import (
    UnsafeURLError,
    _assert_safe_url,
    _ip_is_blocked,
)


def _resolve_to(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    """Make every hostname resolve to `ip`, without touching the network."""

    def fake_getaddrinfo(host, port, *args, **kwargs):  # type: ignore[no-untyped-def]
        family = (
            socket.AF_INET6
            if isinstance(ipaddress.ip_address(ip), ipaddress.IPv6Address)
            else socket.AF_INET
        )
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


class TestPolicy:
    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",            # loopback
            "169.254.169.254",      # cloud metadata — the canonical SSRF target
            "10.1.2.3",             # RFC1918
            "192.168.0.5",          # RFC1918
            "172.16.4.4",           # RFC1918
            "0.0.0.0",              # unspecified
            "::1",                  # IPv6 loopback
            "fd00::1",              # IPv6 ULA (is_private)
            "fe80::1",              # IPv6 link-local
            "::ffff:169.254.169.254",  # IPv4-mapped metadata
        ],
    )
    def test_blocked_addresses(self, ip: str) -> None:
        assert _ip_is_blocked(ipaddress.ip_address(ip)) is True

    @pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700::1111"])
    def test_public_addresses_allowed(self, ip: str) -> None:
        assert _ip_is_blocked(ipaddress.ip_address(ip)) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "gopher://evil.test/",
            "ftp://internal.test/",
        ],
    )
    async def test_non_http_scheme_rejected(self, url: str) -> None:
        with pytest.raises(UnsafeURLError):
            await _assert_safe_url(url)

    @pytest.mark.asyncio
    async def test_literal_ip_needs_no_dns(self) -> None:
        with pytest.raises(UnsafeURLError):
            await _assert_safe_url("http://169.254.169.254/latest/meta-data/")

    @pytest.mark.asyncio
    async def test_hostname_resolving_to_private_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _resolve_to(monkeypatch, "10.0.0.7")
        with pytest.raises(UnsafeURLError):
            await _assert_safe_url("https://looks-public.example.com/x")

    @pytest.mark.asyncio
    async def test_metadata_hostname_rejected(self) -> None:
        with pytest.raises(UnsafeURLError):
            await _assert_safe_url("http://metadata.google.internal/")

    @pytest.mark.asyncio
    async def test_public_host_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _resolve_to(monkeypatch, "93.184.216.34")
        await _assert_safe_url("https://example.com/page")

    @pytest.mark.asyncio
    async def test_unresolvable_host_fails_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Documented behaviour, pinned so a change to it is deliberate: a host
        that does not resolve cannot reach an internal target, and blocking it
        would break every offline/mocked caller in this suite."""

        def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise socket.gaierror("no such host")

        monkeypatch.setattr(socket, "getaddrinfo", boom)
        await _assert_safe_url("https://nowhere.invalid/x")


class TestBothFetchPathsApplyIt:
    """The guard is only a guard if it covers every route to the network."""

    @pytest.mark.asyncio
    async def test_claude_agent_fetcher_refuses_before_calling_the_sdk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from research_pipeline import sdk
        from research_pipeline.claude_agent_fetcher import ClaudeAgentFetcher

        called = False

        async def must_not_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal called
            called = True
            return "{}"

        monkeypatch.setattr(sdk, "llm_call", must_not_run)

        result = await ClaudeAgentFetcher().fetch_url(
            "http://169.254.169.254/latest/meta-data/"
        )

        assert called is False, "the URL reached the WebFetch-enabled prompt"
        assert result.success is False
        assert result.error is not None
        assert result.error.error_type == FetchErrorType.UNKNOWN
        assert "non-public" in result.error.message

    @pytest.mark.asyncio
    async def test_claude_agent_fetcher_rejects_hostname_resolving_private(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from research_pipeline import sdk
        from research_pipeline.claude_agent_fetcher import ClaudeAgentFetcher

        _resolve_to(monkeypatch, "192.168.1.1")

        async def must_not_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("SDK called for a private destination")

        monkeypatch.setattr(sdk, "llm_call", must_not_run)

        result = await ClaudeAgentFetcher().fetch_url("https://intranet.example.com/")
        assert result.success is False

    @pytest.mark.asyncio
    async def test_httpx_path_validates_every_redirect_hop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A public URL that 302s to metadata is the redirect variant of the
        same attack: only the first hop came from the guard's caller."""
        import research_pipeline.fetcher as fetcher_mod

        _resolve_to(monkeypatch, "93.184.216.34")  # the *first* hop is public

        class _Resp:
            def __init__(self, status: int, headers: dict[str, str]):
                self.status_code = status
                self.headers = headers

            async def __aenter__(self):  # type: ignore[no-untyped-def]
                return self

            async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
                return False

            async def aiter_bytes(self):  # type: ignore[no-untyped-def]
                yield b"never reached"

        class _Client:
            def __init__(self):
                self.seen: list[str] = []

            def stream(self, method, url, **kwargs):  # type: ignore[no-untyped-def]
                self.seen.append(url)
                return _Resp(302, {"location": "http://169.254.169.254/"})

        client = _Client()
        with pytest.raises(UnsafeURLError):
            await fetcher_mod._get_validated(client, "https://example.com/start")

        assert client.seen == ["https://example.com/start"], (
            "the redirect target was requested before being validated"
        )
