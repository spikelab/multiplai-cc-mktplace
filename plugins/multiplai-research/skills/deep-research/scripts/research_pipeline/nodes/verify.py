"""VERIFY node — per-claim verdicts that close the verification loop.

After the verification read gathers new findings, this node judges each
flagged claim against ONLY those new findings and returns machine-readable
verdicts. Synthesis renders them as a table with binding instructions
(refuted claims must be corrected/removed, unresolved tagged UNVERIFIED).
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from ..config import ResearchConfig
from ..models import Finding
from ..prompts.verify import VERIFY_VERDICTS_PROMPT
from ..sdk import llm_call_structured

log = logging.getLogger(__name__)


class ClaimVerdict(BaseModel):
    """Verdict for one flagged claim, grounded in verification findings."""

    claim: str
    verdict: Literal["confirmed", "refuted", "unresolved"]
    evidence: list[str] = Field(default_factory=list)


class VerifyVerdicts(BaseModel):
    """Structured response of the verdict LLM call."""

    verdicts: list[ClaimVerdict] = Field(default_factory=list)


async def verify_verdicts(
    config: ResearchConfig,
    claims: list[str],
    new_findings: list[Finding],
) -> list[ClaimVerdict]:
    """Issue a per-claim verdict from the findings added by the verification read.

    Short-circuits without an LLM call when the verification read produced no
    new findings — there is no evidence to judge against, so every flagged
    claim is recorded as unresolved.
    """
    if not claims:
        return []
    if not new_findings:
        log.info(
            "VERIFY: no new findings gathered — recording %d claims unresolved",
            len(claims),
        )
        return [
            ClaimVerdict(claim=c, verdict="unresolved", evidence=[]) for c in claims
        ]

    findings_text = "\n".join(
        f"{i + 1}. [{f.confidence.value}|{f.reputation.value}] {f.fact}"
        + (f' — quote: "{f.quote}"' if f.quote else "")
        + f" (source: {f.source_title})"
        for i, f in enumerate(new_findings)
    )
    claims_text = "\n".join(f"- {c}" for c in claims)
    prompt = VERIFY_VERDICTS_PROMPT.format(claims=claims_text, findings=findings_text)

    result = await llm_call_structured(
        prompt,
        VerifyVerdicts,
        model=config.models.get("verify"),
        effort=config.effort,
        label="verify:verdicts",
    )

    counts: dict[str, int] = {}
    for v in result.verdicts:
        counts[v.verdict] = counts.get(v.verdict, 0) + 1
    log.info(
        "VERIFY: %d verdicts (%s)",
        len(result.verdicts),
        ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "none",
    )
    return result.verdicts
