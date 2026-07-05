"""Tests for Pydantic models — validation, scoring, state transitions."""

import pytest
from build_pipeline.models import (
    ReviewScore, ReviewResult, ReviewIssue, GateResult,
    BlockInfo, BlockStatus, ArtifactInfo, ArtifactStatus, ChangeStatus,
)


class TestReviewResult:
    def test_weighted_average_basic(self):
        r = ReviewResult(scores=[
            ReviewScore(dimension="Architecture", weight=2, score=4, evidence="good"),
            ReviewScore(dimension="Tests", weight=1, score=3, evidence="ok"),
            ReviewScore(dimension="Compliance", weight=3, score=5, evidence="perfect"),
        ])
        # (4*2 + 3*1 + 5*3) / (2+1+3) = (8+3+15)/6 = 26/6 ≈ 4.33
        assert abs(r.weighted_average - 4.333) < 0.01

    def test_weighted_average_empty(self):
        r = ReviewResult(scores=[])
        assert r.weighted_average == 0.0

    def test_passed_above_threshold(self):
        r = ReviewResult(scores=[
            ReviewScore(dimension="A", weight=2, score=4, evidence=""),
            ReviewScore(dimension="B", weight=1, score=3, evidence=""),
        ])
        assert r.passed  # avg = (8+3)/3 = 3.67

    def test_failed_below_threshold(self):
        r = ReviewResult(scores=[
            ReviewScore(dimension="A", weight=2, score=3, evidence=""),
            ReviewScore(dimension="B", weight=1, score=2, evidence=""),
        ])
        assert not r.passed  # avg = (6+2)/3 = 2.67

    def test_failed_with_score_1(self):
        r = ReviewResult(scores=[
            ReviewScore(dimension="A", weight=2, score=5, evidence=""),
            ReviewScore(dimension="B", weight=1, score=1, evidence=""),
        ])
        # avg = (10+1)/3 = 3.67 — above threshold, but B=1 triggers fail
        assert not r.passed

    def test_failing_dimensions(self):
        r = ReviewResult(scores=[
            ReviewScore(dimension="A", weight=2, score=5, evidence=""),
            ReviewScore(dimension="B", weight=1, score=1, evidence=""),
            ReviewScore(dimension="C", weight=1, score=1, evidence=""),
        ])
        assert r.failing_dimensions == ["B", "C"]


class TestBlockInfo:
    def test_default_status(self):
        b = BlockInfo(number=1, name="Test", description="desc")
        assert b.status == BlockStatus.PENDING

    def test_status_transition(self):
        b = BlockInfo(number=1, name="Test", description="desc")
        b.status = BlockStatus.TESTING
        assert b.status == BlockStatus.TESTING


class TestGateResult:
    def test_passed_gate(self):
        g = GateResult(passed=True, reason="All good")
        assert g.passed
        assert g.action is None

    def test_failed_gate_with_action(self):
        g = GateResult(passed=False, reason="Bad", action="fix_it")
        assert not g.passed
        assert g.action == "fix_it"


class TestChangeStatus:
    def test_all_done(self):
        cs = ChangeStatus(
            change_name="test",
            artifacts=[
                ArtifactInfo(id="proposal", generates="proposal.md", status=ArtifactStatus.DONE),
                ArtifactInfo(id="tasks", generates="tasks.md", status=ArtifactStatus.DONE),
            ],
            is_complete=True,
        )
        assert cs.is_complete
