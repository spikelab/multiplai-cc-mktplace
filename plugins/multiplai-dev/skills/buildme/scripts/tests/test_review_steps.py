"""Tests for review_steps — evidence-based code review (diff + standards + model split)."""

from unittest.mock import AsyncMock, patch

import pytest
from build_pipeline.config import BuildConfig
from build_pipeline.llm_steps.review_steps import merge_panel_results, run_code_review
from build_pipeline.models import ReviewFinding, ReviewResult, ReviewScore
from build_pipeline.prompts.review import CODE_REVIEW_PROMPT

REVIEW_OK = ReviewResult(
    scores=[ReviewScore(dimension="Quality", weight=2, score=4, evidence="fine")]
)


def _mock_llm():
    """The reviewer seam: a read-only subagent, not a toolless single-turn call."""
    return patch(
        "build_pipeline.llm_steps.review_steps.agent_call_structured",
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
        # Not `is` — a single reviewer is still merged (to stamp `panel`), so
        # the result is a copy of REVIEW_OK, not the same object.
        assert result.scores == REVIEW_OK.scores
        assert result.panel == ["claude-sonnet-4-6"]
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
        for placeholder in (
            "{diff}",
            "{rubric}",
            "{spec_context}",
            "{implementer_report}",
        ):
            assert placeholder in CODE_REVIEW_PROMPT

    def test_formats_without_error(self):
        rendered = CODE_REVIEW_PROMPT.format(
            diff="d",
            rubric="r",
            spec_context="s",
            standards="st",
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
                "diff",
                "rubric",
                config,
                implementer_report="RED_EVIDENCE_SENTINEL: 3 failed",
            )
        assert "RED_EVIDENCE_SENTINEL: 3 failed" in mock_call.call_args.args[0]

    @pytest.mark.asyncio
    async def test_empty_report_says_none_provided(self):
        config = BuildConfig(model="claude-sonnet-4-6")
        with _mock_llm() as mock_call:
            await run_code_review("diff", "rubric", config)
        assert "(no implementer report provided)" in mock_call.call_args.args[0]


def _result(*, scores=None, findings=None, missing=None, misunderstood=None):
    return ReviewResult(
        scores=scores
        or [ReviewScore(dimension="Quality", weight=2, score=4, evidence="e")],
        findings=findings or [],
        missing=missing or [],
        misunderstood=misunderstood or [],
    )


class TestMergePanelResults:
    def test_single_reviewer_is_unchanged_apart_from_the_panel_stamp(self):
        r = _result()
        merged = merge_panel_results([r], ["model-a"])
        assert merged.scores == r.scores
        assert merged.panel == ["model-a"]

    def test_scores_average_across_the_panel(self):
        a = _result(
            scores=[ReviewScore(dimension="Q", weight=2, score=5, evidence="a")]
        )
        b = _result(
            scores=[ReviewScore(dimension="Q", weight=2, score=3, evidence="b")]
        )
        merged = merge_panel_results([a, b], ["a", "b"])
        assert len(merged.scores) == 1
        assert merged.scores[0].score == 4

    def test_disagreement_collapses_confidence(self):
        """Two reviewers 4 apart carry no information; the graded gate then
        reads the dimension as neutral rather than as a verdict."""
        a = _result(
            scores=[ReviewScore(dimension="Q", weight=2, score=5, evidence="a")]
        )
        b = _result(
            scores=[ReviewScore(dimension="Q", weight=2, score=1, evidence="b")]
        )
        merged = merge_panel_results([a, b], ["a", "b"])
        assert merged.scores[0].confidence == 0.0

    def test_agreement_preserves_confidence(self):
        a = _result(
            scores=[ReviewScore(dimension="Q", weight=2, score=4, evidence="a")]
        )
        b = _result(
            scores=[ReviewScore(dimension="Q", weight=2, score=4, evidence="b")]
        )
        merged = merge_panel_results([a, b], ["a", "b"])
        assert merged.scores[0].confidence == 1.0

    def test_independent_confirmation_raises_finding_confidence(self):
        """Noisy-or: two reviewers each 60% sure of the same defect → 84%."""
        f = dict(claim="unbounded loop", file_path="a.py", line=7, confidence=0.6)
        merged = merge_panel_results(
            [
                _result(findings=[ReviewFinding(**f)]),
                _result(findings=[ReviewFinding(**f)]),
            ],
            ["a", "b"],
        )
        assert len(merged.findings) == 1
        assert merged.findings[0].confidence == pytest.approx(0.84)
        assert merged.findings[0].reviewers == ["a", "b"]

    def test_harshest_severity_wins_on_a_shared_finding(self):
        merged = merge_panel_results(
            [
                _result(
                    findings=[
                        ReviewFinding(
                            claim="x", file_path="a.py", line=1, severity="Minor"
                        )
                    ]
                ),
                _result(
                    findings=[
                        ReviewFinding(
                            claim="x", file_path="a.py", line=1, severity="Critical"
                        )
                    ]
                ),
            ],
            ["a", "b"],
        )
        assert merged.findings[0].severity == "Critical"

    def test_spec_verdicts_are_unioned_not_intersected(self):
        """Reviewers find disjoint sets — requiring consensus would discard
        exactly what the panel exists to surface."""
        merged = merge_panel_results(
            [_result(missing=["retry path"]), _result(missing=["timeout path"])],
            ["a", "b"],
        )
        assert merged.missing == ["retry path", "timeout path"]

    def test_union_dedupes_identical_verdicts(self):
        merged = merge_panel_results(
            [_result(misunderstood=["same"]), _result(misunderstood=["same"])],
            ["a", "b"],
        )
        assert merged.misunderstood == ["same"]

    def test_uncorroborated_dimension_is_discounted(self):
        """One member scoring a dimension nobody else scored has zero spread.

        Without a coverage discount that reads as unanimous agreement — full
        confidence from a single unchallenged opinion.
        """
        a = _result(
            scores=[
                ReviewScore(
                    dimension="Q", weight=2, score=4, evidence="a", confidence=1.0
                )
            ]
        )
        b = _result(
            scores=[
                ReviewScore(
                    dimension="Other", weight=2, score=4, evidence="b", confidence=1.0
                )
            ]
        )
        merged = merge_panel_results([a, b], ["a", "b"])
        by_dim = {s.dimension: s for s in merged.scores}
        assert by_dim["Q"].confidence == 0.5
        assert by_dim["Other"].confidence == 0.5

    def test_score_rounding_is_half_up_not_bankers(self):
        """round() would send a 2/3 split to 2 and a 3/4 split to 4."""

        def pair(x, y):
            return (
                merge_panel_results(
                    [
                        _result(
                            scores=[
                                ReviewScore(
                                    dimension="Q", weight=2, score=x, evidence="a"
                                )
                            ]
                        ),
                        _result(
                            scores=[
                                ReviewScore(
                                    dimension="Q", weight=2, score=y, evidence="b"
                                )
                            ]
                        ),
                    ],
                    ["a", "b"],
                )
                .scores[0]
                .score
            )

        assert pair(2, 3) == 3
        assert pair(3, 4) == 4


class TestPanelResilience:
    """A panel must not make the pipeline less reliable than one reviewer."""

    @staticmethod
    def _panel_config(*models):
        config = BuildConfig(model="claude-sonnet-4-6")
        config.review_panel = list(models)
        return config

    @pytest.mark.asyncio
    async def test_one_failed_member_is_dropped_and_the_review_proceeds(self):
        from build_pipeline.sdk import LLMCallError

        config = self._panel_config("model-a", "model-b")
        with patch(
            "build_pipeline.llm_steps.review_steps.agent_call_structured",
            new_callable=AsyncMock,
            side_effect=[LLMCallError("backend unreachable"), REVIEW_OK],
        ):
            result = await run_code_review("diff", "rubric", config)
        # Merged from the survivor only, and `panel` names who actually reviewed.
        assert result.scores == REVIEW_OK.scores
        assert result.panel == ["model-b"]

    @pytest.mark.asyncio
    async def test_all_members_failing_raises(self):
        from build_pipeline.sdk import LLMCallError

        config = self._panel_config("model-a", "model-b")
        with patch(
            "build_pipeline.llm_steps.review_steps.agent_call_structured",
            new_callable=AsyncMock,
            side_effect=LLMCallError("down"),
        ):
            with pytest.raises(LLMCallError, match="every member"):
                await run_code_review("diff", "rubric", config)

    @pytest.mark.asyncio
    async def test_budget_denial_cannot_be_dropped_as_a_degraded_panel(self):
        from build_pipeline.budget import BudgetExceededError

        config = self._panel_config("model-a", "model-b")
        with patch(
            "build_pipeline.llm_steps.review_steps.agent_call_structured",
            new_callable=AsyncMock,
            side_effect=[BudgetExceededError("cap"), REVIEW_OK],
        ):
            with pytest.raises(BudgetExceededError):
                await run_code_review("diff", "rubric", config)


class TestPanelDispatch:
    @pytest.mark.asyncio
    async def test_no_panel_configured_runs_one_reviewer(self):
        config = BuildConfig(model="claude-sonnet-4-6")
        with _mock_llm() as mock_call:
            result = await run_code_review("diff", "rubric", config)
        assert mock_call.await_count == 1
        assert result.panel == ["claude-sonnet-4-6"]

    @pytest.mark.asyncio
    async def test_panel_runs_one_call_per_member(self):
        config = BuildConfig(model="claude-sonnet-4-6")
        config.review_panel = ["model-a", "model-b", "model-c"]
        with _mock_llm() as mock_call:
            result = await run_code_review("diff", "rubric", config)
        assert mock_call.await_count == 3
        assert result.panel == ["model-a", "model-b", "model-c"]

    @pytest.mark.asyncio
    async def test_review_model_overrides_the_default_when_no_panel(self):
        config = BuildConfig(model="claude-sonnet-4-6")
        config.review_model = "claude-opus-5"
        with _mock_llm() as mock_call:
            await run_code_review("diff", "rubric", config)
        assert mock_call.call_args.kwargs["model"] == "claude-opus-5"


# Every tool that can change the tree. A review step holding any of these could
# fix what it found, which destroys the adjudication seam (findings are
# proposals the orchestrator disposes of) and walks past `unchanged_tests_gate`,
# whose premise is that only the implementer's windows are writable.
WRITE_CAPABLE_TOOLS = {"Write", "Edit", "Bash", "NotebookEdit", "MultiEdit"}


class TestReviewersAreReadOnlySubagents:
    def test_no_review_step_can_write(self):
        """Every review/audit step's tool list, checked against one deny set.

        Asserting on the constants rather than on a call keeps this true for
        steps added later: a new reviewer that reuses REVIEWER_TOOLS is covered
        the day it lands, and one that spells its own list is caught here.
        """
        from build_pipeline.llm_steps.plan_review_steps import PLAN_REVIEW_TOOLS
        from build_pipeline.llm_steps.review_steps import REVIEWER_TOOLS
        from build_pipeline.llm_steps.spec_steps import AUDITOR_TOOLS

        lists = [
            ("REVIEWER_TOOLS", REVIEWER_TOOLS),
            ("AUDITOR_TOOLS", AUDITOR_TOOLS),
            ("PLAN_REVIEW_TOOLS", PLAN_REVIEW_TOOLS),
        ]

        for name, tools in lists:
            assert tools, f"{name} must not be empty"
            offenders = WRITE_CAPABLE_TOOLS.intersection(tools)
            assert not offenders, (
                f"{name} grants write-capable tools: {sorted(offenders)}"
            )

    @pytest.mark.asyncio
    async def test_review_runs_as_a_read_only_subagent_in_the_project_dir(
        self, tmp_path
    ):
        config = BuildConfig(model="claude-sonnet-4-6", project_dir=tmp_path)
        with _mock_llm() as mock_call:
            await run_code_review("diff", "rubric", config)
        kwargs = mock_call.call_args.kwargs
        assert kwargs["allowed_tools"] == ["Read", "Grep", "Glob"]
        assert not WRITE_CAPABLE_TOOLS.intersection(kwargs["allowed_tools"])
        assert kwargs["cwd"] == str(tmp_path)
        assert kwargs["budget_label"] == "review"

    @pytest.mark.asyncio
    async def test_reviewer_turns_are_bounded(self):
        """A reviewer is not an implementer — the turn budget says so."""
        config = BuildConfig(model="claude-sonnet-4-6")
        with _mock_llm() as mock_call:
            await run_code_review("diff", "rubric", config)
        assert mock_call.call_args.kwargs["max_turns"] == 30


# --- PR B: prior assessments and defect locations across a panel ------------

from build_pipeline.models import PriorAssessment


def _assessed(*pairs) -> ReviewResult:
    return ReviewResult(
        scores=REVIEW_OK.scores,
        prior_assessments=[
            PriorAssessment(finding_id=fid, status=status) for fid, status in pairs
        ],
    )


class TestPriorAssessmentMerge:
    @pytest.mark.parametrize(
        "statuses, worst",
        [
            (["resolved", "cannot_verify"], "cannot_verify"),
            (["cannot_verify", "partially_resolved"], "partially_resolved"),
            (["partially_resolved", "still_present"], "still_present"),
            (["still_present", "resolved"], "still_present"),
            (["resolved", "resolved"], "resolved"),
        ],
    )
    def test_the_worst_status_per_finding_wins(self, statuses, worst):
        merged = merge_panel_results(
            [_assessed(("B01-R1-F1", s)) for s in statuses],
            [f"m{i}" for i in range(len(statuses))],
        )
        assert [(a.finding_id, a.status) for a in merged.prior_assessments] == [
            ("B01-R1-F1", worst)
        ]

    def test_one_reviewer_listing_an_id_twice_is_merged_too(self):
        merged = merge_panel_results(
            [_assessed(("B01-R1-F1", "resolved"), ("B01-R1-F1", "still_present"))],
            ["m"],
        )
        assert [(a.finding_id, a.status) for a in merged.prior_assessments] == [
            ("B01-R1-F1", "still_present")
        ]

    def test_ids_keep_the_order_they_were_first_given(self):
        merged = merge_panel_results(
            [
                _assessed(("B", "resolved"), ("A", "resolved")),
                _assessed(("A", "still_present")),
            ],
            ["m1", "m2"],
        )
        assert [(a.finding_id, a.status) for a in merged.prior_assessments] == [
            ("B", "resolved"),
            ("A", "still_present"),
        ]

    def test_an_unknown_status_reads_as_cannot_verify(self):
        assert (
            PriorAssessment(finding_id="x", status="Fixed!").status == "cannot_verify"
        )
        assert (
            PriorAssessment(finding_id="x", status="Still Present").status
            == "still_present"
        )


class TestOtherLocationsMerge:
    def test_a_shared_finding_takes_the_union_of_its_locations(self):
        f = ReviewFinding(
            claim="same bug",
            file_path="a.py",
            line=1,
            defect_class="off-by-one",
            other_locations=["b.py:2"],
        )
        g = f.model_copy(
            update={"other_locations": ["b.py:2", "c.py:3"], "defect_class": ""}
        )
        merged = merge_panel_results(
            [_result(findings=[f]), _result(findings=[g])], ["m1", "m2"]
        )
        assert [x.other_locations for x in merged.findings] == [["b.py:2", "c.py:3"]]
        assert merged.findings[0].defect_class == "off-by-one"

    def test_a_location_given_as_a_string_still_parses(self):
        assert ReviewFinding(claim="c", other_locations="a.py:1").other_locations == [
            "a.py:1"
        ]
