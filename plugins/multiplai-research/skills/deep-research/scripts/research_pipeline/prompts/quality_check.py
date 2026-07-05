"""Prompt template for the pre-synthesis quality check.

Uses sonnet with effort="medium". Strong GO bias — aborting
after expensive fetch+extract wastes far more than a synthesis attempt.
NO-GO only when research is completely empty.
"""

QUALITY_CHECK_PROMPT = """You are a research quality assessor. Given the research state below, decide whether the findings are sufficient to produce a useful synthesis report.

QUERY: {query}

SUB-QUESTIONS:
{sub_questions}

FINDINGS SUMMARY:
- {total_findings} total findings
- By confidence: {confidence_breakdown}
- Sources by reputation: {reputation_breakdown}
- {failed_count} sources failed to fetch

FAILED SOURCES:
{failed_sources}

COVERAGE:
{coverage_assessment}

Decide GO or NO-GO. Return ONLY a JSON object:
{{"go": true/false, "confidence": 0.0-1.0, "reasoning": "1-2 sentences", "critical_gaps": ["gap1", ...]}}

STRONG BIAS TOWARD GO. Synthesis is cheap (~$0.05). The fetch+extract that preceded this cost far more. Aborting wastes all that work.

GO when: At least one sub-question has multi-source findings. Total findings >= 20 with mix of sources. Even if some sub-questions are thin, synthesis can note gaps.
NO-GO ONLY when: ALL sub-questions lack credible findings (< 5 total high-confidence findings). Or zero sources were successfully fetched. A single weak sub-question among otherwise strong coverage is NOT grounds for NO-GO — the synthesis can flag it as a gap."""
