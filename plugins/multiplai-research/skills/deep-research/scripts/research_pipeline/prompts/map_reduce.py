"""Map-reduce synthesis prompts.

When findings exceed the SDK's CLI arg size limit (~80KB), synthesis splits
into two phases:

MAP: Each chunk of findings → condensed intermediate synthesis (preserving
     all key facts, tensions, and citations).
REDUCE: All intermediate syntheses → final report using the standard template.
"""

MAP_PROMPT = """You are the MAP phase of a research synthesis pipeline. Your job is to
condense a chunk of research findings into a faithful intermediate summary.

QUERY: {query}
RESEARCH TYPE: {research_type}

FINDINGS (chunk {chunk_index}/{total_chunks}, {finding_count} findings):
{findings}

SOURCES REFERENCED:
{sources}

Produce a condensed synthesis of these findings. Rules:
- Preserve ALL key facts, claims, and tensions — do not drop information
- Keep inline citations: [Title](url)
- Note confidence levels for each claim (VERIFIED/LIKELY/UNVERIFIED)
- Note any contradictions between findings
- Group related findings into coherent paragraphs
- Target length: 1,500-2,500 words (be thorough, not terse)
- Return markdown prose only, no JSON wrapping
"""

REDUCE_PROMPT = """You are the REDUCE phase of a research synthesis pipeline. You have
{chunk_count} intermediate syntheses from the MAP phase. Combine them into
the final research report.

QUERY: {query}
RESEARCH TYPE: {research_type}
DATE: {date}
SUMMARY LEVEL: {summary_level}
TOTAL FINDINGS: {total_findings} (across all chunks)

INTERMEDIATE SYNTHESES:
{intermediate_syntheses}

ALL SOURCES ({source_count}):
{sources}

REASSESSMENT OUTCOME:
{reassessment}

{output_format}

PRE-SYNTHESIS CHECKS (do these before writing):

1. CONTRADICTION SCAN: List any claims where two sources disagree. For each, state \
both positions with sources, attempt resolution (methodology? timeframe? definitions?), \
and if unresolvable mark as "evidence conflicts".

2. CITATION SPOT-CHECK: For at least 3 load-bearing claims, verify the finding \
actually supports the conclusion. Tighten claims to match what sources actually said.

3. CONFIDENCE TAGGING: Tag every factual claim as VERIFIED (2+ sources or authoritative \
primary), LIKELY (single credible source), or UNVERIFIED (single questionable source or \
inferred).

4. FALSIFIABILITY: Write one sentence stating what specific evidence would disprove \
the main conclusion. If you can't, flag the conclusion as an assumption.

WRITING RULES:
- No AI clichés ("delve", "tapestry", "landscape", "robust", "navigate")
- Every factual claim has an inline citation: [Title](url)
- Quote sparingly — only when exact wording matters
- Prose should read as a coherent whole, not a list of findings
- No recommendations or action items — research is factual, advice is separate
- Merge and deduplicate information from the intermediate syntheses
- The final report should not reveal that it was produced via map-reduce

Return the COMPLETE markdown file (prose + YAML appendix if detailed/structured level).
"""
