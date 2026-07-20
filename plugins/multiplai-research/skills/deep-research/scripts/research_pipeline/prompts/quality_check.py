"""Prompt template for the pre-synthesis quality check.

Uses the parse-tier model (sonnet) at the config.efforts["quality_check"]
effort (default "medium"). Strong GO bias —
aborting after expensive fetch+extract wastes far more than a synthesis
attempt. Thresholds are scaled to the active preset, not absolute counts:
a micro run with 3 sources must not be judged by thorough-run standards.
"""

QUALITY_CHECK_PROMPT = """You are a research quality assessor. Given the research state below, decide whether the findings are sufficient to produce a useful synthesis report.

QUERY: {query}

PRESET: this is a "{preset_name}" run targeting {preset_sources} sources (minimum {min_sources}). Judge sufficiency relative to that scale — a small preset legitimately produces few findings.

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

GO when: At least one sub-question has findings from more than one source, OR the total finding count is healthy for a {preset_name} run (roughly a few findings per targeted source). Even if some sub-questions are thin, synthesis can note gaps.
NO-GO ONLY when: fewer than {nogo_high_conf_threshold} high-confidence findings in total AND no sub-question has multi-source coverage. Or zero sources were successfully fetched. A single weak sub-question among otherwise strong coverage is NOT grounds for NO-GO — the synthesis can flag it as a gap."""
