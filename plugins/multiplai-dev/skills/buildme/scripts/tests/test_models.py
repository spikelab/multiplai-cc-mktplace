"""Tests for Pydantic models — validation, scoring, state transitions."""

import pytest
from build_pipeline.models import (
    ReviewScore, ReviewResult, ReviewIssue, GateResult,
    BlockInfo, BlockStatus, ArtifactInfo, ArtifactStatus, ChangeStatus,
    FindingAdjudication, FindingVerdict, ReviewFinding, ReviewGatePolicy,
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


class TestTwoVerdictReview:
    """passed gates on BOTH verdicts: spec compliance AND score threshold."""

    def _good_scores(self):
        return [ReviewScore(dimension="A", weight=2, score=5, evidence="")]

    def test_clean_spec_verdict_passes(self):
        r = ReviewResult(scores=self._good_scores())
        assert r.spec_compliant
        assert r.passed

    def test_missing_spec_behavior_fails_despite_high_scores(self):
        r = ReviewResult(scores=self._good_scores(), missing=["WHEN empty input THEN 400"])
        assert not r.spec_compliant
        assert not r.passed

    def test_misunderstood_scenario_fails_despite_high_scores(self):
        r = ReviewResult(scores=self._good_scores(), misunderstood=["retry means backoff, not loop"])
        assert not r.spec_compliant
        assert not r.passed

    def test_extra_alone_does_not_fail_spec_verdict(self):
        r = ReviewResult(scores=self._good_scores(), extra=["added a --verbose flag"])
        assert r.spec_compliant
        assert r.passed

    def test_clean_spec_but_low_scores_still_fails(self):
        r = ReviewResult(scores=[ReviewScore(dimension="A", weight=1, score=2, evidence="")])
        assert r.spec_compliant
        assert not r.passed

    def test_passed_with_honours_a_configured_policy(self):
        """`passed` is the DEFAULT-policy view; a configured gate must win.

        With `code_review.gate` set in specs/config.yaml, a bare `passed` would
        report a verdict `review_score_gate` does not apply — the two disagree
        precisely when someone bothered to configure them.
        """
        r = ReviewResult(scores=[ReviewScore(dimension="A", weight=1, score=3, evidence="")])
        assert not r.passed  # 3.0 < the default 3.5 floor
        lenient = ReviewGatePolicy(min_weighted_average=2.5)
        assert r.passed_with(lenient)
        strict = ReviewGatePolicy(min_weighted_average=4.5)
        assert not r.passed_with(strict)

    def test_passed_with_no_policy_matches_the_property(self):
        r = ReviewResult(scores=[ReviewScore(dimension="A", weight=1, score=4, evidence="")])
        assert r.passed_with(None) == r.passed


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


class TestEffectiveScore:
    """Confidence shrinks a score toward neutral — it does not scale it down."""

    def test_full_confidence_is_the_raw_score(self):
        s = ReviewScore(dimension="A", weight=1, score=2, evidence="e", confidence=1.0)
        assert s.effective_score() == 2.0

    def test_zero_confidence_is_neutral(self):
        s = ReviewScore(dimension="A", weight=1, score=1, evidence="e", confidence=0.0)
        assert s.effective_score() == 3.5

    def test_unsure_bad_score_is_not_harsher_than_a_sure_one(self):
        """Multiplying by confidence would invert the meaning — a 40%-sure
        score of 2 would land at 0.8, a hard critical fail."""
        unsure = ReviewScore(dimension="A", weight=1, score=2, evidence="e", confidence=0.4)
        sure = ReviewScore(dimension="A", weight=1, score=2, evidence="e", confidence=1.0)
        assert unsure.effective_score() > sure.effective_score()

    def test_default_confidence_leaves_legacy_fixtures_unchanged(self):
        s = ReviewScore(dimension="A", weight=1, score=4, evidence="e")
        assert s.confidence == 1.0
        assert s.effective_score() == 4.0


class TestFindingsOrDerived:
    def test_findings_pass_through(self):
        r = ReviewResult(
            scores=[],
            findings=[ReviewFinding(claim="c", file_path="a.py", line=1)],
            issues=[ReviewIssue(dimension="D", severity="Major", description="i")],
        )
        assert [f.claim for f in r.findings_or_derived()] == ["c"]

    def test_issues_are_derived_when_findings_are_empty(self):
        """A reviewer on the older prompt still gets adjudicated."""
        r = ReviewResult(
            scores=[],
            issues=[ReviewIssue(dimension="D", severity="Critical",
                                description="bare except", file_path="a.py", line=9)],
        )
        derived = r.findings_or_derived()
        assert len(derived) == 1
        assert derived[0].claim == "bare except"
        assert derived[0].severity == "Critical"
        assert derived[0].file_path == "a.py"

    def test_nothing_to_adjudicate(self):
        assert ReviewResult(scores=[]).findings_or_derived() == []


class TestFindingDedupeKey:
    def test_same_place_same_claim_collides(self):
        a = ReviewFinding(claim="Missing   null check", file_path="a.py", line=3)
        b = ReviewFinding(claim="missing NULL check", file_path="a.py", line=3)
        assert a.dedupe_key() == b.dedupe_key()

    def test_different_line_does_not_collide(self):
        a = ReviewFinding(claim="x", file_path="a.py", line=3)
        b = ReviewFinding(claim="x", file_path="a.py", line=4)
        assert a.dedupe_key() != b.dedupe_key()


class TestFindingAdjudication:
    def test_rejected_findings_are_dropped(self):
        adj = FindingAdjudication(verdicts=[
            FindingVerdict(index=0, accepted=True),
            FindingVerdict(index=1, accepted=False, reason="misread the diff"),
        ])
        assert adj.accepted_indices(2) == {0}

    def test_unjudged_findings_stay_in_play(self):
        """Fail-safe: an adjudicator that silently drops a finding must not
        silently discard a real defect."""
        adj = FindingAdjudication(verdicts=[FindingVerdict(index=0, accepted=False)])
        assert adj.accepted_indices(3) == {1, 2}

    def test_out_of_range_verdicts_are_ignored(self):
        adj = FindingAdjudication(verdicts=[FindingVerdict(index=9, accepted=False)])
        assert adj.accepted_indices(1) == {0}

    def test_no_verdicts_accepts_everything(self):
        assert FindingAdjudication().accepted_indices(2) == {0, 1}


class TestReviewGatePolicy:
    def test_defaults_match_the_previously_hardcoded_numbers(self):
        pol = ReviewGatePolicy()
        assert pol.min_weighted_average == 3.5
        assert pol.critical_score == 1.0

    def test_passed_property_reads_the_policy(self):
        """`passed` and `review_score_gate` must not be able to drift apart."""
        r = ReviewResult(scores=[
            ReviewScore(dimension="A", weight=1, score=3, evidence="e"),
        ])
        assert not r.passed
