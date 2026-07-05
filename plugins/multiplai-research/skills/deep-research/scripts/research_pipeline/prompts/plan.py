"""PLAN prompt — decomposes a research query into focused sub-questions."""


PLAN_PROMPT = """You are the PLAN stage of a research pipeline. Decompose a \
research query into focused sub-questions that guide the subsequent search and \
reading phases.

QUERY: {query}
RESEARCH TYPE: {research_type}
DATE: {date}

PERSONAL CONTEXT (facts from the user's memory files):
{personal_context}

PRIOR KNOWLEDGE (existing findings from workspace):
{prior_knowledge}

RESEARCH TYPE GUIDANCE:
{research_type_guidance}

YOUR JOB:
1. Generate {max_sub_questions} focused sub-questions. Each sub-question targets \
a specific aspect of the main query.
2. If personal context is provided, at least one sub-question must leverage it \
(e.g., "spouse is Chinese citizen" → add a sub-question about spouse visa pathways).
3. If prior knowledge is provided, focus sub-questions on gaps and unknowns, not \
topics already well-covered.
4. State what "good" research output would answer for this query.
5. Identify 2-4 target domain types worth prioritizing (e.g., "government .gov sites \
for regulations", "industry reports for market data").
6. List the specific authority domains whose official websites are primary sources of \
truth for this query. These are the actual product/organization domains that MUST be \
consulted — e.g., if researching "Zendesk vs Intercom pricing", authority_domains \
would be ["zendesk.com", "intercom.com"]. Use bare domain names only (no https://, \
no paths). If no specific authority domains apply, return an empty list.

Return JSON matching this schema:
{{
  "sub_questions": ["sub-question 1", "sub-question 2", ...],
  "target_domains": ["domain type 1", "domain type 2", ...],
  "authority_domains": ["example.com", "another.org"],
  "what_good_looks_like": "1-2 sentences describing successful research output"
}}

Return ONLY valid JSON, no prose wrapper.
"""
