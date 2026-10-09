from __future__ import annotations

import json

import jsonschema
import pytest

from conftest import CLAIM_HIGH, CLAIM_MEDIUM, CLAIM_REFUTED, CLAIM_REJECTED, EXPECTED_HIGH, EXPECTED_MEDIUM, SCHEMA
from review_pipeline.export import plugin_version, to_findings_file, write_findings_file


def _schema() -> dict:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def test_exported_fixture_validates_against_the_viewer_schema(canned_state, tmp_path):
    path = write_findings_file(canned_state, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.validate(data, _schema())
    assert data["schema_version"] == 1


def test_status_mapping(canned_state):
    data = to_findings_file(canned_state)
    by_claim = {f["claim"]: f for f in data["findings"]}
    assert by_claim[CLAIM_HIGH]["status"] == "confirmed"
    assert by_claim[CLAIM_MEDIUM]["status"] == "unverifiable"
    assert by_claim[CLAIM_MEDIUM]["severity"] == "LOW"  # the lowered severity is what is kept
    assert by_claim[CLAIM_REFUTED]["status"] == "refuted"
    assert by_claim[CLAIM_REJECTED]["status"] == "rejected"
    assert "quote not at cited lines" in by_claim[CLAIM_REJECTED]["verdict_reason"]
    assert by_claim[CLAIM_REJECTED]["citations"]  # rejected findings keep their citations
    assert "fix" not in by_claim[CLAIM_HIGH]


def test_expected_behaviour_only_for_shown_findings(canned_state):
    by_claim = {f["claim"]: f for f in to_findings_file(canned_state)["findings"]}
    assert by_claim[CLAIM_HIGH]["expected_behaviour"] == EXPECTED_HIGH
    assert by_claim[CLAIM_MEDIUM]["expected_behaviour"] == EXPECTED_MEDIUM
    assert by_claim[CLAIM_REFUTED]["expected_behaviour"] is None
    assert by_claim[CLAIM_REJECTED]["expected_behaviour"] is None


def test_a_merged_away_finding_is_not_exported(canned_state):
    from review_pipeline.models import Merged

    copy = canned_state.findings[0].with_location(claim="the same defect, reworded")
    canned_state.merged.append(Merged(finding=copy, into=canned_state.findings[0].id, reason="r"))
    ids = [f["id"] for f in to_findings_file(canned_state)["findings"]]
    assert copy.id not in ids and canned_state.findings[0].id in ids


def test_target_and_producer(canned_state):
    data = to_findings_file(canned_state)
    t = data["target"]
    assert (t["base_sha"], t["head_sha"]) == (canned_state.target.base_sha, canned_state.target.head_sha)
    assert t["remote_url"] == "https://github.com/example/booking-engine.git"
    assert data["producer"] == f"multiplai-dev:review {plugin_version()}"
    assert plugin_version() != "unknown"
    assert data["generated_at"].endswith("Z")


def test_written_file_has_sorted_keys_and_two_space_indent(canned_state, tmp_path):
    text = write_findings_file(canned_state, tmp_path).read_text(encoding="utf-8")
    data = json.loads(text)
    assert text == json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_ids_are_unique(canned_state):
    ids = [f["id"] for f in to_findings_file(canned_state)["findings"]]
    assert len(ids) == len(set(ids)) == 4


def _two_stage_resumed_state(canned_state):
    """find:diff-bugs and verify, with a budget stop during verify and a resume an hour later."""
    from review_pipeline.models import Interval

    def iv(a, b):
        return Interval(started_at=f"2026-10-01T{a}.000Z", ended_at=f"2026-10-01T{b}.000Z")

    canned_state.budget = {
        "calls": 3, "input_tokens": 30, "output_tokens": 6, "cache_read_tokens": 300, "cache_creation_tokens": 3,
        "cost_usd": 1.5, "max_usd": 10.0,
        "by_label": {"find:diff-bugs": 1.0, "verify": 0.5},
        "by_stage": {
            "find:diff-bugs": {"calls": 1, "input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 100,
                               "cache_creation_tokens": 1, "cost_usd": 1.0, "no_usage_calls": 0},
            "verify": {"calls": 2, "input_tokens": 20, "output_tokens": 4, "cache_read_tokens": 200,
                       "cache_creation_tokens": 2, "cost_usd": 0.5, "no_usage_calls": 0},
        },
    }
    canned_state.timings = {
        # 10:00-10:02 first run (stopped during verify), 11:00-11:01 the resume.
        "run": [iv("10:00:00", "10:02:00"), iv("11:00:00", "11:01:00")],
        "find": [iv("10:00:00", "10:01:00")],
        "find:diff-bugs": [iv("10:00:00", "10:00:40")],
        "verify": [iv("10:01:00", "10:02:00"), iv("11:00:00", "11:00:30")],
    }
    canned_state.run_config = {"stages": {"find": {"model": "session default", "effort": "session default"},
                                          "verify": {"model": "claude-x", "effort": "high"}},
                               "concurrency": 4, "max_turns": 60}
    canned_state.budget_stops = 1
    canned_state.errors = ["finder history: timed out"]
    return canned_state


def test_run_sums_its_stages_and_wall_time_leaves_out_the_gap(canned_state):
    run = to_findings_file(_two_stage_resumed_state(canned_state))["run"]
    assert run["cost_usd"] == pytest.approx(sum(s["cost_usd"] for s in run["stages"])) == pytest.approx(1.5)
    assert run["calls"] == sum(s["calls"] for s in run["stages"]) == 3
    assert run["tokens"]["total"] == sum(s["tokens"]["total"] for s in run["stages"]) == 339
    assert run["wall_seconds"] == 180  # 2 min + 1 min; the hour between runs is not counted
    assert (run["started_at"], run["ended_at"]) == ("2026-10-01T10:00:00.000Z", "2026-10-01T11:01:00.000Z")


def test_every_run_field_is_filled_from_the_state(canned_state):
    run = to_findings_file(_two_stage_resumed_state(canned_state))["run"]
    assert run["tokens"] == {"input": 30, "output": 6, "cache_read": 300, "cache_write": 3, "total": 339}
    assert (run["max_usd"], run["stopped_by_budget"], run["errors"]) == (10.0, True, 1)
    assert [s["name"] for s in run["stages"]] == ["find:diff-bugs", "verify"]  # the find interval has finder rows
    finder, verify = run["stages"]
    assert (finder["stage"], finder["wall_seconds"], finder["model"]) == ("find", 40, "session default")
    assert (verify["wall_seconds"], verify["model"], verify["effort"]) == (90, "claude-x", "high")
    assert verify["tokens"] == {"input": 20, "output": 4, "cache_read": 200, "cache_write": 2, "total": 226}
    assert run["counts"] == {"found": 4, "rejected": 1, "refuted": 1, "unverifiable": 1, "merged": 0,
                             "repeats": 0, "low_value": 0}


def test_findings_file_with_and_without_run_validates(canned_state):
    data = to_findings_file(_two_stage_resumed_state(canned_state))
    jsonschema.validate(data, _schema())
    del data["run"]
    jsonschema.validate(data, _schema())

def test_exported_checks_validate_against_the_viewer_checks_schema(canned_state, tmp_path):
    from review_pipeline.export import write_checks_file
    from review_pipeline.models import AgentCheck, GateCheck

    high = canned_state.findings[0]
    marked = {**high.citations[0].model_dump(), "gate": "pass", "seen": "diff"}
    canned_state.checks = [AgentCheck(
        stage="find", subject="diff-bugs", given=["diff (3 files)"],
        calls=[{"tool": "Read", "target": "rateplan_service.py", "detail": "lines 1-6"}], outcome="1 finding",
        turns=2, cost_usd=0.3, started_at="2026-10-09T10:00:00.000Z", ended_at="2026-10-09T10:00:05.000Z",
        findings=[{"id": high.id, "claim": high.claim, "severity": high.severity,
                   "failure_scenario": high.failure_scenario, "citations": [marked], "fate": "kept"}])]
    canned_state.gate_checks = [GateCheck(finding_id=high.id, gate="finding_gate", passed=True)]
    data = json.loads(write_checks_file(canned_state, tmp_path).read_text(encoding="utf-8"))
    jsonschema.validate(data, json.loads((SCHEMA.parent / "checks.v1.schema.json").read_text(encoding="utf-8")))


def test_run_counts_repeats_and_low_value_from_the_assessments(canned_state):
    from review_pipeline.models import Assessment

    a, b = canned_state.findings[0], canned_state.findings[1]
    canned_state.assessments = {a.id: Assessment(label="repeat", reason="r"),
                                b.id: Assessment(label="low-value", reason="[covered] r")}
    data = to_findings_file(canned_state)
    assert (data["run"]["counts"]["repeats"], data["run"]["counts"]["low_value"]) == (1, 1)
    jsonschema.validate(data, _schema())
