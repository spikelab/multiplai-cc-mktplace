"""SYNTHESIZE node — produces the final research report.

Uses map-reduce when findings exceed the SDK's CLI arg limit (~80KB).
Single-pass synthesis for smaller finding sets.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import ResearchConfig
from ..models import Finding, Source
from ..prompts.map_reduce import MAP_PROMPT, REDUCE_PROMPT
from ..prompts.synthesize import synthesis_prompt_for
from ..sdk import MAX_PROMPT_BYTES, llm_call
from ..state import ResearchState

log = logging.getLogger(__name__)

# Overhead for the synthesis template (instructions, sources, reassessment, etc.)
# Findings must fit within MAX_PROMPT_BYTES minus this overhead.
TEMPLATE_OVERHEAD_BYTES = 15_000


async def synthesize(config: ResearchConfig, state: ResearchState) -> str:
    """Run the SYNTHESIZE LLM call and return the final markdown report."""
    findings_text = _format_findings(state.findings)
    sources_text = _format_sources(state.sources)
    reassessment_text = _format_reassessment(state)

    # Check if single-pass synthesis fits within CLI arg limits
    template = synthesis_prompt_for(config.preset.summary_level)
    single_pass_prompt = template.format(
        query=config.query,
        research_type=config.research_type,
        date=config.date,
        summary_level=config.preset.summary_level,
        finding_count=len(state.findings),
        findings=findings_text,
        source_count=len(state.sources),
        sources=sources_text,
        reassessment=reassessment_text,
    )

    prompt_bytes = len(single_pass_prompt.encode("utf-8"))
    if prompt_bytes <= MAX_PROMPT_BYTES:
        log.info("SYNTHESIZE: single-pass (%d bytes, under %d limit)", prompt_bytes, MAX_PROMPT_BYTES)
        report = await llm_call(single_pass_prompt, model=config.models.get("synthesize"), effort=config.efforts.get("synthesize"), label="synthesize")
        log.info("SYNTHESIZE: report generated (%d chars)", len(report))
        return _append_failed_sources(report, state)

    # Map-reduce: split findings into chunks, synthesize each, then merge
    log.info(
        "SYNTHESIZE: prompt too large (%d bytes > %d), using map-reduce with %d findings",
        prompt_bytes, MAX_PROMPT_BYTES, len(state.findings),
    )
    report = await _map_reduce_synthesize(config, state, sources_text, reassessment_text)
    return _append_failed_sources(report, state)


async def _map_reduce_synthesize(
    config: ResearchConfig,
    state: ResearchState,
    sources_text: str,
    reassessment_text: str,
) -> str:
    """Split findings into chunks, synthesize each, then merge."""
    # Budget per chunk: leave room for MAP_PROMPT template overhead
    findings_budget = MAX_PROMPT_BYTES - TEMPLATE_OVERHEAD_BYTES
    chunks = _chunk_findings(state.findings, findings_budget)
    log.info("SYNTHESIZE MAP: %d chunks from %d findings", len(chunks), len(state.findings))

    # MAP phase: produce intermediate syntheses (parallel)
    map_tasks = []
    for i, chunk in enumerate(chunks):
        chunk_findings_text = _format_findings_list(chunk)
        # Collect sources referenced by this chunk's findings
        chunk_urls = {f.source_url for f in chunk}
        chunk_sources = [s for s in state.sources if s.url in chunk_urls]
        chunk_sources_text = _format_sources(chunk_sources)

        prompt = MAP_PROMPT.format(
            query=config.query,
            research_type=config.research_type,
            chunk_index=i + 1,
            total_chunks=len(chunks),
            finding_count=len(chunk),
            findings=chunk_findings_text,
            sources=chunk_sources_text,
        )
        map_tasks.append(llm_call(prompt, model=config.models.get("synthesize"), effort=config.efforts.get("synthesize"), label=f"synthesize:map{i}"))

    intermediate_results = await asyncio.gather(*map_tasks, return_exceptions=True)

    intermediates: list[str] = []
    for i, result in enumerate(intermediate_results):
        if isinstance(result, BaseException):
            log.warning("MAP chunk %d failed: %s", i + 1, result)
            continue
        intermediates.append(result)
        log.info("MAP chunk %d: %d chars", i + 1, len(result))

    if not intermediates:
        log.error("SYNTHESIZE MAP: all chunks failed")
        return "# Research synthesis failed\n\nAll map-reduce chunks failed during synthesis."

    # REDUCE phase: merge intermediates into final report
    # Get the output format section from the standard synthesis template
    output_format = _get_output_format(config)
    all_intermediates = "\n\n---\n\n".join(
        f"### Chunk {i+1}/{len(intermediates)}\n\n{text}"
        for i, text in enumerate(intermediates)
    )

    reduce_prompt = REDUCE_PROMPT.format(
        query=config.query,
        research_type=config.research_type,
        date=config.date,
        summary_level=config.preset.summary_level,
        total_findings=len(state.findings),
        chunk_count=len(intermediates),
        intermediate_syntheses=all_intermediates,
        source_count=len(state.sources),
        sources=sources_text,
        reassessment=reassessment_text,
        output_format=output_format,
    )

    reduce_bytes = len(reduce_prompt.encode("utf-8"))
    log.info("SYNTHESIZE REDUCE: prompt %d bytes, %d intermediates", reduce_bytes, len(intermediates))

    # If reduce prompt is also too large, recursively chunk the intermediates
    if reduce_bytes > MAX_PROMPT_BYTES:
        log.warning(
            "REDUCE prompt too large (%d bytes), truncating intermediates",
            reduce_bytes,
        )
        # Keep trimming the longest intermediate until we fit
        while reduce_bytes > MAX_PROMPT_BYTES and intermediates:
            longest_idx = max(range(len(intermediates)), key=lambda i: len(intermediates[i]))
            # Trim to ~60% of current length
            intermediates[longest_idx] = intermediates[longest_idx][: len(intermediates[longest_idx]) * 3 // 5]
            all_intermediates = "\n\n---\n\n".join(
                f"### Chunk {i+1}/{len(intermediates)}\n\n{text}"
                for i, text in enumerate(intermediates)
            )
            reduce_prompt = REDUCE_PROMPT.format(
                query=config.query,
                research_type=config.research_type,
                date=config.date,
                summary_level=config.preset.summary_level,
                total_findings=len(state.findings),
                chunk_count=len(intermediates),
                intermediate_syntheses=all_intermediates,
                source_count=len(state.sources),
                sources=sources_text,
                reassessment=reassessment_text,
                output_format=output_format,
            )
            reduce_bytes = len(reduce_prompt.encode("utf-8"))

    report = await llm_call(reduce_prompt, model=config.models.get("synthesize"), effort=config.efforts.get("synthesize"), label="synthesize:reduce")
    log.info("SYNTHESIZE REDUCE: report generated (%d chars)", len(report))
    return report


def _chunk_findings(findings: list[Finding], budget_bytes: int) -> list[list[Finding]]:
    """Split findings into chunks that each fit within budget_bytes when formatted."""
    _confidence_rank = {"high": 0, "medium": 1, "low": 2}  # matches Confidence enum
    ranked = sorted(findings, key=lambda f: _confidence_rank.get(f.confidence.value, 99))

    chunks: list[list[Finding]] = []
    current_chunk: list[Finding] = []
    current_bytes = 0

    for f in ranked:
        line = _format_single_finding(f, len(current_chunk))
        line_bytes = len(line.encode("utf-8")) + 1  # +1 for newline
        if current_bytes + line_bytes > budget_bytes and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_bytes = 0
        current_chunk.append(f)
        current_bytes += line_bytes

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def _format_single_finding(f: Finding, index: int) -> str:
    """Format a single finding as a numbered line."""
    line = (
        f"{index+1}. [{f.confidence.value}|{f.reputation.value}] {f.fact}\n"
        f"   source: {f.source_title} ({f.source_url})"
    )
    if f.quote:
        line += f'\n   quote: "{f.quote}"'
    if f.date:
        line += f"\n   date: {f.date}"
    return line


def _format_findings(findings: list[Finding]) -> str:
    """Format all findings as numbered lines, sorted by confidence."""
    _confidence_rank = {"high": 0, "medium": 1, "low": 2}  # matches Confidence enum
    ranked = sorted(findings, key=lambda f: _confidence_rank.get(f.confidence.value, 99))
    return "\n".join(_format_single_finding(f, i) for i, f in enumerate(ranked))


def _format_findings_list(findings: list[Finding]) -> str:
    """Format a list of findings (already ordered) as numbered lines."""
    return "\n".join(_format_single_finding(f, i) for i, f in enumerate(findings))


def _format_sources(sources: list[Source]) -> str:
    lines = []
    for i, s in enumerate(sources):
        lines.append(
            f"{i+1}. [{s.reputation.value}] {s.title} — {s.url}"
            + (f" ({s.published_date})" if s.published_date else "")
        )
    return "\n".join(lines)


def _format_reassessment(state: ResearchState) -> str:
    r = state.reassessment
    if r is None:
        return "Not triggered."

    lines = []
    if r.framing_wrong_question or r.new_framing_emerged or r.missing_angle:
        lines.append("Framing issues detected:")
        if r.framing_notes:
            lines.append(f"  - {r.framing_notes}")
    if r.load_bearing_claims:
        lines.append("Load-bearing claims identified:")
        for c in r.load_bearing_claims:
            lines.append(f"  - {c}")
    if r.verify_claims:
        lines.append("Claims flagged for verification:")
        for c in r.verify_claims:
            lines.append(f"  - {c}")
    if state.verdicts:
        lines.append("")
        lines.append("VERDICTS (targeted verification of flagged claims):")
        lines.append("| Claim | Verdict | Evidence |")
        lines.append("|-------|---------|----------|")
        for v in state.verdicts:
            claim = v.claim.replace("|", "/")
            evidence = "; ".join(v.evidence).replace("|", "/") if v.evidence else "—"
            lines.append(f"| {claim} | {v.verdict} | {evidence} |")
        lines.append(
            "Claims with verdict=refuted MUST be corrected or removed from the "
            "report — do not merely footnote them. Claims with verdict=unresolved "
            "MUST be tagged UNVERIFIED."
        )
    if state.refinement_error:
        lines.append(
            f"Refinement was attempted but FAILED ({state.refinement_error}) "
            "— coverage gaps flagged above remain unaddressed."
        )
    if state.verification_error:
        lines.append(
            f"Verification was attempted but FAILED ({state.verification_error}) "
            "— treat flagged claims as unverified."
        )
    if not lines:
        lines.append("Reassessment passed — no issues found.")
    return "\n".join(lines)


def _append_failed_sources(report: str, state: ResearchState) -> str:
    """Append a Failed Sources section to the report.

    This is appended by code (not generated by the LLM) so URLs are always
    accurate and the section is present even if the LLM omits it. Placed
    before the YAML appendix if one exists, otherwise at the end.
    """
    failed = state.failed_sources()
    if not failed:
        return report

    lines = [
        "",
        "## Failed Sources",
        "",
        f"{len(failed)} sources could not be fetched. These may contain "
        "information that would improve this report. Consider retrying manually.",
        "",
        "| # | Source | Error | Why It Matters |",
        "|---|--------|-------|----------------|",
    ]
    for i, s in enumerate(failed):
        error = (s.error or "unknown").replace("|", "/")
        title = (s.title or "untitled")[:60].replace("|", "/")
        lines.append(f"| {i+1} | [{title}]({s.url}) | {error} | {s.snippet[:80] if s.snippet else ''} |")

    failed_section = "\n".join(lines) + "\n"

    # Insert before YAML appendix if present, otherwise append
    yaml_marker = "<!-- STRUCTURED DATA"
    if yaml_marker in report:
        idx = report.index(yaml_marker)
        # Find the preceding --- separator
        sep_idx = report.rfind("---", 0, idx)
        if sep_idx > 0:
            return report[:sep_idx] + failed_section + "\n" + report[sep_idx:]
        return report[:idx] + failed_section + "\n" + report[idx:]
    return report + "\n" + failed_section


def _get_output_format(config: ResearchConfig) -> str:
    """Extract the output format section from the standard synthesis template.

    The REDUCE prompt needs the same output format instructions as single-pass
    synthesis, but without the findings/sources data (those come from intermediates).
    """
    from ..prompts.synthesize import (
        DETAILED_TEMPLATE,
        GIST_TEMPLATE,
        STRUCTURED_TEMPLATE,
        YAML_APPENDIX_FORMAT,
    )

    if config.preset.summary_level == "gist":
        template = GIST_TEMPLATE
    elif config.preset.summary_level == "detailed":
        template = DETAILED_TEMPLATE
    else:
        template = STRUCTURED_TEMPLATE

    # Extract the OUTPUT FORMAT section (everything after {base_header})
    # by replacing the base_header placeholder with empty string
    format_section = template.replace("{base_header}", "").replace(
        "{yaml_appendix_format}", YAML_APPENDIX_FORMAT
    )
    return format_section


# ---------------------------------------------------------------------------
# Incomplete report (early abort)
# ---------------------------------------------------------------------------


def write_incomplete_report(
    config: ResearchConfig,
    state: ResearchState,
    reason: str,
    metadata: dict,
) -> str:
    """Generate a short report explaining why research was aborted.

    Pure template — no LLM call. Used when the critical source gate or
    pre-synthesis quality check determines the research is too compromised
    to produce a useful synthesis.
    """
    failed = state.failed_sources()
    completed = state.completed_sources()

    # Count findings by confidence
    confidence_counts: dict[str, int] = {}
    for f in state.findings:
        key = f.confidence.value
        confidence_counts[key] = confidence_counts.get(key, 0) + 1

    # Count sources by reputation
    reputation_counts: dict[str, int] = {}
    for s in state.sources:
        key = s.reputation.value
        reputation_counts[key] = reputation_counts.get(key, 0) + 1

    lines = [
        f"# {config.query}",
        "",
        f"**Date:** {config.date} | **Status:** INCOMPLETE | **Sources:** "
        f"{len(completed)} extracted, {len(failed)} failed",
        "",
        "## Research Incomplete",
        "",
        f"This research was aborted before synthesis because: **{reason}**",
        "",
    ]

    # Critical gaps
    gaps = metadata.get("critical_gaps") or metadata.get("uncovered_critical") or []
    if gaps:
        lines.append("### Critical Gaps")
        lines.append("")
        for gap in gaps:
            if isinstance(gap, dict):
                q = gap.get("question", "unknown")
                urls = gap.get("failed_sources", [])
                lines.append(f"- **{q}**")
                for u in urls:
                    lines.append(f"  - Failed source: {u}")
            else:
                lines.append(f"- {gap}")
        lines.append("")

    # What was found
    lines.append("### What Was Found")
    lines.append("")
    lines.append(f"- **{len(state.findings)}** findings extracted from "
                 f"**{len(completed)}** sources")
    if confidence_counts:
        parts = [f"{v} {k}" for k, v in sorted(confidence_counts.items())]
        lines.append(f"- By confidence: {', '.join(parts)}")
    if reputation_counts:
        parts = [f"{v} {k}" for k, v in sorted(reputation_counts.items())]
        lines.append(f"- Sources by reputation: {', '.join(parts)}")
    lines.append("")

    # Failed sources table
    if failed:
        lines.append("### Failed Sources")
        lines.append("")
        lines.append(f"{len(failed)} sources could not be fetched:")
        lines.append("")
        lines.append("| # | Source | Reputation | Error |")
        lines.append("|---|--------|------------|-------|")
        for i, s in enumerate(failed):
            error = (s.error or "unknown").replace("|", "/")
            title = (s.title or "untitled")[:60].replace("|", "/")
            lines.append(
                f"| {i+1} | [{title}]({s.url}) | {s.reputation.value} | {error} |"
            )
        lines.append("")

    # Suggestion
    lines.extend([
        "### Suggested Next Steps",
        "",
        "1. Retry the research — transient fetch failures (timeouts, rate limits) "
        "may resolve on a second run",
        "2. Use `--no-claude-tools` to bypass the SDK fetcher and use httpx directly",
        "3. Use `--allow-paid-fallback` to enable paid API fallback for search",
        "4. Manually fetch the failed URLs and provide them as prior knowledge "
        "via `--prior-knowledge`",
    ])

    return "\n".join(lines) + "\n"
