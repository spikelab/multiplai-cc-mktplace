"""status.scorecard and GET /api/scorecard over `epic_fixture` (one repo,
old and new epic folders, runs with and without a buildme version)."""

from __future__ import annotations

import json

import epic_fixture
import pytest
from conftest import PM_TOKEN
from pipeline_viewer import buildme, status
from test_server import _serve


@pytest.fixture(scope="module")
def card(shared_fx, tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    epic_fixture.build(data, shared_fx["repo"])
    return status.scorecard(buildme.scorecard_sources(shared_fx["repo"], data))


def _version(card, v):
    return next(x for x in card.versions if x.version == v)


def _run(card, epic, run):
    return next(r for r in card.runs if r.epic == epic and r.run == run)


def test_epic_folders_old_and_new(shared_fx, tmp_path):
    folders = epic_fixture.build(tmp_path, shared_fx["repo"])
    assert buildme.epic_dirs(shared_fx["repo"], tmp_path) == [
        folders["shop"],
        folders["cart"],
    ]


def test_every_run_once_rows_before_summaries(card):
    assert [(r.epic, r.run, r.source) for r in card.runs] == [
        ("help-page", None, "row"),
        ("cart", "01", "row"),
        ("shop", "03", "summary"),
        ("shop", "02", "summary"),
        ("shop", "01", "summary"),
    ]
    # A review row, another repo's row, a cut line and a newer row_version
    # are not runs here; the last two are counted as skipped.
    assert card.skipped_rows == 2


def test_versions_newest_first_unknown_last(card):
    assert [v.version for v in card.versions] == ["0.157.0", "0.156.0", None]
    assert [v.runs.count for v in card.versions] == [2, 1, 2]


def test_old_runs_causes_inferred_from_text(card):
    old = _version(card, None)
    assert old.merged.count == 1  # a merged in run 01; still merged in 02
    assert old.parked.count == 2  # b in 01, and again with a new reason in 02
    assert old.failed.count == 1  # d in 02
    assert old.blocked.count == 1  # c in 01; held back the same way in 02
    assert [(c.cause, c.count, c.inferred) for c in old.causes] == [
        ("exit-code", 1, 1),
        ("merge-conflict", 1, 1),
        ("review-cap", 1, 1),
    ]
    assert old.spend_usd == pytest.approx(25.0)  # 10, then 25 - 10
    b = next(c for c in _run(card, "shop", "01").stopped if c.id == "b")
    assert (b.cause, b.inferred, b.same_as_previous_run) == ("review-cap", True, False)


def test_recorded_causes_are_not_inferred(card):
    v = _version(card, "0.156.0")
    assert (v.merged.count, v.parked.count, v.failed.count) == (1, 1, 0)
    assert [(c.cause, c.count, c.inferred) for c in v.causes] == [("spec-issues", 1, 0)]
    d = next(c for c in _run(card, "shop", "03").stopped if c.id == "d")
    # Failed in 02 with the same reason: shown on the run, not counted again.
    assert (d.cause, d.inferred, d.same_as_previous_run) == ("exit-code", False, True)
    assert v.spend_usd == pytest.approx(15.0)


def test_rows_and_single_builds(card):
    v = _version(card, "0.157.0")
    assert (v.parked.count, v.failed.count) == (1, 1)
    assert sorted((c.cause, c.count) for c in v.causes) == [
        ("SPEC:NOT_READY", 1),
        ("merge-tests-failed", 1),
    ]
    assert v.spend_usd == pytest.approx(10.0)
    build = _run(card, "help-page", None)
    assert (build.kind, build.outcome, build.cause) == (
        "build",
        "failed",
        "SPEC:NOT_READY",
    )


def test_each_count_links_to_its_runs(card):
    old = _version(card, None)
    shop01 = _run(card, "shop", "01").id
    shop02 = _run(card, "shop", "02").id
    assert [r.run for r in old.runs.refs] == [shop02, shop01]
    assert [(r.run, r.child) for r in old.parked.refs] == [
        (shop02, "b"),
        (shop01, "b"),
    ]
    conflict = next(c for c in old.causes if c.cause == "merge-conflict")
    assert [(r.run, r.child) for r in conflict.refs] == [(shop02, "b")]


def test_no_records_is_an_empty_scorecard(shared_fx, tmp_path):
    card = status.scorecard(buildme.scorecard_sources(shared_fx["repo"], tmp_path))
    assert (card.versions, card.runs, card.skipped_rows) == ([], [], 0)


def test_run_without_a_summary_is_a_run_with_nothing_known(shared_fx, tmp_path):
    folders = epic_fixture.build(tmp_path, shared_fx["repo"])
    (folders["shop"] / "runs" / "02" / "summary.json").write_text("{not json")
    (folders["shop"] / "runs" / "04").mkdir()  # killed before its summary
    card = status.scorecard(buildme.scorecard_sources(shared_fx["repo"], tmp_path))
    shop = {r.run: r for r in card.runs if r.epic == "shop"}
    assert sorted(shop) == ["01", "02", "03", "04"]
    assert (shop["02"].source, shop["02"].stopped, shop["02"].spend_usd) == (
        "run-folder",
        [],
        None,
    )
    # Run 03 is compared with run 01, the last one with a summary.
    assert shop["03"].spend_usd == pytest.approx(30.0)
    d = next(c for c in shop["03"].stopped if c.id == "d")
    assert not d.same_as_previous_run
    assert _version(card, None).runs.count == 3


def test_scorecard_route(shared_fx, tmp_path):
    epic_fixture.build(tmp_path, shared_fx["repo"])
    viewer, client = _serve(shared_fx)
    viewer.scorecard_sources = lambda repo: buildme.scorecard_sources(repo, tmp_path)
    try:
        code, body = client.get("/api/scorecard", token=PM_TOKEN)
        assert code == 200
        assert body["repo"] == buildme.repo_folder(shared_fx["repo"])
        assert [v["version"] for v in body["versions"]] == ["0.157.0", "0.156.0", None]
        assert body["generated_at"]
        code, _ = client.post("/api/scorecard", {}, token=PM_TOKEN)
        assert code == 405
    finally:
        viewer.httpd.shutdown()
        viewer.httpd.server_close()


def test_scorecard_route_without_buildme(shared_fx):
    viewer, client = _serve(shared_fx)
    try:
        code, body = client.get("/api/scorecard", token=PM_TOKEN)
        assert code == 200 and body["versions"] == [] and body["runs"] == []
    finally:
        viewer.httpd.shutdown()
        viewer.httpd.server_close()


def test_fixture_rows_are_valid_json_lines(tmp_path, shared_fx):
    epic_fixture.build(tmp_path, shared_fx["repo"])
    lines = (tmp_path / "runs.jsonl").read_text().splitlines()
    assert sum(1 for line in lines if line.endswith("}")) == len(lines) - 1
    assert json.loads(lines[0])["kind"] == "epic"
