"""Tests for quality gates — pure code, fast, deterministic."""

from __future__ import annotations

import pytest

from research_pipeline.gates import (
    cluster_queries,
    coverage_gate,
    deduplicate_findings,
    min_sources_gate,
    query_diversity_gate,
    reassess_gate,
)
from research_pipeline.models import Confidence, Finding, ReassessResult, ReputationTier


class TestDeduplicateFindings:
    def _make_finding(self, fact: str, confidence: str = "medium") -> Finding:
        return Finding(
            fact=fact,
            source_url="https://example.com",
            source_title="Example",
            confidence=Confidence(confidence),
        )

    def test_identical_findings_dedup_to_one(self) -> None:
        findings = [
            self._make_finding("httpx supports async HTTP requests", "high"),
            self._make_finding("httpx supports async HTTP requests", "medium"),
            self._make_finding("httpx supports async HTTP requests", "low"),
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 1
        assert result[0].confidence == Confidence.HIGH  # keeps highest

    def test_similar_findings_cluster(self) -> None:
        findings = [
            self._make_finding("httpx supports async HTTP client requests", "high"),
            self._make_finding("httpx provides async HTTP client functionality", "medium"),
            self._make_finding("python httpx async HTTP library client", "low"),
        ]
        result = deduplicate_findings(findings, similarity_threshold=0.3)
        # All three share "httpx", "async", "http", "client" — should cluster
        assert len(result) <= 2  # at most 2 clusters

    def test_distinct_findings_preserved(self) -> None:
        findings = [
            self._make_finding("httpx supports async HTTP requests"),
            self._make_finding("Django uses synchronous request handling by default"),
            self._make_finding("PostgreSQL supports JSONB column type for document storage"),
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 3  # all distinct

    def test_keeps_highest_confidence_per_cluster(self) -> None:
        findings = [
            self._make_finding("trafilatura extraction score outperforms readability extraction score", "low"),
            self._make_finding("trafilatura extraction score beats readability extraction library", "high"),
            self._make_finding("trafilatura extraction score higher than readability extraction", "medium"),
        ]
        result = deduplicate_findings(findings, similarity_threshold=0.3)
        assert len(result) == 1
        assert result[0].confidence == Confidence.HIGH

    def test_empty_input(self) -> None:
        assert deduplicate_findings([]) == []

    def test_single_finding(self) -> None:
        f = self._make_finding("one fact")
        assert deduplicate_findings([f]) == [f]

    def test_realistic_reduction(self) -> None:
        """Simulate 5 sources all reporting the same 3 facts = 15 findings → ~3."""
        findings = []
        for i in range(5):
            findings.append(self._make_finding(f"httpx supports async HTTP client library source{i}", "medium"))
            findings.append(self._make_finding(f"aiohttp has memory leak issues in version three source{i}", "high"))
            findings.append(self._make_finding(f"FastAPI is built on top of Starlette framework source{i}", "medium"))
        assert len(findings) == 15
        result = deduplicate_findings(findings, similarity_threshold=0.3)
        # Should collapse to roughly 3 clusters
        assert len(result) <= 6  # generous bound
        assert len(result) >= 3  # at least the 3 distinct facts


class TestQueryClustering:
    def test_similar_queries_cluster_together(self) -> None:
        queries = [
            "python async http client library",
            "python async http client best practices",
            "python async http client benchmark",
        ]
        clusters = cluster_queries(queries, similarity_threshold=0.4)
        assert len(clusters) == 1

    def test_distinct_queries_cluster_separately(self) -> None:
        queries = [
            "python async http client",
            "react component lifecycle hooks",
            "postgres index optimization",
        ]
        clusters = cluster_queries(queries)
        assert len(clusters) == 3

    def test_mixed_queries(self) -> None:
        queries = [
            "python httpx async",
            "python httpx timeout",  # similar to above
            "go net http client",
            "postgres vacuum",
        ]
        clusters = cluster_queries(queries, similarity_threshold=0.3)
        # httpx queries cluster, go and postgres are separate
        assert len(clusters) == 3


class TestQueryDiversityGate:
    def test_passes_with_enough_clusters(self) -> None:
        queries = [
            "python async http",
            "react hooks",
            "postgres tuning",
        ]
        result = query_diversity_gate(queries, min_clusters=3)
        assert result.passed
        assert "3 clusters" in result.reason

    def test_fails_with_too_few_clusters(self) -> None:
        queries = [
            "python async http",
            "python asynchronous http",
            "python http async",
        ]
        result = query_diversity_gate(queries, min_clusters=3, similarity_threshold=0.3)
        assert not result.passed
        assert result.action == "expand_queries"

    def test_empty_queries_fails(self) -> None:
        result = query_diversity_gate([])
        assert not result.passed
        assert result.action == "expand_queries"


class TestMinSourcesGate:
    def test_passes_at_threshold(self) -> None:
        result = min_sources_gate(source_count=10, min_sources=10)
        assert result.passed

    def test_passes_above_threshold(self) -> None:
        result = min_sources_gate(source_count=15, min_sources=10)
        assert result.passed

    def test_fails_below_threshold(self) -> None:
        result = min_sources_gate(source_count=3, min_sources=10)
        assert not result.passed
        assert result.action == "retry_search"
        assert "3/10" in result.reason


class TestCoverageGate:
    def test_all_covered_by_keyword_overlap(self) -> None:
        sub_questions = [
            "What are the performance characteristics of httpx?",
            "How does React handle component lifecycle?",
        ]
        findings = [
            Finding(
                fact="httpx has good performance with connection pooling",
                source_url="https://a",
                source_title="A",
                reputation=ReputationTier.ESTABLISHED,
            ),
            Finding(
                fact="React uses lifecycle methods for component initialization",
                source_url="https://b",
                source_title="B",
                reputation=ReputationTier.ESTABLISHED,
            ),
        ]
        result = coverage_gate(findings, sub_questions)
        assert result.passed

    def test_uncovered_question_fails(self) -> None:
        sub_questions = [
            "What are httpx performance characteristics?",
            "How does golang handle concurrency patterns?",
        ]
        findings = [
            Finding(
                fact="httpx uses connection pooling for performance",
                source_url="https://a",
                source_title="A",
            ),
        ]
        result = coverage_gate(findings, sub_questions)
        assert not result.passed
        assert result.action == "targeted_search"
        assert "golang" in str(result.metadata.get("uncovered_questions", []))

    def test_explicit_sub_question_tag(self) -> None:
        """Findings tagged with relates_to_sub_question count as covering."""
        sub_questions = ["Q1", "Q2"]
        findings = [
            Finding(
                fact="unrelated keywords here",
                source_url="https://a",
                source_title="A",
                relates_to_sub_question=0,
            ),
            Finding(
                fact="more unrelated text",
                source_url="https://b",
                source_title="B",
                relates_to_sub_question=1,
            ),
        ]
        result = coverage_gate(findings, sub_questions)
        assert result.passed

    def test_no_sub_questions_passes_vacuously(self) -> None:
        result = coverage_gate([], [])
        assert result.passed


class TestReassessGate:
    def test_none_passes(self) -> None:
        result = reassess_gate(None)
        assert result.passed

    def test_clean_reassessment_passes(self) -> None:
        reassessment = ReassessResult()  # all defaults = no issues
        result = reassess_gate(reassessment)
        assert result.passed

    def test_refinement_triggered(self) -> None:
        reassessment = ReassessResult(
            framing_wrong_question=True,
            refinement_needed=True,
            refinement_queries=["new query 1", "new query 2"],
        )
        result = reassess_gate(reassessment)
        assert not result.passed
        assert "refine" in result.action  # type: ignore[operator]

    def test_verify_triggered(self) -> None:
        reassessment = ReassessResult(
            load_bearing_claims=["claim X is critical"],
            verify_claims=["claim X"],
            verify_queries=["specific fact check query"],
        )
        result = reassess_gate(reassessment)
        assert not result.passed
        assert "verify" in result.action  # type: ignore[operator]

    def test_both_refinement_and_verify(self) -> None:
        reassessment = ReassessResult(
            refinement_needed=True,
            refinement_queries=["q1"],
            verify_claims=["c1"],
            verify_queries=["vq1"],
        )
        result = reassess_gate(reassessment)
        assert not result.passed
        assert "refine" in result.action  # type: ignore[operator]
        assert "verify" in result.action  # type: ignore[operator]
