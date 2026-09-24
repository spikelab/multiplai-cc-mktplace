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
