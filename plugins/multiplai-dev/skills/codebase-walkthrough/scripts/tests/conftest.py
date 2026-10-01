from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

import fixture_repo  # noqa: E402

from walkthrough_pipeline import budget, fetcher  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """No real multiplai.conf, no inherited trust, a fresh unlimited ledger."""
    monkeypatch.setenv("CLAUDE_MULTIPLAI_HOME", str(tmp_path / "no-conf"))
    monkeypatch.delenv("WALKTHROUGH_TRUST_REPO", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    budget.start(None)


@pytest.fixture(scope="session")
def ws(tmp_path_factory) -> dict[str, Path]:
    return fixture_repo.build(tmp_path_factory.mktemp("ws") / "ws")


@pytest.fixture
def fresh_ws(tmp_path) -> dict[str, Path]:
    """A workspace a test may change (commits, moved lines)."""
    return fixture_repo.build(tmp_path / "ws")


@pytest.fixture
def fake_fetch(monkeypatch, ws):
    """Serve https://docs.acme.test/<path> from the fixture's acme-docs/. No network."""
    fetched: list[str] = []

    async def fetch_url(url, client, *, allowed_host=None, **kw):
        fetched.append(url)
        assert url.startswith("https://docs.acme.test/"), url
        f = ws["docs"] / url[len("https://docs.acme.test/"):]
        if not f.is_file():
            return fetcher.Fetched(url=url, status=404, error="HTTP 404")
        return fetcher.Fetched(url=url, status=200, text=f.read_text(encoding="utf-8"))

    monkeypatch.setattr(fetcher, "fetch_url", fetch_url)
    return fetched
