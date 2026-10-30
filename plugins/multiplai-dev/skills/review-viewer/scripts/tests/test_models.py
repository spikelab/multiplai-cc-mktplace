from __future__ import annotations

import json
import subprocess
from sys import executable as PYTHON

import jsonschema
import pytest
from conftest import FIXTURE
from pydantic import ValidationError
from review_viewer.models import (
    SCHEMA_PATH,
    FindingsFile,
    finding_id,
    load_findings,
    schema_text,
)


def fixture_data() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_validates():
    ff = load_findings(FIXTURE)
    assert [f.severity for f in ff.findings] == [
        "HIGH",
        "MEDIUM",
        "LOW",
        "LOW",
        "MEDIUM",
    ]
    assert [f.status for f in ff.findings] == [
        "confirmed",
        "unverifiable",
        "refuted",
        "confirmed",
        "rejected",
    ]
    assert ff.findings[0].expected_behaviour.startswith("Items without a qty")
    assert ff.findings[2].expected_behaviour is None  # refuted: nothing to expect


def test_a_file_with_the_fix_older_reviews_wrote_still_loads():
    data = fixture_data()
    for f in data["findings"]:
        del f["expected_behaviour"]
        f["fix"] = None
    data["findings"][0]["fix"] = {
        "description": "Default the quantity to 1.",
        "patch_sketch": None,
        "open_questions": [],
        "premises": [
            {
                "statement": "Items without qty mean one",
                "kind": "external",
                "citation": None,
            }
        ],
    }
    ff = FindingsFile.model_validate(data)
    assert ff.findings[0].fix.description == "Default the quantity to 1."
    assert ff.findings[0].expected_behaviour is None


def test_a_finding_in_the_trim_section_loads_and_an_unknown_section_does_not():
    data = fixture_data()
    data["findings"][0]["section"] = "trim"
    assert FindingsFile.model_validate(data).findings[0].section == "trim"
    jsonschema.validate(data, json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    data["findings"][0]["section"] = "nits"
    with pytest.raises(ValidationError):
        FindingsFile.model_validate(data)


def test_a_finding_rated_depends_on_data_loads():
    """multiplai-dev 0.132: the review's new impact, in the model and the committed schema."""
    data = fixture_data()
    data["findings"][0]["impact"] = "depends-on-data"
    assert FindingsFile.model_validate(data).findings[0].impact == "depends-on-data"
    jsonschema.validate(data, json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def test_fixture_ids_are_stable_hashes():
    for f in load_findings(FIXTURE).findings:
        assert f.id == finding_id(f.file, f.line_start, f.claim)


def test_empty_citations_rejected():
    data = fixture_data()
    data["findings"][0]["citations"] = []
    with pytest.raises(ValidationError):
        FindingsFile.model_validate(data)


def test_unknown_top_level_key_rejected():
    data = fixture_data()
    data["surprise"] = True
    with pytest.raises(ValidationError):
        FindingsFile.model_validate(data)


def test_wrong_schema_version_rejected():
    data = fixture_data()
    data["schema_version"] = 2
    with pytest.raises(ValidationError):
        FindingsFile.model_validate(data)


def test_committed_schema_matches_fresh_export():
    assert SCHEMA_PATH.read_text(encoding="utf-8") == schema_text(), (
        "run: python -m review_viewer export-schema"
    )


def test_fixture_validates_against_committed_schema():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(fixture_data(), schema)


def test_committed_schema_rejects_empty_citations():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    data = fixture_data()
    data["findings"][1]["citations"] = []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, schema)


def _validate_cli(*paths) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, "-m", "review_viewer", "validate", *map(str, paths)],
        capture_output=True,
        text=True,
    )


def test_validate_cli_ok_and_broken(tmp_path):
    ok = _validate_cli(FIXTURE)
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.startswith(f"OK {FIXTURE} (5 findings)")
    broken = tmp_path / "broken.json"
    data = fixture_data()
    data["findings"][0]["citations"] = []
    broken.write_text(json.dumps(data), encoding="utf-8")
    bad = _validate_cli(broken)
    assert bad.returncode == 1
    assert "FAIL" in bad.stdout


# --- summary.json v1 -----------------------------------------------------------------

from review_viewer.models import (
    SUMMARY_SCHEMA_PATH,
    InboxRow,
    Summary,
    summary_schema_text,
)

SHA = "a" * 40


def summary_data() -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-09-26T10:00:00Z",
        "base_sha": SHA,
        "head_sha": "b" * 40,
        "assessments": [
            {"topic": "tests", "verdict": "note", "title": "T", "detail_md": "d"}
        ],
        "risk": {"tier": 1, "tier_why": "w", "revertable": True, "revert_why": "r"},
        "complete": False,
    }


def test_committed_summary_schema_matches_fresh_export():
    assert SUMMARY_SCHEMA_PATH.read_text(encoding="utf-8") == summary_schema_text(), (
        "run: python -m review_viewer export-schema"
    )


def test_summary_validates_against_model_and_committed_schema():
    Summary.model_validate(summary_data())
    schema = json.loads(SUMMARY_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(summary_data(), schema)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(surprise=True),
        lambda d: d.update(overview_md="prose about the code"),
        lambda d: d.update(steps=[]),
        lambda d: d["risk"].update(tier=4),
        lambda d: d["assessments"][0].update(verdict="great"),
        lambda d: d.update(schema_version=2),
        lambda d: d.update(head_sha="short"),
    ],
)
def test_summary_rejects(mutate):
    data = summary_data()
    mutate(data)
    with pytest.raises(ValidationError):
        Summary.model_validate(data)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            data, json.loads(SUMMARY_SCHEMA_PATH.read_text(encoding="utf-8"))
        )


def test_inbox_row_has_no_step_id_and_an_optional_full_walkthrough_flag():
    row = InboxRow(id="q-1", ts="t", target="t", kind="question", text="x")
    assert row.full_walkthrough is False and "step_id" not in row.model_dump()
    assert InboxRow(
        id="q-1", ts="t", target="t", kind="question", text="x", full_walkthrough=True
    ).full_walkthrough
    with pytest.raises(ValidationError):
        InboxRow(
            id="q-1", ts="t", target="t", kind="question", text="x", step_id="core"
        )


def test_findings_with_path_ids_load_and_bad_ids_are_refused(tmp_path):
    def with_paths(paths) -> dict:
        data = fixture_data()
        data["findings"][0]["paths"] = paths
        return data

    good = tmp_path / "good.json"
    good.write_text(json.dumps(with_paths(["p1", "p2"])))
    assert load_findings(good).findings[0].paths == ["p1", "p2"]
    for bad in (["p1", "p1"], [""]):
        path_ = tmp_path / "bad.json"
        path_.write_text(json.dumps(with_paths(bad)))
        with pytest.raises(ValidationError):
            load_findings(path_)
