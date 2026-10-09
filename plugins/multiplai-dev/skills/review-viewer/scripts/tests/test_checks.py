"""checks.json beside findings.json: the contract, loading, and serving."""

from __future__ import annotations

import json
import logging

import jsonschema

from review_viewer.models import (CHECKS_SCHEMA_PATH, ChecksFile, FindingsFile, checks_schema_text,
                                  load_findings)

HIGH = "b561bd34ce"


def checks_for(findings_path) -> dict:
    ff = json.loads(findings_path.read_text(encoding="utf-8"))
    cite = {"path": "app/service.py", "line_start": 1, "line_end": 1, "quote": "x", "gate": "pass", "seen": "read"}
    return {
        "schema_version": 1, "generated_at": "2026-10-09T10:00:00Z", "producer": "multiplai-dev:review 0.26.0",
        "target": ff["target"],
        "agents": [
            {"stage": "find", "subject": "diff-bugs", "given": ["diff (2 files)"],
             "calls": [{"tool": "Read", "target": "app/service.py", "detail": "whole file"}],
             "outcome": "1 finding", "turns": 3, "cost_usd": 0.4, "started_at": "2026-10-09T10:00:00.000Z",
             "ended_at": "2026-10-09T10:00:09.000Z", "error": "",
             "findings": [{"id": HIGH, "claim": "c", "severity": "HIGH", "failure_scenario": "s",
                           "citations": [cite], "fate": "kept", "into": None, "rule": None}],
             "verdict": None},
            {"stage": "verify", "subject": HIGH, "given": [f"finding {HIGH}"], "calls": [],
             "outcome": "confirmed", "turns": 1, "cost_usd": 0.1, "started_at": "2026-10-09T10:00:10.000Z",
             "ended_at": "2026-10-09T10:00:12.000Z", "error": "", "findings": [],
             "verdict": {"status": "confirmed", "reason": "r", "citations": [cite], "lowered": False}},
        ],
        "gates": [{"finding_id": HIGH, "gate": "finding_gate", "passed": True, "rule": ""}],
    }


def test_committed_checks_schema_matches_fresh_export():
    assert CHECKS_SCHEMA_PATH.read_text(encoding="utf-8") == checks_schema_text(), \
        "run: python -m review_viewer export-schema"


def test_a_checks_file_validates_against_the_model_and_the_schema(findings_path):
    data = checks_for(findings_path)
    ChecksFile.model_validate(data)
    schema = json.loads(CHECKS_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(data, schema)


def test_findings_with_verifier_citations_validate(findings_path):
    data = json.loads(findings_path.read_text(encoding="utf-8"))
    data["findings"][0]["verifier_citations"] = [
        {"path": "app/service.py", "line_start": 1, "line_end": 1, "quote": "x"}]
    assert FindingsFile.model_validate(data).findings[0].verifier_citations[0].path == "app/service.py"


def test_the_server_serves_checks_found_beside_findings(start_live, findings_path):
    (findings_path.parent / "checks.json").write_text(json.dumps(checks_for(findings_path)), encoding="utf-8")
    live = start_live()
    status, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert status == 200
    assert [a["stage"] for a in detail["checks"]["agents"]] == ["find", "verify"]
    assert len(detail["findings"]["findings"]) == 3  # findings are served as before


def test_no_checks_file_still_serves(start_live):
    live = start_live()
    status, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert status == 200 and detail["checks"] is None


def test_an_invalid_checks_file_is_ignored_with_a_warning(start_live, findings_path, caplog):
    bad = checks_for(findings_path)
    bad["agents"][0]["calls"][0]["result"] = "file contents"  # unknown key
    (findings_path.parent / "checks.json").write_text(json.dumps(bad), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="review_viewer"):
        live = start_live()
    status, detail = live.request("GET", f"/api/targets/{live.slug}")
    assert status == 200 and detail["checks"] is None
    assert "not a valid checks.json v1" in caplog.text


def test_checks_for_other_commits_are_ignored(start_live, findings_path, caplog):
    other = checks_for(findings_path)
    other["target"]["head_sha"] = "0" * 40
    (findings_path.parent / "checks.json").write_text(json.dumps(other), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="review_viewer"):
        live = start_live()
    assert live.request("GET", f"/api/targets/{live.slug}")[1]["checks"] is None
    assert "not the reviewed commits" in caplog.text


def test_a_verifier_citation_path_may_be_opened(tmp_path, findings_path):
    from review_viewer.gitdata import allowed_paths

    data = json.loads(findings_path.read_text(encoding="utf-8"))
    data["findings"][0]["verifier_citations"] = [
        {"path": "docs/only-the-verifier-read-this.md", "line_start": 1, "line_end": 1, "quote": "x"}]
    findings_path.write_text(json.dumps(data), encoding="utf-8")
    ff = load_findings(findings_path)
    assert "docs/only-the-verifier-read-this.md" in allowed_paths(ff.target, ff)
