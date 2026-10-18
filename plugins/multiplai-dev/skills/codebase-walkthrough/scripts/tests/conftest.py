from __future__ import annotations

import copy
import sys
from pathlib import Path

import code_facts
import code_facts.paths
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


# multiplai-core 0.21: a step with no configured model or effort runs on
# MULTIPLAI_MODEL / MULTIPLAI_EFFORT, else the Claude Code session's own
# (multiplai_core.session). Run inside a session, the developer's values would
# set a test's model, so clear them and hide the developer's multiplai.conf
# from that lookup. A test that wants one sets it with monkeypatch.
_SESSION_DEFAULT_VARS = (
    "MULTIPLAI_MODEL",
    "MULTIPLAI_EFFORT",
    "MULTIPLAI_SESSION_MODEL",
    "MULTIPLAI_SESSION_EFFORT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_EFFORT",
)


@pytest.fixture(autouse=True)
def _no_session_defaults(monkeypatch):
    for key in _SESSION_DEFAULT_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("multiplai_core.session.load_multiplai_conf",
                        lambda: {"_sections": {}})


class MapPathsSpy:
    """The real `map_paths`, recording each call's arguments and the result it hands back."""

    def __init__(self, reorder=None):
        self.real = code_facts.map_paths
        self.reorder = reorder
        self.calls: list[dict] = []
        self.results: list[code_facts.PathsResult] = []

    async def __call__(self, **kw):
        self.calls.append({**kw, "changed_lines": copy.deepcopy(kw.get("changed_lines"))})
        result = await self.real(**kw)
        if self.reorder:
            result = self.reorder(result)
        self.results.append(result)
        return result


@pytest.fixture
def spy(monkeypatch):
    """The real `map_paths`, spied on, under every name the trace stage could call it by."""
    from walkthrough_pipeline.stages import trace
    s = MapPathsSpy()
    for owner in (code_facts, code_facts.paths, trace):
        monkeypatch.setattr(owner, "map_paths", s, raising=False)
    return s
