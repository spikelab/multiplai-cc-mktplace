"""`needs` in findings.json: optional, validated, and served to the page as written."""

from __future__ import annotations

import json

import jsonschema
import pytest
from conftest import FIXTURE
from pydantic import ValidationError
from review_viewer.models import SCHEMA_PATH, FindingsFile

NEED = {
    "what": "GitHub's rules on main.",
    "blocks": "review",
    "cause": "lookup-failed",
    "command": "gh api repos/example/booking-engine/rules/branches/main",
    "source": "pipeline",
}


def with_needs(data: dict) -> dict:
    finding_need = {
        **NEED,
        "blocks": data["findings"][1]["id"],
        "source": "verifier",
        "cause": "no-access",
        "command": "",
        "where": "Console > Dataform > example > Workflow execution logs",
    }
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
    assert (
        ff.needs[0].command.startswith("gh api")
        and ff.findings[1].needs[0].cause == "no-access"
    )


@pytest.mark.parametrize(
    "bad", [{"cause": "forbidden"}, {"source": "user"}, {"extra": 1}]
)
def test_a_malformed_need_is_rejected(bad):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["needs"] = [{**NEED, **bad}]
    with pytest.raises(ValidationError):
        FindingsFile.model_validate(data)


def test_server_serves_needs_and_a_file_without_them(start_live, findings_path):
    live = start_live()
    status, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert status == 200 and detail["findings"]["needs"] == []

    findings_path.write_text(
        json.dumps(with_needs(json.loads(findings_path.read_text()))), encoding="utf-8"
    )
    live = start_live(path=findings_path)
    status, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert status == 200
    assert detail["findings"]["needs"][0] == {
        **NEED,
        "where": "",
    }  # a need written without `where` gets ""
    served = detail["findings"]["findings"][1]["needs"][0]
    assert served["source"] == "verifier" and served["where"].startswith(
        "Console > Dataform"
    )


def _need(what, command, blocks="review", **extra) -> dict:
    return {
        "what": what,
        "blocks": blocks,
        "cause": "no-access",
        "command": command,
        "where": "",
        "source": "finder" if blocks == "review" else "verifier",
        **extra,
    }


CACHE = "aws elasticache describe-cache-clusters --cache-cluster-id example-cache"
DOCS = "gh api repos/example-org/example-app/contents/docs/api.md"
FLAGS = "kubectl get configmap flags --namespace example -o yaml"


def eleven_needs(finding_id: str) -> list[dict]:
    """Eleven needs over three distinct commands, four with none: the shape of a real review's list."""
    return [
        _need("Whether example-cache runs Redis 7.", CACHE),
        _need("Which page size the API docs give.", DOCS),
        _need("Whether the API docs still allow 500 items a page.", DOCS),
        _need("Whether the version comment on the cache is true.", CACHE),
        _need("How the billing team reads the nightly export.", ""),
        _need("Whether the two account ids are the shop's.", ""),
        _need("Whether the vendor still retries on a 429.", ""),
        _need("The engine version of example-cache, which the config claims.", CACHE),
        _need("Whether the checkout flag is on.", FLAGS),
        _need(
            "Whether the wiki page the README links exists.",
            "",
            where="The repo's wiki",
        ),
        _need("The page size the API docs give.", DOCS, blocks=finding_id),
    ]


def test_a_file_with_eleven_needs_validates_with_and_without_need_commands():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fid = data["findings"][1]["id"]
    data["needs"] = eleven_needs(fid)
    data["findings"][1]["needs"] = [data["needs"][-1]]
    jsonschema.validate(data, schema)
    assert FindingsFile.model_validate(data).need_commands == []

    data["need_commands"] = [
        {
            "id": "N1",
            "command": CACHE,
            "why": "Whether example-cache runs Redis 7.",
            "blocks": ["review"],
            "where": "",
            "whats": [n["what"] for n in data["needs"] if n["command"] == CACHE],
        },
        {
            "id": "N2",
            "command": DOCS,
            "why": "The page size the API docs give.",
            "blocks": [fid],
            "where": "",
            "whats": [n["what"] for n in data["needs"] if n["command"] == DOCS],
        },
        {
            "id": "N3",
            "command": FLAGS,
            "why": "Whether the checkout flag is on.",
            "blocks": ["review"],
            "where": "",
            "whats": ["Whether the checkout flag is on."],
        },
    ]
    data["findings"][0]["unchecked"] = ["Whether the vendor still caps retries."]
    jsonschema.validate(data, schema)
    ff = FindingsFile.model_validate(data)
    assert [c.id for c in ff.need_commands] == ["N1", "N2", "N3"]
    assert len(ff.needs) == 11 and ff.findings[0].unchecked == [
        "Whether the vendor still caps retries."
    ]


@pytest.mark.parametrize(
    "bad", [{"id": "X1"}, {"command": ""}, {"blocks": []}, {"extra": 1}]
)
def test_a_malformed_need_command_is_rejected(bad):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    good = {
        "id": "N1",
        "command": "gh api x",
        "why": "w",
        "blocks": ["review"],
        "whats": ["w"],
    }
    data["need_commands"] = [{**good, **bad}]
    with pytest.raises(ValidationError):
        FindingsFile.model_validate(data)
