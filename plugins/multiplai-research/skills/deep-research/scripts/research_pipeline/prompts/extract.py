"""READ-stage extraction prompt — extracts structured findings from markdown."""


EXTRACT_PROMPT = """You are the READ stage of a research pipeline. Extract structured \
findings from a fetched web page.

QUERY: {query}
SUB-QUESTIONS:
{sub_questions}

SOURCE:
Title: {title}
URL: {url}
Reputation: {reputation}

CONTENT (markdown extracted from the page):
---
{content}
---

YOUR JOB:
1. Read the content carefully.
2. Extract every fact, data point, or claim that is relevant to the query or any \
sub-question. Be thorough — capture nuance and specifics, not just headlines.
3. For each finding, include a direct quote if the exact wording matters.
4. Identify the publication date if visible.
5. Identify up to 3 LINKS from the content that point to primary sources or deeper \
data worth following (skip navigation, related posts, author bios).
6. Tag each finding with the sub-question index it relates to (0-indexed from the \
sub-questions list above). Use -1 if it relates to the main query but no specific \
sub-question.
7. Assess confidence for each finding:
   - high = source is authoritative for this fact, or it cross-references other sources
   - medium = single credible source
   - low = inferred, speculative, or from a questionable part of the content

Return JSON matching this schema:
{{
  "findings": [
    {{
      "fact": "one sentence",
      "quote": "direct quote or null",
      "date": "YYYY-MM-DD or null",
      "relates_to_sub_question": 0,
      "confidence": "high|medium|low"
    }},
    ...
  ],
  "follow_links": [
    {{"url": "https://...", "reason": "why this is worth fetching"}},
    ...
  ]
}}

Rules:
- If the content is empty or clearly unrelated to the query, return empty "findings".
- Keep each fact to ONE sentence but preserve specifics (numbers, names, dates).
- Return ONLY valid JSON.
"""
