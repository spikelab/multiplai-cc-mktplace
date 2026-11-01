"""A component of a change built from one plan reads its change's
`implementation-plan.md` (`change_manager.reads_change_plan`): it never
writes a plan, never edits the change's, and a change of the plan makes the
component's own documents stale. A change whose components were each planned
on their own keeps building that way."""

from __future__ import annotations

import os

import pytest
from build_pipeline import spec_generator
from build_pipeline.change_manager import (
    ChangeManager,
    plan_path,
    reads_change_plan,
)
from build_pipeline.config import BuildConfig
from build_pipeline.models import PlanEdit
from build_pipeline.prompts.spec_generation import CHANGE_PLAN_EDIT_RULE
from build_pipeline.spec_generator import (
    PlanRepairRejected,
    apply_plan_edits,
    reconcile_plan,
)
from build_pipeline.state import BuildState, SpecGenState
from build_pipeline.tdd_engine import assemble_context

from .test_nested_components import _block, _config, write_nested

PLAN = "# Implementation plan\n\n## Decisions\n\nOne session store.\n"


@pytest.fixture
def change(tmp_path):
    root = tmp_path / "specs" / "changes" / "e"
    write_nested(root)
    return root


def test_a_component_with_no_plan_reads_its_changes(change):
    a, b = change / "components" / "a", change / "components" / "b"
    # No plan anywhere yet: a new change, planned once for every component.
    assert reads_change_plan(a) and reads_change_plan(b)
    assert plan_path(a) == change / "implementation-plan.md"
    (change / "implementation-plan.md").write_text(PLAN)
    assert plan_path(b) == change / "implementation-plan.md"
    # A single change, and the change itself, read their own.
    assert not reads_change_plan(change)


def test_a_change_whose_components_were_planned_one_by_one_keeps_that(change):
    a, b = change / "components" / "a", change / "components" / "b"
    (a / "design.md").write_text("# Design of a\n")
    assert not reads_change_plan(a) and plan_path(a) == a / "design.md"
    # b has no plan yet: it writes its own, as a did.
    assert not reads_change_plan(b) and plan_path(b) == b / "implementation-plan.md"
    # Once the change has a plan, a component with none reads it.
    (change / "implementation-plan.md").write_text(PLAN)
    assert reads_change_plan(b) and not reads_change_plan(a)


def test_an_archived_component_reads_its_archived_changes_plan(tmp_path):
    root = tmp_path / "specs" / "archive" / "2026-11-01-e"
    write_nested(root)
    (root / "implementation-plan.md").write_text(PLAN)
    assert plan_path(root / "components" / "a") == root / "implementation-plan.md"


def test_the_writers_and_the_tdd_agents_get_the_changes_plan(change):
    (change / "implementation-plan.md").write_text(PLAN)
    config = _config(change.parents[2], "e/b")
    cm = ChangeManager(config.specs_dir)
    deps = cm.artifact_context(config.change_dir, "tasks")["dependencies"]
    assert deps["design"] == os.path.join("..", "..", "implementation-plan.md")
    assert config.design_path == change / "implementation-plan.md"
    assert "One session store." in assemble_context(_block(), config, "implementer")
    status = cm.artifact_status(config.change_dir)
    assert status["design"].value == "done"


@pytest.mark.asyncio
async def test_a_component_never_writes_the_plan(change):
    config = _config(change.parents[2], "e/b")
    state = BuildState(change_name="e/b", tier="advanced", spec_gen=SpecGenState())
    with pytest.raises(RuntimeError, match="Run /buildme e to write it"):
        await spec_generator._generate_single_artifact(
            ChangeManager(config.specs_dir), config.change_dir, "design", config, state
        )
    assert not (config.change_dir / "implementation-plan.md").exists()
    assert not (change / "implementation-plan.md").exists()


def _planned(change, config: BuildConfig) -> BuildState:
    """Component b with its documents written and their hashes recorded."""
    (change / "implementation-plan.md").write_text(PLAN)
    for name in ("unknowns.md", "tasks.md", "rubric.md"):
        (config.change_dir / name).write_text(f"# {name}\n")
    state = BuildState(change_name="e/b", tier="advanced", spec_gen=SpecGenState())
    reconcile_plan(config, state)
    assert state.spec_gen.stale_artifacts == []
    return state


@pytest.mark.parametrize(
    "edit",
    ["plan", "specs", "proposal"],
)
def test_what_would_make_the_plan_stale_makes_the_components_documents_stale(
    change, edit
):
    config = _config(change.parents[2], "e/b")
    state = _planned(change, config)
    before = state.spec_gen.revision
    target = {
        "plan": change / "implementation-plan.md",
        "specs": config.change_dir / "specs.md",
        "proposal": change / "proposal.md",
    }[edit]
    target.write_text(target.read_text() + "\nEdited.\n")
    reconcile_plan(config, state)
    assert state.spec_gen.stale_artifacts == ["rubric", "tasks", "unknowns"]
    assert state.spec_gen.revision != before


def test_a_rewrite_may_not_edit_the_changes_plan(change):
    (change / "implementation-plan.md").write_text(PLAN)
    b = change / "components" / "b"
    (b / "tasks.md").write_text("## 1. Form\n\nKeep the form.\n")
    edits = [
        PlanEdit(file="tasks.md", old_str="Keep the form.", new_str="Keep it."),
        PlanEdit(file="implementation-plan.md", old_str="One", new_str="Two"),
    ]
    with pytest.raises(PlanRepairRejected, match="approved implementation plan"):
        apply_plan_edits(b, edits)
    assert (change / "implementation-plan.md").read_text() == PLAN
    # tasks.md alone is applied as before.
    assert apply_plan_edits(b, edits[:1]) == {"tasks.md": "## 1. Form\n\nKeep it.\n"}


@pytest.mark.asyncio
async def test_the_rewrite_is_told_to_edit_only_tasks_md(change, monkeypatch):
    from build_pipeline.llm_steps import plan_review_steps
    from build_pipeline.models import PlanEdits

    prompts = []

    async def fake(prompt, model_type, **kw):
        prompts.append(prompt)
        return PlanEdits(edits=[])

    monkeypatch.setattr(plan_review_steps, "agent_call_structured", fake)
    (change / "implementation-plan.md").write_text(PLAN)
    config = _config(change.parents[2], "e/b")
    await plan_review_steps.run_plan_edit(config.change_dir, config, [])
    assert CHANGE_PLAN_EDIT_RULE in prompts[0]
    assert "One session store." in prompts[0]

    single = change.parents[1] / "single"
    single.mkdir()
    (single / "implementation-plan.md").write_text(PLAN)
    await plan_review_steps.run_plan_edit(single, config, [])
    assert CHANGE_PLAN_EDIT_RULE not in prompts[1]
