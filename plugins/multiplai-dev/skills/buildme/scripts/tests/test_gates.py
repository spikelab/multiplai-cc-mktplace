"""Tests for quality gates — pure code assertions."""

import pytest
from pathlib import Path

from build_pipeline.gates import (
    agent_status_gate,
    parse_agent_status,
    red_gate,
    review_score_gate,
    review_iteration_gate,
    run_test_suite,
    wiring_task_gate,
    baseline_test_gate,
    integration_gate,
)
from build_pipeline.models import ReviewResult, ReviewScore


class TestReviewScoreGate:
    def test_passes_above_threshold(self):
        r = ReviewResult(scores=[
            ReviewScore(dimension="A", weight=2, score=4, evidence=""),
            ReviewScore(dimension="B", weight=1, score=4, evidence=""),
        ])
        result = review_score_gate(r)
        assert result.passed

    def test_fails_below_threshold(self):
        r = ReviewResult(scores=[
            ReviewScore(dimension="A", weight=2, score=2, evidence=""),
            ReviewScore(dimension="B", weight=1, score=3, evidence=""),
        ])
        result = review_score_gate(r)
        assert not result.passed
        assert result.action == "fix_low_scores"

    def test_fails_with_dimension_at_1(self):
        r = ReviewResult(scores=[
            ReviewScore(dimension="A", weight=2, score=5, evidence=""),
            ReviewScore(dimension="B", weight=1, score=1, evidence=""),
        ])
        result = review_score_gate(r)
        assert not result.passed
        assert result.action == "fix_critical_dimension"
        assert "B" in result.metadata["failing_dimensions"]

    def test_fails_on_spec_verdict_despite_high_scores(self):
        r = ReviewResult(
            scores=[ReviewScore(dimension="A", weight=2, score=5, evidence="")],
            missing=["WHEN empty input THEN 400"],
            misunderstood=["retry semantics"],
        )
        result = review_score_gate(r)
        assert not result.passed
        assert result.action == "fix_spec_compliance"
        assert "WHEN empty input THEN 400" in result.reason
        assert result.metadata["missing"] == ["WHEN empty input THEN 400"]

    def test_extra_alone_does_not_trip_spec_verdict(self):
        r = ReviewResult(
            scores=[ReviewScore(dimension="A", weight=2, score=4, evidence="")],
            extra=["bonus flag"],
        )
        assert review_score_gate(r).passed


class TestReviewIterationGate:
    def test_within_limit(self):
        assert review_iteration_gate(0).passed
        assert review_iteration_gate(1).passed
        assert review_iteration_gate(2).passed

    def test_at_limit(self):
        result = review_iteration_gate(3)
        assert not result.passed
        assert result.action == "halt_build"

    def test_custom_limit(self):
        assert review_iteration_gate(4, max_iterations=5).passed
        assert not review_iteration_gate(5, max_iterations=5).passed


class TestWiringTaskGate:
    def test_not_app_passes(self, tmp_path):
        tasks = tmp_path / "tasks.md"
        tasks.write_text("## 1. Setup\n- [ ] 1.1 Create module\n")
        result = wiring_task_gate(tasks, tmp_path)
        assert result.passed
        assert "Not detected as app" in result.reason

    def test_app_with_wiring_task_passes(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "__main__.py").write_text("pass")
        tasks = tmp_path / "tasks.md"
        tasks.write_text("## 8. Wiring\n- [ ] Wire entry point\n")
        result = wiring_task_gate(tasks, tmp_path)
        assert result.passed

    def test_app_without_wiring_task_fails(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "__main__.py").write_text("pass")
        tasks = tmp_path / "tasks.md"
        tasks.write_text("## 1. Setup\n- [ ] 1.1 Create module\n")
        result = wiring_task_gate(tasks, tmp_path)
        assert not result.passed
        assert "wiring" in result.reason.lower()


@pytest.fixture
def trust_repo(monkeypatch):
    """Gates that execute the repo's test_command require an explicit trust opt-in."""
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")


class TestBaselineTestGate:
    def test_no_test_command_passes(self, tmp_path):
        result = baseline_test_gate("", tmp_path)
        assert result.passed

    def test_passing_tests(self, tmp_path, trust_repo):
        result = baseline_test_gate("true", tmp_path)  # 'true' command always exits 0
        assert result.passed

    def test_failing_tests(self, tmp_path, trust_repo):
        result = baseline_test_gate("false", tmp_path)  # 'false' command always exits 1
        assert not result.passed

    def test_untrusted_repo_refuses_to_run(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BUILDME_TRUST_REPO", raising=False)
        result = baseline_test_gate("true", tmp_path)
        assert not result.passed
        assert "not trusted" in result.reason


class TestRedGate:
    """RED gate: tests must fail for the right reason before implementation."""

    def test_passes_on_assertion_failure(self):
        output = "FAILED tests/test_auth.py::test_login - AssertionError: expected token"
        result = red_gate(output, 1)
        assert result.passed
        assert "right reason" in result.reason

    def test_passes_on_not_implemented(self):
        output = "FAILED tests/test_auth.py::test_login - NotImplementedError"
        assert red_gate(output, 1).passed

    def test_passes_on_missing_attribute(self):
        output = (
            "FAILED tests/test_auth.py::test_login - "
            "AttributeError: module 'auth' has no attribute 'login'"
        )
        assert red_gate(output, 1).passed

    def test_suite_passing_means_rewrite_tests(self):
        result = red_gate("5 passed in 0.3s", 0)
        assert not result.passed
        assert result.action == "rewrite_tests"

    def test_collection_error_means_fix_tests(self):
        output = "ERROR collecting tests/test_auth.py\nSyntaxError: invalid syntax"
        result = red_gate(output, 2)
        assert not result.passed
        assert result.action == "fix_tests"

    def test_syntax_error_means_fix_tests(self):
        output = "E   SyntaxError: invalid syntax (test_auth.py, line 12)"
        result = red_gate(output, 2)
        assert not result.passed
        assert result.action == "fix_tests"

    def test_unrecognized_failure_means_fix_tests(self):
        result = red_gate("something exploded unrecognizably", 1)
        assert not result.passed
        assert result.action == "fix_tests"

    def test_unrunnable_suite_means_fix_tests(self):
        """exit_code=None (untrusted repo / missing binary) is not RED proof."""
        result = red_gate("Repo not trusted", None)
        assert not result.passed
        assert result.action == "fix_tests"

    def test_passes_on_terse_lowercase_summary(self):
        """pytest -q --tb=no emits only a summary count — no FAILED lines."""
        assert red_gate("1 failed, 3 passed in 0.12s", 1).passed

    def test_passes_on_jest_style_output(self):
        """Jest/Vitest print `FAIL <file>` and a lowercase `N failed` summary —
        no AssertionError, no uppercase FAILED."""
        output = (
            "FAIL src/auth.test.js\n"
            "  ● login › returns a token\n"
            "Tests: 1 failed, 2 passed, 3 total\n"
        )
        assert red_gate(output, 1).passed

    def test_zero_failed_summary_is_not_red_proof(self):
        result = red_gate("0 failed, 5 passed", 1)
        assert not result.passed
        assert result.action == "fix_tests"

    def test_lowercase_fail_in_prose_is_not_red_proof(self):
        """`fail` in ordinary prose (vs the uppercase FAIL marker) proves nothing."""
        result = red_gate("warning: flaky tests may fail intermittently", 1)
        assert not result.passed
        assert result.action == "fix_tests"


class TestRunTestSuite:
    def test_returns_exit_code_and_output(self, tmp_path, trust_repo):
        code, output = run_test_suite("true", tmp_path)
        assert code == 0

    def test_nonzero_exit(self, tmp_path, trust_repo):
        code, _ = run_test_suite("false", tmp_path)
        assert code == 1

    def test_untrusted_repo_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BUILDME_TRUST_REPO", raising=False)
        code, output = run_test_suite("true", tmp_path)
        assert code is None
        assert "not trusted" in output

    def test_missing_binary_returns_none(self, tmp_path, trust_repo):
        code, output = run_test_suite("definitely-not-a-command-xyz", tmp_path)
        assert code is None


class TestIntegrationGate:
    def test_passing(self, tmp_path, trust_repo):
        result = integration_gate("true", tmp_path)
        assert result.passed

    def test_failing(self, tmp_path, trust_repo):
        result = integration_gate("false", tmp_path)
        assert not result.passed
        assert result.action == "spawn_fix_agent"

    def test_untrusted_repo_refuses_to_run(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BUILDME_TRUST_REPO", raising=False)
        result = integration_gate("true", tmp_path)
        assert not result.passed
        assert "not trusted" in result.reason


class TestParseAgentStatus:
    """Agents close their report with a REQUIRED STATUS slot (Item 7)."""

    def test_parses_plain_slot(self):
        assert parse_agent_status("Wrote tests.\n\nSTATUS: DONE\nTESTS_RUN: pytest\n") == "DONE"

    def test_parses_underscored_variant(self):
        assert parse_agent_status("STATUS: DONE_WITH_CONCERNS\n") == "DONE_WITH_CONCERNS"
        assert parse_agent_status("STATUS: NEEDS_CONTEXT\n") == "NEEDS_CONTEXT"
        assert parse_agent_status("STATUS: BLOCKED\n") == "BLOCKED"

    def test_parses_bold_and_bulleted_markdown(self):
        assert parse_agent_status("**STATUS:** DONE\n") == "DONE"
        assert parse_agent_status("- STATUS: BLOCKED\n") == "BLOCKED"

    def test_lowercase_is_normalized(self):
        assert parse_agent_status("status: blocked\n") == "BLOCKED"

    def test_last_occurrence_wins(self):
        """A status quoted mid-report never outranks the agent's final verdict."""
        out = "I was told to report STATUS: DONE when finished.\n\nSTATUS: BLOCKED\n"
        assert parse_agent_status(out) == "BLOCKED"

    def test_missing_slot_returns_none(self):
        assert parse_agent_status("All finished, tests pass.") is None
        assert parse_agent_status("") is None


class TestAgentStatusGate:
    def test_done_passes(self):
        r = agent_status_gate("STATUS: DONE\n", "Implementer")
        assert r.passed
        assert r.metadata["status"] == "DONE"

    def test_done_with_concerns_passes(self):
        r = agent_status_gate("STATUS: DONE_WITH_CONCERNS\nMock is thin.\n", "Implementer")
        assert r.passed
        assert r.metadata["status"] == "DONE_WITH_CONCERNS"

    def test_needs_context_fails_and_surfaces_reason(self):
        out = "The spec names Widget.render() which does not exist.\n\nSTATUS: NEEDS_CONTEXT\n"
        r = agent_status_gate(out, "TestWriter")
        assert not r.passed
        assert r.metadata["status"] == "NEEDS_CONTEXT"
        assert "TestWriter" in r.reason
        # The agent's own words reach the operator, not just the status token
        assert "Widget.render()" in r.reason
        assert r.action == "escalate_to_human"

    def test_blocked_fails(self):
        r = agent_status_gate("STATUS: BLOCKED\n", "Implementer")
        assert not r.passed
        assert r.metadata["status"] == "BLOCKED"

    def test_missing_slot_passes_but_is_reported(self):
        """Deterministic gates are the real verification — a missing slot is
        reported, not fatal."""
        r = agent_status_gate("Done, all tests pass.", "Implementer")
        assert r.passed
        assert r.metadata["status"] is None
        assert "no STATUS slot" in r.reason
