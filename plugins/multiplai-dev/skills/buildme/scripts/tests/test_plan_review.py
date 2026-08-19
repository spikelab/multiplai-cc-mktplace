"""Tests for the PLAN_REVIEW phase — prompt, step, and the one-pass stage.

The phase automates the review the dark-factory board already claims exists
("specs -> impl plan, reviewed by another eng"). Three things have to stay
true and each has tests here: the reviewer is read-only, it regenerates the
plan at most once, and it never applies a split.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from build_pipeline.change_manager import ChangeManager
from build_pipeline.config import BuildConfig
from build_pipeline.llm_steps import plan_review_steps
from build_pipeline.llm_steps.plan_review_steps import (
    PLAN_REVIEW_ACTIONABLE,
    PLAN_REVIEW_PROPOSE_ONLY_CATEGORIES,
    PLAN_REVIEW_TOOLS,
    _split_context,
    plan_review_findings_text,
    run_plan_review,
    run_plan_review_stage,
)
from build_pipeline.models import (
    BuildPhase,
    PlanReviewCut,
    PlanReviewFinding,
    PlanReviewResult,
)
from build_pipeline.prompts.plan_review import NO_USE_CASES, PLAN_REVIEW_PROMPT
from build_pipeline.state import BuildState, SpecGenState

CATEGORIES = (
    "rubric-conflict",
    "block-contradiction",
    "constraint-violation",
    "over-prescription",
    "use-case-coverage",
    "oversized-plan",
)


# --- 1. The prompt --------------------------------------------------------

class TestPlanReviewPrompt:
    def test_every_shipped_category_is_in_the_prompt(self):
        for category in CATEGORIES:
            assert f"`{category}`" in PLAN_REVIEW_PROMPT

    def test_prompt_reads_the_five_documents_the_phase_needs(self):
        for placeholder in ("{proposal_content}", "{design_content}",
                            "{use_cases_content}", "{tasks_content}",
                            "{rubric_content}"):
            assert placeholder in PLAN_REVIEW_PROMPT

    def test_findings_carry_a_file_a_location_a_claim_and_a_reason(self):
        """The seat has to be swappable: the output shape is what a human
        reviewer leaves on the plan PR, not a machine-only format."""
        for field in ("file_path", "location", "claim", "reason"):
            assert f'"{field}"' in PLAN_REVIEW_PROMPT
        assert set(PlanReviewFinding.model_fields) >= {
            "file_path", "location", "claim", "reason",
        }

    def test_oversized_plan_proposes_and_never_performs(self):
        assert "proposed_cut" in PLAN_REVIEW_PROMPT
        assert "signature boundary" in PLAN_REVIEW_PROMPT
        assert "You are proposing a split. You are not performing one." in (
            PLAN_REVIEW_PROMPT
        )

    def test_prompt_states_the_reviewer_cannot_write(self):
        flat = " ".join(PLAN_REVIEW_PROMPT.split())
        assert "You cannot write, edit or run anything, and you must not try." in flat

    def test_prompt_formats_with_the_triggers(self):
        rendered = PLAN_REVIEW_PROMPT.format(
            proposal_content="p", design_content="d", use_cases_content="u",
            tasks_content="t", rubric_content="r", split_context="",
            block_trigger=8, package_trigger=3,
        )
        flat = " ".join(rendered.split())
        assert "A block count above 8" in flat
        assert "more than 3 distinct top-level packages" in flat
        # The braces in the JSON examples survive .format() as literal braces.
        assert '"category": "rubric-conflict"' in rendered


# --- 2. The read-only subagent -------------------------------------------

def _stub_config(tmp_path, **overrides):
    config = SimpleNamespace(
        project_dir=tmp_path,
        specs_dir=tmp_path / "specs",
        review_model="review-model",
        review_effort="review-effort",
        plan_review_model="plan-model",
        plan_review_effort="plan-effort",
        plan_split_block_trigger=8,
        plan_split_package_trigger=3,
        reference_docs_text=lambda: "",
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _change_dir(tmp_path, *, use_cases: bool = True) -> Path:
    change_dir = tmp_path / "specs" / "changes" / "c"
    change_dir.mkdir(parents=True)
    (change_dir / "proposal.md").write_text("## Why\nBecause.\n")
    (change_dir / "design.md").write_text("## Decisions\nOne.\n")
    (change_dir / "tasks.md").write_text("## Block 1\n- [ ] do it\n")
    (change_dir / "rubric.md").write_text("## Criteria\n- correctness\n")
    if use_cases:
        (change_dir / "use-cases.md").write_text(
            "## Personas\nOps.\n\n## Use cases\nExport the log.\n")
    return change_dir


class TestRunPlanReview:
    @pytest.mark.asyncio
    async def test_reviewer_gets_read_only_tools_and_nothing_else(self, tmp_path):
        change_dir = _change_dir(tmp_path)
        call = AsyncMock(return_value=PlanReviewResult(findings=[]))
        with patch.object(plan_review_steps, "agent_call_structured", call):
            await run_plan_review(change_dir, _stub_config(tmp_path))

        kwargs = call.await_args.kwargs
        assert kwargs["allowed_tools"] == ["Read", "Grep", "Glob"]
        assert not ({"Write", "Edit", "Bash", "NotebookEdit"}
                    & set(kwargs["allowed_tools"]))

    def test_the_tool_list_constant_can_never_write(self):
        assert PLAN_REVIEW_TOOLS == ["Read", "Grep", "Glob"]

    @pytest.mark.asyncio
    async def test_uses_the_plan_review_model_and_effort(self, tmp_path):
        change_dir = _change_dir(tmp_path)
        call = AsyncMock(return_value=PlanReviewResult(findings=[]))
        with patch.object(plan_review_steps, "agent_call_structured", call):
            await run_plan_review(change_dir, _stub_config(tmp_path))

        assert call.await_args.kwargs["model"] == "plan-model"
        assert call.await_args.kwargs["effort"] == "plan-effort"

    @pytest.mark.asyncio
    async def test_falls_back_to_the_reviewer_settings_when_absent(self, tmp_path):
        """The plan-review config fields land in a different work item, so the
        step must not depend on their arrival order."""
        config = _stub_config(tmp_path)
        del config.plan_review_model
        del config.plan_review_effort
        del config.plan_split_block_trigger
        del config.plan_split_package_trigger
        change_dir = _change_dir(tmp_path)

        call = AsyncMock(return_value=PlanReviewResult(findings=[]))
        with patch.object(plan_review_steps, "agent_call_structured", call):
            await run_plan_review(change_dir, config)

        assert call.await_args.kwargs["model"] == "review-model"
        assert call.await_args.kwargs["effort"] == "review-effort"
        assert "above 8" in call.await_args.args[0]

    @pytest.mark.asyncio
    async def test_reads_the_use_case_artifact_when_it_exists(self, tmp_path):
        change_dir = _change_dir(tmp_path, use_cases=True)
        call = AsyncMock(return_value=PlanReviewResult(findings=[]))
        with patch.object(plan_review_steps, "agent_call_structured", call):
            await run_plan_review(change_dir, _stub_config(tmp_path))

        assert "Export the log." in call.await_args.args[0]

    @pytest.mark.asyncio
    async def test_degrades_gracefully_when_use_cases_are_absent(self, tmp_path):
        change_dir = _change_dir(tmp_path, use_cases=False)
        call = AsyncMock(return_value=PlanReviewResult(findings=[]))
        with patch.object(plan_review_steps, "agent_call_structured", call):
            result = await run_plan_review(change_dir, _stub_config(tmp_path))

        assert result.findings == []
        assert NO_USE_CASES in call.await_args.args[0]


# --- 3. The unwired oversized-plan seam ----------------------------------

class TestSplitContextSeam:
    def test_seam_is_unwired_and_returns_no_context(self, tmp_path):
        """`plan_split.py` is a separate work item. Until it is wired in, the
        prompt does the partition itself, so "" is a complete behaviour."""
        assert _split_context(_stub_config(tmp_path), tmp_path) == ""

    def test_the_seam_is_documented_where_the_wiring_goes(self):
        assert "UNWIRED SEAM" in _split_context.__doc__
        assert "plan_split" in _split_context.__doc__

    @pytest.mark.asyncio
    async def test_an_empty_seam_still_produces_a_reviewable_prompt(self, tmp_path):
        change_dir = _change_dir(tmp_path)
        call = AsyncMock(return_value=PlanReviewResult(findings=[]))
        with patch.object(plan_review_steps, "agent_call_structured", call):
            await run_plan_review(change_dir, _stub_config(tmp_path), split_context="")

        assert "`oversized-plan`" in call.await_args.args[0]


# --- 4. Which findings reach the one regeneration pass -------------------

def _finding(category="rubric-conflict", severity="major", **kw):
    return PlanReviewFinding(
        category=category, severity=severity,
        file_path=kw.pop("file_path", "tasks.md"),
        location=kw.pop("location", "Block 2"),
        claim=kw.pop("claim", "The block mandates a global."),
        reason=kw.pop("reason", "The rubric scores globals down."),
        **kw,
    )


class TestFindingsText:
    def test_empty_when_nothing_actionable(self):
        assert plan_review_findings_text([]) == ""
        assert plan_review_findings_text([_finding(severity="minor")]) == ""

    def test_carries_the_location_the_claim_and_the_reason(self):
        text = plan_review_findings_text([_finding()])
        assert "tasks.md" in text and "Block 2" in text
        assert "The block mandates a global." in text
        assert "The rubric scores globals down." in text

    @pytest.mark.parametrize("severity", PLAN_REVIEW_ACTIONABLE)
    def test_critical_and_major_reach_the_pass(self, severity):
        assert plan_review_findings_text([_finding(severity=severity)])

    def test_oversized_plan_never_reaches_the_regeneration(self):
        """It is a proposal for a human — feeding it to a rewriter would have
        the rewriter perform the split inside one plan, which is the opposite
        of what the finding asks for."""
        assert "oversized-plan" in PLAN_REVIEW_PROPOSE_ONLY_CATEGORIES
        big = _finding(
            category="oversized-plan", severity="critical",
            proposed_cut=[PlanReviewCut(
                ticket="Migration only", blocks=[1],
                boundary="Produces: add_retry_column()")],
        )
        assert plan_review_findings_text([big]) == ""


# --- 5. The stage: one pass, checked before the model call ---------------

def _stage_setup(tmp_path, change="plan-c"):
    config = BuildConfig(
        project_dir=tmp_path, change_name=change, mode="only",
        auto=True, spec_only=True, skip_research=True,
    )
    config.specs_dir = tmp_path / "specs"
    cm = ChangeManager(config.specs_dir)
    cm.init_specs()
    cm.create_change(change)
    (config.change_dir / "proposal.md").write_text("## Why\nBecause.\n")
    (config.change_dir / "design.md").write_text("## Decisions\nOne.\n")
    (config.change_dir / "tasks.md").write_text("## Block 1\n- [ ] do it\n")
    (config.change_dir / "rubric.md").write_text("## Criteria\n- correctness\n")
    state_path = tmp_path / "state.json"
    state = BuildState(
        change_name=change, mode="only", tier="advanced",
        phase=BuildPhase.DESIGN_AUDIT, state_file=str(state_path),
    )
    return config, state, state_path


class TestPlanReviewStage:
    @pytest.mark.asyncio
    async def test_clean_review_marks_the_stage_done_without_regenerating(
        self, tmp_path,
    ):
        config, state, state_path = _stage_setup(tmp_path)
        review = AsyncMock(return_value=PlanReviewResult(findings=[]))
        regen = AsyncMock(return_value="rewritten")
        with patch.object(plan_review_steps, "run_plan_review", review), \
                patch("build_pipeline.llm_steps.spec_steps.generate_artifact", regen):
            findings = await run_plan_review_stage(
                config.change_dir, config, state, state_path)

        assert findings == []
        assert state.spec_gen.plan_review_done is True
        assert state.spec_gen.plan_review_regen_done is False
        regen.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_actionable_findings_regenerate_the_plan_exactly_once(
        self, tmp_path,
    ):
        config, state, state_path = _stage_setup(tmp_path)
        review = AsyncMock(side_effect=[
            PlanReviewResult(findings=[_finding(severity="critical")]),
            PlanReviewResult(findings=[]),  # the report-only re-check
        ])
        regen = AsyncMock(return_value="## Block 1\n- [ ] do it properly\n")
        with patch.object(plan_review_steps, "run_plan_review", review), \
                patch("build_pipeline.llm_steps.spec_steps.generate_artifact", regen):
            findings = await run_plan_review_stage(
                config.change_dir, config, state, state_path)

        assert len(findings) == 1
        assert regen.await_count == 1
        assert regen.await_args.args[0] == "tasks"
        assert "do it properly" in (config.change_dir / "tasks.md").read_text()
        # One regeneration, then ONE report-only re-check. No loop.
        assert review.await_count == 2
        assert state.spec_gen.plan_review_regen_done is True
        assert state.spec_gen.plan_review_done is True

    @pytest.mark.asyncio
    async def test_a_recorded_regeneration_is_report_only_on_resume(self, tmp_path):
        config, state, state_path = _stage_setup(tmp_path)
        state.spec_gen = SpecGenState(plan_review_regen_done=True)
        review = AsyncMock(
            return_value=PlanReviewResult(findings=[_finding(severity="critical")]))
        regen = AsyncMock()
        with patch.object(plan_review_steps, "run_plan_review", review), \
                patch("build_pipeline.llm_steps.spec_steps.generate_artifact", regen):
            await run_plan_review_stage(
                config.change_dir, config, state, state_path)

        regen.assert_not_awaited()
        assert review.await_count == 1
        assert state.spec_gen.plan_review_done is True

    @pytest.mark.asyncio
    async def test_a_spent_stage_is_checked_before_the_model_call(self, tmp_path):
        config, state, state_path = _stage_setup(tmp_path)
        state.spec_gen = SpecGenState(plan_review_done=True)
        review = AsyncMock()
        with patch.object(plan_review_steps, "run_plan_review", review):
            findings = await run_plan_review_stage(
                config.change_dir, config, state, state_path)

        assert findings == []
        review.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_review_is_not_recorded_as_done(self, tmp_path):
        """An LLM failure is a reason to try again on a resume, unlike a
        completed review."""
        config, state, state_path = _stage_setup(tmp_path)
        review = AsyncMock(side_effect=RuntimeError("model exploded"))
        with patch.object(plan_review_steps, "run_plan_review", review):
            findings = await run_plan_review_stage(
                config.change_dir, config, state, state_path)

        assert findings == []
        assert state.spec_gen.plan_review_done is False

    @pytest.mark.asyncio
    async def test_a_failed_regeneration_leaves_the_reviewed_plan_standing(
        self, tmp_path,
    ):
        config, state, state_path = _stage_setup(tmp_path)
        before = (config.change_dir / "tasks.md").read_text()
        review = AsyncMock(
            return_value=PlanReviewResult(findings=[_finding(severity="critical")]))
        regen = AsyncMock(side_effect=RuntimeError("generation exploded"))
        with patch.object(plan_review_steps, "run_plan_review", review), \
                patch("build_pipeline.llm_steps.spec_steps.generate_artifact", regen):
            await run_plan_review_stage(
                config.change_dir, config, state, state_path)

        assert (config.change_dir / "tasks.md").read_text() == before
        # The pass has been spent — a failed regeneration is not a reason to
        # try again later.
        assert state.spec_gen.plan_review_regen_done is True
        assert state.spec_gen.plan_review_done is True

    @pytest.mark.asyncio
    async def test_an_oversized_plan_finding_changes_nothing_on_disk(self, tmp_path):
        """The offer to split is a proposal the next gate approves. No code
        path here creates a ticket, moves a card, or splits tasks.md."""
        config, state, state_path = _stage_setup(tmp_path)
        before = (config.change_dir / "tasks.md").read_text()
        cut = PlanReviewCut(ticket="Migration only", blocks=[1],
                            boundary="Produces: add_retry_column()")
        review = AsyncMock(return_value=PlanReviewResult(findings=[
            _finding(category="oversized-plan", severity="critical",
                     proposed_cut=[cut]),
        ]))
        regen = AsyncMock()
        with patch.object(plan_review_steps, "run_plan_review", review), \
                patch("build_pipeline.llm_steps.spec_steps.generate_artifact", regen):
            findings = await run_plan_review_stage(
                config.change_dir, config, state, state_path)

        regen.assert_not_awaited()
        assert review.await_count == 1
        assert (config.change_dir / "tasks.md").read_text() == before
        assert findings[0].proposed_cut[0].boundary.startswith("Produces:")
        # Nothing new appeared in the change directory.
        assert not list(config.change_dir.glob("*ticket*"))

    @pytest.mark.asyncio
    async def test_flags_survive_a_checkpoint_round_trip(self, tmp_path):
        config, state, state_path = _stage_setup(tmp_path)
        review = AsyncMock(return_value=PlanReviewResult(findings=[]))
        with patch.object(plan_review_steps, "run_plan_review", review):
            await run_plan_review_stage(
                config.change_dir, config, state, state_path)

        written = json.loads(state_path.read_text())["spec_gen"]
        assert written["plan_review_done"] is True
        assert written["plan_review_regen_done"] is False
