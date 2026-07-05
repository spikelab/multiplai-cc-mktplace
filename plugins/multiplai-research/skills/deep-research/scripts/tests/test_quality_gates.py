"""Tests for the new quality gates: critical_source_gate, write_incomplete_report,
QualityCheckResult model, fetch fallback state tracking, and stage ordering."""

from __future__ import annotations

from pathlib import Path

import pytest

from research_pipeline.gates import critical_source_gate
from research_pipeline.models import (
    Finding,
    QualityCheckResult,
    ReputationTier,
    Source,
    SourceStatus,
)
from research_pipeline.state import ResearchState, Stage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source(
    url: str,
    title: str,
    reputation: ReputationTier = ReputationTier.EMERGING,
    status: SourceStatus = SourceStatus.EXTRACTED,
    error: str | None = None,
) -> Source:
    return Source(
        url=url,
        title=title,
        snippet=title,
        reputation=reputation,
        status=status,
        error=error,
    )


def _make_finding(
    fact: str,
    source_url: str = "https://example.com",
    reputation: ReputationTier = ReputationTier.EMERGING,
    relates_to: int | None = None,
) -> Finding:
    return Finding(
        fact=fact,
        source_url=source_url,
        source_title="Example",
        reputation=reputation,
        relates_to_sub_question=relates_to,
    )


# ---------------------------------------------------------------------------
# Critical source gate
# ---------------------------------------------------------------------------


class TestCriticalSourceGate:
    def test_passes_when_no_critical_sources_failed(self) -> None:
        sources = [
            _make_source("https://blog.com", "Blog Post", ReputationTier.EMERGING),
            _make_source("https://other.com", "Other", ReputationTier.EMERGING,
                         SourceStatus.FAILED, "timeout"),
        ]
        findings = [_make_finding("some fact")]
        result = critical_source_gate(sources, findings, ["What is X?"])
        assert result.passed
        assert "No critical sources failed" in result.reason

    def test_passes_when_critical_failed_but_credible_coverage_exists(self) -> None:
        """AUTHORITATIVE source failed, but another ESTABLISHED source covers the question."""
        sources = [
            _make_source("https://docs.gov", "Official Docs", ReputationTier.AUTHORITATIVE,
                         SourceStatus.FAILED, "timeout"),
            _make_source("https://github.com/repo", "GitHub Repo", ReputationTier.ESTABLISHED),
        ]
        findings = [
            _make_finding(
                "Django PostgreSQL migration requires psycopg",
                "https://github.com/repo",
                ReputationTier.ESTABLISHED,
                relates_to=0,
            ),
        ]
        result = critical_source_gate(
            sources, findings,
            ["How to migrate Django to PostgreSQL?"],
        )
        assert result.passed
        assert "credible coverage" in result.reason

    def test_fails_when_critical_failed_and_only_emerging_coverage(self) -> None:
        """AUTHORITATIVE source failed and only EMERGING sources cover the question."""
        sources = [
            _make_source(
                "https://docs.cloud.google.com/sql", "Cloud SQL Django Guide",
                ReputationTier.AUTHORITATIVE, SourceStatus.FAILED, "timeout",
            ),
            _make_source("https://blog.example.com", "Random Blog", ReputationTier.EMERGING),
        ]
        findings = [
            _make_finding(
                "Connect Django to Cloud SQL using the connector",
                "https://blog.example.com",
                ReputationTier.EMERGING,
                relates_to=0,
            ),
        ]
        result = critical_source_gate(
            sources, findings,
            ["How to connect Django to Cloud SQL?"],
        )
        assert not result.passed
        assert result.action == "abort"
        assert len(result.metadata["uncovered_critical"]) == 1

    def test_fails_only_for_relevant_failed_sources(self) -> None:
        """Failed AUTHORITATIVE source must be relevant to the uncovered question."""
        sources = [
            _make_source(
                "https://arxiv.org/paper", "Quantum Computing Paper",
                ReputationTier.AUTHORITATIVE, SourceStatus.FAILED, "timeout",
            ),
            _make_source("https://blog.com", "Django Blog", ReputationTier.EMERGING),
        ]
        findings = [
            _make_finding(
                "Django migration steps",
                "https://blog.com",
                ReputationTier.EMERGING,
                relates_to=0,
            ),
        ]
        # The failed source is about quantum computing, not Django — should pass
        result = critical_source_gate(
            sources, findings,
            ["How to migrate Django?"],
        )
        assert result.passed

    def test_multiple_sub_questions_partial_coverage(self) -> None:
        """Gate fails if ANY sub-question lacks credible coverage due to failed source."""
        sources = [
            _make_source(
                "https://pgloader.readthedocs.io", "pgloader docs",
                ReputationTier.ESTABLISHED, SourceStatus.FAILED, "exit code 1",
            ),
            _make_source("https://github.com/django", "Django Repo", ReputationTier.ESTABLISHED),
        ]
        findings = [
            _make_finding(
                "Django supports PostgreSQL backend",
                "https://github.com/django",
                ReputationTier.ESTABLISHED,
                relates_to=0,
            ),
            # No findings for sub-question 1 from credible sources
            _make_finding(
                "pgloader handles MySQL to PostgreSQL migration",
                "https://random-blog.com",
                ReputationTier.EMERGING,
                relates_to=1,
            ),
        ]
        result = critical_source_gate(
            sources, findings,
            ["How to configure Django for PostgreSQL?",
             "How to use pgloader for data migration?"],
        )
        assert not result.passed
        # Only the pgloader question should be flagged
        assert len(result.metadata["uncovered_critical"]) == 1
        assert "pgloader" in result.metadata["uncovered_critical"][0]["question"]


# ---------------------------------------------------------------------------
# QualityCheckResult model
# ---------------------------------------------------------------------------


class TestQualityCheckResult:
    def test_go_result(self) -> None:
        r = QualityCheckResult(go=True, confidence=0.85, reasoning="Good coverage")
        assert r.go
        assert r.confidence == 0.85
        assert r.critical_gaps == []

    def test_no_go_result(self) -> None:
        r = QualityCheckResult(
            go=False, confidence=0.3,
            reasoning="Missing official docs",
            critical_gaps=["No Cloud SQL coverage"],
        )
        assert not r.go
        assert len(r.critical_gaps) == 1

    def test_defaults(self) -> None:
        r = QualityCheckResult(go=True)
        assert r.confidence == 0.5
        assert r.reasoning == ""
        assert r.critical_gaps == []


# ---------------------------------------------------------------------------
# State: new stages and fields
# ---------------------------------------------------------------------------


class TestNewStages:
    @pytest.fixture
    def fresh_state(self, tmp_path: Path) -> ResearchState:
        return ResearchState.new(
            query="test", output_file=tmp_path / "out.md", state_file=tmp_path / "state.json",
        )

    def test_critical_source_gate_stage_exists(self) -> None:
        assert Stage.CRITICAL_SOURCE_GATE_PASSED.value == "critical_source_gate_passed"

    def test_quality_check_stage_exists(self) -> None:
        assert Stage.QUALITY_CHECK_PASSED.value == "quality_check_passed"

    def test_stage_ordering(self, fresh_state: ResearchState) -> None:
        """New stages are in the correct position between existing stages."""
        fresh_state.advance_to(Stage.COVERAGE_GATE_PASSED)
        assert fresh_state.is_complete(Stage.COVERAGE_GATE_PASSED)
        assert not fresh_state.is_complete(Stage.CRITICAL_SOURCE_GATE_PASSED)

        fresh_state.advance_to(Stage.CRITICAL_SOURCE_GATE_PASSED)
        assert fresh_state.is_complete(Stage.CRITICAL_SOURCE_GATE_PASSED)
        assert not fresh_state.is_complete(Stage.REASSESS_COMPLETE)

        fresh_state.advance_to(Stage.REASSESS_GATE_PASSED)
        assert not fresh_state.is_complete(Stage.QUALITY_CHECK_PASSED)

        fresh_state.advance_to(Stage.QUALITY_CHECK_PASSED)
        assert fresh_state.is_complete(Stage.QUALITY_CHECK_PASSED)
        assert not fresh_state.is_complete(Stage.SYNTHESIZE_COMPLETE)

    def test_tavily_fallback_count_field(self, fresh_state: ResearchState) -> None:
        assert fresh_state.tavily_fallback_count == 0
        fresh_state.tavily_fallback_count = 3
        fresh_state.checkpoint()
        loaded = ResearchState.load(Path(fresh_state.state_file))
        assert loaded.tavily_fallback_count == 3

    def test_old_state_file_loads_with_new_fields(self, tmp_path: Path) -> None:
        """Existing state files without new fields should load fine (Pydantic defaults)."""
        import json

        state_file = tmp_path / "old_state.json"
        # Simulate an old state file without tavily_fallback_count
        old_data = {
            "query": "test",
            "output_file": str(tmp_path / "out.md"),
            "state_file": str(state_file),
            "started_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "stage": "coverage_gate_passed",
            "plan": None,
            "search_results": [],
            "sources": [],
            "findings": [],
            "reassessment": None,
            "total_fetches": 10,
            "sub_state_files": [],
        }
        state_file.write_text(json.dumps(old_data))
        loaded = ResearchState.load(state_file)
        assert loaded.tavily_fallback_count == 0  # default
        assert loaded.stage.value == "coverage_gate_passed"


# ---------------------------------------------------------------------------
# Incomplete report
# ---------------------------------------------------------------------------


class TestWriteIncompleteReport:
    def test_basic_output(self, tmp_path: Path) -> None:
        from research_pipeline.nodes.synthesize import write_incomplete_report
        from research_pipeline.config import PRESETS, ResearchConfig

        config = ResearchConfig(
            query="Test query",
            output_dir=tmp_path,
            preset=PRESETS["quick"],
        )
        state = ResearchState.new(
            query="Test query",
            output_file=tmp_path / "out.md",
            state_file=tmp_path / "state.json",
        )
        state.sources = [
            _make_source("https://docs.gov", "Official", ReputationTier.AUTHORITATIVE,
                         SourceStatus.FAILED, "timeout"),
        ]
        state.findings = [
            _make_finding("Some fact", reputation=ReputationTier.EMERGING),
        ]

        report = write_incomplete_report(
            config, state,
            "1 sub-question lacks credible coverage",
            {"uncovered_critical": [{"question": "How?", "failed_sources": ["https://docs.gov"]}]},
        )

        assert "INCOMPLETE" in report
        assert "Test query" in report
        assert "credible coverage" in report
        assert "https://docs.gov" in report
        assert "Suggested Next Steps" in report
