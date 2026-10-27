"""The plan rewrite edits in place, loops a bounded number of times, and stops
at the spend cap (PR D of plan-buildme-planning-checks-2026-10-27.md, items
18-21 and row S8)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from build_pipeline import budget
from build_pipeline import spec_generator as owner
from build_pipeline.llm_steps import plan_review_steps
from build_pipeline.llm_steps.plan_review_steps import (
    run_plan_review,
    run_plan_review_stage,
)
from build_pipeline.models import (
    PlanEdit,
    PlanEdits,
    PlanReviewFinding,
    PlanReviewResult,
    PriorAssessment,
)
from build_pipeline.spec_generator import PlanBlocked, PlanRepairRejected
from build_pipeline.state import BuildState

from tests.planning_fakes import edits_from
from tests.test_plan_review import _change_dir, _stage_setup, _stub_config

TASKS = "## 1. Behavior\n- [ ] do it\n"
REWRITES = [
    "## 1. Behavior\n- [ ] do it properly\n",
    "## 1. Behavior\n- [ ] do it right\n",
]


@pytest.fixture(autouse=True)
def rubric_writer():
    with patch(
        "build_pipeline.spec_generator.generate_rubric",
        AsyncMock(return_value="# Current rubric"),
    ):
        yield


def _add_planning_section(config) -> None:
    path = config.specs_dir / "config.yaml"
    before = path.read_text() if path.exists() else ""
    path.write_text(before + "planning:\n  max_usd: 60\n")


def _gap(claim: str, severity: str = "critical") -> PlanReviewFinding:
    return PlanReviewFinding(
        category="coverage",
        severity=severity,
        file_path="tasks.md",
        claim=claim,
        reason="fix it",
    )


def _statuses(**status: str) -> list[PriorAssessment]:
    return [PriorAssessment(finding_id=i, status=s) for i, s in status.items()]


def _recheck(**status: str) -> PlanReviewResult:
    return PlanReviewResult(prior_assessments=_statuses(**status))


async def _run(config, state, state_path, answers, rewrites=REWRITES):
    review = AsyncMock(side_effect=answers)
    writer = AsyncMock(side_effect=list(rewrites))
    edit = edits_from(writer)
    with (
        patch.object(plan_review_steps, "run_plan_review", review),
        patch.object(plan_review_steps, "run_plan_edit", edit),
    ):
        await run_plan_review_stage(config.change_dir, config, state, state_path)
    return review, edit


# --- apply_plan_edits ----------------------------------------------------------


def _staged(tmp_path, **texts):
    for name, text in {"design.md": "# D\none\n", "tasks.md": TASKS, **texts}.items():
        (tmp_path / name).write_text(text)
    return tmp_path


def test_edits_apply_in_order_each_to_the_text_the_last_left(tmp_path):
    staged = _staged(tmp_path)
    edited = owner.apply_plan_edits(
        staged,
        [
            PlanEdit(file="tasks.md", old_str="do it", new_str="do it now"),
            PlanEdit(file="tasks.md", old_str="it now", new_str="it today"),
            PlanEdit(file="design.md", old_str="one", new_str="two"),
        ],
    )
    assert (staged / "tasks.md").read_text() == "## 1. Behavior\n- [ ] do it today\n"
    assert (staged / "design.md").read_text() == "# D\ntwo\n"
    assert set(edited) == {"tasks.md", "design.md"}


@pytest.mark.parametrize(
    ("old", "message"),
    [("not there", "not found"), ("o", "found 2 times")],
)
def test_an_edit_that_does_not_match_once_rejects_and_writes_nothing(
    tmp_path, old, message
):
    staged = _staged(tmp_path, **{"design.md": "# D\nfoo\n"})
    with pytest.raises(
        PlanRepairRejected, match=f"Plan edit 2 in design.md.*{message}"
    ):
        owner.apply_plan_edits(
            staged,
            [
                PlanEdit(file="tasks.md", old_str="do it", new_str="do it now"),
                PlanEdit(file="design.md", old_str=old, new_str="x"),
            ],
        )
    assert (staged / "tasks.md").read_text() == TASKS
    assert (staged / "design.md").read_text() == "# D\nfoo\n"


def test_no_edits_is_a_rejection_with_a_blocking_finding(tmp_path):
    with pytest.raises(PlanRepairRejected, match="no edits") as rejected:
        owner.apply_plan_edits(_staged(tmp_path), [])
    assert rejected.value.blocking[0]["severity"] == "critical"
    assert "no edits" in rejected.value.blocking[0]["text"]


def test_edits_that_change_nothing_are_a_rejection(tmp_path):
    staged = _staged(tmp_path)
    with pytest.raises(PlanRepairRejected, match="change nothing"):
        owner.apply_plan_edits(
            staged,
            [
                PlanEdit(file="tasks.md", old_str="do it", new_str="do that"),
                PlanEdit(file="tasks.md", old_str="do that", new_str="do it"),
            ],
        )
    assert (staged / "tasks.md").read_text() == TASKS


@pytest.mark.asyncio
async def test_a_missed_edit_blocks_the_step_and_the_resume_refuses(tmp_path):
    """Row S4: the rejection is saved as the blocking findings at this
    revision, so a resume refuses at no cost."""
    config, state, state_path = _stage_setup(tmp_path)
    miss = AsyncMock(
        return_value=PlanEdits(
            edits=[PlanEdit(file="tasks.md", old_str="absent", new_str="x")]
        )
    )
    review = AsyncMock(return_value=PlanReviewResult(findings=[_gap("GAP")]))
    with (
        patch.object(plan_review_steps, "run_plan_review", review),
        patch.object(plan_review_steps, "run_plan_edit", miss),
        pytest.raises(PlanRepairRejected),
    ):
        await run_plan_review_stage(config.change_dir, config, state, state_path)
    saved = BuildState.load(state_path)
    assert "not found" in saved.spec_gen.steps["plan_review"].blocking_findings
    with (
        patch.object(plan_review_steps, "run_plan_review", review),
        pytest.raises(PlanBlocked),
    ):
        await run_plan_review_stage(config.change_dir, config, saved, state_path)
    assert review.await_count == 1


# --- The loop ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_rewrite_that_resolves_everything_passes(tmp_path):
    config, state, state_path = _stage_setup(tmp_path)
    _review, edit = await _run(
        config,
        state,
        state_path,
        [
            PlanReviewResult(findings=[_gap("ONE"), _gap("TWO")]),
            _recheck(**{"PR-F1": "resolved", "PR-F2": "still_present"}),
            _recheck(**{"PR-F2": "resolved"}),
        ],
    )
    step = state.spec_gen.steps["plan_review"]
    assert state.spec_gen.step_passed("plan_review")
    assert edit.await_count == 2
    assert (step.repair_attempts, step.blocking_counts) == (2, [2, 1])
    assert [f["finding_id"] for f in edit.await_args_list[1].args[2]] == ["PR-F2"]
    assert (config.change_dir / "tasks.md").read_text() == REWRITES[1]
    assert (config.change_dir / "plan-edits-1.diff").exists()
    assert "+- [ ] do it right" in (config.change_dir / "plan-edits-2.diff").read_text()


@pytest.mark.asyncio
async def test_it_stops_at_the_rewrite_limit(tmp_path, capsys):
    config, state, state_path = _stage_setup(tmp_path)
    with pytest.raises(PlanBlocked, match="2 of planning.max_rewrites 2"):
        await _run(
            config,
            state,
            state_path,
            [
                PlanReviewResult(findings=[_gap("ONE"), _gap("TWO"), _gap("THREE")]),
                _recheck(
                    **{
                        "PR-F1": "resolved",
                        "PR-F2": "still_present",
                        "PR-F3": "still_present",
                    }
                ),
                _recheck(**{"PR-F2": "resolved", "PR-F3": "still_present"}),
            ],
        )
    step = BuildState.load(state_path).spec_gen.steps["plan_review"]
    assert step.outcome == "blocked" and step.repair_attempts == 2
    assert step.blocked_revision == state.spec_gen.step_revision("plan_review")
    assert "THREE" in step.blocking_findings
    assert (
        "plan_review blocked: 2 of planning.max_rewrites 2" in capsys.readouterr().out
    )


@pytest.mark.asyncio
async def test_it_stops_when_a_rewrite_does_not_lower_the_count(tmp_path, capsys):
    """Row S7: the recheck's count is not lower, so no second rewrite."""
    config, state, state_path = _stage_setup(tmp_path)
    with pytest.raises(PlanBlocked):
        _, _edit = await _run(
            config,
            state,
            state_path,
            [
                PlanReviewResult(findings=[_gap("ONE")]),
                _recheck(**{"PR-F1": "still_present"}),
            ],
        )
    assert (
        "plan_review blocked: rewrite 1 left 1 blocking findings (was 1)"
        in capsys.readouterr().out
    )
    step = BuildState.load(state_path).spec_gen.steps["plan_review"]
    assert (step.repair_attempts, step.blocking_counts) == (1, [1])


@pytest.mark.asyncio
async def test_it_stops_at_the_spend_cap_before_a_rewrite(
    tmp_path, capsys, monkeypatch
):
    """Row S8: spend is checked before the admission; no rewrite is paid for."""
    config, state, state_path = _stage_setup(tmp_path)
    config.planning_max_usd = 5.0
    monkeypatch.setattr(
        budget.get_budget(), "usd_by_label", {"spec:design": 3.0, "plan_review": 2.5}
    )
    with pytest.raises(PlanBlocked):
        _, _edit = await _run(
            config,
            state,
            state_path,
            [PlanReviewResult(findings=[_gap("ONE")])],
        )
    assert (
        "plan_review blocked: planning spend $5.50 reached planning.max_usd $5.00"
        in capsys.readouterr().out
    )
    step = BuildState.load(state_path).spec_gen.steps["plan_review"]
    assert step.repair_attempts == 0
    assert step.blocked_revision == state.spec_gen.step_revision("plan_review")


def test_planning_spend_counts_only_planning_labels(monkeypatch):
    monkeypatch.setattr(
        budget.get_budget(),
        "usd_by_label",
        {
            "spec:design": 1.0,
            "spec:tasks": 1.0,
            "plan_rewrite": 1.0,
            "plan_review": 1.0,
            "rubric": 1.0,
            "explainer": 1.0,
            "implementer": 50.0,
            "codebase_analysis": 50.0,
        },
    )
    assert owner.planning_spend() == 6.0


# --- The recheck is shown the diff ---------------------------------------------


@pytest.mark.asyncio
async def test_the_recheck_prompt_contains_the_diff(tmp_path):
    change_dir = _change_dir(tmp_path)
    call = AsyncMock(return_value=PlanReviewResult(findings=[]))
    with patch.object(plan_review_steps, "agent_call_structured", call):
        await run_plan_review(
            change_dir,
            _stub_config(tmp_path),
            prior=[{"finding_id": "PR-F1", "severity": "critical", "text": "GAP"}],
            changed_lines="-- [ ] do it\n+- [ ] do it properly\n",
        )
    prompt = call.await_args.args[0]
    assert "+- [ ] do it properly" in prompt
    assert "caused by them" in prompt and "A new critical finding" in prompt


@pytest.mark.asyncio
async def test_the_stage_passes_this_attempts_diff_to_the_recheck(tmp_path):
    config, state, state_path = _stage_setup(tmp_path)
    review, _ = await _run(
        config,
        state,
        state_path,
        [PlanReviewResult(findings=[_gap("ONE")]), _recheck(**{"PR-F1": "resolved"})],
    )
    first, recheck = review.await_args_list
    assert first.kwargs.get("changed_lines", "") == ""
    assert (
        recheck.kwargs["changed_lines"]
        == (config.change_dir / "plan-edits-1.diff").read_text()
    )


# --- Row S8: config.yaml's planning: section -----------------------------------


def test_a_planning_section_is_cut_out_with_its_blank_lines():
    text = "a: 1\n\nplanning:\n  max_usd: 60\n  # note\n\nb: 2\n"
    rest, section = owner._split_planning_section(text)
    assert rest == "a: 1\n\nb: 2\n"
    assert section == "planning:\n  max_usd: 60\n  # note\n\n"
    assert owner._split_planning_section("a: 1\n") == ("a: 1\n", "")


def test_raising_the_cap_marks_nothing_stale_and_reopens_the_check(tmp_path):
    config, state, _state_path = _stage_setup(tmp_path)
    (config.specs_dir / "config.yaml").write_text("project: x\n")
    owner.reconcile_plan(config, state)
    sg = state.spec_gen
    hashes, revision = dict(sg.artifact_hashes), sg.revision
    stale = list(sg.stale_artifacts)
    check_revision = sg.step_revision("plan_review")
    _add_planning_section(config)
    owner.reconcile_plan(config, state)
    assert sg.artifact_hashes["config.yaml"] == hashes["config.yaml"]
    assert "planning" in sg.artifact_hashes and "planning" not in hashes
    assert sg.revision == revision
    assert sg.stale_artifacts == stale
    assert sg.step_revision("plan_review") != check_revision
    assert sg.step_revision("explainers") == state.spec_gen.step_revision("explainers")


@pytest.mark.asyncio
async def test_a_step_blocked_at_the_cap_reopens_with_a_fresh_count(
    tmp_path, monkeypatch
):
    config, state, state_path = _stage_setup(tmp_path)
    (config.specs_dir / "config.yaml").write_text("project: x\n")
    config.planning_max_usd = 1.0
    monkeypatch.setattr(budget.get_budget(), "usd_by_label", {"plan_review": 2.0})
    with pytest.raises(PlanBlocked):
        await _run(
            config, state, state_path, [PlanReviewResult(findings=[_gap("ONE")])]
        )
    saved = BuildState.load(state_path)
    stale = list(saved.spec_gen.stale_artifacts)
    _add_planning_section(config)
    config.planning_max_usd = 60.0
    _review, edit = await _run(
        config,
        saved,
        state_path,
        [PlanReviewResult(findings=[_gap("ONE")]), _recheck(**{"PR-F1": "resolved"})],
    )
    assert saved.spec_gen.step_passed("plan_review")
    assert edit.await_count == 1
    assert set(saved.spec_gen.stale_artifacts) <= set(stale)
