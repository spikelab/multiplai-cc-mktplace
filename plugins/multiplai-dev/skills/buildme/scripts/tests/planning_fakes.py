"""Deterministic planning/publication fixtures for routing tests.

Completion-contract tests use the public entry points without these fixtures.
"""
from build_pipeline import git_ops
from build_pipeline.orchestrator import _run_publish


def _plan_fixture(config, state):
    """Coherent planning output for phase-wiring tests (planner tested separately)."""
    from build_pipeline.change_manager import ARTIFACT_DAG
    from build_pipeline.spec_generator import reconcile_plan

    from tests.spec_fixtures import write_specs
    root = config.change_dir
    write_specs(root)
    for name, text in {"design": "# Design", "unknowns": "# Unknowns", "tasks": "## 1. Behavior\nReturn a value", "rubric": "# Rubric"}.items():
        path = root / f"{name}.md"
        if not path.exists():
            path.write_text(text)
    reconcile_plan(config, state, regenerated=set(ARTIFACT_DAG))
    state.spec_gen.record_step("tasks_audit", "passed")
    state.spec_gen.record_step("explainers", "passed")
    state.checkpoint(config.state_file_path())


def _prepare_and_publish(config, state, progress, **kwargs):
    """Publication-only tests start after the real preparation/verification boundary."""
    import os

    from build_pipeline.gates import final_verification
    from build_pipeline.orchestrator import _pre_publish_sections, _worktree_section
    os.environ["BUILDME_TRUST_REPO"] = "1"
    config.test_command = config.test_command or "true"
    docs = kwargs.get("docs_dir", config.change_dir)
    sections = [p for p in [_worktree_section(state, docs)] if p is not None]
    sections += _pre_publish_sections(config, state, docs)
    state.publish_sections = [str(p) for p in sections]
    gate = final_verification(config.test_command, config.project_dir, parallel=config.parallel_tests,
                             plan_revision=state.spec_gen.revision if state.spec_gen else "")
    assert gate.passed, gate.reason
    state.verification = gate.model_dump()
    return _run_publish(config, state, progress, **kwargs)



from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def accepted_planning(monkeypatch, request):
    """Supply complete plans to tests of intake, boards and extraction routing."""
    from build_pipeline import orchestrator, spec_generator
    from build_pipeline.llm_steps import plan_review_steps, spec_steps
    from build_pipeline.models import GateResult, PlanReviewResult

    async def generated(cm, change_dir, config, state):
        _plan_fixture(config, state)
        git_ops.commit_stage(config, f"docs(specs): {config.change_name} — fixture plan", [str(change_dir.relative_to(config.project_dir))])

    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    real = orchestrator.run_orchestrator
    async def run(config, args):
        config.test_command = config.test_command or "true"
        return await real(config, args)

    monkeypatch.setattr(spec_generator, "_generate_all_artifacts", generated)
    monkeypatch.setattr(spec_steps, "run_tasks_audit", AsyncMock(return_value=[]))
    monkeypatch.setattr(spec_steps, "run_design_audit", AsyncMock(return_value=[]))
    monkeypatch.setattr(plan_review_steps, "run_plan_review", AsyncMock(return_value=PlanReviewResult(findings=[])))
    monkeypatch.setattr("build_pipeline.llm_steps.docs_steps.run_docs_update", AsyncMock(return_value=([], GateResult(passed=True, reason="fixture docs"))))
    monkeypatch.setattr(orchestrator, "run_orchestrator", run)
    if hasattr(request.module, "run_orchestrator"):
        monkeypatch.setattr(request.module, "run_orchestrator", run)
