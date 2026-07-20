"""Adversarial review (challenge mode) — stress-tests a completed report.

Returns structured robustness scores plus a markdown review body. The score
table written to the -challenge.md file is generated in code from the
structured values, so file and machine-readable scores can never diverge.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..config import ResearchConfig
from ..models import Finding
from ..prompts.challenge import ADVERSARIAL_REVIEW_PROMPT
from ..sdk import llm_call_structured

log = logging.getLogger(__name__)

# Report chars fed to the reviewer — beyond this the prompt notes the cut.
MAX_REPORT_CHARS = 50_000


class ChallengeReview(BaseModel):
    """Structured adversarial review: robustness scores + markdown body."""

    evidence_strength: int = Field(ge=1, le=5)
    argument_coherence: int = Field(ge=1, le=5)
    counter_argument_resistance: int = Field(ge=1, le=5)
    weakest_claims: list[str] = Field(default_factory=list)
    review_markdown: str

    @property
    def overall(self) -> float:
        return (
            self.evidence_strength
            + self.argument_coherence
            + self.counter_argument_resistance
        ) / 3


def render_review(review: ChallengeReview) -> str:
    """Full markdown for the -challenge.md file: score table + review body."""
    lines = [
        "| Dimension | Score (1-5) |",
        "|-----------|-------------|",
        f"| Evidence strength | {review.evidence_strength} |",
        f"| Argument coherence | {review.argument_coherence} |",
        f"| Counter-argument resistance | {review.counter_argument_resistance} |",
        f"| **Overall** | **{review.overall:.1f}** |",
        "",
        "",
    ]
    return "\n".join(lines) + review.review_markdown


def _format_findings(findings: list[Finding]) -> str:
    lines = []
    for i, f in enumerate(findings):
        lines.append(
            f"{i + 1}. [{f.confidence.value}|{f.reputation.value}] {f.fact}"
            + (f' — quote: "{f.quote}"' if f.quote else "")
            + f" (source: {f.source_title})"
        )
    return "\n".join(lines) or "No findings available."


async def adversarial_review(
    config: ResearchConfig, report: str, findings: list[Finding]
) -> ChallengeReview:
    """Run the adversarial review LLM call and return the structured review.

    The deduped findings ground the reviewer: it can spot-check that the
    report's citations are actually supported by extracted evidence.
    """
    prompt = ADVERSARIAL_REVIEW_PROMPT.format(
        report=report[:MAX_REPORT_CHARS],
        findings=_format_findings(findings),
        date=config.date,
    )
    if len(report) > MAX_REPORT_CHARS:
        prompt += "\n\nNOTE: report truncated at 50,000 chars for review"

    review = await llm_call_structured(
        prompt,
        ChallengeReview,
        model=config.models.get("adversarial"),
        effort=config.effort,
        label="adversarial",
    )
    log.info(
        "ADVERSARIAL: overall=%.1f (evidence=%d coherence=%d resistance=%d), "
        "%d weakest claims, %d chars review",
        review.overall,
        review.evidence_strength,
        review.argument_coherence,
        review.counter_argument_resistance,
        len(review.weakest_claims),
        len(review.review_markdown),
    )
    return review
