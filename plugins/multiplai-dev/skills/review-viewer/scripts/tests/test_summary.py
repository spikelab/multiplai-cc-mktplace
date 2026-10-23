"""`summary put|status` and the `/summary` route.

Stop and resume rows of `plan-viewer-drop-walkthrough-2026-10-22.md`: S1 (a put
killed before its atomic replace leaves the previous file; `put` again gives the
same result) and S2 (a hand-edited file that no longer parses is served as
absent; the next put replaces it).
"""

from __future__ import annotations

import copy
import json
import subprocess
from sys import executable as PYTHON

import pytest
from review_viewer import summary
from review_viewer.models import Summary, load_findings


@pytest.fixture
def ff(findings_path):
    return load_findings(findings_path)


@pytest.fixture
def complete(ff) -> dict:
    t = ff.target
    return {
        "schema_version": 1,
        "generated_at": "2026-09-26T10:00:00Z",
        "base_sha": t.base_sha,
        "head_sha": t.head_sha,
        "assessments": [
            {
                "topic": "commits",
                "verdict": "good",
                "title": "Messages explain it",
                "detail_md": "Each commit says why.",
            },
            {
                "topic": "tests",
                "verdict": "concern",
                "title": "Refunds untested",
                "detail_md": "`refund()` has no test.",
            },
        ],
        "risk": {
            "tier": 3,
            "tier_why": "refunds move money",
            "revertable": True,
            "revert_why": "no migration or external call",
        },
        "complete": True,
    }


def errors_for(data, ff) -> list[str]:
    return summary.check(Summary.model_validate(data), ff)


def test_complete_summary_passes(complete, ff):
    assert errors_for(complete, ff) == []


def test_a_summary_holds_no_prose_about_the_code(complete):
    for field in ("overview_md", "steps", "skipped"):
        with pytest.raises(ValueError):
            Summary.model_validate({**complete, field: "x"})


def test_rule1_shas_must_match_the_served_target(complete, ff):
    complete["head_sha"] = "0" * 40
    errs = errors_for(complete, ff)
    assert len(errs) == 1 and "are not the served target's" in errs[0]


def test_rule6_complete_needs_commits_and_tests_assessments(complete, ff):
    complete["assessments"] = [
        a for a in complete["assessments"] if a["topic"] != "tests"
    ]
    assert errors_for(complete, ff) == [
        "summary: complete is true but there is no 'tests' assessment"
    ]
    complete["complete"] = False
    assert errors_for(complete, ff) == []


def test_rule6_one_assessment_per_topic_except_other(complete, ff):
    extra = {"topic": "other", "verdict": "note", "title": "x", "detail_md": "y"}
    complete["assessments"] += [extra, dict(extra)]
    assert errors_for(complete, ff) == []
    complete["assessments"].append(dict(complete["assessments"][0]))
    assert errors_for(complete, ff) == [
        "assessment commits: more than one assessment has this topic"
    ]


def test_complete_needs_a_risk_block(complete, ff):
    del complete["risk"]
    assert errors_for(complete, ff) == [
        "summary: complete is true but there is no 'risk' block"
    ]
    complete["complete"] = False
    assert errors_for(complete, ff) == []


def test_risk_tier_is_0_to_3(complete):
    complete["risk"]["tier"] = 4
    with pytest.raises(ValueError):
        Summary.model_validate(complete)


def test_size_and_risk_topics_load_but_put_rejects_them(complete, ff):
    complete["assessments"] += [
        {"topic": "size", "verdict": "good", "title": "Small", "detail_md": "x"},
        {"topic": "risk", "verdict": "note", "title": "Some", "detail_md": "x"},
    ]
    assert errors_for(complete, ff) == [
        "assessment size: not an assessment topic any more (size is measured from git)",
        "assessment risk: not an assessment topic any more (risk is scored from the `risk` block)",
    ]


def test_assessment_title_fits_one_line(complete, ff):
    complete["assessments"][0]["title"] = (
        "PR body is thorough; neither commit has a body"
    )
    assert errors_for(complete, ff) == [
        "assessment commits: title is 46 characters, over 32; put the rest in detail_md"
    ]
    # A file written before 0.22 with a longer title still loads.
    Summary.model_validate(complete)


def test_the_schema_advertises_the_title_limit_put_enforces():
    schema = Summary.model_json_schema()
    assert (
        schema["$defs"]["SummaryAssessment"]["properties"]["title"]["maxLength"]
        == summary.TITLE_MAX
    )


def test_assessment_verdicts_and_topics_are_closed_sets(complete):
    bad = copy.deepcopy(complete)
    bad["assessments"][0]["verdict"] = "great"
    with pytest.raises(ValueError):
        Summary.model_validate(bad)
    bad = copy.deepcopy(complete)
    bad["assessments"][0]["topic"] = "vibes"
    with pytest.raises(ValueError):
        Summary.model_validate(bad)


# --- CLI and route ------------------------------------------------------------------------


def _cli(*args, cwd=None):
    return subprocess.run(
        [PYTHON, "-m", "review_viewer", *map(str, args)],
        check=False,
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def test_put_publishes_and_the_route_serves_it(start_live, complete, tmp_path):
    """S1: a second put of the same file gives the same published summary."""
    live = start_live()
    route = f"/api/targets/{live.slug}/summary"
    assert live.request("GET", route)[0] == 404
    assert live.request("GET", route, token=None)[0] == 401
    assert live.request("GET", route, token="wrong")[0] == 401

    early = copy.deepcopy(complete)
    early["assessments"], early["complete"] = early["assessments"][:1], False
    src = tmp_path / "sm.json"
    src.write_text(json.dumps(early))
    proc = _cli("summary", "put", "--box", live.box, "--file", src)
    assert proc.returncode == 0, proc.stderr
    assert "in progress" in proc.stdout
    status, body = live.request("GET", route)
    assert status == 200 and body["complete"] is False

    status_out = _cli("summary", "status", "--box", live.box)
    assert status_out.returncode == 0
    assert (
        f"head_sha: {live.viewer.targets[live.slug].findings.target.head_sha}\n"
        in status_out.stdout
    )
    assert "assessments missing (1): tests" in status_out.stdout

    src.write_text(json.dumps(complete))
    assert _cli("summary", "put", "--box", live.box, "--file", src).returncode == 0
    first = summary.summary_path(live.box).read_text()
    assert _cli("summary", "put", "--box", live.box, "--file", src).returncode == 0
    assert summary.summary_path(live.box).read_text() == first
    status, body = live.request("GET", route)
    assert status == 200 and body["complete"] is True
    out = summary.summary_path(live.box)
    assert out == live.findings.parent / "summary.json"
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    assert (
        "assessments missing (0)" in _cli("summary", "status", "--box", live.box).stdout
    )


def test_s2_an_edited_summary_that_no_longer_parses_is_absent_until_the_next_put(
    start_live, complete, tmp_path
):
    live = start_live()
    route = f"/api/targets/{live.slug}/summary"
    src = tmp_path / "sm.json"
    src.write_text(json.dumps(complete))
    assert _cli("summary", "put", "--box", live.box, "--file", src).returncode == 0
    summary.summary_path(live.box).write_text('{"schema_version": 1, "steps": []}')
    assert live.request("GET", route)[0] == 404
    assert _cli("summary", "put", "--box", live.box, "--file", src).returncode == 0
    assert live.request("GET", route)[0] == 200


def test_put_rejects_with_exit_2_and_publishes_nothing(start_live, complete, tmp_path):
    live = start_live()
    complete["assessments"][0]["title"] = "x" * 40
    src = tmp_path / "sm.json"
    src.write_text(json.dumps(complete))
    proc = _cli("summary", "put", "--box", live.box, "--file", src)
    assert proc.returncode == 2
    assert "title is 40 characters" in proc.stderr, proc.stderr
    assert not summary.summary_path(live.box).exists()
    assert live.request("GET", f"/api/targets/{live.slug}/summary")[0] == 404


def test_put_rejects_invalid_json_shape(start_live, tmp_path):
    live = start_live()
    src = tmp_path / "sm.json"
    src.write_text('{"schema_version": 1}')
    proc = _cli("summary", "put", "--box", live.box, "--file", src)
    assert proc.returncode == 2 and "not a valid summary.json v1" in proc.stderr


def test_put_without_a_served_target(tmp_path):
    proc = _cli("summary", "put", "--box", tmp_path, "--file", tmp_path / "x.json")
    assert proc.returncode == 2 and "no served target" in proc.stderr


def test_the_walkthrough_command_is_gone(tmp_path):
    proc = _cli("walkthrough", "put", "--box", tmp_path, "--file", tmp_path / "x")
    assert proc.returncode == 2 and "invalid choice: 'walkthrough'" in proc.stderr


def test_target_detail_carries_pr_and_notice(findings_path):
    from conftest import free_port
    from review_viewer import netinfo, server

    ff = load_findings(findings_path)
    box = findings_path.parent / "viewer"
    meta = {ff.target.slug: {"pr": {"number": 7, "title": "T"}, "notice": "stale"}}
    viewer = server.build_viewer(
        [(ff, box)], agent="A", session_id="s", idle_minutes=0, meta=meta
    )
    server.bind(viewer, "127.0.0.1", free_port(), 1)
    server.publish(viewer, netinfo.display_urls(viewer.port))
    try:
        served = summary.load_served(box)
        assert served.pr == {"number": 7, "title": "T"} and served.notice == "stale"
        assert served.findings.target.head_sha == ff.target.head_sha
    finally:
        viewer.httpd.server_close()
        server.unpublish(viewer)
