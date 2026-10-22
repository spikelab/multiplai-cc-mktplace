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
from multiplai_core.specs import mark_ready


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def accepted_build(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    git(tmp_path, "init", "-qb", "buildme/contract")
    config = BuildConfig(
        project_dir=tmp_path,
        change_name="contract",
        auto=False,
        git=GitToggles(worktree=False, push=True, pr="none"),
        test_command=f'{sys.executable} -c "from app import value; assert value == 1"',
    )
    config.specs_dir = tmp_path / "specs"
    root = config.change_dir
    (root / "requirements").mkdir(parents=True)
    for name, content in {
        "proposal.md": "## Capabilities\n- `value`\n",
        "requirements/value.md": "### Requirement: Value\n#### Scenario: Read\nWHEN read\nTHEN returns one\n",
        "design.md": "# Design\nA constant",
        "use-cases.md": "# Use cases\nRead one",
        "unknowns.md": "# Unknowns\nNone",
        "tasks.md": "## 1. Value\nReturn one",
        "rubric.md": "# Rubric\nCorrect value",
    }.items():
        (root / name).write_text(content)
    (tmp_path / "app.py").write_text("value = 1\n")
    mark_ready(root)  # pm-spec's handoff
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "accepted implementation")
    config.pipeline_branch = "buildme/contract"
    state = BuildState(
        change_name="contract",
        tier="advanced",
        phase=BuildPhase.PUBLISH,
        branch=config.pipeline_branch,
        worktree_path=str(tmp_path),
        state_file=str(config.state_file_path()),
        tdd=TDDState(
            blocks=[
                BlockInfo(
                    number=1,
                    name="Value",
                    description="Return one",
                    status=BlockStatus.DONE,
                )
            ],
            current_block=1,
            baseline_tests_pass=True,
            refactor_all_done=True,
            mutation_done=True,
            e2e_done=True,
        ),
    )
    reconcile_plan(config, state)
    state.spec_gen.record_step("design_audit", "passed")
    state.spec_gen.record_step("plan_review", "passed")
    state.checkpoint(config.state_file_path())
    args = argparse.Namespace(
        change="contract",
        project_dir=str(tmp_path),
        auto=False,
        spec_only=False,
        skip_research=True,
        research_path="",
        session_id="",
        block=None,
    )
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
async def test_failed_writer_leaving_green_partial_edits_still_blocks(
    accepted_build, monkeypatch
):
    config, _, args, push = accepted_build

    def partial(cfg, *_):
        (cfg.project_dir / "partial.py").write_text("unfinished = True\n")
        raise RuntimeError("writer timed out")

    monkeypatch.setattr(orchestrator, "PRE_PUBLISH_CONTRIBUTORS", [partial])
    assert await orchestrator.run_orchestrator(config, args) == 1
    push.assert_not_called()
    assert (config.project_dir / "partial.py").exists()


@pytest.mark.asyncio
async def test_publish_failure_reuses_unchanged_verification(
    accepted_build, monkeypatch
):
    config, _, args, push = accepted_build
    push.return_value = git_ops.GitResult([], 1, "", "offline")
    prepare = Mock(return_value=None)
    monkeypatch.setattr(orchestrator, "PRE_PUBLISH_CONTRIBUTORS", [prepare])
    assert await orchestrator.run_orchestrator(config, args) == 0
    saved = BuildState.load(config.state_file_path())
    assert saved.phase == BuildPhase.PUBLISH
    assert saved.publication == "failed"
    assert saved.result.kind == "verified-code"
    monkeypatch.setattr(
        "build_pipeline.gates.final_verification",
        Mock(side_effect=AssertionError("unnecessary recheck")),
    )
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


def test_a_build_on_a_feature_branch_opens_its_pr_against_it(
    accepted_build, monkeypatch
):
    """T4: the PR of a build of a change on a feature branch targets it."""
    config, state, _, push = accepted_build
    config.git.pr = "draft"
    config.feature_branch = "feat/contract"
    state.verification = final_verification(
        config.test_command,
        config.project_dir,
        parallel=config.parallel_tests,
        plan_revision=state.spec_gen.revision,
    ).model_dump()
    opened = []

    def pr_skill(argv, cwd=None, timeout=None):
        opened.append(argv)
        return git_ops.GitResult(argv, 0, "PR:https://example.invalid/pull/1\n", "")

    monkeypatch.setattr(git_ops, "_run_pr_skill", pr_skill)
    assert orchestrator._run_publish(
        config, state, ProgressWriter(config.progress_file_path())
    )
    push.assert_called_once()
    assert opened and opened[0][opened[0].index("--base") + 1] == "feat/contract"


def test_push_refuses_stale_verification(accepted_build):
    config, state, _, push = accepted_build
    state.verification = final_verification(
        config.test_command,
        config.project_dir,
        parallel=config.parallel_tests,
        plan_revision=state.spec_gen.revision,
    ).model_dump()
    (config.project_dir / "app.py").write_text("value = 2\n")
    with pytest.raises(RuntimeError, match="missing or stale"):
        orchestrator._run_publish(
            config, state, ProgressWriter(config.progress_file_path())
        )
    push.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["false", ""])
async def test_standalone_requires_final_checks_even_when_optional_steps_done(
    accepted_build, command
):
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
async def test_cancelled_standalone_tdd_persists_current_spend(
    accepted_build, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    from build_pipeline import budget

    config, state, args, _ = accepted_build
    state.phase = BuildPhase.TDD_BUILD
    state.tdd.current_block = 0
    state.tdd.blocks[0].status = BlockStatus.PENDING
    state.checkpoint(config.state_file_path())

    async def cancelled(*a):
        budget.record(
            SimpleNamespace(cost_usd=0.3, input_tokens=30), label="test_writer"
        )
        raise asyncio.CancelledError()

    monkeypatch.setattr("build_pipeline.tdd_engine.run_block_tdd", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await run_tdd_engine(config, args)
    assert BuildState.load(config.state_file_path()).budget["cost_usd"] == 0.3


@pytest.mark.asyncio
async def test_plan_review_repair_revalidates_design_before_spec_only_success(
    accepted_build, monkeypatch
):
    from build_pipeline.models import (
        PlanReviewFinding,
        PlanReviewResult,
        PriorAssessment,
    )
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
    monkeypatch.setattr(
        "build_pipeline.llm_steps.spec_steps.run_design_audit", design_audit
    )
    monkeypatch.setattr(
        "build_pipeline.llm_steps.spec_steps.run_tasks_audit",
        AsyncMock(return_value=[]),
    )
    repair = PlanReviewFinding(
        category="use-case-coverage",
        severity="major",
        file_path="tasks.md",
        location="block 1",
        claim="Missing case",
        reason="Cover the case",
    )
    review = AsyncMock(
        side_effect=[
            PlanReviewResult(findings=[repair]),
            PlanReviewResult(
                findings=[],
                prior_assessments=[
                    PriorAssessment(finding_id="PR-F1", status="resolved")
                ],
            ),
        ]
    )
    monkeypatch.setattr(
        "build_pipeline.llm_steps.plan_review_steps.run_plan_review", review
    )
    monkeypatch.setattr(
        "build_pipeline.llm_steps.spec_steps.generate_artifact",
        AsyncMock(return_value="## 1. Value\nReturn one including the missing case"),
    )
    monkeypatch.setattr(
        "build_pipeline.spec_generator.generate_rubric",
        AsyncMock(return_value="# Updated rubric"),
    )
    assert await orchestrator.run_orchestrator(config, args) == 0
    saved = BuildState.load(completed_state_path(config.state_file_path()))
    assert saved.spec_gen.step_passed("design_audit")
    assert saved.spec_gen.step_passed("plan_review")
    assert design_audit.await_count == 2
    assert review.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture",
    [
        "build-state-pre-prototype-tdd.json",
        "build-state-pre-docs-update.json",
        "build-state-pre-refactor-all.json",
        "build-state-pre-mutation.json",
        "build-state-pre-findings.json",
    ],
)
async def test_legacy_implementation_resume_refuses_without_rewriting_code(
    accepted_build, monkeypatch, fixture
):
    import json

    from build_pipeline import sdk

    config, _, args, push = accepted_build
    raw = json.loads((Path(__file__).parent / "fixtures" / fixture).read_text())
    raw.update(
        change_name=config.change_name,
        branch=config.pipeline_branch,
        worktree_path=str(config.project_dir),
        state_file=str(config.state_file_path()),
    )
    config.state_file_path().write_text(json.dumps(raw))
    head = git(config.project_dir, "rev-parse", "HEAD")
    before = (config.project_dir / "app.py").read_bytes()
    provider = AsyncMock(
        side_effect=AssertionError("Legacy implementation must not be rewritten")
    )
    monkeypatch.setattr(sdk, "run_agent", provider)
    assert await orchestrator.run_orchestrator(config, args) == 1
    provider.assert_not_awaited()
    push.assert_not_called()
    assert git(config.project_dir, "rev-parse", "HEAD") == head
    assert (config.project_dir / "app.py").read_bytes() == before
    saved = BuildState.load(config.state_file_path())
    assert saved.tdd.model_dump() == BuildState.model_validate(raw).tdd.model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cap", "cancel"])
async def test_mid_block_budget_stop_resumes_at_implementer_with_original_red_tests(
    accepted_build, monkeypatch, interruption
):
    import asyncio
    from types import SimpleNamespace

    from build_pipeline import budget, sdk, tdd_engine
    from build_pipeline.models import AgentResult

    config, state, args, _ = accepted_build
    config.test_command = f"{sys.executable} -m pytest -q"
    config.budget_max_usd = 0.1 if interruption == "cap" else 1
    config.git.push = False
    state.phase = BuildPhase.TDD_BUILD
    state.tdd = TDDState(
        blocks=[BlockInfo(number=1, name="Value", description="Return two")],
        refactor_all_done=True,
        mutation_done=True,
        e2e_done=True,
        baseline_tests_pass=True,
    )
    state.checkpoint(config.state_file_path())
    calls = []

    async def provider(prompt, **kwargs):
        calls.append(prompt)
        if prompt == "write tests":
            (config.project_dir / "test_value.py").write_text(
                "from app import value\ndef test_value():\n    assert value == 2\n"
            )
        else:
            assert prompt == "implement"
            if interruption == "cancel" and calls.count("implement") == 1:
                raise asyncio.CancelledError()
            (config.project_dir / "app.py").write_text("value = 2  # implemented\n")
        return SimpleNamespace(
            text="STATUS: DONE\n",
            turns=1,
            files_changed=[],
            usage=SimpleNamespace(cost_usd=0.2, input_tokens=10),
        )

    monkeypatch.setattr(sdk, "run_agent", provider)

    async def writer(**kwargs):
        return await sdk.agent_call(
            "write tests", allowed_tools=["Write"], cwd=str(config.project_dir)
        )

    async def implementer(**kwargs):
        return await sdk.agent_call(
            "implement", allowed_tools=["Write"], cwd=str(config.project_dir)
        )

    monkeypatch.setattr(tdd_engine, "run_test_writer", writer)
    monkeypatch.setattr(tdd_engine, "run_implementer", implementer)
    monkeypatch.setattr(
        tdd_engine,
        "run_refactorer",
        AsyncMock(return_value=AgentResult(success=True, output="STATUS: DONE")),
    )
    monkeypatch.setattr(
        tdd_engine, "_run_integration_and_review", AsyncMock(return_value=True)
    )
    with pytest.raises(
        budget.BudgetExceededError if interruption == "cap" else asyncio.CancelledError
    ):
        await run_tdd_engine(config, args)
    saved = BuildState.load(config.state_file_path())
    assert saved.tdd.blocks[0].status == BlockStatus.IMPLEMENTING
    assert saved.tdd.blocks[0].red_evidence
    assert saved.tdd.blocks[0].test_commit
    assert saved.budget["cost_usd"] == 0.2
    tests_before = (config.project_dir / "test_value.py").read_bytes()
    config.budget_max_usd = 1
    if interruption == "cancel":
        assert await run_tdd_engine(config, args) == 1
        assert calls == ["write tests", "implement"]
        from build_pipeline.__main__ import build_parser

        resume = build_parser().parse_args(
            ["tdd", "--change", "contract", "--accept-unknown-spend"]
        )
        config.accept_unknown_spend = resume.accept_unknown_spend
    assert await run_tdd_engine(config, args) == 0
    assert calls == ["write tests", "implement"] + (
        ["implement"] if interruption == "cancel" else []
    )
    assert (config.project_dir / "test_value.py").read_bytes() == tests_before
    completed = BuildState.load(
        config.state_file_path().with_name(".build-state.complete.json")
    )
    assert completed.verification["passed"]
    assert completed.budget["cost_usd"] == 0.4
    if interruption == "cancel":
        assert completed.budget["accepted_unknown_calls"] == 1
        assert completed.budget["unaccounted_calls"] == ["agent"]


@pytest.mark.parametrize("output", ["report.json", "results/test.db", ".coverage"])
def test_ignored_reports_survive_verification_commit_and_restart(
    accepted_build, output
):
    config, _, _, _ = accepted_build
    root = config.project_dir
    (root / ".gitignore").write_text(output + "\n")
    git(root, "add", ".gitignore")
    git(root, "commit", "-qm", "ignore reports")
    command = (
        f"{sys.executable} -c \"from pathlib import Path; p=Path('{output}'); "
        "p.parent.mkdir(parents=True, exist_ok=True); p.write_text('report')\""
    )
    assert final_verification(command, root).passed
    assert git_ops.tree_is_clean(root)
    assert git_ops.commit_tree(root, "reports only") is None
    (root / "app.py").write_text("value = 2\n")
    assert git_ops.commit_tree(root, "implementation")
    assert git(root, "ls-files", "--", output) == ""
    script = (
        f"import sys; sys.path.insert(0, {str(Path(git_ops.__file__).parents[1])!r}); "
        "from build_pipeline import git_ops; from pathlib import Path; "
        f"root=Path({str(root)!r}); assert git_ops.tree_is_clean(root); "
        "assert git_ops.commit_tree(root, 'fresh process') is None"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=root, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_verification_names_unignored_report_and_gitignore_remedy(accepted_build):
    config, _, _, _ = accepted_build
    result = final_verification(
        f"{sys.executable} -c \"from pathlib import Path; Path('report.json').write_text('report')\"",
        config.project_dir,
    )
    assert not result.passed
    assert "report.json" in result.reason and ".gitignore" in result.reason
    assert not git_ops.tree_is_clean(config.project_dir)


def test_tracked_report_remains_a_protected_input(accepted_build):
    config, _, _, _ = accepted_build
    root = config.project_dir
    (root / "report.txt").write_text("tracked")
    git(root, "add", "report.txt")
    git(root, "commit", "-qm", "track report")
    (root / ".gitignore").write_text("report.txt\n")
    result = final_verification(
        f"{sys.executable} -c \"from pathlib import Path; Path('report.txt').write_text('changed')\"",
        root,
    )
    assert not result.passed and "report.txt" in result.reason


def test_agent_pytest_reports_are_ignored_through_block_commit(accepted_build):
    from build_pipeline import tdd_engine

    config, state, _, _ = accepted_build
    root = config.project_dir
    (root / ".gitignore").write_text("test-results/\n")
    git(root, "add", ".gitignore")
    git(root, "commit", "-qm", "ignore test reports")
    (root / "test_value.py").write_text(
        "from app import value\ndef test_value():\n    assert value == 1\n"
    )
    config.test_command = (
        f"{sys.executable} -m pytest -q --junitxml=test-results/junit.xml"
    )
    for _ in range(4):
        result = subprocess.run(
            config.test_command.split(), cwd=root, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr
    assert tdd_engine._git_commit_block_phase(config, "test", state.tdd.blocks[0])
    assert git(root, "ls-files", "--", "test-results/junit.xml") == ""
    assert (root / "test-results/junit.xml").is_file()
    assert final_verification(config.test_command, root).passed
    assert git_ops.tree_is_clean(root)


def test_committed_junit_report_can_be_recovered_using_verification_remedy(
    accepted_build,
):
    import shlex

    from build_pipeline.gates import run_test_suite

    config, _, _, _ = accepted_build
    root = config.project_dir
    (root / "test_value.py").write_text(
        "from app import value\ndef test_value():\n    assert value == 2\n"
    )
    # Keep interpreter caches out so the assertions isolate report recovery.
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    command = f"{shlex.quote(sys.executable)} -m pytest -q --junitxml=junit.xml"
    assert run_test_suite(command, root)[0] == 1
    assert git_ops.commit_tree(root, "test: RED and generated report")
    assert git(root, "ls-files", "--", "junit.xml") == "junit.xml"
    (root / "app.py").write_text("value = 2  # implemented\n")
    assert git_ops.commit_tree(root, "impl: return two")
    failed = final_verification(command, root)
    assert not failed.passed and "junit.xml" in failed.reason
    assert "1 passed" in failed.metadata["output"]
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\njunit.xml\n")
    git(root, "add", ".gitignore")
    git(root, "commit", "-qm", "ignore report without untracking")
    still_failed = final_verification(command, root)
    assert not still_failed.passed and "junit.xml" in still_failed.reason
    assert "1 passed" in still_failed.metadata["output"]
    assert "git rm --cached <path>" in still_failed.reason
    assert "add it to .gitignore, and commit" in still_failed.reason
    # Follow the remedy: the working report is preserved, and HEAD stops tracking it.
    git(root, "rm", "--cached", "junit.xml")
    assert (root / "junit.xml").exists()
    git(root, "add", ".gitignore")
    git(root, "commit", "-qm", "stop tracking generated report")
    assert final_verification(command, root).passed
    assert git_ops.tree_is_clean(root)
    assert not git(root, "ls-files", "--", "junit.xml")
