from __future__ import annotations

import json
import subprocess
from sys import executable as PYTHON

import jsonschema
import pytest
from pydantic import ValidationError

from conftest import FIXTURE
from review_viewer.models import SCHEMA_PATH, FindingsFile, finding_id, load_findings, schema_text


def fixture_data() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_validates():
    ff = load_findings(FIXTURE)
    assert [f.severity for f in ff.findings] == ["HIGH", "MEDIUM", "LOW"]
    assert [f.status for f in ff.findings] == ["confirmed", "unverifiable", "refuted"]
    assert any(p.kind == "external" for p in ff.findings[0].fix.premises)


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
    assert SCHEMA_PATH.read_text(encoding="utf-8") == schema_text(), \
        "run: python -m review_viewer export-schema"


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
    return subprocess.run([PYTHON, "-m", "review_viewer", "validate", *map(str, paths)],
                          capture_output=True, text=True)


def test_validate_cli_ok_and_broken(tmp_path):
    ok = _validate_cli(FIXTURE)
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.startswith(f"OK {FIXTURE} (3 findings)")
    broken = tmp_path / "broken.json"
    data = fixture_data()
    data["findings"][0]["citations"] = []
    broken.write_text(json.dumps(data), encoding="utf-8")
    bad = _validate_cli(broken)
    assert bad.returncode == 1
    assert "FAIL" in bad.stdout
