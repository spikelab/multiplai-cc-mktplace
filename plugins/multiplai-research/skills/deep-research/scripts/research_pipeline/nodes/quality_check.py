"""Quality check node — pre-synthesis go/no-go assessment.

Runs after REASSESS, before SYNTHESIZE. Uses sonnet with effort="medium". Prevents synthesis on research that's completely empty,
but has a strong GO bias — aborting after expensive fetch+extract is
almost always the worse outcome.
"""

from __future__ import annotations

import logging

from ..config import ResearchConfig
from ..models import QualityCheckResult, SourceStatus
from ..prompts.quality_check import QUALITY_CHECK_PROMPT
from ..sdk import llm_call_structured
from ..state import ResearchState

log = logging.getLogger(__name__)


def _summarize_findings(state: ResearchState) -> dict:
    """Build summary stats for the quality check prompt."""
    confidence_counts: dict[str, int] = {}
    for f in state.findings:
        key = f.confidence.value
        confidence_counts[key] = confidence_counts.get(key, 0) + 1

    reputation_counts: dict[str, int] = {}
    for s in state.sources:
        if s.status == SourceStatus.EXTRACTED:
            key = s.reputation.value
            reputation_counts[key] = reputation_counts.get(key, 0) + 1

    failed = state.failed_sources()
    failed_lines = []
    for s in failed:
        failed_lines.append(f"- [{s.reputation.value}] {s.title[:60]} ({s.url}) — {s.error or 'unknown'}")

    # Coverage: for each sub-question, count findings
    coverage_lines = []
    sub_questions = state.plan.sub_questions if state.plan else []
    for i, q in enumerate(sub_questions):
        tagged = sum(1 for f in state.findings if f.relates_to_sub_question == i)
        coverage_lines.append(f"- Q{i+1}: {tagged} directly tagged findings — {q[:80]}")

    return {
        "total_findings": len(state.findings),
        "confidence_breakdown": ", ".join(f"{v} {k}" for k, v in sorted(confidence_counts.items())) or "none",
        "reputation_breakdown": ", ".join(f"{v} {k}" for k, v in sorted(reputation_counts.items())) or "none",
        "failed_count": len(failed),
        "failed_sources": "\n".join(failed_lines) if failed_lines else "None",
        "coverage_assessment": "\n".join(coverage_lines) if coverage_lines else "No sub-questions defined",
    }


async def quality_check(config: ResearchConfig, state: ResearchState) -> QualityCheckResult:
    """Cheap LLM assessment: is this research worth synthesizing?"""
    summary = _summarize_findings(state)
    sub_questions = state.plan.sub_questions if state.plan else []

    prompt = QUALITY_CHECK_PROMPT.format(
        query=config.query,
        sub_questions="\n".join(f"{i+1}. {q}" for i, q in enumerate(sub_questions)),
        **summary,
    )

    try:
        result = await llm_call_structured(
            prompt,
            QualityCheckResult,
            model=config.models.get("quality_check", "sonnet"),
            effort="medium",
            label="quality_check",
        )
        log.info(
            "Quality check: go=%s confidence=%.2f reason=%s",
            result.go, result.confidence, result.reasoning[:100],
        )
        return result
    except Exception as e:  # noqa: BLE001
        # If the quality check itself fails, default to GO — don't block
        # the pipeline on a failed quality check
        log.warning("Quality check failed, defaulting to GO: %s", e)
        return QualityCheckResult(
            go=True,
            confidence=0.0,
            reasoning=f"Quality check failed ({e}), proceeding by default",
        )
