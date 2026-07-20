"""CHALLENGE prompt — generates contrarian/devil's-advocate queries."""


CHALLENGE_PROMPT = """You are the CHALLENGE stage of a research pipeline. Generate \
devil's-advocate queries that deliberately seek contrarian perspectives. The goal is \
to prevent echo-chamber research by finding minority views and criticisms.

QUERY: {query}
RESEARCH TYPE: {research_type}
SUB-QUESTIONS:
{sub_questions}
PRIMARY QUERIES (already planned):
{primary_queries}

YOUR JOB:
Generate 2-3 contrarian queries per main sub-question (max 8 total). Use patterns like:
- "problems with [X]"
- "alternatives to [X]"
- "criticism of [X]"
- "what most people miss about [X]"
- "[X] is overrated" / "[X] is underrated"
- "unintended consequences of [X]"
- "why [popular choice] fails"

RESEARCH TYPE-SPECIFIC PATTERNS:
{research_type_guidance}

Rules:
- Skip if the topic is purely factual (e.g., "capital of France").
- Don't force weak contrarian takes — only include ones that actually probe weaknesses.
- Include today's year from DATE: {date} in queries where recency matters.
- Keep queries focused; 3-8 words each.

Return JSON matching this schema:
{{
  "contrarian_queries": ["query 1", "query 2", ...]
}}

Return ONLY valid JSON.
"""


ADVERSARIAL_REVIEW_PROMPT = """You are an adversarial reviewer — a devil's advocate \
for research quality. Your job is to stress-test a completed research document. You \
are NOT the researcher. You are the skeptic.

RESEARCH REPORT:
{report}

EXTRACTED FINDINGS (the evidence base the report was synthesized from):
{findings}

INSTRUCTIONS:

1. Identify the WEAKEST claims — specifically:
   - Single-source claims that bear significant weight in the conclusion
   - Logic gaps: where the conclusion doesn't follow from the evidence presented
   - Unexamined assumptions: things taken as given that could be wrong
   - Missing perspectives: viewpoints or stakeholders not represented
   - Recency risks: findings that may already be outdated

2. Spot-check grounding: pick several report claims that cite sources and check \
whether they are actually supported by the EXTRACTED FINDINGS above (facts and \
quotes). A claim that appears in the report but has no supporting finding is a \
grounding failure — call it out explicitly.

3. Test the falsifiability statement: is it genuine (names specific, concrete \
evidence that could be sought) or fake (unfalsifiable or trivially unlikely)?

4. Rate robustness on 3 dimensions (integer 1-5 each):
   - evidence_strength
   - argument_coherence
   - counter_argument_resistance

5. Write the review body in this markdown format (do NOT include a score table — \
it is generated separately from your numeric scores):

# Adversarial Review: [Research Topic]

**Date:** {date}

## Weakest Claims

### [Claim 1]
- **The claim:** [...]
- **The weakness:** [single source / logic gap / assumption / ungrounded]
- **Impact if wrong:** [...]
- **Suggested test:** [how to verify or disprove]

## Grounding Spot-Check

[Which claims you checked against the findings, and what you found.]

## Falsifiability Assessment

[Genuine, weak, or missing? What would a strong version look like?]

## Missing Perspectives

- [Perspective/stakeholder not represented and why it matters]

## Verdict

[2-3 sentences: overall assessment. Is this research safe to act on? What would \
make it stronger?]

Be genuinely critical, not performatively harsh.

Return JSON matching this schema:
{{
  "evidence_strength": 1-5,
  "argument_coherence": 1-5,
  "counter_argument_resistance": 1-5,
  "weakest_claims": ["one-line summary of each weakest claim", ...],
  "review_markdown": "the complete markdown review body"
}}

Return ONLY valid JSON.
"""
