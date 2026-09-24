from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from fixture_repo import build  # noqa: E402

FIXTURE = TESTS / "fixtures" / "findings.example.json"


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Keep the live-viewer index and container detection out of the real machine."""
    monkeypatch.setenv("REVIEW_VIEWER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MULTIPLAI_CONTAINER", "0")
    monkeypatch.delenv("REVIEW_VIEWER_HOST", raising=False)
    monkeypatch.delenv("REVIEW_VIEWER_URL_HOST", raising=False)


@pytest.fixture(scope="session")
def fixture_repo(tmp_path_factory) -> tuple[Path, str, str]:
    repo = tmp_path_factory.mktemp("repo") / "fixture-repo"
    base, head = build(repo)
    return repo, base, head


@pytest.fixture
def findings_path(tmp_path, fixture_repo) -> Path:
    """The example findings, copied into a fresh review directory and pointed
    at the fixture repo built for this run."""
    repo, base, head = fixture_repo
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert (data["target"]["base_sha"], data["target"]["head_sha"]) == (base, head), \
        "fixture_repo.py no longer produces the shas the fixture names"
    data["target"]["repo_path"] = str(repo)
    review = tmp_path / "review"
    review.mkdir()
    out = review / "findings.json"
    out.write_text(json.dumps(data), encoding="utf-8")
    return out


# --- a live server in a thread --------------------------------------------------

import socket  # noqa: E402
import threading  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
from dataclasses import dataclass  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Live:
    viewer: object
    thread: threading.Thread
    box: Path
    findings: Path

    @property
    def port(self) -> int:
        return self.viewer.port

    @property
    def token(self) -> str:
        return self.viewer.token

    @property
    def slug(self) -> str:
        return next(iter(self.viewer.targets))

    def request(self, method: str, route: str, body=None, *, token: str | None = "",
                headers: dict | None = None, ctype: str = "application/json"):
        """(status, parsed json or raw bytes). token="" means use the real one."""
        data = None
        hdrs = dict(headers or {})
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            hdrs["Content-Type"] = ctype
        if token == "":
            token = self.token
        if token is not None:
            hdrs["X-Review-Token"] = token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{route}", data=data,
                                     method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as err:
            status, raw = err.code, err.read()
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw


@pytest.fixture
def start_live(findings_path):
    from review_viewer import netinfo, server

    started: list[Live] = []

    def _start(*, idle: float = 0, session_id: str = "sess-A", path: Path | None = None) -> Live:
        from review_viewer.models import load_findings
        fpath = path or findings_path
        ff = load_findings(fpath)
        viewer = server.build_viewer([(ff, fpath.parent / "viewer")], agent="Claude Test",
                                     session_id=session_id, idle_minutes=idle)
        port = free_port()
        assert server.bind(viewer, "127.0.0.1", port, 1) is not None
        server.publish(viewer, netinfo.display_urls(port))
        t = threading.Thread(target=server.run, args=(viewer,), daemon=True)
        t.start()
        live = Live(viewer, t, (fpath.parent / "viewer").resolve(), fpath)
        started.append(live)
        return live

    yield _start
    for live in started:
        if not live.viewer.stopped.is_set():
            live.viewer.httpd.shutdown()
        live.thread.join(5)
