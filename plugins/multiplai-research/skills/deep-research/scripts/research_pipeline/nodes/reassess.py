"""REASSESS node — LLM evaluates findings against 6 checkpoint questions.

Accepts an optional findings_override to use a deduped+capped subset instead
of the full state.findings. This keeps the context budget manageable — the
full set is preserved in state for SYNTHESIZE.

If the formatted prompt still exceeds 50KB, chunks into 2 parallel calls
and merges the results.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import ResearchConfig
from ..models import Finding, ReassessResult
from ..prompts.reassess import REASSESS_PROMPT
from ..sdk import llm_call_structured
from ..state import ResearchState

log = logging.getLogger(__name__)

MAX_REASSESS_PROMPT_BYTES = 50_000


async def reassess(
    config: ResearchConfig,
    state: ResearchState,
    findings_override: list[Finding] | None = None,
) -> ReassessResult:
    """Run the REASSESS LLM call.

    Args:
        findings_override: If provided, use this (deduped+capped) subset
            instead of state.findings. The pipeline passes a reduced set
            to manage context budget.
    """
    findings = findings_override if findings_override is not None else state.findings
    sub_questions = state.plan.sub_questions if state.plan else []

    findings_text = _format_findings(findings)
    prompt = REASSESS_PROMPT.format(
        query=config.query,
        sub_questions="\n".join(f"- {q}" for q in sub_questions),
        findings=findings_text,
    )

    # Check prompt size — chunk if too large
    prompt_bytes = len(prompt.encode("utf-8"))
    if prompt_bytes > MAX_REASSESS_PROMPT_BYTES:
        log.info(
            "REASSESS prompt too large (%d bytes > %d), chunking into 2 calls",
            prompt_bytes,
            MAX_REASSESS_PROMPT_BYTES,
        )
        return await _chunked_reassess(config, findings, sub_questions)

    result = await llm_call_structured(
        prompt,
        ReassessResult,
        model=config.models.get("reassess"),
        effort=config.efforts.get("reassess"),
        label="reassess",
    )

    log.info(
        "REASSESS: framing_issues=%s, verify_claims=%d, refinement=%s",
        result.framing_wrong_question or result.new_framing_emerged or result.missing_angle,
        len(result.verify_claims),
        result.refinement_needed,
    )
    return result


async def _chunked_reassess(
    config: ResearchConfig,
    findings: list[Finding],
    sub_questions: list[str],
) -> ReassessResult:
    """Split findings into 2 chunks, run REASSESS on each, merge results."""
    mid = len(findings) // 2
    chunk_a = findings[:mid]
    chunk_b = findings[mid:]

    log.info("REASSESS chunked: %d + %d findings", len(chunk_a), len(chunk_b))

    sub_q_text = "\n".join(f"- {q}" for q in sub_questions)

    prompt_a = REASSESS_PROMPT.format(
        query=config.query,
        sub_questions=sub_q_text,
        findings=_format_findings(chunk_a),
    )
    prompt_b = REASSESS_PROMPT.format(
        query=config.query,
        sub_questions=sub_q_text,
        findings=_format_findings(chunk_b),
    )

    result_a, result_b = await asyncio.gather(
        llm_call_structured(prompt_a, ReassessResult, model=config.models.get("reassess"), effort=config.efforts.get("reassess"), label="reassess:chunk_a"),
        llm_call_structured(prompt_b, ReassessResult, model=config.models.get("reassess"), effort=config.efforts.get("reassess"), label="reassess:chunk_b"),
    )

    # Merge: OR of booleans, union of lists
    merged = ReassessResult(
        framing_wrong_question=result_a.framing_wrong_question or result_b.framing_wrong_question,
        new_framing_emerged=result_a.new_framing_emerged or result_b.new_framing_emerged,
        missing_angle=result_a.missing_angle or result_b.missing_angle,
        framing_notes="\n".join(filter(None, [result_a.framing_notes, result_b.framing_notes])),
        load_bearing_claims=result_a.load_bearing_claims + result_b.load_bearing_claims,
        conflation_claims=result_a.conflation_claims + result_b.conflation_claims,
        convenience_bias_claims=result_a.convenience_bias_claims + result_b.convenience_bias_claims,
        refinement_needed=result_a.refinement_needed or result_b.refinement_needed,
        # sorted(): set order is randomized per process (hash seed) — unsorted
        # merges would make reruns non-deterministic downstream (query order,
        # prompt content, cache keys).
        refinement_queries=sorted(set(result_a.refinement_queries + result_b.refinement_queries)),
        verify_claims=sorted(set(result_a.verify_claims + result_b.verify_claims)),
        verify_queries=sorted(set(result_a.verify_queries + result_b.verify_queries)),
    )

    log.info(
        "REASSESS (merged): framing_issues=%s, verify_claims=%d, refinement=%s",
        merged.framing_wrong_question or merged.new_framing_emerged or merged.missing_angle,
        len(merged.verify_claims),
        merged.refinement_needed,
    )
    return merged


def _format_findings(findings: list[Finding]) -> str:
    """Format findings for inclusion in the reassess prompt."""
    lines = []
    for i, f in enumerate(findings):
        lines.append(
            f"{i+1}. [{f.confidence.value}] {f.fact} "
            f"(source: {f.source_title}, reputation: {f.reputation.value})"
        )
    return "\n".join(lines)
