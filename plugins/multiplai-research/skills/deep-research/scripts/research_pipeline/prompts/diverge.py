"""DIVERGE prompt — expands query vocabulary with mechanism-level angles."""


DIVERGE_PROMPT = """You are the DIVERGE stage of a research pipeline. Expand the \
search space with mechanism-level queries that find entities which don't describe \
themselves using the analyst's vocabulary.

QUERY: {query}
RESEARCH TYPE: {research_type}
SUB-QUESTIONS:
{sub_questions}

YOUR JOB:
1. For each sub-question, generate 1-2 MECHANISM-level queries. Examples:
   - Adjacent mechanisms: different approaches to the same goal
   - Solution vocabulary: terms practitioners use vs. terms analysts use
   - Incumbent workarounds: how people solve this today without a dedicated tool
   - Platform plays: features inside larger products that address this

2. Generate 2-4 DIRECTORY queries targeting platforms where relevant entities are \
indexed (e.g., "site:crunchbase.com {{category}}", "Product Hunt {{category}} 2026", \
"site:github.com awesome-{{topic}}", "site:arxiv.org {{topic}}").

3. Generate 2-3 PRIMARY keyword queries — direct phrasings of the sub-questions for \
a search engine.

RESEARCH TYPE GUIDANCE:
{research_type_guidance}

Return JSON matching this schema:
{{
  "primary_queries": ["..."],
  "mechanism_queries": ["..."],
  "directory_queries": ["..."]
}}

Rules:
- Include today's year (extract from DATE: {date}) in time-sensitive queries.
- Keep queries focused; 3-8 words each.
- For directory queries, use `site:` operators.
- Return ONLY valid JSON.
"""
