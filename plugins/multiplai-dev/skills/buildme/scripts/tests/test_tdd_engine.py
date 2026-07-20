"""Tests for TDD engine — block parsing, context assembly, agent selection, gates."""

import os
import re
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import subprocess

from build_pipeline.tdd_engine import (
    parse_blocks,
    assemble_context,
    run_block_tdd,
    run_test_quality_check,
    run_tdd_engine,
    _capture_block_diff,
    _detect_entry_point,
    _git_commit_block_phase,
    _run_final_review,
    _run_integration_and_review,
    _run_quality_review,
    _verify_entry_point,
    EMPTY_TREE_SHA,
    WEAK_TEST_PATTERNS,
    MAX_REVIEW_ITERATIONS,
    EXIT_SUCCESS,
    EXIT_BUILD_FAILURE,
    EXIT_AGENT_TIMEOUT,
)
from build_pipeline.config import BuildConfig
from build_pipeline.models import (
    AgentResult,
    BlockInfo,
    BlockStatus,
    FinalReviewVerdict,
    GateResult,
    ReviewResult,
    ReviewScore,
    WeakTestFinding,
)
from build_pipeline.models import TestQualityAudit as QualityAudit  # Test* name breaks pytest collection
from build_pipeline.state import BuildState, TDDState
from build_pipeline.progress import ProgressWriter


# --- Sample tasks.md content ---

TASKS_COARSE = """\
## 1. Core Infrastructure

Set up the project structure with base models, configuration loading,
and database connection management.

Satisfies: core-infra, config-loading

## 2. Authentication Engine

Implement JWT-based auth with refresh tokens, role-based access control,
and session management.

Satisfies: auth-engine, rbac

## 3. API Layer

Build REST endpoints for all CRUD operations with proper validation,
error handling, and pagination.

Satisfies: api-endpoints, validation
"""

TASKS_CHECKBOX = """\
## 1. Core Infrastructure

- [ ] 1.1 Create project skeleton with pyproject.toml
- [ ] 1.2 Set up base models with Pydantic
- [ ] 1.3 Configure database connection

## 2. Authentication Engine

- [ ] 2.1 Implement JWT token generation
- [ ] 2.2 Add refresh token flow
- [ ] 2.3 Role-based access control
- [ ] 2.4 Session management

## 3. API Layer

- [ ] 3.1 CRUD endpoints
- [ ] 3.2 Input validation middleware
- [ ] 3.3 Error handling
"""

TASKS_MIXED = """\
## 1. Setup

Initialize the project.

Satisfies: setup

## 2. Features

- [ ] 2.1 Add feature A
- [ ] 2.2 Add feature B
"""


class TestParseBlocks:
    def test_parse_coarse_format(self, tmp_path):
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(TASKS_COARSE)
        blocks = parse_blocks(tasks_file)

        assert len(blocks) == 3
        assert blocks[0].number == 1
        assert blocks[0].name == "Core Infrastructure"
        assert "project structure" in blocks[0].description
        assert blocks[0].satisfies == ["core-infra", "config-loading"]

        assert blocks[1].number == 2
        assert blocks[1].name == "Authentication Engine"
        assert blocks[1].satisfies == ["auth-engine", "rbac"]

        assert blocks[2].number == 3
        assert blocks[2].name == "API Layer"

    def test_parse_checkbox_format(self, tmp_path):
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(TASKS_CHECKBOX)
        blocks = parse_blocks(tasks_file)

        assert len(blocks) == 3
        assert blocks[0].number == 1
        assert blocks[0].name == "Core Infrastructure"
        # Checkbox items assembled into description
        assert "Create project skeleton" in blocks[0].description
        assert "Set up base models" in blocks[0].description
        assert "Configure database" in blocks[0].description

        assert blocks[1].number == 2
        assert "JWT token generation" in blocks[1].description

    def test_parse_mixed_format(self, tmp_path):
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(TASKS_MIXED)
        blocks = parse_blocks(tasks_file)

        assert len(blocks) == 2
        assert blocks[0].satisfies == ["setup"]
        assert "Initialize" in blocks[0].description
        assert "Add feature A" in blocks[1].description
        assert "Add feature B" in blocks[1].description

    def test_parse_missing_file(self, tmp_path):
        blocks = parse_blocks(tmp_path / "nonexistent.md")
        assert blocks == []

    def test_parse_empty_file(self, tmp_path):
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text("")
        blocks = parse_blocks(tasks_file)
        assert blocks == []

    def test_parse_single_block(self, tmp_path):
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text("## 1. Only Block\n\nJust one block here.\n\nSatisfies: solo\n")
        blocks = parse_blocks(tasks_file)

        assert len(blocks) == 1
        assert blocks[0].name == "Only Block"
        assert blocks[0].satisfies == ["solo"]
        assert "Just one block" in blocks[0].description

    def test_block_numbers_are_ints(self, tmp_path):
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(TASKS_COARSE)
        blocks = parse_blocks(tasks_file)
        for block in blocks:
            assert isinstance(block.number, int)

    def test_satisfies_empty_when_absent(self, tmp_path):
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text("## 1. No Satisfies\n\nJust a description.\n")
        blocks = parse_blocks(tasks_file)
        assert blocks[0].satisfies == []


class TestAssembleContext:
    def _make_config(self, tmp_path):
        """Create a BuildConfig with a populated change directory."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        specs_root = project_dir / "specs"
        change_dir = specs_root / "changes" / "test-change"
        change_dir.mkdir(parents=True)

        # Design doc
        (change_dir / "design.md").write_text("# Design\nSome design content.")

        # Requirements (BDD scenarios) — flat one file per capability
        req_dir = change_dir / "requirements"
        req_dir.mkdir(parents=True)
        (req_dir / "auth.md").write_text("# Auth Requirements\nWHEN user logs in THEN token issued.")

        # Rubric
        (change_dir / "rubric.md").write_text("# Rubric\nCode quality criteria.")

        config = BuildConfig(
            project_dir=project_dir,
            change_name="test-change",
            project_description="A test project for testing.",
            config_dir=tmp_path / "claude-config",
            core_memory_files=[],
            stack_memory_files=[],
            additional_memory_files=[],
        )
        config.specs_dir = specs_root
        return config

    def test_includes_block_info(self, tmp_path):
        config = self._make_config(tmp_path)
        block = BlockInfo(number=1, name="Auth", description="Build auth module", satisfies=["auth"])
        ctx = assemble_context(block, config, "test_writer")
        assert "Block 1: Auth" in ctx
        assert "Build auth module" in ctx
        assert "auth" in ctx

    def test_includes_design_doc(self, tmp_path):
        config = self._make_config(tmp_path)
        block = BlockInfo(number=1, name="X", description="desc")
        ctx = assemble_context(block, config, "implementer")
        assert "Design Document" in ctx
        assert "Some design content" in ctx

    def test_includes_specs(self, tmp_path):
        config = self._make_config(tmp_path)
        block = BlockInfo(number=1, name="X", description="desc")
        ctx = assemble_context(block, config, "test_writer")
        assert "Auth Requirements" in ctx
        assert "WHEN user logs in" in ctx

    def test_includes_rubric(self, tmp_path):
        config = self._make_config(tmp_path)
        block = BlockInfo(number=1, name="X", description="desc")
        ctx = assemble_context(block, config, "implementer")
        assert "Evaluation Rubric" in ctx
        assert "Code quality criteria" in ctx

    def test_includes_project_description(self, tmp_path):
        config = self._make_config(tmp_path)
        block = BlockInfo(number=1, name="X", description="desc")
        ctx = assemble_context(block, config, "test_writer")
        assert "A test project for testing" in ctx

    def test_missing_optional_files(self, tmp_path):
        """Context assembly works even if design/specs/rubric are missing."""
        project_dir = tmp_path / "bare-project"
        project_dir.mkdir()
        specs_root = project_dir / "specs"
        change_dir = specs_root / "changes" / "bare"
        change_dir.mkdir(parents=True)

        config = BuildConfig(
            project_dir=project_dir,
            change_name="bare",
            config_dir=tmp_path / "claude-config",
            core_memory_files=[],
            stack_memory_files=[],
            additional_memory_files=[],
        )
        config.specs_dir = specs_root

        block = BlockInfo(number=1, name="Solo", description="Just this")
        ctx = assemble_context(block, config, "test_writer")
        assert "Block 1: Solo" in ctx
        assert "Just this" in ctx


class TestModelAdaptiveAgentSelection:
    """Test that tier-dependent behavior drives the right agent configuration."""

    def test_advanced_tier_no_refactor(self):
        config = BuildConfig(tier="advanced")
        assert not config.refactor_phase
        assert config.tdd_phases == ["test", "implement"]
        assert config.implementer_prompt_style == "clean"

    def test_standard_tier_has_refactor(self):
        config = BuildConfig(tier="standard")
        assert config.refactor_phase
        assert config.tdd_phases == ["test", "implement", "refactor"]
        assert config.implementer_prompt_style == "minimum"

    def test_advanced_agent_scope_per_block(self):
        config = BuildConfig(tier="advanced")
        assert config.agent_scope == "per_block"

    def test_standard_agent_scope_per_task(self):
        config = BuildConfig(tier="standard")
        assert config.agent_scope == "per_task"


class TestTestQualityCheck:
    def test_passes_clean_tests(self):
        content = """\
def test_addition():
    assert add(2, 3) == 5

def test_subtraction():
    assert subtract(5, 3) == 2

def test_empty_list():
    result = process([])
    assert result == []
"""
        config = BuildConfig(gates=MagicMock(test_quality_enabled=True))
        result = run_test_quality_check(content, "", config)
        assert result.passed

    def test_fails_assert_true(self):
        content = """\
def test_something():
    assert True

def test_another():
    assert True

def test_real():
    assert add(1, 1) == 2
"""
        config = BuildConfig(gates=MagicMock(test_quality_enabled=True))
        result = run_test_quality_check(content, "", config)
        # 2 out of 3 are weak = 66%, above 20% threshold
        assert not result.passed
        assert "weak" in result.reason.lower()

    def test_fails_empty_bodies(self):
        content = """\
def test_a():
    pass

def test_b():
    pass

def test_c():
    pass
"""
        config = BuildConfig(gates=MagicMock(test_quality_enabled=True))
        result = run_test_quality_check(content, "", config)
        assert not result.passed

    def test_passes_when_disabled(self):
        content = "def test_x():\n    assert True\n"
        config = BuildConfig(gates=MagicMock(test_quality_enabled=False))
        result = run_test_quality_check(content, "", config)
        assert result.passed
        assert "disabled" in result.reason.lower()

    def test_fails_no_tests(self):
        content = "# No tests here\npass\n"
        config = BuildConfig(gates=MagicMock(test_quality_enabled=True))
        result = run_test_quality_check(content, "", config)
        assert not result.passed
        assert "No test functions" in result.reason

    def test_low_ratio_passes(self):
        """One weak test out of many is acceptable."""
        tests = ["def test_real_%d():\n    assert compute(%d) == %d\n" % (i, i, i*2) for i in range(10)]
        tests.append("def test_weak():\n    assert True\n")
        content = "\n".join(tests)
        config = BuildConfig(gates=MagicMock(test_quality_enabled=True))
        result = run_test_quality_check(content, "", config)
        # 1 out of 11 = ~9%, below 20%
        assert result.passed


class TestWeakPatterns:
    """Test that the WEAK_TEST_PATTERNS regexes match what they should."""

    def test_assert_true_pattern(self):
        assert WEAK_TEST_PATTERNS[0].search("    assert True")
        assert WEAK_TEST_PATTERNS[0].search("assert True")
        assert not WEAK_TEST_PATTERNS[0].search("assert result is True")

    def test_assert_not_none_sole_assertion(self):
        # The pattern matches assert X is not None at end of line
        assert WEAK_TEST_PATTERNS[1].search("    assert result is not None")
        assert WEAK_TEST_PATTERNS[1].search("assert x is not None")

    def test_empty_body_pattern(self):
        text = "def test_foo():\n    pass"
        assert WEAK_TEST_PATTERNS[2].search(text)

        text_ellipsis = "def test_foo():\n    ..."
        assert WEAK_TEST_PATTERNS[2].search(text_ellipsis)


class TestIntegrationGateWiring:
    """Test that integration gate results drive the correct pipeline behavior."""

    def test_passing_gate_continues(self):
        gate = GateResult(passed=True, reason="All tests pass")
        assert gate.passed

    def test_failing_gate_has_action(self):
        gate = GateResult(
            passed=False,
            reason="Tests failing",
            action="spawn_fix_agent",
            metadata={"stderr": "AssertionError", "stdout": "1 failed"},
        )
        assert not gate.passed
        assert gate.action == "spawn_fix_agent"


class TestReviewLoopCounting:
    """Test review iteration counting and limits."""

    def test_iterations_within_limit(self):
        from build_pipeline.gates import review_iteration_gate
        for i in range(MAX_REVIEW_ITERATIONS):
            result = review_iteration_gate(i, MAX_REVIEW_ITERATIONS)
            assert result.passed

    def test_iteration_at_limit_fails(self):
        from build_pipeline.gates import review_iteration_gate
        result = review_iteration_gate(MAX_REVIEW_ITERATIONS, MAX_REVIEW_ITERATIONS)
        assert not result.passed
        assert result.action == "halt_build"

    def test_max_review_iterations_is_3(self):
        assert MAX_REVIEW_ITERATIONS == 3


class TestPromptTemplates:
    """Test that prompt templates have correct placeholders."""

    def test_test_writer_prompt_placeholders(self):
        from build_pipeline.prompts.test_writing import TEST_WRITER_PROMPT
        placeholders = re.findall(r"\{(\w+)\}", TEST_WRITER_PROMPT)
        assert "block_name" in placeholders
        assert "block_description" in placeholders
        assert "specs" in placeholders
        assert "context_bundle" in placeholders
        assert "test_command" in placeholders

    def test_implementer_clean_placeholders(self):
        from build_pipeline.prompts.implementation import IMPLEMENTER_PROMPT_CLEAN
        placeholders = re.findall(r"\{(\w+)\}", IMPLEMENTER_PROMPT_CLEAN)
        assert "block_name" in placeholders
        assert "failing_tests" in placeholders
        assert "context_bundle" in placeholders

    def test_implementer_minimum_placeholders(self):
        from build_pipeline.prompts.implementation import IMPLEMENTER_PROMPT_MINIMUM
        placeholders = re.findall(r"\{(\w+)\}", IMPLEMENTER_PROMPT_MINIMUM)
        assert "failing_tests" in placeholders
        assert "context_bundle" in placeholders

    def test_refactor_prompt_placeholders(self):
        from build_pipeline.prompts.implementation import REFACTOR_PROMPT
        placeholders = re.findall(r"\{(\w+)\}", REFACTOR_PROMPT)
        assert "block_name" in placeholders
        assert "context_bundle" in placeholders
        assert "test_command" in placeholders

    def test_prompts_can_be_formatted(self):
        """All prompts format without error when given the right kwargs."""
        from build_pipeline.prompts.test_writing import TEST_WRITER_PROMPT
        from build_pipeline.prompts.implementation import (
            IMPLEMENTER_PROMPT_CLEAN,
            IMPLEMENTER_PROMPT_MINIMUM,
            REFACTOR_PROMPT,
        )
        result = TEST_WRITER_PROMPT.format(
            block_name="Test", block_description="desc",
            specs="specs", context_bundle="ctx", test_command="pytest",
        )
        assert "Test" in result

        IMPLEMENTER_PROMPT_CLEAN.format(
            block_name="Test", block_description="desc",
            failing_tests="tests", context_bundle="ctx", test_command="pytest",
        )
        IMPLEMENTER_PROMPT_MINIMUM.format(
            block_name="Test", block_description="desc",
            failing_tests="tests", context_bundle="ctx", test_command="pytest",
        )
        REFACTOR_PROMPT.format(
            block_name="Test", block_description="desc",
            context_bundle="ctx", test_command="pytest",
        )


class TestTDDStepToolAllowlists:
    """Verify tool allowlists and timeouts for each agent type."""

    def test_test_writer_tools(self):
        from build_pipeline.llm_steps.tdd_steps import TEST_WRITER_TOOLS, TEST_WRITER_MAX_TURNS, TEST_WRITER_TIMEOUT
        assert "Read" in TEST_WRITER_TOOLS
        assert "Write" in TEST_WRITER_TOOLS
        assert "Bash" in TEST_WRITER_TOOLS
        assert "Glob" in TEST_WRITER_TOOLS
        assert "Grep" in TEST_WRITER_TOOLS
        assert "Edit" not in TEST_WRITER_TOOLS  # test writers don't edit
        assert TEST_WRITER_MAX_TURNS == 30
        assert TEST_WRITER_TIMEOUT == 20 * 60

    def test_implementer_tools(self):
        from build_pipeline.llm_steps.tdd_steps import IMPLEMENTER_TOOLS, IMPLEMENTER_MAX_TURNS, IMPLEMENTER_TIMEOUT
        assert "Read" in IMPLEMENTER_TOOLS
        assert "Write" in IMPLEMENTER_TOOLS
        assert "Edit" in IMPLEMENTER_TOOLS  # implementers CAN edit
        assert "Bash" in IMPLEMENTER_TOOLS
        assert IMPLEMENTER_MAX_TURNS == 50
        assert IMPLEMENTER_TIMEOUT == 30 * 60

    def test_refactorer_tools(self):
        from build_pipeline.llm_steps.tdd_steps import REFACTORER_TOOLS, REFACTORER_MAX_TURNS, REFACTORER_TIMEOUT
        assert "Read" in REFACTORER_TOOLS
        assert "Edit" in REFACTORER_TOOLS
        assert REFACTORER_MAX_TURNS == 30
        assert REFACTORER_TIMEOUT == 15 * 60


# A suite result that satisfies the RED gate: non-zero exit, genuine failure.
RED_SUITE_RESULT = (1, "FAILED tests/test_x.py::test_a - AssertionError: not implemented")


@pytest.fixture
def red_gate_passes():
    """Make the RED gate see a properly failing suite (most engine tests
    exercise other phases and just need RED to be satisfied)."""
    with patch("build_pipeline.tdd_engine.run_test_suite", return_value=RED_SUITE_RESULT):
        yield


@pytest.fixture
def quality_audit_overturns():
    """Have the LLM test-quality auditor overturn the static scan (most engine
    tests use placeholder agent output with no test code, which the static
    scan flags; the audit path itself is tested separately)."""
    from build_pipeline.models import TestQualityAudit
    with patch(
        "build_pipeline.tdd_engine._audit_test_quality",
        new_callable=AsyncMock, return_value=TestQualityAudit(passed=True),
    ):
        yield


class TestRunBlockTDD:
    """Test run_block_tdd with mocked agents."""

    @pytest.fixture(autouse=True)
    def _red(self, red_gate_passes, quality_audit_overturns):
        yield

    @pytest.fixture
    def setup(self, tmp_path):
        """Create config, state, and progress for block TDD tests."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        specs_root = project_dir / "specs"
        change_dir = specs_root / "changes" / "test"
        change_dir.mkdir(parents=True)
        (change_dir / "design.md").write_text("# Design")

        config = BuildConfig(
            project_dir=project_dir,
            change_name="test",
            tier="advanced",
            test_command="pytest -xvs",
            config_dir=tmp_path / "config",
            core_memory_files=[],
            stack_memory_files=[],
            additional_memory_files=[],
        )
        config.specs_dir = specs_root

        block = BlockInfo(number=1, name="Block1", description="Test block")
        state = BuildState(
            change_name="test",
            mode="only",
            tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )

        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("test", "only", "advanced", 1)

        return config, state, progress, block

    @pytest.mark.asyncio
    async def test_successful_block(self, setup):
        config, state, progress, block = setup
        ok_result = AgentResult(success=True, output="Tests written", files_changed=["test_x.py"])

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=ok_result), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=ok_result):
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        assert block.status == BlockStatus.IMPLEMENTING

    @pytest.mark.asyncio
    async def test_test_writer_failure(self, setup):
        config, state, progress, block = setup
        fail_result = AgentResult(success=False, error="Agent crashed")

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=fail_result):
            result = await run_block_tdd(block, config, state, progress)

        assert result is False
        assert block.status == BlockStatus.FAILED
        # An ordinary (non-timeout) failure must NOT set the timeout flag.
        assert block.timed_out is False

    @pytest.mark.asyncio
    async def test_test_writer_timeout_sets_flag(self, setup):
        """agent_call degrades a timeout to a failed AgentResult(timed_out=True);
        run_block_tdd must surface that as block.timed_out so the orchestrator
        can distinguish a real timeout from an ordinary build failure."""
        config, state, progress, block = setup
        timeout_result = AgentResult(
            success=False, error="Agent timed out after 1200s", timed_out=True
        )

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=timeout_result):
            result = await run_block_tdd(block, config, state, progress)

        assert result is False
        assert block.status == BlockStatus.FAILED
        assert block.timed_out is True

    @pytest.mark.asyncio
    async def test_implementer_timeout_sets_flag(self, setup):
        config, state, progress, block = setup
        ok_result = AgentResult(success=True, output="Tests written")
        timeout_result = AgentResult(
            success=False, error="Agent timed out after 1800s", timed_out=True
        )

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=ok_result), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=timeout_result):
            result = await run_block_tdd(block, config, state, progress)

        assert result is False
        assert block.timed_out is True

    @pytest.mark.asyncio
    async def test_implementer_failure(self, setup):
        config, state, progress, block = setup
        ok_result = AgentResult(success=True, output="Tests written")
        fail_result = AgentResult(success=False, error="Impl failed")

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=ok_result), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=fail_result):
            result = await run_block_tdd(block, config, state, progress)

        assert result is False
        assert block.status == BlockStatus.FAILED

    @pytest.mark.asyncio
    async def test_noncontiguous_block_number_indexed_by_position(self, setup):
        """Block state is indexed by list position, not block.number - 1.

        With a block numbered 5 living at list index 0, the old block.number-1=4
        index was out of range for the 1-element list, so mark_block_status
        silently no-oped and the FAILED status was never recorded.
        """
        config, state, progress, _ = setup
        block = BlockInfo(number=5, name="Block5", description="non-contiguous")
        state.tdd.blocks = [block]
        state.tdd.current_block = 0
        fail_result = AgentResult(success=False, error="crash")

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=fail_result):
            result = await run_block_tdd(block, config, state, progress)

        assert result is False
        assert state.tdd.blocks[0].status == BlockStatus.FAILED

    @pytest.mark.asyncio
    async def test_standard_tier_runs_refactorer(self, setup):
        config, state, progress, block = setup
        config.tier = "standard"
        ok_result = AgentResult(success=True, output="Done")

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=ok_result), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=ok_result), \
             patch("build_pipeline.tdd_engine.run_refactorer", new_callable=AsyncMock, return_value=ok_result) as mock_refactor:
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        mock_refactor.assert_called_once()

    @pytest.mark.asyncio
    async def test_advanced_tier_skips_refactorer(self, setup):
        config, state, progress, block = setup
        config.tier = "advanced"
        ok_result = AgentResult(success=True, output="Done")

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=ok_result), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=ok_result), \
             patch("build_pipeline.tdd_engine.run_refactorer", new_callable=AsyncMock, return_value=ok_result) as mock_refactor:
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        mock_refactor.assert_not_called()


class TestGitCommitScoping:
    """_git_commit_block_phase must not leak buildme bookkeeping into user commits."""

    def _init_repo(self, repo: Path):
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / ".gitkeep").write_text("")
        subprocess.run(["git", "add", ".gitkeep"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    def test_bookkeeping_files_excluded_from_block_commit(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        self._init_repo(project_dir)
        specs_root = project_dir / "specs"
        change_dir = specs_root / "changes" / "feat"
        change_dir.mkdir(parents=True)

        config = BuildConfig(project_dir=project_dir, change_name="feat")
        config.specs_dir = specs_root

        # A block's real code change, plus buildme's own side-writes.
        (project_dir / "module.py").write_text("x = 1\n")
        config.progress_file_path().write_text("# progress\n")
        config.state_file_path().write_text("{}\n")

        sha = _git_commit_block_phase(config, "impl", BlockInfo(number=1, name="B", description="d"))
        assert sha is not None

        committed = subprocess.run(
            ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
            cwd=project_dir, capture_output=True, text=True, check=True,
        ).stdout.split()
        assert "module.py" in committed
        assert "build-progress.md" not in committed
        assert not any(".build-state.json" in f for f in committed)

        # The bookkeeping files must remain untracked afterward.
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=project_dir, capture_output=True, text=True, check=True,
        ).stdout
        assert "build-progress.md" not in tracked
        assert ".build-state.json" not in tracked


class TestRedGateWiring:
    """The RED gate runs between the test-writer and implementer phases."""

    @pytest.fixture(autouse=True)
    def _quality(self, quality_audit_overturns):
        yield

    @pytest.fixture
    def setup(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        specs_root = project_dir / "specs"
        change_dir = specs_root / "changes" / "test"
        change_dir.mkdir(parents=True)

        config = BuildConfig(
            project_dir=project_dir,
            change_name="test",
            tier="advanced",
            test_command="pytest -xvs",
            config_dir=tmp_path / "config",
            core_memory_files=[],
            stack_memory_files=[],
            additional_memory_files=[],
        )
        config.specs_dir = specs_root

        block = BlockInfo(number=1, name="Block1", description="Test block")
        state = BuildState(
            change_name="test", mode="only", tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("test", "only", "advanced", 1)
        return config, state, progress, block

    @pytest.mark.asyncio
    async def test_red_pass_stores_evidence(self, setup):
        config, state, progress, block = setup
        ok = AgentResult(success=True, output="Tests written")

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=ok), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=ok), \
             patch("build_pipeline.tdd_engine.run_test_suite", return_value=RED_SUITE_RESULT):
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        assert "AssertionError" in block.red_evidence
        # Evidence also lands in the progress file
        assert "RED evidence" in progress.path.read_text()

    @pytest.mark.asyncio
    async def test_red_failure_retries_test_writer_once_then_passes(self, setup):
        """First run: suite passes (tests test nothing) → one test-writer retry;
        second run fails properly → RED confirmed."""
        config, state, progress, block = setup
        ok = AgentResult(success=True, output="Tests written")
        suite_results = iter([(0, "5 passed"), RED_SUITE_RESULT])

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=ok) as tw, \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=ok), \
             patch("build_pipeline.tdd_engine.run_test_suite", side_effect=lambda *a, **k: next(suite_results)):
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        assert tw.call_count == 2  # initial + one RED retry
        # The retry prompt carries the gate's reason
        retry_desc = tw.call_args_list[1].kwargs["block_description"]
        assert "RED Gate Failure" in retry_desc
        assert block.red_evidence  # captured on the second (passing) run

    @pytest.mark.asyncio
    async def test_red_failure_twice_fails_block(self, setup):
        config, state, progress, block = setup
        ok = AgentResult(success=True, output="Tests written")

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=ok) as tw, \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=ok) as impl, \
             patch("build_pipeline.tdd_engine.run_test_suite", return_value=(0, "5 passed")):
            result = await run_block_tdd(block, config, state, progress)

        assert result is False
        assert block.status == BlockStatus.FAILED
        assert tw.call_count == 2
        impl.assert_not_called()  # never implemented without RED proof

    @pytest.mark.asyncio
    async def test_broken_tests_get_fix_tests_action(self, setup):
        """Collection errors drive the fix_tests action into the retry prompt."""
        config, state, progress, block = setup
        ok = AgentResult(success=True, output="Tests written")
        suite_results = iter([
            (2, "ERROR collecting tests/test_x.py\nSyntaxError: invalid syntax"),
            RED_SUITE_RESULT,
        ])

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=ok) as tw, \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=ok), \
             patch("build_pipeline.tdd_engine.run_test_suite", side_effect=lambda *a, **k: next(suite_results)):
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        assert "fix_tests" in tw.call_args_list[1].kwargs["block_description"]

    @pytest.mark.asyncio
    async def test_no_test_command_skips_red_gate(self, setup):
        config, state, progress, block = setup
        config.test_command = ""
        ok = AgentResult(success=True, output="Tests written")

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=ok), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=ok), \
             patch("build_pipeline.tdd_engine.run_test_suite") as rts:
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        rts.assert_not_called()
        assert block.red_evidence == ""


class TestEvidenceBasedReview:
    """The per-block quality review must see the block's actual diff."""

    @pytest.fixture(autouse=True)
    def _red(self, red_gate_passes, quality_audit_overturns):
        yield

    def _init_empty_repo(self, repo: Path) -> None:
        """git init with committer identity but zero commits."""
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)

    def _init_repo(self, repo: Path) -> str:
        self._init_empty_repo(repo)
        (repo / "module.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "module.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def _make_config(self, tmp_path) -> BuildConfig:
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        specs_root = project_dir / "specs"
        change_dir = specs_root / "changes" / "feat"
        change_dir.mkdir(parents=True)
        (change_dir / "rubric.md").write_text("# Rubric\nQuality criteria.")
        config = BuildConfig(project_dir=project_dir, change_name="feat")
        config.specs_dir = specs_root
        return config

    @pytest.mark.asyncio
    async def test_quality_review_passes_nonempty_diff(self, tmp_path):
        """_run_quality_review hands run_code_review the block's real diff."""
        config = self._make_config(tmp_path)
        baseline = self._init_repo(config.project_dir)

        # Simulate the block's work: a committed impl change on top of baseline.
        (config.project_dir / "module.py").write_text("x = 1\ny = 2  # block change\n")
        subprocess.run(["git", "commit", "-aqm", "impl(block-1)"], cwd=config.project_dir, check=True)

        block = BlockInfo(
            number=1, name="Feature", description="Add y",
            satisfies=["feat"], baseline_commit=baseline,
        )
        review = ReviewResult(scores=[ReviewScore(dimension="Q", weight=2, score=4, evidence="e")])
        with patch(
            "build_pipeline.tdd_engine.run_code_review",
            new_callable=AsyncMock, return_value=review,
        ) as mock_review:
            result = await _run_quality_review(block, config)

        assert result is review
        diff = mock_review.call_args.args[0]
        assert diff  # non-empty — the reviewer sees actual code
        assert "y = 2  # block change" in diff
        # Rubric and standards are pushed too.
        assert "Quality criteria" in mock_review.call_args.args[1]
        assert "standards" in mock_review.call_args.kwargs
        assert "Add y" in mock_review.call_args.kwargs["spec_context"]

    def test_capture_block_diff_from_baseline(self, tmp_path):
        """Diff spans baseline → working tree: commits AND uncommitted edits."""
        config = self._make_config(tmp_path)
        baseline = self._init_repo(config.project_dir)

        (config.project_dir / "module.py").write_text("x = 1\ny = 2\n")
        subprocess.run(["git", "commit", "-aqm", "impl"], cwd=config.project_dir, check=True)
        (config.project_dir / "module.py").write_text("x = 1\ny = 2\nz = 3  # uncommitted fix\n")

        block = BlockInfo(number=1, name="B", description="d", baseline_commit=baseline)
        diff = _capture_block_diff(config, block)
        assert "y = 2" in diff
        assert "z = 3  # uncommitted fix" in diff

    def test_capture_block_diff_falls_back_to_head(self, tmp_path):
        """Without a recorded baseline, uncommitted changes still get reviewed."""
        config = self._make_config(tmp_path)
        self._init_repo(config.project_dir)
        (config.project_dir / "module.py").write_text("x = 42\n")

        block = BlockInfo(number=1, name="B", description="d")  # no baseline_commit
        diff = _capture_block_diff(config, block)
        assert "x = 42" in diff

    def test_capture_block_diff_non_repo_returns_empty(self, tmp_path):
        config = self._make_config(tmp_path)  # project_dir is not a git repo
        block = BlockInfo(number=1, name="B", description="d")
        assert _capture_block_diff(config, block) == ""

    @pytest.mark.asyncio
    async def test_run_block_tdd_records_baseline(self, tmp_path):
        """run_block_tdd stamps the pre-block HEAD as the diff baseline."""
        config = self._make_config(tmp_path)
        config.tier = "advanced"
        config.test_command = "true"
        config.config_dir = tmp_path / "config"
        config.core_memory_files = []
        head = self._init_repo(config.project_dir)

        block = BlockInfo(number=1, name="B", description="d")
        state = BuildState(
            change_name="feat", mode="only", tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("feat", "only", "advanced", 1)

        ok = AgentResult(success=True, output="done")
        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=ok), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=ok):
            assert await run_block_tdd(block, config, state, progress)

        assert block.baseline_commit == head

    @pytest.mark.asyncio
    async def test_run_block_tdd_empty_repo_baseline_is_empty_tree(self, tmp_path):
        """Fresh `git init` (no commits): baseline falls back to the empty tree,
        so block 1's committed work is visible to the reviewer."""
        config = self._make_config(tmp_path)
        config.tier = "advanced"
        config.test_command = "true"
        config.config_dir = tmp_path / "config"
        config.core_memory_files = []
        self._init_empty_repo(config.project_dir)  # no commits — HEAD unresolvable

        block = BlockInfo(number=1, name="B", description="d")
        state = BuildState(
            change_name="feat", mode="only", tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("feat", "only", "advanced", 1)

        ok = AgentResult(success=True, output="done")
        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=ok), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=ok):
            assert await run_block_tdd(block, config, state, progress)

        assert block.baseline_commit == EMPTY_TREE_SHA

    def test_capture_block_diff_empty_tree_baseline_sees_committed_work(self, tmp_path):
        """With the empty-tree baseline, work committed during block 1 in a
        previously-empty repo shows up in the review diff."""
        config = self._make_config(tmp_path)
        self._init_empty_repo(config.project_dir)

        # Simulate block 1: first-ever commit lands during the block.
        (config.project_dir / "module.py").write_text("x = 1  # block-1 work\n")
        subprocess.run(["git", "add", "module.py"], cwd=config.project_dir, check=True)
        subprocess.run(["git", "commit", "-qm", "impl(block-1)"], cwd=config.project_dir, check=True)

        block = BlockInfo(number=1, name="B", description="d", baseline_commit=EMPTY_TREE_SHA)
        diff = _capture_block_diff(config, block)
        assert "x = 1  # block-1 work" in diff

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resume_status", [BlockStatus.IMPLEMENTING, BlockStatus.REVIEWING])
    async def test_run_block_tdd_resumed_midblock_does_not_stamp_baseline(
        self, tmp_path, resume_status, caplog
    ):
        """A pre-baseline checkpoint resumed mid-block (IMPLEMENTING/REVIEWING,
        baseline_commit=None) must NOT stamp current HEAD — HEAD already holds
        the block's own commits, and stamping would hide them from the
        reviewer. baseline stays None (documented `git diff HEAD` fallback)
        and a warning is logged."""
        config = self._make_config(tmp_path)
        config.tier = "advanced"
        config.test_command = "true"
        config.config_dir = tmp_path / "config"
        config.core_memory_files = []
        self._init_repo(config.project_dir)

        # Simulate the block's own committed work already at HEAD.
        (config.project_dir / "module.py").write_text("x = 1  # block work\n")
        subprocess.run(["git", "add", "module.py"], cwd=config.project_dir, check=True)
        subprocess.run(["git", "commit", "-qm", "impl(block-1)"], cwd=config.project_dir, check=True)

        block = BlockInfo(number=1, name="B", description="d", status=resume_status)
        assert block.baseline_commit is None
        state = BuildState(
            change_name="feat", mode="only", tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("feat", "only", "advanced", 1)

        ok = AgentResult(success=True, output="done")
        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=ok), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=ok), \
             caplog.at_level("WARNING"):
            assert await run_block_tdd(block, config, state, progress)

        assert block.baseline_commit is None
        assert any("pre-baseline" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_run_block_tdd_resumed_testing_still_stamps_baseline(self, tmp_path):
        """TESTING means no phase commit is guaranteed yet — stamping HEAD on
        resume is still correct there."""
        config = self._make_config(tmp_path)
        config.tier = "advanced"
        config.test_command = "true"
        config.config_dir = tmp_path / "config"
        config.core_memory_files = []
        head = self._init_repo(config.project_dir)

        block = BlockInfo(number=1, name="B", description="d", status=BlockStatus.TESTING)
        state = BuildState(
            change_name="feat", mode="only", tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("feat", "only", "advanced", 1)

        ok = AgentResult(success=True, output="done")
        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=ok), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=ok):
            assert await run_block_tdd(block, config, state, progress)

        assert block.baseline_commit == head


class TestExitCodes:
    def test_exit_codes_defined(self):
        assert EXIT_SUCCESS == 0
        assert EXIT_BUILD_FAILURE == 1
        assert EXIT_AGENT_TIMEOUT == 3


class TestRunTDDEngineEntryPoint:
    """Test the main run_tdd_engine entry point with mocked dependencies."""

    @pytest.fixture
    def tdd_setup(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        specs_root = project_dir / "specs"
        change_dir = specs_root / "changes" / "feat"
        change_dir.mkdir(parents=True)

        # Write tasks.md
        (change_dir / "tasks.md").write_text(
            "## 1. Setup\n\nInit the project.\n\nSatisfies: setup\n"
        )

        config = BuildConfig(
            project_dir=project_dir,
            change_name="feat",
            tier="advanced",
            test_command="true",  # always passes
            mode="only",
            config_dir=tmp_path / "config",
            core_memory_files=[],
            stack_memory_files=[],
            additional_memory_files=[],
        )
        config.specs_dir = specs_root

        args = MagicMock()
        args.block = None

        return config, args

    @pytest.mark.asyncio
    async def test_no_blocks_returns_failure(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        specs_root = project_dir / "specs"
        change_dir = specs_root / "changes" / "empty"
        change_dir.mkdir(parents=True)
        (change_dir / "tasks.md").write_text("")  # empty

        config = BuildConfig(
            project_dir=project_dir,
            change_name="empty",
            mode="only",
            tier="advanced",
            config_dir=tmp_path / "config",
            core_memory_files=[],
            stack_memory_files=[],
            additional_memory_files=[],
        )
        config.specs_dir = specs_root

        args = MagicMock()
        args.block = None

        result = await run_tdd_engine(config, args)
        assert result == EXIT_BUILD_FAILURE

    @pytest.mark.asyncio
    async def test_baseline_gate_failure(self, tdd_setup):
        config, args = tdd_setup
        config.test_command = "false"  # always fails

        result = await run_tdd_engine(config, args)
        assert result == EXIT_BUILD_FAILURE

    @pytest.mark.asyncio
    async def test_state_checkpoint_on_parse(self, tdd_setup):
        """After parsing blocks, state is checkpointed."""
        config, args = tdd_setup

        # Make baseline pass but block TDD fail immediately
        ok_result = AgentResult(success=False, error="fail")
        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=ok_result):
            await run_tdd_engine(config, args)

        # State file should exist from the checkpoint
        assert config.state_file_path().exists()

    @pytest.mark.asyncio
    async def test_agent_timeout_returns_exit_code_3(self, tdd_setup):
        """A real timeout-shaped AgentResult drives run_tdd_engine to EXIT_AGENT_TIMEOUT."""
        config, args = tdd_setup
        timeout_result = AgentResult(
            success=False, error="Agent timed out after 1200s", timed_out=True
        )

        with patch.dict(os.environ, {"BUILDME_TRUST_REPO": "1"}), \
             patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=timeout_result):
            result = await run_tdd_engine(config, args)

        assert result == EXIT_AGENT_TIMEOUT

    @pytest.mark.asyncio
    async def test_agent_plain_failure_returns_build_failure(self, tdd_setup):
        """A non-timeout agent failure returns EXIT_BUILD_FAILURE, not EXIT_AGENT_TIMEOUT."""
        config, args = tdd_setup
        fail_result = AgentResult(success=False, error="agent crashed")

        with patch.dict(os.environ, {"BUILDME_TRUST_REPO": "1"}), \
             patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=fail_result):
            result = await run_tdd_engine(config, args)

        assert result == EXIT_BUILD_FAILURE


# Test content the static weak-test scan flags (1/1 weak) vs. accepts.
WEAK_TESTS_OUTPUT = "def test_something():\n    assert True\n"
GOOD_TESTS_OUTPUT = "def test_addition():\n    assert add(2, 3) == 5\n"


class TestWeakTestEnforcement:
    """Phase A.5 is enforced: confirmed-weak tests fail the block, not just warn."""

    @pytest.fixture(autouse=True)
    def _red(self, red_gate_passes):
        yield

    @pytest.fixture
    def setup(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        specs_root = project_dir / "specs"
        (specs_root / "changes" / "test").mkdir(parents=True)

        config = BuildConfig(
            project_dir=project_dir, change_name="test", tier="advanced",
            test_command="pytest -xvs", config_dir=tmp_path / "config",
            core_memory_files=[], stack_memory_files=[], additional_memory_files=[],
        )
        config.specs_dir = specs_root

        block = BlockInfo(number=1, name="Block1", description="Test block")
        state = BuildState(
            change_name="test", mode="only", tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("test", "only", "advanced", 1)
        return config, state, progress, block

    @pytest.mark.asyncio
    async def test_static_pass_skips_auditor(self, setup):
        config, state, progress, block = setup
        ok = AgentResult(success=True, output=GOOD_TESTS_OUTPUT)

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=ok), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=ok), \
             patch("build_pipeline.tdd_engine._audit_test_quality", new_callable=AsyncMock) as audit:
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        audit.assert_not_called()

    @pytest.mark.asyncio
    async def test_auditor_overturns_static_scan(self, setup):
        config, state, progress, block = setup
        ok = AgentResult(success=True, output=WEAK_TESTS_OUTPUT)

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=ok) as tw, \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=ok), \
             patch("build_pipeline.tdd_engine._audit_test_quality", new_callable=AsyncMock,
                   return_value=QualityAudit(passed=True)):
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        assert tw.call_count == 1  # no retry when the auditor overturns

    @pytest.mark.asyncio
    async def test_confirmed_weak_retries_then_passes(self, setup):
        config, state, progress, block = setup
        confirm = QualityAudit(
            passed=False, weak_count=1, total_tests=1,
            weak_tests=[WeakTestFinding(
                file="test_x.py", test_name="test_something",
                pattern="assert True", suggestion="assert the actual behavior",
            )],
        )
        tw_results = [
            AgentResult(success=True, output=WEAK_TESTS_OUTPUT),
            AgentResult(success=True, output=GOOD_TESTS_OUTPUT),
        ]
        ok = AgentResult(success=True, output="done")

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, side_effect=tw_results) as tw, \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=ok), \
             patch("build_pipeline.tdd_engine._audit_test_quality", new_callable=AsyncMock,
                   return_value=confirm) as audit:
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        assert tw.call_count == 2
        retry_desc = tw.call_args_list[1].kwargs["block_description"]
        assert "Test Quality Failure" in retry_desc
        assert "assert the actual behavior" in retry_desc  # findings carried into the retry
        audit.assert_called_once()  # rescan passed, no re-audit needed

    @pytest.mark.asyncio
    async def test_still_weak_after_retry_fails_block(self, setup):
        config, state, progress, block = setup
        weak = AgentResult(success=True, output=WEAK_TESTS_OUTPUT)
        confirm = QualityAudit(passed=False, weak_count=1, total_tests=1)

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=weak), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock) as impl, \
             patch("build_pipeline.tdd_engine._audit_test_quality", new_callable=AsyncMock,
                   return_value=confirm) as audit:
            result = await run_block_tdd(block, config, state, progress)

        assert result is False
        assert block.status == BlockStatus.FAILED
        assert audit.call_count == 2  # initial audit + re-audit after retry
        impl.assert_not_called()  # weak tests never gate an implementation

    @pytest.mark.asyncio
    async def test_auditor_error_fails_block(self, setup):
        """An unverifiable quality gate fails the block — never silently passes."""
        config, state, progress, block = setup
        weak = AgentResult(success=True, output=WEAK_TESTS_OUTPUT)

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=weak), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock) as impl, \
             patch("build_pipeline.tdd_engine._audit_test_quality", new_callable=AsyncMock,
                   side_effect=RuntimeError("SDK down")):
            result = await run_block_tdd(block, config, state, progress)

        assert result is False
        assert block.status == BlockStatus.FAILED
        impl.assert_not_called()


class TestReviewExhaustionEnforcement:
    """Review exhaustion FAILs the block by default; --lenient-review restores accept."""

    @pytest.fixture
    def setup(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        specs_root = project_dir / "specs"
        (specs_root / "changes" / "test").mkdir(parents=True)

        config = BuildConfig(
            project_dir=project_dir, change_name="test", tier="advanced",
            test_command="",  # integration gate skips — review loop is the subject
            config_dir=tmp_path / "config",
            core_memory_files=[], stack_memory_files=[], additional_memory_files=[],
        )
        config.specs_dir = specs_root

        block = BlockInfo(number=1, name="Block1", description="d",
                          status=BlockStatus.IMPLEMENTING)
        state = BuildState(
            change_name="test", mode="only", tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("test", "only", "advanced", 1)
        return config, state, progress, block

    def _failing_review(self):
        return ReviewResult(scores=[ReviewScore(dimension="Q", weight=2, score=2, evidence="weak")])

    def _passing_review(self):
        return ReviewResult(scores=[ReviewScore(dimension="Q", weight=2, score=5, evidence="solid")])

    @pytest.mark.asyncio
    async def test_exhaustion_fails_block(self, setup, capsys):
        config, state, progress, block = setup
        fix = AgentResult(success=True, output="fixed")

        with patch("build_pipeline.tdd_engine._run_quality_review", new_callable=AsyncMock,
                   return_value=self._failing_review()), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock,
                   return_value=fix) as fixer:
            result = await _run_integration_and_review(block, config, state, progress)

        assert result is False
        assert block.status == BlockStatus.FAILED
        assert "REVIEW_EXHAUSTED" in capsys.readouterr().out
        # No fix agent after the last review — its work would go unreviewed.
        assert fixer.call_count == MAX_REVIEW_ITERATIONS - 1

    @pytest.mark.asyncio
    async def test_lenient_review_accepts_and_continues(self, setup, capsys):
        config, state, progress, block = setup
        config.lenient_review = True
        fix = AgentResult(success=True, output="fixed")

        with patch("build_pipeline.tdd_engine._run_quality_review", new_callable=AsyncMock,
                   return_value=self._failing_review()), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=fix):
            result = await _run_integration_and_review(block, config, state, progress)

        assert result is True
        assert block.status == BlockStatus.DONE
        assert "REVIEW_EXHAUSTED" not in capsys.readouterr().out
        assert "--lenient-review" in progress.path.read_text()

    @pytest.mark.asyncio
    async def test_passing_review_marks_done(self, setup, capsys):
        config, state, progress, block = setup

        with patch("build_pipeline.tdd_engine._run_quality_review", new_callable=AsyncMock,
                   return_value=self._passing_review()), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock) as fixer:
            result = await _run_integration_and_review(block, config, state, progress)

        assert result is True
        assert block.status == BlockStatus.DONE
        assert "COMPLETE" in capsys.readouterr().out
        fixer.assert_not_called()
        # GREEN evidence is recorded even on the no-test-command path.
        assert block.green_evidence
        assert "GREEN evidence" in progress.path.read_text()


class TestFinalReviewFailClosed:
    """The final review returns a structured verdict and fails closed on errors."""

    def _make(self, tmp_path, with_rubric=True):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        specs_root = project_dir / "specs"
        change_dir = specs_root / "changes" / "feat"
        change_dir.mkdir(parents=True)
        if with_rubric:
            (change_dir / "rubric.md").write_text("# Rubric\nQuality criteria.")
        config = BuildConfig(project_dir=project_dir, change_name="feat")
        config.specs_dir = specs_root
        state = BuildState(
            change_name="feat", mode="only", tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[BlockInfo(number=1, name="B", description="d")]),
        )
        return config, state

    @pytest.mark.asyncio
    async def test_no_rubric_skips(self, tmp_path):
        config, state = self._make(tmp_path, with_rubric=False)
        result = await _run_final_review(config, state)
        assert result.passed is True
        assert "No rubric" in result.reason

    @pytest.mark.asyncio
    async def test_llm_error_fails_closed(self, tmp_path):
        config, state = self._make(tmp_path)
        with patch("build_pipeline.tdd_engine.llm_call_structured", new_callable=AsyncMock,
                   side_effect=RuntimeError("api down")):
            result = await _run_final_review(config, state)
        assert result.passed is False
        assert result.action == "final_review_error"

    @pytest.mark.asyncio
    async def test_structured_verdict_passes_through(self, tmp_path):
        config, state = self._make(tmp_path)
        verdict = FinalReviewVerdict(passed=True, summary="Coherent build, blocks integrate.")
        with patch("build_pipeline.tdd_engine.llm_call_structured", new_callable=AsyncMock,
                   return_value=verdict):
            result = await _run_final_review(config, state)
        assert result.passed is True
        assert "Coherent build" in result.reason

    @pytest.mark.asyncio
    async def test_structured_verdict_failure_carries_issues(self, tmp_path):
        config, state = self._make(tmp_path)
        verdict = FinalReviewVerdict(
            passed=False, summary="Block 2 wired to a stub.",
            issues=["api.py: handler returns hardcoded value"],
        )
        with patch("build_pipeline.tdd_engine.llm_call_structured", new_callable=AsyncMock,
                   return_value=verdict):
            result = await _run_final_review(config, state)
        assert result.passed is False
        assert "hardcoded value" in result.reason

    @pytest.mark.asyncio
    async def test_engine_fails_build_on_final_review_error(self, tmp_path):
        config, state = self._make(tmp_path)
        (config.change_dir / "tasks.md").write_text("## 1. Setup\n\nInit.\n\nSatisfies: s\n")
        config.test_command = "true"
        config.config_dir = tmp_path / "config"
        config.core_memory_files = []
        args = MagicMock()
        args.block = None

        with patch.dict(os.environ, {"BUILDME_TRUST_REPO": "1"}), \
             patch("build_pipeline.tdd_engine.run_block_tdd", new_callable=AsyncMock, return_value=True), \
             patch("build_pipeline.tdd_engine._run_integration_and_review", new_callable=AsyncMock, return_value=True), \
             patch("build_pipeline.tdd_engine.llm_call_structured", new_callable=AsyncMock,
                   side_effect=RuntimeError("api down")):
            result = await run_tdd_engine(config, args)

        assert result == EXIT_BUILD_FAILURE

    @pytest.mark.asyncio
    async def test_engine_lenient_continues_past_final_review_error(self, tmp_path):
        config, state = self._make(tmp_path)
        (config.change_dir / "tasks.md").write_text("## 1. Setup\n\nInit.\n\nSatisfies: s\n")
        config.test_command = "true"
        config.config_dir = tmp_path / "config"
        config.core_memory_files = []
        config.lenient_review = True
        args = MagicMock()
        args.block = None

        with patch.dict(os.environ, {"BUILDME_TRUST_REPO": "1"}), \
             patch("build_pipeline.tdd_engine.run_block_tdd", new_callable=AsyncMock, return_value=True), \
             patch("build_pipeline.tdd_engine._run_integration_and_review", new_callable=AsyncMock, return_value=True), \
             patch("build_pipeline.tdd_engine.llm_call_structured", new_callable=AsyncMock,
                   side_effect=RuntimeError("api down")):
            result = await run_tdd_engine(config, args)

        assert result == EXIT_SUCCESS


class TestEntryPointSmokeRun:
    """_verify_entry_point actually executes the entry point — no fabricated pass."""

    def _config(self, tmp_path) -> BuildConfig:
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        return BuildConfig(project_dir=project_dir, change_name="feat")

    def test_library_project_passes_by_assumption(self, tmp_path):
        config = self._config(tmp_path)
        result = _verify_entry_point(config)
        assert result.passed is True
        assert "library" in result.reason

    def test_untrusted_repo_reports_unverified(self, tmp_path):
        config = self._config(tmp_path)
        (config.project_dir / "main.py").write_text("print('hi')\n")
        with patch.dict(os.environ, {"BUILDME_TRUST_REPO": ""}):
            result = _verify_entry_point(config)
        assert result.passed is False
        assert "not smoke-run" in result.reason

    def test_smoke_run_passes_with_evidence(self, tmp_path):
        config = self._config(tmp_path)
        (config.project_dir / "main.py").write_text(
            "import sys\nprint('usage: demo')\nsys.exit(0)\n"
        )
        with patch.dict(os.environ, {"BUILDME_TRUST_REPO": "1"}):
            result = _verify_entry_point(config)
        assert result.passed is True
        assert "usage: demo" in result.metadata["output"]

    def test_smoke_run_failure_reports_exit_code(self, tmp_path):
        config = self._config(tmp_path)
        (config.project_dir / "main.py").write_text(
            "import sys\nprint('boom')\nsys.exit(3)\n"
        )
        with patch.dict(os.environ, {"BUILDME_TRUST_REPO": "1"}):
            result = _verify_entry_point(config)
        assert result.passed is False
        assert "exit 3" in result.reason

    def test_detect_src_layout_package(self, tmp_path):
        config = self._config(tmp_path)
        pkg = config.project_dir / "src" / "mytool"
        pkg.mkdir(parents=True)
        (pkg / "__main__.py").write_text("print('hi')\n")
        entry = _detect_entry_point(config)
        assert entry is not None
        label, argv, env = entry
        assert label == "python -m mytool"
        assert str(config.project_dir / "src") in env["PYTHONPATH"]

    def test_detect_node_main(self, tmp_path):
        config = self._config(tmp_path)
        (config.project_dir / "package.json").write_text('{"main": "index.js"}')
        (config.project_dir / "index.js").write_text("console.log('hi')\n")
        entry = _detect_entry_point(config)
        assert entry is not None
        assert entry[0] == "node index.js"

    def test_tests_package_not_treated_as_entry_point(self, tmp_path):
        config = self._config(tmp_path)
        tests_pkg = config.project_dir / "tests"
        tests_pkg.mkdir()
        (tests_pkg / "__main__.py").write_text("print('test runner')\n")
        assert _detect_entry_point(config) is None
