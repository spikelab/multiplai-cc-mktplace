"""Public completion boundaries with real repositories and local test processes.

Only provider and publication side effects are doubled. Planning state represents
an already accepted revision, so late writes and resume cannot hide behind mocks
of the final gate.
"""
import argparse
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from build_pipeline import git_ops, orchestrator
from build_pipeline.config import BuildConfig, GitToggles
from build_pipeline.gates import final_verification
from build_pipeline.models import BlockInfo, BlockStatus, BuildPhase
from build_pipeline.progress import ProgressWriter
from build_pipeline.spec_generator import reconcile_plan
from build_pipeline.state import BuildState, TDDState
from build_pipeline.tdd_engine import run_tdd_engine


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def accepted_build(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    git(tmp_path, "init", "-qb", "buildme/contract")
    config = BuildConfig(project_dir=tmp_path, change_name="contract", mode="only",
                         auto=False, git=GitToggles(worktree=False, push=True, pr="none"),
                         test_command=f'{sys.executable} -c "from app import value; assert value == 1"')
    config.specs_dir = tmp_path / "specs"
    root = config.change_dir
    (root / "requirements").mkdir(parents=True)
    for name, content in {
        "proposal.md": "## Capabilities\n- `value`\n",
        "requirements/value.md": "### Requirement: Value\n#### Scenario: Read\nWHEN read\nTHEN returns one\n",
        "design.md": "# Design\nA constant", "use-cases.md": "# Use cases\nRead one",
        "unknowns.md": "# Unknowns\nNone", "tasks.md": "## 1. Value\nReturn one",
        "rubric.md": "# Rubric\nCorrect value",
    }.items():
        (root / name).write_text(content)
    (tmp_path / "app.py").write_text("value = 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "accepted implementation")
    config.pipeline_branch = "buildme/contract"
    state = BuildState(change_name="contract", mode="only", tier="advanced", phase=BuildPhase.PUBLISH,
                       branch=config.pipeline_branch, worktree_path=str(tmp_path),
                       state_file=str(config.state_file_path()),
                       tdd=TDDState(blocks=[BlockInfo(number=1, name="Value", description="Return one", status=BlockStatus.DONE)],
                                    current_block=1, baseline_tests_pass=True, refactor_all_done=True, mutation_done=True, e2e_done=True))
    reconcile_plan(config, state)
    state.spec_gen.design_audit_done = True
    state.spec_gen.plan_review_done = True
    state.checkpoint(config.state_file_path())
    args = argparse.Namespace(mode="only", change="contract", project_dir=str(tmp_path), auto=False,
                              spec_only=False, skip_research=True, interview_summary="", research_path="", context_files=[], session_id="", block=None)
    monkeypatch.setattr(orchestrator, "PRE_PUBLISH_CONTRIBUTORS", [])
    monkeypatch.setattr(git_ops, "has_remote", lambda *a: True)
    push = Mock(return_value=git_ops.GitResult([], 0, "", ""))
    monkeypatch.setattr(git_ops, "push_branch", push)
    return config, state, args, push


@pytest.mark.asyncio
async def test_last_writer_breakage_blocks_build_and_push(accepted_build, monkeypatch):
    config, state, args, push = accepted_build
    def late_writer(cfg, *_):
        (cfg.project_dir / "app.py").write_text("value = 2\n")
        git(cfg.project_dir, "add", "app.py")
        git(cfg.project_dir, "commit", "-qm", "late writer")
    monkeypatch.setattr(orchestrator, "PRE_PUBLISH_CONTRIBUTORS", [late_writer])
    assert await orchestrator.run_orchestrator(config, args) == 1
    push.assert_not_called()
    saved = BuildState.load(config.state_file_path())
    assert saved.verification["passed"] is False
    assert "AssertionError" in saved.verification["metadata"]["output"]


@pytest.mark.asyncio
async def test_failed_writer_leaving_green_partial_edits_still_blocks(accepted_build, monkeypatch):
    config, _, args, push = accepted_build
    def partial(cfg, *_):
        (cfg.project_dir / "partial.py").write_text("unfinished = True\n")
        raise RuntimeError("writer timed out")
    monkeypatch.setattr(orchestrator, "PRE_PUBLISH_CONTRIBUTORS", [partial])
    assert await orchestrator.run_orchestrator(config, args) == 1
    push.assert_not_called()
    assert (config.project_dir / "partial.py").exists()


@pytest.mark.asyncio
async def test_publish_failure_reuses_unchanged_verification(accepted_build, monkeypatch):
    config, _, args, push = accepted_build
    push.return_value = git_ops.GitResult([], 1, "", "offline")
    prepare = Mock(return_value=None)
    monkeypatch.setattr(orchestrator, "PRE_PUBLISH_CONTRIBUTORS", [prepare])
    assert await orchestrator.run_orchestrator(config, args) == 0
    saved = BuildState.load(config.state_file_path())
    assert saved.phase == BuildPhase.PUBLISH
    assert saved.publication == "failed"
    assert saved.result.kind == "verified-code"
    monkeypatch.setattr("build_pipeline.gates.final_verification", Mock(side_effect=AssertionError("unnecessary recheck")))
    push.return_value = git_ops.GitResult([], 0, "", "")
    assert await orchestrator.run_orchestrator(config, args) == 0
    assert prepare.call_count == 1
    assert push.call_count == 2


@pytest.mark.asyncio
async def test_publish_retry_rechecks_changed_inputs(accepted_build):
    config, _, args, push = accepted_build
    push.return_value = git_ops.GitResult([], 1, "", "offline")
    assert await orchestrator.run_orchestrator(config, args) == 0
    (config.project_dir / "app.py").write_text("value = 3\n")
    git(config.project_dir, "add", "app.py")
    git(config.project_dir, "commit", "-qm", "changed after verification")
    assert await orchestrator.run_orchestrator(config, args) == 1
    assert push.call_count == 1


def test_push_refuses_stale_verification(accepted_build):
    config, state, _, push = accepted_build
    state.verification = final_verification(config.test_command, config.project_dir,
        parallel=config.parallel_tests, plan_revision=state.spec_gen.revision).model_dump()
    (config.project_dir / "app.py").write_text("value = 2\n")
    with pytest.raises(RuntimeError, match="missing or stale"):
        orchestrator._run_publish(config, state, ProgressWriter(config.progress_file_path()))
    push.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["false", ""])
async def test_standalone_requires_final_checks_even_when_optional_steps_done(accepted_build, command):
    config, state, args, push = accepted_build
    config.test_command = command
    state.phase = BuildPhase.TDD_BUILD
    state.checkpoint(config.state_file_path())
    assert await run_tdd_engine(config, args) != 0
    assert not BuildState.load(config.state_file_path()).verification["passed"]
    push.assert_not_called()


@pytest.mark.asyncio
async def test_no_push_no_worktree_verifies_without_forced_commit(accepted_build):
    config, state, args, push = accepted_build
    config.git.push = False
    state.branch = None
    state.worktree_path = None
    state.checkpoint(config.state_file_path())
    (config.project_dir / "app.py").write_text("value = 1  # uncommitted\n")
    head = git(config.project_dir, "rev-parse", "HEAD")
    assert await orchestrator.run_orchestrator(config, args) == 0
    assert git(config.project_dir, "rev-parse", "HEAD") == head
    push.assert_not_called()


@pytest.mark.asyncio
async def test_historical_complete_never_republishes_or_rewrites(accepted_build):
    config, state, args, push = accepted_build
    state.phase = BuildPhase.COMPLETE
    state.checkpoint(config.state_file_path())
    before = config.state_file_path().read_bytes()
    assert await orchestrator.run_orchestrator(config, args) == 0
    push.assert_not_called()
    assert config.state_file_path().read_bytes() == before

@pytest.mark.asyncio
async def test_cancelled_standalone_tdd_persists_current_spend(accepted_build, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from build_pipeline import budget
    config, state, args, _ = accepted_build
    state.phase = BuildPhase.TDD_BUILD
    state.tdd.current_block = 0
    state.tdd.blocks[0].status = BlockStatus.PENDING
    state.checkpoint(config.state_file_path())
    async def cancelled(*a):
        budget.record(SimpleNamespace(cost_usd=.3, input_tokens=30), label="test_writer")
        raise asyncio.CancelledError()
    monkeypatch.setattr("build_pipeline.tdd_engine.run_block_tdd", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await run_tdd_engine(config, args)
    assert BuildState.load(config.state_file_path()).budget["cost_usd"] == .3


@pytest.mark.asyncio
async def test_plan_review_repair_revalidates_design_before_spec_only_success(accepted_build, monkeypatch):
    from build_pipeline.models import PlanReviewFinding, PlanReviewResult
    from build_pipeline.state import completed_state_path
    config, state, args, _ = accepted_build
    config.spec_only = True
    config.skip_explainers = True
    state.phase = BuildPhase.REVIEW
    state.tdd = None
    state.spec_gen.steps.clear()
    state.spec_gen.record_step("prototype", "skipped")
    state.checkpoint(config.state_file_path())
    design_audit = AsyncMock(return_value=[])
    monkeypatch.setattr("build_pipeline.llm_steps.spec_steps.run_design_audit", design_audit)
    monkeypatch.setattr("build_pipeline.llm_steps.spec_steps.run_tasks_audit", AsyncMock(return_value=[]))
    repair = PlanReviewFinding(category="use-case-coverage", severity="major", file_path="tasks.md",
                               location="block 1", claim="Missing case", reason="Cover the case")
    review = AsyncMock(side_effect=[PlanReviewResult(findings=[repair]), PlanReviewResult(findings=[])])
    monkeypatch.setattr("build_pipeline.llm_steps.plan_review_steps.run_plan_review", review)
    monkeypatch.setattr("build_pipeline.llm_steps.spec_steps.generate_artifact", AsyncMock(return_value="## 1. Value\nReturn one including the missing case"))
    monkeypatch.setattr("build_pipeline.spec_generator.generate_rubric", AsyncMock(return_value="# Updated rubric"))
    assert await orchestrator.run_orchestrator(config, args) == 0
    saved = BuildState.load(completed_state_path(config.state_file_path()))
    assert saved.spec_gen.step_passed("design_audit")
    assert saved.spec_gen.step_passed("plan_review")
    assert design_audit.await_count == 2
    assert review.await_count == 2


@pytest.mark.asyncio
async def test_implementation_resume_does_not_repeat_superseded_prototype(accepted_build, monkeypatch):
    config, state, args, _ = accepted_build
    config.spec_only = True
    config.skip_explainers = True
    state.phase = BuildPhase.TDD_BUILD
    state.spec_gen.record_step("prototype")
    (config.change_dir / "design.md").write_text("# Design after audit repair")
    reconcile_plan(config, state, regenerated={"design", "unknowns", "tasks", "rubric"})
    for step in ("design_audit", "plan_review", "tasks_audit", "explainers"):
        state.spec_gen.record_step(step)
    state.checkpoint(config.state_file_path())
    prototype = AsyncMock(side_effect=AssertionError("must not repeat prototype after implementation began"))
    monkeypatch.setattr(orchestrator, "_run_prototype_phase", prototype)
    assert await orchestrator.run_orchestrator(config, args) == 0
    prototype.assert_not_awaited()
