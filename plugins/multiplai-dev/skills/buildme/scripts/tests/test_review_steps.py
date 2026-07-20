"""Tests for review_steps — evidence-based code review (diff + standards + model split)."""

import pytest
from unittest.mock import AsyncMock, patch

from build_pipeline.config import BuildConfig
from build_pipeline.llm_steps.review_steps import run_code_review
from build_pipeline.models import ReviewResult, ReviewScore
from build_pipeline.prompts.review import CODE_REVIEW_PROMPT


REVIEW_OK = ReviewResult(
    scores=[ReviewScore(dimension="Quality", weight=2, score=4, evidence="fine")]
)


def _mock_llm():
    return patch(
        "build_pipeline.llm_steps.review_steps.llm_call_structured",
        new_callable=AsyncMock,
        return_value=REVIEW_OK,
    )


class TestRunCodeReviewPromptContent:
    @pytest.mark.asyncio
    async def test_prompt_contains_diff_and_standards(self):
        """The reviewer must see the actual diff and the pushed standards."""
        config = BuildConfig(model="claude-sonnet-4-6")
        with _mock_llm() as mock_call:
            result = await run_code_review(
                "DIFF_SENTINEL: +def added(): pass",
                "RUBRIC_SENTINEL",
                config,
                spec_context="SPEC_SENTINEL",
                standards="STANDARDS_SENTINEL: no bare except",
            )
        assert result is REVIEW_OK
        prompt = mock_call.call_args.args[0]
        assert "DIFF_SENTINEL: +def added(): pass" in prompt
        assert "STANDARDS_SENTINEL: no bare except" in prompt
        assert "RUBRIC_SENTINEL" in prompt
        assert "SPEC_SENTINEL" in prompt

    @pytest.mark.asyncio
    async def test_empty_standards_says_none_provided(self):
        config = BuildConfig(model="claude-sonnet-4-6")
        with _mock_llm() as mock_call:
            await run_code_review("some diff", "rubric", config)
        prompt = mock_call.call_args.args[0]
        assert "(no standards provided)" in prompt

    @pytest.mark.asyncio
    async def test_empty_spec_context_says_none_provided(self):
        config = BuildConfig(model="claude-sonnet-4-6")
        with _mock_llm() as mock_call:
            await run_code_review("some diff", "rubric", config)
        prompt = mock_call.call_args.args[0]
        assert "(no spec context provided)" in prompt

    @pytest.mark.asyncio
    async def test_empty_diff_flagged_in_prompt(self):
        config = BuildConfig(model="claude-sonnet-4-6")
        with _mock_llm() as mock_call:
            await run_code_review("", "rubric", config)
        prompt = mock_call.call_args.args[0]
        assert "(no diff captured)" in prompt


class TestReviewModelSplit:
    @pytest.mark.asyncio
    async def test_review_model_used_when_set(self):
        config = BuildConfig(model="claude-sonnet-4-6", review_model="claude-opus-4-6")
        with _mock_llm() as mock_call:
            await run_code_review("diff", "rubric", config)
        assert mock_call.call_args.kwargs["model"] == "claude-opus-4-6"

    @pytest.mark.asyncio
    async def test_review_model_falls_back_to_model(self):
        """review_model=None → the review runs on config.model."""
        config = BuildConfig(model="claude-sonnet-4-6", review_model=None)
        with _mock_llm() as mock_call:
            await run_code_review("diff", "rubric", config)
        assert mock_call.call_args.kwargs["model"] == "claude-sonnet-4-6"


class TestCodeReviewPromptTemplate:
    def test_has_standards_section_and_placeholder(self):
        assert "## Coding Standards" in CODE_REVIEW_PROMPT
        assert "{standards}" in CODE_REVIEW_PROMPT

    def test_keeps_existing_placeholders(self):
        for placeholder in ("{diff}", "{rubric}", "{spec_context}", "{implementer_report}"):
            assert placeholder in CODE_REVIEW_PROMPT

    def test_formats_without_error(self):
        rendered = CODE_REVIEW_PROMPT.format(
            diff="d", rubric="r", spec_context="s", standards="st",
            implementer_report="rep",
        )
        assert '"scores"' in rendered  # JSON contract preserved

    def test_treats_implementer_report_as_unverified_claims(self):
        assert "unverified claims" in CODE_REVIEW_PROMPT
        assert "ground truth" in CODE_REVIEW_PROMPT

    def test_spec_compliance_verdict_categories(self):
        for category in ("missing", "extra", "misunderstood"):
            assert f'"{category}"' in CODE_REVIEW_PROMPT

    def test_severity_calibration_and_strengths_first(self):
        assert "cannot be trusted until fixed" in CODE_REVIEW_PROMPT
        assert "strengths" in CODE_REVIEW_PROMPT.lower()

    def test_issue_description_contract(self):
        # Every finding must say what/why/how in one place.
        assert "why it matters" in CODE_REVIEW_PROMPT
        assert "how to fix" in CODE_REVIEW_PROMPT


class TestImplementerReportThreading:
    @pytest.mark.asyncio
    async def test_report_reaches_the_prompt(self):
        config = BuildConfig(model="claude-sonnet-4-6")
        with _mock_llm() as mock_call:
            await run_code_review(
                "diff", "rubric", config,
                implementer_report="RED_EVIDENCE_SENTINEL: 3 failed",
            )
        assert "RED_EVIDENCE_SENTINEL: 3 failed" in mock_call.call_args.args[0]

    @pytest.mark.asyncio
    async def test_empty_report_says_none_provided(self):
        config = BuildConfig(model="claude-sonnet-4-6")
        with _mock_llm() as mock_call:
            await run_code_review("diff", "rubric", config)
        assert "(no implementer report provided)" in mock_call.call_args.args[0]
