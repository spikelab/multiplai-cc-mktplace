"""Quality gates — code assertions between pipeline stages.

Gates enforce minimums that the LLM cannot override. Each gate takes state
and returns a GateResult(passed, reason, action). Failed gates trigger
specific recovery actions that the pipeline orchestrator executes.
"""

from __future__ import annotations

import logging
import re

from .models import Finding, GateResult, ReassessResult, ReputationTier, Source, SourceStatus

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "is", "are", "was", "were",
        "be", "been", "being", "have", "has", "had", "do", "does", "did",
        "will", "would", "could", "should", "may", "might", "can", "of",
        "in", "on", "at", "to", "for", "with", "from", "by", "as", "into",
        "about", "through", "during", "before", "after", "above", "below",
        "between", "under", "up", "down", "out", "off", "over", "it", "its",
        "this", "that", "these", "those", "what", "which", "who", "whom",
        "how", "why", "when", "where", "i", "you", "he", "she", "we", "they",
        "me", "him", "her", "us", "them", "my", "your", "his", "our", "their",
        "if", "then", "else", "not", "no", "so", "than",
    }
)


def _tokenize(text: str) -> set[str]:
    """Extract significant keywords from text (lowercase, stopwords removed)."""
    words = re.findall(r"\b[a-z][a-z0-9]{2,}\b", text.lower())
    return {w for w in words if w not in STOPWORDS}


def _jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two token sets."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


# ---------------------------------------------------------------------------
# Finding deduplication (for REASSESS context budget)
# ---------------------------------------------------------------------------


def deduplicate_findings(
    findings: list[Finding], similarity_threshold: float = 0.5
) -> list[Finding]:
    """Cluster findings by keyword overlap, keep highest-confidence per cluster.

    Uses the same greedy single-linkage clustering as cluster_queries().
    When multiple findings say essentially the same thing (e.g., 5 sources all
    report "httpx supports async"), only the highest-confidence representative
    survives. The full list is preserved in state.findings for SYNTHESIZE —
    this function is ONLY for reducing the REASSESS input.

    Returns findings sorted by confidence (high first).
    """
    if not findings:
        return []

    _confidence_rank = {"high": 0, "medium": 1, "low": 2}

    clusters: list[list[Finding]] = []
    cluster_tokens: list[set[str]] = []

    for finding in findings:
        tokens = _tokenize(f"{finding.fact} {finding.quote or ''}")
        placed = False
        for i, c_tokens in enumerate(cluster_tokens):
            if _jaccard_similarity(tokens, c_tokens) >= similarity_threshold:
                clusters[i].append(finding)
                cluster_tokens[i] = c_tokens | tokens
                placed = True
                break
        if not placed:
            clusters.append([finding])
            cluster_tokens.append(tokens)

    # From each cluster, keep the highest-confidence finding
    representatives: list[Finding] = []
    for cluster in clusters:
        best = min(
            cluster,
            key=lambda f: _confidence_rank.get(f.confidence.value, 99),
        )
        representatives.append(best)

    # Sort by confidence (high first)
    representatives.sort(key=lambda f: _confidence_rank.get(f.confidence.value, 99))

    log.info(
        "deduplicate_findings: %d → %d (%d clusters, threshold=%.2f)",
        len(findings),
        len(representatives),
        len(clusters),
        similarity_threshold,
    )
    return representatives


# ---------------------------------------------------------------------------
# Query diversity gate
# ---------------------------------------------------------------------------


def cluster_queries(queries: list[str], similarity_threshold: float = 0.6) -> list[list[str]]:
    """Cluster queries by keyword overlap.

    Simple greedy single-linkage clustering: each query is added to the first
    cluster where it has similarity >= threshold with any member, else starts
    a new cluster.
    """
    clusters: list[list[str]] = []
    cluster_tokens: list[set[str]] = []

    for query in queries:
        tokens = _tokenize(query)
        placed = False
        for i, c_tokens in enumerate(cluster_tokens):
            if _jaccard_similarity(tokens, c_tokens) >= similarity_threshold:
                clusters[i].append(query)
                cluster_tokens[i] = c_tokens | tokens
                placed = True
                break
        if not placed:
            clusters.append([query])
            cluster_tokens.append(tokens)

    return clusters


def query_diversity_gate(
    queries: list[str], min_clusters: int = 3, similarity_threshold: float = 0.6
) -> GateResult:
    """Check that generated queries cover enough distinct angles."""
    if not queries:
        return GateResult(
            passed=False,
            reason="No queries generated",
            action="expand_queries",
        )

    clusters = cluster_queries(queries, similarity_threshold=similarity_threshold)

    if len(clusters) >= min_clusters:
        return GateResult(
            passed=True,
            reason=f"{len(clusters)} clusters found across {len(queries)} queries",
            metadata={"clusters": len(clusters), "queries": len(queries)},
        )

    return GateResult(
        passed=False,
        reason=f"Only {len(clusters)} clusters (need {min_clusters})",
        action="expand_queries",
        metadata={"clusters": len(clusters), "min_clusters": min_clusters},
    )


# ---------------------------------------------------------------------------
# Min sources gate
# ---------------------------------------------------------------------------


def min_sources_gate(source_count: int, min_sources: int) -> GateResult:
    """Check that enough sources survived triage."""
    if source_count >= min_sources:
        return GateResult(
            passed=True,
            reason=f"{source_count} sources (min {min_sources})",
            metadata={"sources": source_count, "min_sources": min_sources},
        )
    return GateResult(
        passed=False,
        reason=f"Only {source_count}/{min_sources} sources",
        action="retry_search",
        metadata={"sources": source_count, "min_sources": min_sources},
    )


# ---------------------------------------------------------------------------
# Coverage gate
# ---------------------------------------------------------------------------


def _finding_relates_to_question(finding: Finding, question: str) -> bool:
    """Does a finding relate to a sub-question via keyword overlap?"""
    finding_tokens = _tokenize(f"{finding.fact} {finding.quote or ''}")
    question_tokens = _tokenize(question)
    if not question_tokens:
        return True  # vacuously related
    # At least 30% of question's significant tokens appear in the finding
    overlap = finding_tokens & question_tokens
    return len(overlap) / len(question_tokens) >= 0.3


def coverage_gate(findings: list[Finding], sub_questions: list[str]) -> GateResult:
    """Check that every sub-question has at least one finding."""
    if not sub_questions:
        return GateResult(
            passed=True,
            reason="No sub-questions to cover",
        )

    uncovered: list[str] = []
    for i, question in enumerate(sub_questions):
        # Findings can be explicitly tagged or matched by keywords
        if any(f.relates_to_sub_question == i for f in findings):
            continue
        if any(_finding_relates_to_question(f, question) for f in findings):
            continue
        uncovered.append(question)

    if not uncovered:
        return GateResult(
            passed=True,
            reason=f"All {len(sub_questions)} sub-questions covered",
            metadata={"sub_questions": len(sub_questions), "findings": len(findings)},
        )

    return GateResult(
        passed=False,
        reason=f"Uncovered: {len(uncovered)}/{len(sub_questions)} sub-questions",
        action="targeted_search",
        metadata={"uncovered_questions": uncovered},
    )


# ---------------------------------------------------------------------------
# Critical source gate
# ---------------------------------------------------------------------------


CREDIBLE_TIERS = frozenset({ReputationTier.AUTHORITATIVE, ReputationTier.ESTABLISHED})

# Lower threshold for source-to-question matching because source titles/snippets
# are shorter and less descriptive than full findings.
_SOURCE_RELEVANCE_THRESHOLD = 0.2


def _source_relevant_to_question(source: Source, question: str) -> bool:
    """Does a source's title + snippet suggest relevance to a sub-question?"""
    source_tokens = _tokenize(f"{source.title} {source.snippet}")
    question_tokens = _tokenize(question)
    if not question_tokens:
        return True
    overlap = source_tokens & question_tokens
    return len(overlap) / len(question_tokens) >= _SOURCE_RELEVANCE_THRESHOLD


def critical_source_gate(
    sources: list[Source],
    findings: list[Finding],
    sub_questions: list[str],
) -> GateResult:
    """Check if failed AUTHORITATIVE/ESTABLISHED sources left fatal coverage gaps.

    Conservative gate — only aborts when:
    1. Critical sources (AUTHORITATIVE or ESTABLISHED) failed to fetch, AND
    2. The sub-questions those sources were relevant to have NO coverage
       from other credible (AUTHORITATIVE/ESTABLISHED) sources.

    This prevents spending tokens on synthesis when key official documentation
    was unreachable and no credible alternative covered the topic.
    """
    failed_critical = [
        s for s in sources
        if s.status == SourceStatus.FAILED
        and s.reputation in CREDIBLE_TIERS
    ]

    if not failed_critical:
        return GateResult(
            passed=True,
            reason="No critical sources failed",
        )

    # For each sub-question, check if it has credible coverage
    uncovered_critical: list[dict] = []
    for i, question in enumerate(sub_questions):
        # Find findings for this sub-question (same logic as coverage_gate)
        q_findings = [
            f for f in findings
            if f.relates_to_sub_question == i
            or _finding_relates_to_question(f, question)
        ]

        # Check if any finding comes from a credible source
        has_credible = any(f.reputation in CREDIBLE_TIERS for f in q_findings)
        if has_credible:
            continue

        # No credible coverage — check if any failed critical source was relevant
        relevant_failures = [
            s for s in failed_critical
            if _source_relevant_to_question(s, question)
        ]
        if relevant_failures:
            uncovered_critical.append({
                "question": question,
                "failed_sources": [s.url for s in relevant_failures],
            })

    if uncovered_critical:
        failed_urls = list({s.url for s in failed_critical})
        return GateResult(
            passed=False,
            reason=(
                f"{len(uncovered_critical)} sub-question(s) lack credible coverage "
                f"due to {len(failed_critical)} failed critical source(s)"
            ),
            action="abort",
            metadata={
                "uncovered_critical": uncovered_critical,
                "failed_critical_urls": failed_urls,
            },
        )

    return GateResult(
        passed=True,
        reason=(
            f"{len(failed_critical)} critical source(s) failed but all "
            f"sub-questions have credible coverage from other sources"
        ),
        metadata={"failed_critical_count": len(failed_critical)},
    )


# ---------------------------------------------------------------------------
# Reassess gate
# ---------------------------------------------------------------------------


def reassess_gate(reassessment: ReassessResult | None) -> GateResult:
    """Parse REASSESS output and determine if refinement/verification cycles fire."""
    if reassessment is None:
        return GateResult(passed=True, reason="No reassessment (skipped)")

    actions = []
    if reassessment.refinement_needed and reassessment.refinement_queries:
        actions.append("refine")
    if reassessment.verify_claims and reassessment.verify_queries:
        actions.append("verify")

    if not actions:
        return GateResult(passed=True, reason="Reassessment: no issues found")

    return GateResult(
        passed=False,
        reason=f"Reassessment triggered: {', '.join(actions)}",
        action="|".join(actions),  # "refine" | "verify" | "refine|verify"
        metadata={
            "refinement_queries": reassessment.refinement_queries,
            "verify_claims": reassessment.verify_claims,
            "verify_queries": reassessment.verify_queries,
        },
    )
