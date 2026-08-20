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


# --- 3. The wired oversized-plan seam ------------------------------------

SPLITTABLE_TASKS = """# Tasks

## 1. Parse the export log

Read ingest/reader.py and yield rows.

Interfaces:
- Produces: `parse_log(path: Path) -> list[Row]`
- Consumes: (none)

## 2. Store the parsed rows

Write the rows through ingest/store.py.

Interfaces:
- Produces: `store_rows(rows: list[Row]) -> None`
- Consumes: `parse_log(path: Path) -> list[Row]`

## 3. Render the dashboard page

Adds dashboard/views.py rendering the summary.

Interfaces:
- Produces: `render_dashboard() -> str`
- Consumes: (none)
"""

CHAINED_TASKS = """# Tasks

## 1. Parse the export log

Interfaces:
- Produces: `parse_log(path: Path) -> list[Row]`

## 2. Store the parsed rows

Interfaces:
- Produces: `store_rows(rows: list[Row]) -> None`
- Consumes: `parse_log(path: Path) -> list[Row]`

## 3. Summarise the stored rows

Interfaces:
- Produces: `summarise() -> Summary`
- Consumes: `store_rows(rows: list[Row]) -> None`
"""

MIGRATION_PLUS_FEATURE_TASKS = """# Tasks

## 1. Add the archived column

A schema change: alter table exports to add column archived.

Interfaces:
- Produces: `0007_add_archived` migration
- Consumes: (none)

## 2. Render the dashboard page

Interfaces:
- Produces: `render_dashboard() -> str`
- Consumes: (none)
"""


def _tasks_dir(tmp_path, text: str, name: str = "split") -> Path:
    change_dir = tmp_path / name
    change_dir.mkdir(parents=True)
    (change_dir / "tasks.md").write_text(text)
    return change_dir


class TestSplitContextSeam:
    """`_split_context` hands the reviewer buildme's own parsed graph.

    It replaces the reviewer's reading of every `Interfaces:` section with the
    graph the builder itself will run on — a better input to check 2, not a new
    check. Everything here stays a proposal: no ticket, no card, no edit.
    """

    def test_a_splittable_plan_names_its_groups_and_their_cut(self, tmp_path):
        context = _split_context(
            _stub_config(tmp_path), _tasks_dir(tmp_path, SPLITTABLE_TASKS))

        assert "2 independently-shippable group(s)" in context
        assert "Group 1: blocks 1, 2 — Parse the export log; Store the parsed rows" in context
        assert "Group 2: block 3 — Render the dashboard page" in context
        # `is_clean` is rendered: a cut no signature crosses is the cheap one.
        assert "Cut after group 1 (clean): between block 2 and block 3" in context
        # The module's own composite answer reaches the reviewer instead of
        # being computed and discarded. Here it is "no": the plan comes apart,
        # but at 3 blocks no trigger fired and no block is high-risk.
        assert "no cut is indicated" in context
        assert "arithmetic, not a finding" in context

    def test_the_cut_carries_the_exact_signature_boundary_it_crosses(self, tmp_path):
        context = _split_context(
            _stub_config(tmp_path), _tasks_dir(tmp_path, SPLITTABLE_TASKS))

        # Verbatim signatures, not a paraphrase: a cut a reviewer cannot name a
        # boundary for is not a cut (PLAN_REVIEW_PROMPT).
        assert "boundary: parse_log(path: Path) -> list[Row]; store_rows(rows: list[Row]) -> None" in context
        assert "crosses: nothing — no signature spans this cut" in context

    def test_the_composite_verdict_says_split_when_the_arithmetic_says_so(
        self, tmp_path,
    ):
        """`PlanSplitAssessment.should_split` is the module's stated top-level
        answer. Rendering it is what stops the reviewer re-deriving it from
        prose."""
        tasks = "# Tasks\n\n" + "\n".join(
            f"## {i}. Widget {i}\n\nRender widget {i}.\n\n"
            f"Interfaces:\n- Produces: `render_widget_{i}(data: dict) -> str`\n"
            for i in range(1, 12)
        )
        context = _split_context(
            _stub_config(tmp_path), _tasks_dir(tmp_path, tasks))

        assert "Size trigger (>8): FIRED" in context
        assert "THIS PLAN COMES APART" in context
        assert "arithmetic, not a finding" in context

    def test_a_fully_chained_plan_renders_as_one_atomic_group_with_the_reason(
        self, tmp_path,
    ):
        context = _split_context(
            _stub_config(tmp_path), _tasks_dir(tmp_path, CHAINED_TASKS))

        assert "1 independently-shippable group(s)" in context
        assert "Group 1: blocks 1, 2, 3" in context
        assert "one atomic change and cannot be reviewed or reverted in pieces" in context
        assert "Cut after group" not in context

    def test_atomicity_quotes_the_literal_keyword_that_matched(self, tmp_path):
        context = _split_context(
            _stub_config(tmp_path),
            _tasks_dir(tmp_path, MIGRATION_PLUS_FEATURE_TASKS))

        assert "SPLIT: high-risk work ships beside unrelated feature work" in context
        assert 'migration: "migration"' in context
        assert '"alter table"' in context
        assert "unrelated feature work: block 2 (Render the dashboard page)" in context

    def test_the_package_spread_names_which_block_reached_which_package(
        self, tmp_path,
    ):
        context = _split_context(
            _stub_config(tmp_path), _tasks_dir(tmp_path, SPLITTABLE_TASKS))

        assert "### Package spread — 2 top-level package(s)" in context
        assert "- ingest: blocks 1, 2" in context
        assert "- dashboard: block 3" in context

    def test_a_missing_tasks_md_renders_no_context_and_does_not_raise(
        self, tmp_path,
    ):
        """A change with no plan yet still gets reviewed: the prompt's check 2
        tells the reviewer to do the partition itself."""
        assert _split_context(_stub_config(tmp_path), tmp_path / "nowhere") == ""

    def test_an_unparseable_tasks_md_renders_no_context(self, tmp_path):
        change_dir = _tasks_dir(tmp_path, "just prose, no numbered headings\n")
        assert _split_context(_stub_config(tmp_path), change_dir) == ""

    def test_blocks_with_no_interfaces_say_the_graph_is_unavailable(
        self, tmp_path,
    ):
        """Said out loud rather than returned as "": with no signatures every
        block is its own component, and silence would read as a clean split."""
        change_dir = _tasks_dir(
            tmp_path, "## 1. Do it\n\n- [ ] a\n\n## 2. Do more\n\n- [ ] b\n")
        context = _split_context(_stub_config(tmp_path), change_dir)

        assert "graph unavailable" in context
        assert "independently-shippable" not in context

    def test_an_unexpected_failure_degrades_to_no_context(self, tmp_path):
        change_dir = _tasks_dir(tmp_path, SPLITTABLE_TASKS)
        with patch("build_pipeline.tdd_engine.parse_blocks",
                   side_effect=RuntimeError("boom")):
            assert _split_context(_stub_config(tmp_path), change_dir) == ""

    def test_the_triggers_are_read_from_config(self, tmp_path):
        config = _stub_config(
            tmp_path, plan_split_block_trigger=2, plan_split_package_trigger=1)
        context = _split_context(config, _tasks_dir(tmp_path, SPLITTABLE_TASKS))

        assert "Size trigger (>2): FIRED" in context
        assert "Package trigger (>1): FIRED" in context

    def test_the_triggers_fall_back_when_the_config_object_is_stale(
        self, tmp_path,
    ):
        config = _stub_config(tmp_path)
        del config.plan_split_block_trigger
        del config.plan_split_package_trigger
        context = _split_context(config, _tasks_dir(tmp_path, SPLITTABLE_TASKS))

        assert "Size trigger (>8): not fired" in context
        assert "Package trigger (>3): not fired" in context

    def test_the_placeholder_trigger_is_never_presented_as_a_verdict(
        self, tmp_path,
    ):
        context = _split_context(
            _stub_config(tmp_path), _tasks_dir(tmp_path, SPLITTABLE_TASKS))

        assert "A trigger is not a verdict" in context
        assert "unmeasured placeholder" in context

    def test_building_the_graph_writes_nothing(self, tmp_path):
        """It proposes; a gate disposes. Reading tasks.md must not touch it."""
        change_dir = _tasks_dir(tmp_path, SPLITTABLE_TASKS)
        before = sorted(p.name for p in change_dir.iterdir())
        tasks = (change_dir / "tasks.md").read_text()

        _split_context(_stub_config(tmp_path), change_dir)

        assert sorted(p.name for p in change_dir.iterdir()) == before
        assert (change_dir / "tasks.md").read_text() == tasks
        assert "oversized-plan" in PLAN_REVIEW_PROPOSE_ONLY_CATEGORIES

    @pytest.mark.asyncio
    async def test_the_rendered_context_reaches_the_prompts_split_context_slot(
        self, tmp_path,
    ):
        change_dir = _change_dir(tmp_path)
        (change_dir / "tasks.md").write_text(SPLITTABLE_TASKS)
        context = _split_context(_stub_config(tmp_path), change_dir)

        call = AsyncMock(return_value=PlanReviewResult(findings=[]))
        with patch.object(plan_review_steps, "agent_call_structured", call):
            await run_plan_review(
                change_dir, _stub_config(tmp_path), split_context=context)

        prompt = call.await_args.args[0]
        assert "Group 2: block 3 — Render the dashboard page" in prompt
        assert "boundary: parse_log(path: Path) -> list[Row]" in prompt

    @pytest.mark.asyncio
    async def test_the_stage_parses_the_plan_and_feeds_the_graph_to_the_prompt(
        self, tmp_path,
    ):
        """The whole wiring, end to end: nothing passes `split_context` in by
        hand in production — the stage builds it from tasks.md."""
        config, state, state_path = _stage_setup(tmp_path, change="plan-split")
        (config.change_dir / "tasks.md").write_text(SPLITTABLE_TASKS)

        call = AsyncMock(return_value=PlanReviewResult(findings=[]))
        with patch.object(plan_review_steps, "agent_call_structured", call):
            await run_plan_review_stage(
                config.change_dir, config, state, state_path)

        prompt = call.await_args.args[0]
        assert "## Block dependency partition" in prompt
        assert "Group 1: blocks 1, 2" in prompt

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
    async def test_the_rewritten_plan_is_re_audited_for_shape(self, tmp_path):
        """The shape audit is what catches a horizontally-decomposed plan, and
        it only ever ran inside `_generate_single_artifact` — so the plan the
        build is actually built against had never been through it."""
        config, state, state_path = _stage_setup(tmp_path)
        review = AsyncMock(side_effect=[
            PlanReviewResult(findings=[_finding(severity="critical")]),
            PlanReviewResult(findings=[]),
        ])
        regen = AsyncMock(return_value="## Block 1\n- [ ] do it properly\n")
        shape_audit = AsyncMock(return_value=[])
        with patch.object(plan_review_steps, "run_plan_review", review), \
                patch("build_pipeline.llm_steps.spec_steps.generate_artifact", regen), \
                patch("build_pipeline.llm_steps.spec_steps.run_tasks_audit",
                      shape_audit), \
                patch("build_pipeline.spec_generator.generate_rubric",
                      AsyncMock(return_value="## Criteria\n- fresh\n")):
            await run_plan_review_stage(config.change_dir, config, state, state_path)

        shape_audit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_rubric_is_regenerated_against_the_rewritten_plan(
        self, tmp_path,
    ):
        """ARTIFACT_DAG declares rubric requires tasks, and the per-block review
        scores blocks against rubric.md — leaving it describing the plan this
        rewrite replaced scores new blocks on old criteria."""
        config, state, state_path = _stage_setup(tmp_path)
        review = AsyncMock(side_effect=[
            PlanReviewResult(findings=[_finding(severity="critical")]),
            PlanReviewResult(findings=[]),
        ])
        regen = AsyncMock(return_value="## Block 1\n- [ ] do it properly\n")
        rubric = AsyncMock(return_value="## Criteria\n- covers the new blocks\n")
        with patch.object(plan_review_steps, "run_plan_review", review), \
                patch("build_pipeline.llm_steps.spec_steps.generate_artifact", regen), \
                patch("build_pipeline.llm_steps.spec_steps.run_tasks_audit",
                      AsyncMock(return_value=[])), \
                patch("build_pipeline.spec_generator.generate_rubric", rubric):
            await run_plan_review_stage(config.change_dir, config, state, state_path)

        rubric.assert_awaited_once()
        assert "covers the new blocks" in (config.change_dir / "rubric.md").read_text()

    @pytest.mark.asyncio
    async def test_a_failed_re_audit_or_rubric_leaves_the_plan_standing(
        self, tmp_path,
    ):
        """Both follow-ups are non-fatal, like every other step in this stage."""
        config, state, state_path = _stage_setup(tmp_path)
        review = AsyncMock(side_effect=[
            PlanReviewResult(findings=[_finding(severity="critical")]),
            PlanReviewResult(findings=[]),
        ])
        regen = AsyncMock(return_value="## Block 1\n- [ ] do it properly\n")
        with patch.object(plan_review_steps, "run_plan_review", review), \
                patch("build_pipeline.llm_steps.spec_steps.generate_artifact", regen), \
                patch("build_pipeline.llm_steps.spec_steps.run_tasks_audit",
                      AsyncMock(side_effect=RuntimeError("audit exploded"))), \
                patch("build_pipeline.spec_generator.generate_rubric",
                      AsyncMock(side_effect=RuntimeError("rubric exploded"))):
            findings = await run_plan_review_stage(
                config.change_dir, config, state, state_path)

        assert len(findings) == 1
        assert "do it properly" in (config.change_dir / "tasks.md").read_text()
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
