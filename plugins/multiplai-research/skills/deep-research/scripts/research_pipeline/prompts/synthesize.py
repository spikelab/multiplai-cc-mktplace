"""SYNTHESIZE prompt — writes the final research report with YAML appendix.

One base template + summary-level-specific guidance. Output is markdown prose +
fenced YAML data appendix, matching the existing skill's format for downstream
compatibility.
"""


BASE_HEADER = """You are the SYNTHESIZE stage of a research pipeline. You write the \
final research report. This is the creative core — genuinely reason across all \
findings, identify tensions, verify logic, and produce a document the reader can \
act on.

QUERY: {query}
RESEARCH TYPE: {research_type}
DATE: {date}
SUMMARY LEVEL: {summary_level}

FINDINGS ({finding_count}):
{findings}

SOURCES ({source_count}):
{sources}

REASSESSMENT OUTCOME:
{reassessment}

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

"""


STRUCTURED_TEMPLATE = """{base_header}

OUTPUT FORMAT ({summary_level}, 1,000-2,000 words prose + YAML appendix):

```markdown
# [Research Topic]

**Date:** {date} | **Type:** {research_type} | **Confidence:** high|medium|low | **Sources used:** N

## Summary

[2-3 paragraph executive summary synthesizing the answer. Coherent narrative, not a list. \
Inline citations throughout.]

## Findings

### [Finding Title 1]

[2-3 sentences with inline citations. Connect the dots between sources.]

**Confidence:** high|medium|low

### [Finding Title 2]
...

## Minority Views & Tensions

[Present dissenting positions with sources. Describe disagreements narratively. If \
sources agree, say "Sources showed strong consensus — no significant dissent found \
across N sources."]

## Verified Claims

[Only if VERIFY cycle triggered. One paragraph per verified claim.]

## Gaps & Open Questions

- [What couldn't be answered]
- [What remains open]

## Falsifiability

[2-3 sentences: what specific evidence would weaken these conclusions?]

## Sources

| # | Source | Reputation | Relevance | Date |
|---|--------|------------|-----------|------|
| 1 | [Title](url) | authoritative | why useful | YYYY-MM-DD |
```

Followed by the YAML appendix (see YAML_APPENDIX_FORMAT below).

{yaml_appendix_format}

Return the COMPLETE markdown file (prose + YAML appendix). No wrapper JSON.
"""


GIST_TEMPLATE = """{base_header}

OUTPUT FORMAT (gist, 300-500 words prose + YAML appendix):

```markdown
# [Research Topic]

**Date:** {date} | **Confidence:** high|medium|low | **Sources used:** N

[1 paragraph direct answer with inline citations.]

**Key takeaways:**
- [Most important finding] ([Source](url))
- [Second] ([Source](url))
- [Third] ([Source](url))

**Caveats:** [1-2 sentences if any. Omit if none.]

**Falsifiability:** [1 sentence: what would weaken this conclusion.]
```

Followed by the YAML appendix.

{yaml_appendix_format}

Return the COMPLETE markdown file.
"""


DETAILED_TEMPLATE = """{base_header}

OUTPUT FORMAT (detailed, 2,500-4,000 words prose + YAML appendix):

```markdown
# [Research Topic]

**Date:** {date} | **Type:** {research_type} | **Confidence:** high|medium|low | **Sources:** N consulted

## Summary

[2-3 paragraph executive summary.]

## Methodology

Searched N queries, consulted M sources, used K after triage. Followed L links.
[If REASSESS triggered refinement or verification, note what changed.]

## Findings

### [Finding Title 1]

[2-4 paragraphs with inline citations. Weave together evidence from multiple sources.]

**Confidence:** high|medium|low — [why]

(more findings...)

## Minority Views & Contrarian Perspectives

### [Perspective]
[Claim, who holds it, why it's notable, strength, limitations.]

## Tensions & Trade-offs

### [Tension]
**Position A:** [Source](url) argues X, based on Y.
**Position B:** [Source](url) contends Z, pointing to W.
**Why they differ:** [methodology, timeframe, definitions, etc.]
**Resolution:** [which is more credible, OR "genuinely contested"]

## Verified Claims

[If VERIFY cycle triggered.]

## Second-Order Implications

**If [key finding] is true, then:**
- [Implication 1]
- [Implication 2]

## Gaps & Open Questions

- [Gap 1]: No sources addressed [topic].
- [Gap 2]: Conflicting information with no resolution.

## Unverified Claims

- "[Claim]" — [Source](url). Unverified because: [reason].

## Falsifiability

What would disprove or weaken these conclusions:
- **[Main conclusion 1]:** Would be undermined if [specific evidence].
- **[Main conclusion 2]:** Would be weakened if [specific evidence].

## All Sources

| # | Source | Reputation | Used | Relevance | Date |
|---|--------|------------|------|-----------|------|
```

Followed by the YAML appendix.

{yaml_appendix_format}

Return the COMPLETE markdown file.
"""


YAML_APPENDIX_FORMAT = """
After the prose, append this data section:

---

<!-- STRUCTURED DATA — machine-readable, do not edit above this line -->

```yaml
index:
  questions_investigated:
    - "sub-question text"
  questions_open:
    - "open gap"
  sources_consulted: N
  total_findings: N
  findings_by_confidence:
    verified: N
    likely: N
    unverified: N
  sources_by_reputation:
    authoritative: N
    established: N
    emerging: N
  falsifiability: "one-line from your falsifiability check"

meta:
  query: "original query"
  date: "YYYY-MM-DD"
  research_type: "{research_type}"
  preset: "{summary_level}"
  confidence: high|medium|low
  confidence_reason: "why"
  falsifiability: "what would disprove conclusions"

findings:
  - fact: "one-sentence finding"
    source: "[Title](url)"
    reputation: authoritative|established|emerging|questionable
    confidence: high|medium|low
    date: "YYYY-MM-DD or undated"

sources:
  - title: "Title"
    url: "url"
    reputation: authoritative
    relevance: "why useful"
    date: "YYYY-MM-DD"

tensions:
  - topic: "..."
    position_a: {{claim: "...", source: "..."}}
    position_b: {{claim: "...", source: "..."}}
    resolution: "..."

gaps:
  - "what couldn't be found"
```
"""


def synthesis_prompt_for(summary_level: str) -> str:
    """Return the full prompt template for a summary level."""
    if summary_level == "gist":
        template = GIST_TEMPLATE
    elif summary_level == "detailed":
        template = DETAILED_TEMPLATE
    else:
        template = STRUCTURED_TEMPLATE

    return template.replace(
        "{yaml_appendix_format}", YAML_APPENDIX_FORMAT
    ).replace("{base_header}", BASE_HEADER)
