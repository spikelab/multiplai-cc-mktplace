"""VERIFY prompt — per-claim verdicts from verification-cycle findings."""


VERIFY_VERDICTS_PROMPT = """You are the VERIFICATION stage of a research pipeline. \
Earlier analysis flagged specific claims as suspect (load-bearing single-source \
claims, possible conflations, or convenience-bias risks). Targeted searches were \
run and the NEW FINDINGS below were gathered from those verification sources.

Issue a verdict for EACH flagged claim, judged ONLY against the new findings:

- "confirmed": at least one credible new finding directly supports the claim
- "refuted": at least one credible new finding directly contradicts the claim
- "unresolved": the new findings neither clearly support nor contradict the claim

FLAGGED CLAIMS:
{claims}

NEW FINDINGS (gathered during verification):
{findings}

Rules:
- Judge strictly from the new findings listed above — do not use outside knowledge.
- "evidence" must reference specific findings by number (e.g. "finding 3: ...").
- Every flagged claim must appear exactly once in the output, verbatim.
- When in doubt between confirmed and unresolved, choose unresolved.

Return JSON matching this schema:
{{
  "verdicts": [
    {{"claim": "...", "verdict": "confirmed|refuted|unresolved", "evidence": ["finding 3: ...", ...]}}
  ]
}}

Return ONLY valid JSON.
"""
