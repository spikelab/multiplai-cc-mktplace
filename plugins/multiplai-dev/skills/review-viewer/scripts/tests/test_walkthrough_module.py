"""A codebase-walkthrough's walkthrough.json loads as a tree target, through the viewer's own checks.

`fixtures/module-walkthrough.json` is what codebase-walkthrough's `run` writes for the
`bookings` module of its own fixture workspace; its suite fails when the two drift
(test_pipeline.py `test_the_viewer_fixture_is_what_run_writes`). This suite cannot import
that pipeline, so it rebuilds the same workspace (fixed dates, so the same commit) from
the walkthrough suite's `fixture_repo.py` and serves the module as a tree.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import jsonschema
import pytest

from review_viewer import gitdata, walkthrough
from review_viewer.models import WALKTHROUGH_SCHEMA_PATH, Walkthrough

TESTS = Path(__file__).resolve().parent
FIXTURE = TESTS / "fixtures" / "module-walkthrough.json"
WALKTHROUGH_REPO = TESTS.parents[2] / "codebase-walkthrough" / "scripts" / "tests" / "fixture_repo.py"


def _walkthrough_fixture_repo():
    # Its own name: this suite already has a `fixture_repo` module on sys.path.
    spec = importlib.util.spec_from_file_location("walkthrough_fixture_repo", WALKTHROUGH_REPO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def engine(tmp_path_factory) -> Path:
    return _walkthrough_fixture_repo().build(tmp_path_factory.mktemp("wt") / "ws")["engine"]


@pytest.fixture
def module_ff(engine):
    return gitdata.diff_findings(gitdata.tree_target(engine, "HEAD", "bookings"))


@pytest.fixture
def data() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_fixture_names_the_rebuilt_commit(module_ff, data):
    assert data["head_sha"] == module_ff.target.head_sha, \
        "codebase-walkthrough's fixture_repo.py changed: regenerate fixtures/module-walkthrough.json"
    assert data["base_sha"] == gitdata.EMPTY_TREE


def test_a_module_walkthrough_passes_the_viewer_checks(module_ff, data):
    jsonschema.validate(data, json.loads(Path(WALKTHROUGH_SCHEMA_PATH).read_text(encoding="utf-8")))
    wt = Walkthrough.model_validate(data)
    assert wt.complete and not wt.assessments and wt.risk is None
    assert walkthrough.check(wt, module_ff) == []
    assert walkthrough.coverage(wt, module_ff) == ([], [])


def test_put_publishes_a_module_walkthrough(module_ff, data, tmp_path):
    box = tmp_path / "viewer"
    box.mkdir()
    wt, errors = walkthrough.put(box, json.dumps(data), module_ff)
    assert errors == []
    assert walkthrough.load_walkthrough(box) == wt


def test_a_tree_walkthrough_must_still_cover_every_file(module_ff, data):
    gone = data["skipped"].pop()["path"]
    assert walkthrough.check(Walkthrough.model_validate(data), module_ff) == [
        f"walkthrough: complete is true but changed file {gone} has no anchor in any step and is not in skipped"]


def test_a_tree_target_needs_no_assessments_or_risk_but_a_change_does(module_ff, data):
    wt = Walkthrough.model_validate(data)
    assert walkthrough.must_assess(module_ff) == ()
    change = module_ff.model_copy(update={"target": module_ff.target.model_copy(
        update={"base_sha": module_ff.target.head_sha})})
    assert walkthrough.must_assess(change) == walkthrough.MUST_ASSESS
    errors = walkthrough.check(wt, change)
    assert "walkthrough: complete is true but there is no 'commits' assessment" in errors
    assert "walkthrough: complete is true but there is no 'risk' block" in errors
