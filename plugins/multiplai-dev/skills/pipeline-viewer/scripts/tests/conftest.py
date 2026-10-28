from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from fixture_repo import PEOPLE, build
from viewer_kit.people import Person, save_people

PM_TOKEN = "tok-pm-" + "a" * 30
ENG_TOKEN = "tok-eng-" + "b" * 30
OTHER_TOKEN = "tok-other-" + "c" * 30


def people_map() -> dict[str, Person]:
    pm, eng = PEOPLE["pm"], PEOPLE["engineer"]
    return {
        PM_TOKEN: Person(name=pm[0], email=pm[1], role="pm", slack_id=pm[2]),
        ENG_TOKEN: Person(name=eng[0], email=eng[1], role="engineer", slack_id=eng[2]),
        OTHER_TOKEN: Person(
            name="Oz Onlooker", email="oz@example.org", role="engineer"
        ),
    }


GIT_IDENTITY = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTIPLAI_CONTAINER", "0")
    monkeypatch.delenv("REVIEW_VIEWER_HOST", raising=False)
    monkeypatch.delenv("REVIEW_VIEWER_URL_HOST", raising=False)
    monkeypatch.delenv("SLACK_TOKEN", raising=False)
    monkeypatch.delenv("WORKSPACE", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in GIT_IDENTITY:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)


def _make(root: Path) -> dict:
    fx = build(root)
    fx["box"].mkdir(mode=0o700)
    save_people(fx["box"] / "people.json", people_map())
    return fx


@pytest.fixture(scope="session")
def shared_fx(tmp_path_factory) -> dict:
    """Read-only tests share one fixture; nothing may write to it."""
    import os

    old = os.environ.get("HOME")
    home = tmp_path_factory.mktemp("home")
    os.environ["HOME"] = str(home)
    saved = {v: os.environ.pop(v) for v in GIT_IDENTITY if v in os.environ}
    try:
        return _make(tmp_path_factory.mktemp("shared"))
    finally:
        if old is not None:
            os.environ["HOME"] = old
        os.environ.update(saved)


@pytest.fixture
def fx(tmp_path) -> dict:
    """A fresh fixture for a test that writes."""
    return _make(tmp_path / "fx")
