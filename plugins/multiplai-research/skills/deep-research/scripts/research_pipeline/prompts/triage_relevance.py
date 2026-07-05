"""TRIAGE relevance scoring prompt — scores borderline sources 1-5."""


TRIAGE_RELEVANCE_PROMPT = """You are the TRIAGE stage of a research pipeline. Score \
borderline sources for relevance to the research query.

QUERY: {query}
SUB-QUESTIONS:
{sub_questions}

BORDERLINE SOURCES TO SCORE:
{sources}

For each source, score 1-5 for relevance:
- 5 = direct primary source for a sub-question
- 4 = strong secondary source, clearly relevant
- 3 = partial relevance, may contain useful context
- 2 = tangentially related, low signal
- 1 = off-topic, should be excluded

Return JSON matching this schema:
{{
  "scores": [
    {{"url": "https://...", "score": 4, "reason": "brief justification"}},
    ...
  ]
}}

Rules:
- Include every source from the borderline list.
- Reason should be 10-20 words, factual not sales-pitch.
- Return ONLY valid JSON.
"""
