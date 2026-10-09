"""`needs` in findings.json: optional, validated, and served to the page as written."""

from __future__ import annotations

import json

import jsonschema
import pytest
from pydantic import ValidationError

from conftest import FIXTURE
from review_viewer.models import SCHEMA_PATH, FindingsFile

NEED = {"what": "GitHub's rules on main.", "blocks": "review", "cause": "lookup-failed",
        "command": "gh api repos/example/booking-engine/rules/branches/main", "source": "pipeline"}


def with_needs(data: dict) -> dict:
    finding_need = {**NEED, "blocks": data["findings"][1]["id"], "source": "verifier",
                    "cause": "no-access", "command": ""}
    data["findings"][1]["needs"] = [finding_need]
    data["needs"] = [NEED, finding_need]
    return data


def test_findings_file_with_and_without_needs_validates():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    plain = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert "needs" not in plain
    jsonschema.validate(plain, schema)
    assert FindingsFile.model_validate(plain).needs == []
    data = with_needs(json.loads(FIXTURE.read_text(encoding="utf-8")))
    jsonschema.validate(data, schema)
    ff = FindingsFile.model_validate(data)
    assert ff.needs[0].command.startswith("gh api") and ff.findings[1].needs[0].cause == "no-access"


@pytest.mark.parametrize("bad", [{"cause": "forbidden"}, {"source": "user"}, {"extra": 1}])
def test_a_malformed_need_is_rejected(bad):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["needs"] = [{**NEED, **bad}]
    with pytest.raises(ValidationError):
        FindingsFile.model_validate(data)


def test_server_serves_needs_and_a_file_without_them(start_live, findings_path):
    live = start_live()
    status, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert status == 200 and detail["findings"]["needs"] == []

    findings_path.write_text(json.dumps(with_needs(json.loads(findings_path.read_text()))), encoding="utf-8")
    live = start_live(path=findings_path)
    status, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert status == 200
    assert detail["findings"]["needs"][0] == NEED
    assert detail["findings"]["findings"][1]["needs"][0]["source"] == "verifier"
