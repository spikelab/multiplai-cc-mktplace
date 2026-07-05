"""REASSESS prompt — 6 checkpoint questions on framing and claims."""


REASSESS_PROMPT = """You are the REASSESS stage of a research pipeline. After all \
reading is complete, pause and check whether the original framing still holds and \
whether any claims need verification.

QUERY: {query}
SUB-QUESTIONS:
{sub_questions}

FINDINGS COLLECTED:
{findings}

Answer the 6 checkpoint questions:

FRAMING CHECKS:
1. Did the sources reveal that the original sub-questions were asking the wrong thing?
2. Did a term, concept, or framing appear repeatedly that wasn't in the original queries?
3. Is there a major angle that no source covered — suggesting a gap in search strategy?

CLAIMS CHECKS:
4. LOAD-BEARING CLAIMS: Which findings, if wrong, would invalidate the main \
conclusion? List them explicitly.
5. CONFLATION CHECK: Are any findings treating two distinct things as one? (product \
vs product line, entity vs parent, announced vs shipped, correlation vs causation)
6. CONVENIENCE BIAS: Are any findings suspiciously aligned with what the user wants \
to hear?

DECISION LOGIC:
- If ANY of questions 1-3 is yes → trigger refinement cycle: generate 3-5 new search \
queries.
- If ANY of questions 4-6 flags a claim → trigger verification: generate 1-2 targeted \
verification queries per flagged claim (use exact entity names and specific attributes).

Return JSON matching this schema:
{{
  "framing_wrong_question": false,
  "new_framing_emerged": false,
  "missing_angle": false,
  "framing_notes": "",
  "load_bearing_claims": ["claim 1", "claim 2"],
  "conflation_claims": [],
  "convenience_bias_claims": [],
  "refinement_needed": false,
  "refinement_queries": [],
  "verify_claims": ["specific claim to verify"],
  "verify_queries": ["targeted query with entity name"]
}}

Rules:
- Be honest — if the research is solid, return empty arrays for all issues.
- Verification queries must use PRECISE entity names, not broad topic searches.
- Return ONLY valid JSON.
"""
