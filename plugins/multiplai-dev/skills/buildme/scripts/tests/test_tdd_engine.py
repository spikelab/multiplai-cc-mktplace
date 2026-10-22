"""Tests for TDD engine — block parsing, context assembly, agent selection, gates."""

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from build_pipeline import budget as budget_mod
from build_pipeline import git_ops
from build_pipeline.config import BuildConfig
from build_pipeline.models import (
    AgentResult,
    BlockInfo,
    BlockStatus,
    BuildPhase,
    FindingAdjudication,
    FindingVerdict,
    GateResult,
    ReviewFinding,
    ReviewResult,
    ReviewScore,
    WeakTestFinding,
)
from build_pipeline.models import (
    TestQualityAudit as QualityAudit,  # Test* name breaks pytest collection
)
from build_pipeline.progress import ProgressWriter
from build_pipeline.state import BuildState, TDDState
from build_pipeline.tdd_engine import (
    _TEST_FILE_RE,
    _TEST_PATH_RE,
    EMPTY_TREE_SHA,
    EXIT_AGENT_TIMEOUT,
    EXIT_BUILD_FAILURE,
    EXIT_SUCCESS,
    MAX_REVIEW_ITERATIONS,
    WEAK_TEST_PATTERNS,
    _adjudicate_review_findings,
    _capture_block_diff,
    _detect_entry_point,
    _enforce_test_integrity,
    _git_commit_block_phase,
    _read_block_test_files,
    _run_integration_and_review,
    _run_quality_review,
    _run_refactor_all,
    _snapshot_all_test_files,
    _snapshot_test_files,
    _verify_entry_point,
    assemble_context,
    capture_build_diff,
    parse_blocks,
    run_block_tdd,
    run_tdd_engine,
    run_test_quality_check,
)


@pytest.fixture(autouse=True)
def _no_live_refactorer():
    """No test may spawn a real refactor agent.

    The refactor step runs on every tier now, so every `run_block_tdd` test
    reaches it whether or not it cares about refactoring. Default it to a
    successful no-op agent here; tests that assert on the refactorer patch it
    again locally, and the inner patch wins.
    """
    with (
        patch(
            "build_pipeline.tdd_engine.run_refactorer",
            new_callable=AsyncMock,
            return_value=AgentResult(success=True, output="nothing to refactor"),
        ),
        patch(
            "build_pipeline.tdd_engine.run_refactor_all",
            new_callable=AsyncMock,
            return_value=AgentResult(success=True, output="already consistent"),
        ),
    ):
        yield


# These tests isolate agent orchestration: their agent doubles return reports
# without writing files. Real Git/snapshot/rollback coverage lives in the
# integrity, refactor-verification and nested-project tests below.
@pytest.fixture(autouse=True)
def _unit_agent_evidence(request, monkeypatch):
    classes = {
        "TestRunBlockTDD",
        "TestRedGateWiring",
        "TestWeakTestEnforcement",
        "TestReviewExhaustionEnforcement",
        "TestAgentStatusEnforcement",
        "TestImplementationNotesLoop",
        "TestEvidenceBasedReview",
        "TestIntegrationFixCircuitBreaker",
        "TestRefactorAllOrdering",
        "TestCheckpointLifecycleOwnership",
        "TestMutationStepPlacement",
    }
    if request.cls and request.cls.__name__ in classes:
        monkeypatch.setattr(git_ops, "commit_tree", lambda *a, **kw: None)
        monkeypatch.setattr(
            "build_pipeline.tdd_engine._list_block_test_files",
            lambda *a: ["tests/test_x.py"],
        )
        monkeypatch.setattr(
            "build_pipeline.tdd_engine._snapshot_test_files",
            lambda *a: {"tests/test_x.py": "unchanged"},
        )
        monkeypatch.setattr(
            "build_pipeline.tdd_engine.integration_gate",
            lambda *a, **kw: GateResult(passed=True, reason="unit suite"),
        )
    yield


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
        tasks_file.write_text(
            "## 1. Only Block\n\nJust one block here.\n\nSatisfies: solo\n"
        )
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


class TestParseBlockInterfaces:
    """The `Interfaces:` section threads cross-block signatures (Item 6)."""

    def test_parses_produces_and_consumes(self, tmp_path):
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(
            "## 1. Token Issuer\n\nIssue tokens.\n\n"
            "Interfaces:\n"
            "- Produces: issue_token(user_id: str) -> str\n"
            "- Produces: TokenError(Exception)\n"
            "Satisfies: auth\n\n"
            "## 2. Login Endpoint\n\nWire login.\n\n"
            "Interfaces:\n"
            "- Consumes: Block 1: issue_token(user_id: str) -> str\n"
            "- Produces: POST /login -> {token: str}\n"
            "Satisfies: auth\n"
        )
        blocks = parse_blocks(tasks_file)

        assert blocks[0].produces == [
            "issue_token(user_id: str) -> str",
            "TokenError(Exception)",
        ]
        assert blocks[0].consumes == []
        assert blocks[1].consumes == ["Block 1: issue_token(user_id: str) -> str"]
        assert blocks[1].produces == ["POST /login -> {token: str}"]

    def test_none_marker_yields_empty_lists(self, tmp_path):
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(
            "## 1. Standalone\n\nDoes its own thing.\n\n"
            "Interfaces:\n"
            "- Produces: (none)\n"
            "Satisfies: solo\n"
        )
        blocks = parse_blocks(tasks_file)
        assert blocks[0].produces == []
        assert blocks[0].consumes == []

    def test_absent_section_yields_empty_lists(self, tmp_path):
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(
            "## 1. Old Format\n\nNo interfaces here.\n\nSatisfies: x\n"
        )
        blocks = parse_blocks(tasks_file)
        assert blocks[0].produces == []
        assert blocks[0].consumes == []

    def test_stops_at_first_non_bullet_line(self, tmp_path):
        """A prose line ends the section — later bullets are not interfaces."""
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(
            "## 1. Block\n\nDesc.\n\n"
            "Interfaces:\n"
            "- Produces: alpha() -> int\n"
            "Notes follow here.\n"
            "- Produces: should_not_be_parsed() -> None\n"
        )
        blocks = parse_blocks(tasks_file)
        assert blocks[0].produces == ["alpha() -> int"]


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
        (req_dir / "auth.md").write_text(
            "# Auth Requirements\nWHEN user logs in THEN token issued."
        )

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
        block = BlockInfo(
            number=1, name="Auth", description="Build auth module", satisfies=["auth"]
        )
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
        # Implementer/refactorer/reviewer receive requirements only via the
        # context bundle, so they must be present here.
        config = self._make_config(tmp_path)
        block = BlockInfo(number=1, name="X", description="desc")
        ctx = assemble_context(block, config, "implementer")
        assert "Auth Requirements" in ctx
        assert "WHEN user logs in" in ctx

    def test_test_writer_bundle_omits_requirements(self, tmp_path):
        # The test_writer already gets the full requirements set through its
        # dedicated ``specs`` prompt slot; assemble_context must NOT include
        # them again, or a single test-writer prompt ships requirements twice.
        config = self._make_config(tmp_path)
        block = BlockInfo(number=1, name="X", description="desc")
        ctx = assemble_context(block, config, "test_writer")
        assert "Auth Requirements" not in ctx
        assert "## Requirements:" not in ctx

    def test_includes_rubric(self, tmp_path):
        config = self._make_config(tmp_path)
        block = BlockInfo(number=1, name="X", description="desc")
        ctx = assemble_context(block, config, "implementer")
        assert "Evaluation Rubric" in ctx
        assert "Code quality criteria" in ctx

    def test_test_writer_gets_unknowns_and_refactorer_does_not(self, tmp_path):
        """B1's payoff: the documented edge cases reach the agent that turns
        them into tests. The refactorer has no use for them, so the document
        stays out of its bundle."""
        config = self._make_config(tmp_path)
        config.unknowns_path.write_text(
            "# Unknowns\n\n## mlx-whisper\n\n### Edge cases & failure modes\n"
            "- Silence transcribes as 'thanks for watching', not an empty string.\n"
        )
        block = BlockInfo(number=1, name="X", description="desc")

        writer_ctx = assemble_context(block, config, "test_writer")
        assert "thanks for watching" in writer_ctx
        assert "Unknowns — dependencies new to this project" in writer_ctx

        refactorer_ctx = assemble_context(block, config, "refactorer")
        assert "thanks for watching" not in refactorer_ctx
        assert "Unknowns" not in refactorer_ctx

    def test_missing_unknowns_file_is_fine(self, tmp_path):
        config = self._make_config(tmp_path)
        assert not config.unknowns_path.exists()
        block = BlockInfo(number=1, name="X", description="desc")
        assert "Unknowns" not in assemble_context(block, config, "test_writer")

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


class TestContextConstraintsAndInterfaces:
    """Global Constraints + cross-block interfaces reach agent prompts (Item 6)."""

    def _make_config(self, tmp_path, design_text):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        specs_root = project_dir / "specs"
        change_dir = specs_root / "changes" / "c"
        change_dir.mkdir(parents=True)
        (change_dir / "design.md").write_text(design_text)

        config = BuildConfig(
            project_dir=project_dir,
            change_name="c",
            config_dir=tmp_path / "claude-config",
            core_memory_files=[],
            stack_memory_files=[],
            additional_memory_files=[],
        )
        config.specs_dir = specs_root
        return config

    def test_injects_global_constraints_verbatim(self, tmp_path):
        config = self._make_config(
            tmp_path,
            "# Design\n\n## Global Constraints\n"
            "- Python >= 3.11\n"
            "- Queue name is exactly `dolce-jobs-v2`\n\n"
            "## Decisions\nSomething else.\n",
        )
        block = BlockInfo(number=1, name="X", description="desc")
        ctx = assemble_context(block, config, "implementer")

        assert "Global Constraints (project-wide, non-negotiable)" in ctx
        assert "Queue name is exactly `dolce-jobs-v2`" in ctx

    def test_no_constraints_section_when_design_lacks_one(self, tmp_path):
        config = self._make_config(
            tmp_path, "# Design\n\n## Decisions\nNo constraints.\n"
        )
        block = BlockInfo(number=1, name="X", description="desc")
        ctx = assemble_context(block, config, "implementer")
        assert "Global Constraints (project-wide" not in ctx

    def test_injects_own_interfaces(self, tmp_path):
        config = self._make_config(tmp_path, "# Design\n")
        block = BlockInfo(
            number=2,
            name="Login",
            description="desc",
            produces=["POST /login -> {token: str}"],
            consumes=["Block 1: issue_token(user_id: str) -> str"],
        )
        ctx = assemble_context(block, config, "implementer")

        assert "## Interfaces (this block)" in ctx
        assert "- Produces: POST /login -> {token: str}" in ctx
        assert "- Consumes: Block 1: issue_token(user_id: str) -> str" in ctx

    def test_injects_earlier_block_produces(self, tmp_path):
        config = self._make_config(tmp_path, "# Design\n")
        b1 = BlockInfo(
            number=1,
            name="Issuer",
            description="d",
            produces=["issue_token(user_id: str) -> str"],
        )
        b2 = BlockInfo(number=2, name="Login", description="d")
        b3 = BlockInfo(
            number=3,
            name="Later",
            description="d",
            produces=["later_only() -> None"],
        )
        ctx = assemble_context(b2, config, "implementer", blocks=[b1, b2, b3])

        assert "Interfaces from earlier blocks" in ctx
        assert "Block 1 (Issuer) produces: issue_token(user_id: str) -> str" in ctx
        # Later blocks' interfaces are not leaked backwards
        assert "later_only()" not in ctx

    def test_no_earlier_interfaces_section_for_first_block(self, tmp_path):
        config = self._make_config(tmp_path, "# Design\n")
        b1 = BlockInfo(number=1, name="First", description="d", produces=["a() -> int"])
        ctx = assemble_context(b1, config, "test_writer", blocks=[b1])
        assert "Interfaces from earlier blocks" not in ctx


class TestModelAdaptiveAgentSelection:
    """Test that tier-dependent behavior drives the right agent configuration."""

    def test_advanced_tier_has_refactor(self):
        """The refactor step is verified and reverted on failure, so it is on
        for the advanced tier too — only the implementer prompt still differs."""
        config = BuildConfig(tier="advanced")
        assert config.refactor_phase
        assert config.tdd_phases == ["test", "implement", "refactor"]
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
        tests = [
            "def test_real_%d():\n    assert compute(%d) == %d\n" % (i, i, i * 2)
            for i in range(10)
        ]
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

    def test_assert_called_alone_pattern(self):
        assert WEAK_TEST_PATTERNS[3].search("    assert client.ping.called")
        assert WEAK_TEST_PATTERNS[3].search("assert mock.called")
        # A comparison on call_count is not the bare `.called` pattern
        assert not WEAK_TEST_PATTERNS[3].search("    assert mock.call_count == 2")
        # Real-behavior assertion mentioning 'called' in a name stays clean
        assert not WEAK_TEST_PATTERNS[3].search("    assert times_called(log) == 3")


class TestMockAntiPatterns:
    """Per-function heuristics: mock-assertion-only and mock-setup-dominant."""

    def _check(self, content):
        config = BuildConfig(gates=MagicMock(test_quality_enabled=True))
        return run_test_quality_check(content, "", config)

    def test_mock_assertion_only_flagged(self):
        content = """\
def test_notifier_calls_sender():
    sender = MagicMock()
    notifier = Notifier(sender)
    notifier.notify("hi")
    sender.send.assert_called_once_with("hi")
"""
        result = self._check(content)
        assert not result.passed
        assert any("mock-assertion-only" in f for f in result.metadata["findings"])

    def test_mock_setup_dominant_flagged(self):
        content = """\
def test_transform_result():
    a = MagicMock()
    a.load.return_value = [1]
    b = MagicMock()
    b.norm.return_value = 2
    result = run(a, b)
    assert result == [2]
"""
        result = self._check(content)
        assert not result.passed
        assert any("mock-setup-dominant" in f for f in result.metadata["findings"])

    def test_mock_used_with_behavior_assertion_passes(self):
        """Mocks are fine when the test asserts an observable outcome and
        setup stays proportionate."""
        content = """\
def test_greets_user():
    repo = MagicMock()
    repo.get.return_value = "Ada"
    assert greet(repo) == "Hello Ada"
    assert repo.get.call_count == 1
"""
        result = self._check(content)
        assert result.passed

    def test_plain_behavior_tests_pass(self):
        content = """\
def test_add_reports_sum():
    calc = Calculator()
    assert calc.add(2, 3) == 5
"""
        result = self._check(content)
        assert result.passed

    def test_assert_called_alone_fails_check(self):
        content = """\
def test_ping_called():
    client = MagicMock()
    ping(client)
    assert client.ping.called
"""
        result = self._check(content)
        assert not result.passed


class TestAntiPatternPrompts:
    """The five testing anti-patterns appear as positive gate checks."""

    def test_test_writer_quality_gates(self):
        from build_pipeline.prompts.test_writing import TEST_WRITER_PROMPT

        assert "Quality Gates" in TEST_WRITER_PROMPT
        assert "tests real behavior" in TEST_WRITER_PROMPT
        assert "Mocks honor the real contract" in TEST_WRITER_PROMPT
        assert "Side effects are understood" in TEST_WRITER_PROMPT
        assert "production code\n   stays test-free" in TEST_WRITER_PROMPT
        assert "poll" in TEST_WRITER_PROMPT

    def test_auditor_prompt_covers_mock_patterns(self):
        from build_pipeline.prompts.test_writing import TEST_QUALITY_PROMPT

        assert "Mock-assertion-only" in TEST_QUALITY_PROMPT
        assert "Mock-setup-dominant" in TEST_QUALITY_PROMPT
        assert "sleep" in TEST_QUALITY_PROMPT.lower()

    def test_rubric_prompt_scores_anti_patterns(self):
        from build_pipeline.prompts.rubric_prompts import RUBRIC_PROMPT

        assert "anti-patterns" in RUBRIC_PROMPT
        assert "assert_called_*" in RUBRIC_PROMPT

    def test_rubric_template_scores_anti_patterns(self):
        from build_pipeline.change_manager import TEMPLATES

        assert "interrogate mocks" in TEMPLATES["rubric"]
        assert "sleep" in TEMPLATES["rubric"]


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

    def test_clean_prompt_does_not_claim_there_is_no_refactor_phase(self):
        """There is one on every tier now — the prompt said the opposite."""
        from build_pipeline.prompts.implementation import IMPLEMENTER_PROMPT_CLEAN

        assert "no refactor phase" not in IMPLEMENTER_PROMPT_CLEAN
        assert "no separate refactoring phase" not in IMPLEMENTER_PROMPT_CLEAN
        assert "refactor pass" in IMPLEMENTER_PROMPT_CLEAN

    def test_refactor_prompt_placeholders(self):
        from build_pipeline.prompts.implementation import REFACTOR_PROMPT

        placeholders = re.findall(r"\{(\w+)\}", REFACTOR_PROMPT)
        assert "block_name" in placeholders
        assert "context_bundle" in placeholders
        assert "test_command" in placeholders

    def test_refactor_all_prompt_placeholders(self):
        from build_pipeline.prompts.implementation import REFACTOR_ALL_PROMPT

        placeholders = re.findall(r"\{(\w+)\}", REFACTOR_ALL_PROMPT)
        assert {"diff", "design", "rubric", "test_command"} <= set(placeholders)
        REFACTOR_ALL_PROMPT.format(
            diff="d",
            design="des",
            rubric="r",
            test_command="pytest",
        )

    def test_refactor_prompts_forbid_touching_tests(self):
        """The gate is the enforcement, but the prompt must not leave the agent
        guessing — and neither prompt offers the TEST CHANGE REQUIRED hatch."""
        from build_pipeline.prompts.implementation import (
            REFACTOR_ALL_PROMPT,
            REFACTOR_PROMPT,
        )

        for prompt in (REFACTOR_PROMPT, REFACTOR_ALL_PROMPT):
            assert "test file" in prompt.lower()
            assert "TEST CHANGE REQUIRED" not in prompt

    def test_prompts_can_be_formatted(self):
        """All prompts format without error when given the right kwargs."""
        from build_pipeline.prompts.implementation import (
            IMPLEMENTER_PROMPT_CLEAN,
            IMPLEMENTER_PROMPT_MINIMUM,
            REFACTOR_PROMPT,
        )
        from build_pipeline.prompts.test_writing import TEST_WRITER_PROMPT

        result = TEST_WRITER_PROMPT.format(
            block_name="Test",
            block_description="desc",
            specs="specs",
            context_bundle="ctx",
            test_command="pytest",
            max_turns=30,
            half=15,
        )
        assert "Test" in result

        IMPLEMENTER_PROMPT_CLEAN.format(
            block_name="Test",
            block_description="desc",
            failing_tests="tests",
            context_bundle="ctx",
            test_command="pytest",
        )
        IMPLEMENTER_PROMPT_MINIMUM.format(
            block_name="Test",
            block_description="desc",
            failing_tests="tests",
            context_bundle="ctx",
            test_command="pytest",
        )
        REFACTOR_PROMPT.format(
            block_name="Test",
            block_description="desc",
            context_bundle="ctx",
            test_command="pytest",
        )


class TestTurnBudget:
    """Each test writer's prompt states its turn cap, and the cap is the one
    the caller passes, which the engine takes from the config."""

    @pytest.mark.parametrize(
        "name", ["TEST_WRITER_PROMPT", "MUTATION_TEST_WRITER_PROMPT"]
    )
    def test_the_prompt_states_the_cap_and_the_turn_to_have_a_draft_by(self, name):
        from build_pipeline.prompts import test_writing

        text = getattr(test_writing, name).format(
            block_name="b",
            block_description="d",
            specs="s",
            context_bundle="c",
            survivors="m",
            test_command="pytest",
            max_turns=25,
            half=12,
        )
        assert "You have 25 turns" in text
        assert "by turn 12" in text
        assert "kept if you run out of turns" in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("step", ["run_test_writer", "run_mutation_test_writer"])
    async def test_the_step_hands_its_cap_to_the_agent_and_the_prompt(self, step):
        from build_pipeline.llm_steps import tdd_steps

        call = AsyncMock(return_value=AgentResult(success=True, output="STATUS: DONE"))
        args = (
            ("b", "d", "specs", "ctx", "pytest")
            if step == "run_test_writer"
            else ("m", "pytest")
        )
        with patch.object(tdd_steps, "agent_call", call):
            await getattr(tdd_steps, step)(*args, max_turns=25)
        assert call.await_args.kwargs["max_turns"] == 25
        assert "You have 25 turns" in call.await_args.args[0]
        with patch.object(tdd_steps, "agent_call", call):
            await getattr(tdd_steps, step)(*args)
        assert call.await_args.kwargs["max_turns"] == tdd_steps.TEST_WRITER_MAX_TURNS


class TestTDDStepToolAllowlists:
    """Verify tool allowlists and timeouts for each agent type."""

    def test_test_writer_tools(self):
        from build_pipeline.llm_steps.tdd_steps import (
            TEST_WRITER_MAX_TURNS,
            TEST_WRITER_TIMEOUT,
            TEST_WRITER_TOOLS,
        )

        assert "Read" in TEST_WRITER_TOOLS
        assert "Write" in TEST_WRITER_TOOLS
        assert "Bash" in TEST_WRITER_TOOLS
        assert "Glob" in TEST_WRITER_TOOLS
        assert "Grep" in TEST_WRITER_TOOLS
        assert "Edit" not in TEST_WRITER_TOOLS  # test writers don't edit
        assert TEST_WRITER_MAX_TURNS == 30
        assert TEST_WRITER_TIMEOUT == 20 * 60

    def test_implementer_tools(self):
        from build_pipeline.llm_steps.tdd_steps import (
            IMPLEMENTER_MAX_TURNS,
            IMPLEMENTER_TIMEOUT,
            IMPLEMENTER_TOOLS,
        )

        assert "Read" in IMPLEMENTER_TOOLS
        assert "Write" in IMPLEMENTER_TOOLS
        assert "Edit" in IMPLEMENTER_TOOLS  # implementers CAN edit
        assert "Bash" in IMPLEMENTER_TOOLS
        assert IMPLEMENTER_MAX_TURNS == 50
        assert IMPLEMENTER_TIMEOUT == 30 * 60

    def test_refactorer_tools(self):
        from build_pipeline.llm_steps.tdd_steps import (
            REFACTORER_MAX_TURNS,
            REFACTORER_TIMEOUT,
            REFACTORER_TOOLS,
        )

        assert "Read" in REFACTORER_TOOLS
        assert "Edit" in REFACTORER_TOOLS
        assert REFACTORER_MAX_TURNS == 30
        assert REFACTORER_TIMEOUT == 15 * 60

    def test_refactor_all_uses_the_refactorer_tool_list(self):
        """Same work, more room: the whole-change pass reads a build-sized diff
        but must not gain any capability the per-block refactorer lacks."""
        import inspect

        from build_pipeline.llm_steps import tdd_steps

        assert tdd_steps.REFACTOR_ALL_MAX_TURNS > tdd_steps.REFACTORER_MAX_TURNS
        assert tdd_steps.REFACTOR_ALL_TIMEOUT > tdd_steps.REFACTORER_TIMEOUT
        source = inspect.getsource(tdd_steps.run_refactor_all)
        assert "allowed_tools=REFACTORER_TOOLS" in source
        assert 'budget_label="refactor_all"' in source


# A suite result that satisfies the RED gate: non-zero exit, genuine failure.
RED_SUITE_RESULT = (
    1,
    "FAILED tests/test_x.py::test_a - AssertionError: not implemented",
)


@pytest.fixture
def red_gate_passes():
    """Make the RED gate see a properly failing suite (most engine tests
    exercise other phases and just need RED to be satisfied)."""
    with patch(
        "build_pipeline.tdd_engine.run_test_suite", return_value=RED_SUITE_RESULT
    ) as run:
        yield run


@pytest.fixture
def quality_audit_overturns():
    """Have the LLM test-quality auditor overturn the static scan (most engine
    tests use placeholder agent output with no test code, which the static
    scan flags; the audit path itself is tested separately)."""
    from build_pipeline.models import TestQualityAudit

    with patch(
        "build_pipeline.tdd_engine._audit_test_quality",
        new_callable=AsyncMock,
        return_value=TestQualityAudit(passed=True),
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
            tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )

        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("test", "advanced", 1)

        return config, state, progress, block

    @pytest.mark.asyncio
    async def test_successful_block(self, setup):
        config, state, progress, block = setup
        ok_result = AgentResult(
            success=True, output="Tests written", files_changed=["test_x.py"]
        )

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok_result,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok_result,
            ),
        ):
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        assert block.status == BlockStatus.IMPLEMENTING

    @pytest.mark.asyncio
    async def test_the_test_writer_gets_the_configured_turn_cap(self, setup):
        config, state, progress, block = setup
        config.test_writer_max_turns = 17
        ok_result = AgentResult(
            success=True, output="Tests written", files_changed=["test_x.py"]
        )

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok_result,
            ) as tw,
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok_result,
            ),
        ):
            await run_block_tdd(block, config, state, progress)

        assert tw.await_args.kwargs["max_turns"] == 17

    @pytest.mark.asyncio
    async def test_test_writer_failure(self, setup):
        config, state, progress, block = setup
        fail_result = AgentResult(success=False, error="Agent crashed")

        with patch(
            "build_pipeline.tdd_engine.run_test_writer",
            new_callable=AsyncMock,
            return_value=fail_result,
        ):
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

        with patch(
            "build_pipeline.tdd_engine.run_test_writer",
            new_callable=AsyncMock,
            return_value=timeout_result,
        ):
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

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok_result,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=timeout_result,
            ),
        ):
            result = await run_block_tdd(block, config, state, progress)

        assert result is False
        assert block.timed_out is True

    @pytest.mark.asyncio
    async def test_an_agent_error_stops_the_block_resumably(self, setup):
        """An agent call that errored says nothing about the block's work."""
        config, state, progress, block = setup
        fail_result = AgentResult(success=False, error="CLI exited 1")

        with patch(
            "build_pipeline.tdd_engine.run_test_writer",
            new_callable=AsyncMock,
            return_value=fail_result,
        ):
            assert await run_block_tdd(block, config, state, progress) is False

        assert block.status == BlockStatus.FAILED
        assert block.resumable is True
        assert block.timed_out is False

    @pytest.mark.asyncio
    async def test_an_agent_that_reports_blocked_is_not_resumable(self, setup):
        config, state, progress, block = setup
        blocked = AgentResult(
            success=True, output="Cannot be done.\n\nSTATUS: BLOCKED\n"
        )

        with patch(
            "build_pipeline.tdd_engine.run_test_writer",
            new_callable=AsyncMock,
            return_value=blocked,
        ):
            assert await run_block_tdd(block, config, state, progress) is False

        assert block.status == BlockStatus.FAILED
        assert block.resumable is False

    @pytest.mark.asyncio
    async def test_a_rerun_clears_the_last_attempts_stop_flags(self, setup):
        config, state, progress, block = setup
        block.status = BlockStatus.FAILED
        block.timed_out = True
        block.resumable = True
        blocked = AgentResult(
            success=True, output="Cannot be done.\n\nSTATUS: BLOCKED\n"
        )

        with patch(
            "build_pipeline.tdd_engine.run_test_writer",
            new_callable=AsyncMock,
            return_value=blocked,
        ):
            assert await run_block_tdd(block, config, state, progress) is False

        assert block.timed_out is False
        assert block.resumable is False

    @pytest.mark.asyncio
    async def test_implementer_failure(self, setup):
        config, state, progress, block = setup
        ok_result = AgentResult(success=True, output="Tests written")
        fail_result = AgentResult(success=False, error="Impl failed")

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok_result,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=fail_result,
            ),
        ):
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

        with patch(
            "build_pipeline.tdd_engine.run_test_writer",
            new_callable=AsyncMock,
            return_value=fail_result,
        ):
            result = await run_block_tdd(block, config, state, progress)

        assert result is False
        assert state.tdd.blocks[0].status == BlockStatus.FAILED

    @pytest.mark.asyncio
    async def test_standard_tier_runs_refactorer(self, setup):
        config, state, progress, block = setup
        config.tier = "standard"
        ok_result = AgentResult(success=True, output="Done")

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok_result,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok_result,
            ),
            patch(
                "build_pipeline.tdd_engine.run_refactorer",
                new_callable=AsyncMock,
                return_value=ok_result,
            ) as mock_refactor,
        ):
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        mock_refactor.assert_called_once()

    @pytest.mark.asyncio
    async def test_advanced_tier_also_runs_refactorer(self, setup):
        """The advanced tier used to skip the refactor step. It no longer does:
        the step is verified and self-reverting, so it runs on both tiers."""
        config, state, progress, block = setup
        config.tier = "advanced"
        ok_result = AgentResult(success=True, output="Done")

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok_result,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok_result,
            ),
            patch(
                "build_pipeline.tdd_engine.run_refactorer",
                new_callable=AsyncMock,
                return_value=ok_result,
            ) as mock_refactor,
        ):
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        mock_refactor.assert_called_once()


def _git_init_project(project_dir: Path) -> str:
    """A project repo with one commit. Returns its SHA."""
    subprocess.run(["git", "init", "-q"], cwd=project_dir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=project_dir, check=True
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=project_dir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project_dir, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=project_dir, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _git_log_subjects(project_dir: Path) -> list[str]:
    return subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()


TEST_FILE_REL = "tests/test_thing.py"
ORIGINAL_TEST_SOURCE = "def test_addition():\n    assert add(2, 3) == 5\n"


class TestBlockRefactorVerification:
    """The per-block refactor is verified, never trusted.

    The refactorer runs after the implementation is already committed, so the
    pipeline can afford to be strict: if the suite is not green afterwards, or
    a single test file changed, the whole refactor diff is thrown away and the
    block keeps the implementation that was green. A refactor can improve a
    block or do nothing — it can never break one.
    """

    @pytest.fixture(autouse=True)
    def _red(self, red_gate_passes, quality_audit_overturns):
        red_gate_passes.return_value = (
            1,
            "FAILED tests/test_thing.py::test_addition - AssertionError\n1 failed",
        )
        yield

    @pytest.fixture
    def setup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        change_dir = project_dir / "specs" / "changes" / "test"
        change_dir.mkdir(parents=True)
        (change_dir / "design.md").write_text("# Design")
        (project_dir / "module.py").write_text("x = 1\n")
        _git_init_project(project_dir)

        config = BuildConfig(
            project_dir=project_dir,
            change_name="test",
            tier="advanced",
            test_command="true",  # green; individual tests flip it to "false"
            config_dir=tmp_path / "config",
            core_memory_files=[],
            stack_memory_files=[],
            additional_memory_files=[],
        )
        config.specs_dir = project_dir / "specs"

        block = BlockInfo(number=1, name="Block1", description="Test block")
        state = BuildState(
            change_name="test",
            tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("test", "advanced", 1)
        return config, state, progress, block

    @staticmethod
    def _agent_writing(project_dir: Path, rel: str, content: str, output: str = "done"):
        """An agent mock whose side effect is writing one file."""

        def _run(*_a, **_kw):
            path = project_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            return AgentResult(success=True, output=output)

        return _run

    async def _run(self, config, state, progress, block, refactorer, implementer=None):
        """Drive one block with a test writer and implementer that produce real
        files, so the refactor step has a committed baseline to rewind to."""
        write_tests = self._agent_writing(
            config.project_dir, TEST_FILE_REL, ORIGINAL_TEST_SOURCE, "tests written"
        )
        write_impl = implementer or self._agent_writing(
            config.project_dir,
            "module.py",
            "def add(a, b):\n    return a + b\n",
            "implemented",
        )
        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                side_effect=write_tests,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                side_effect=write_impl,
            ),
            patch(
                "build_pipeline.tdd_engine.run_refactorer",
                new_callable=AsyncMock,
                side_effect=refactorer,
            ),
        ):
            return await run_block_tdd(block, config, state, progress)

    @pytest.mark.asyncio
    async def test_verified_refactor_is_committed_and_noted(self, setup):
        config, state, progress, block = setup
        refactorer = self._agent_writing(
            config.project_dir,
            "module.py",
            "def add(a: int, b: int) -> int:\n    return a + b\n",
            "Tidied add().\nSURPRISES: the design named it sum()\nSPEC_IMPACT: clarify\n",
        )

        assert await self._run(config, state, progress, block, refactorer) is True

        assert block.refactor_commit is not None
        assert _git_log_subjects(config.project_dir)[0] == "refactor(block-1): Block1"
        assert (
            (config.project_dir / "module.py").read_text().startswith("def add(a: int")
        )
        roles = [n.role for n in block.notes]
        assert "refactorer" in roles

    @pytest.mark.asyncio
    async def test_red_suite_after_refactor_reverts_and_continues(self, setup):
        """A refactor that breaks the suite is discarded — the block keeps the
        green implementation and the build carries on."""
        config, state, progress, block = setup
        config.test_command = SUITE_RED_IF_BROKEN
        refactorer = self._agent_writing(
            config.project_dir,
            "module.py",
            "def add(a, b):\n    return a - b  # broken\n",
        )

        # The block still succeeds...
        assert await self._run(config, state, progress, block, refactorer) is True
        # ...with the implementation restored and no refactor commit.
        assert (
            config.project_dir / "module.py"
        ).read_text() == "def add(a, b):\n    return a + b\n"
        assert block.refactor_commit is None
        assert not any(
            s.startswith("refactor(") for s in _git_log_subjects(config.project_dir)
        )

    @pytest.mark.asyncio
    async def test_refactor_that_creates_files_has_them_cleaned_on_revert(self, setup):
        """`reset --hard` alone leaves new files behind, where the next
        whole-tree commit would sweep reverted work back into the build."""
        config, state, progress, block = setup
        config.test_command = shlex.join(
            [
                sys.executable,
                "-c",
                "import pathlib,sys; sys.exit(pathlib.Path('extracted.py').exists())",
            ]
        )
        refactorer = self._agent_writing(
            config.project_dir, "extracted.py", "# new helper\n"
        )

        assert await self._run(config, state, progress, block, refactorer) is True
        assert not (config.project_dir / "extracted.py").exists()

    @pytest.mark.asyncio
    async def test_test_file_mutation_reverts_the_refactor(self, setup):
        """The suite is green, but the refactorer moved the bar it is measured
        against — the same failure as a red suite."""
        config, state, progress, block = setup
        refactorer = self._agent_writing(
            config.project_dir,
            TEST_FILE_REL,
            "def test_addition():\n    assert True\n",  # gutted
        )

        assert await self._run(config, state, progress, block, refactorer) is True
        assert (config.project_dir / TEST_FILE_REL).read_text() == ORIGINAL_TEST_SOURCE
        assert block.refactor_commit is None

    @pytest.mark.asyncio
    async def test_declaring_a_test_change_does_not_excuse_it(self, setup):
        """`TEST CHANGE REQUIRED:` is an implementer's escape hatch. The
        refactor window is given no report at all, so the declaration cannot
        reach the gate — a refactorer is behavior-preserving by definition and
        has no case for editing the tests."""
        config, state, progress, block = setup
        refactorer = self._agent_writing(
            config.project_dir,
            TEST_FILE_REL,
            "def test_addition():\n    assert True\n",
            output="TEST CHANGE REQUIRED: the test had a typo\n",
        )

        assert await self._run(config, state, progress, block, refactorer) is True
        assert (config.project_dir / TEST_FILE_REL).read_text() == ORIGINAL_TEST_SOURCE
        assert block.refactor_commit is None
        assert block.test_change_claims == []

    @pytest.mark.asyncio
    async def test_implementer_test_change_is_not_blamed_on_the_refactorer(self, setup):
        """An implementer may legitimately change a test file (the
        `TEST CHANGE REQUIRED:` path — the change rides in the impl commit).
        The refactor window is measured from the tree the implementer left
        behind, not from the RED-gate freeze, so a source-only refactor after
        such a change must be verified and kept — not discarded with the
        implementer's edit blamed on the refactorer."""
        config, state, progress, block = setup
        changed_test = "def test_addition():\n    assert add(2, 3) == 5  # fixed typo\n"

        def impl_and_fix_test(*_a, **_kw):
            (config.project_dir / "module.py").write_text(
                "def add(a, b):\n    return a + b\n"
            )
            (config.project_dir / TEST_FILE_REL).write_text(changed_test)
            return AgentResult(
                success=True,
                output="implemented\nTEST CHANGE REQUIRED: the test had a typo\n",
            )

        refactorer = self._agent_writing(
            config.project_dir,
            "module.py",
            "def add(a: int, b: int) -> int:\n    return a + b\n",
        )

        assert (
            await self._run(
                config,
                state,
                progress,
                block,
                refactorer,
                implementer=impl_and_fix_test,
            )
            is True
        )

        assert block.refactor_commit is not None
        assert _git_log_subjects(config.project_dir)[0] == "refactor(block-1): Block1"
        assert (
            (config.project_dir / "module.py").read_text().startswith("def add(a: int")
        )
        # The implementer's test change is untouched by the refactor verdict.
        assert (config.project_dir / TEST_FILE_REL).read_text() == changed_test

    @pytest.mark.asyncio
    async def test_failed_refactor_agent_leaves_the_block_alone(self, setup):
        """An agent that errored wrote nothing to verify — no revert, no commit,
        and the block still passes."""
        config, state, progress, block = setup

        def _fail(*_a, **_kw):
            return AgentResult(success=False, error="agent crashed")

        assert await self._run(config, state, progress, block, _fail) is True
        assert block.refactor_commit is None
        assert (
            config.project_dir / "module.py"
        ).read_text() == "def add(a, b):\n    return a + b\n"


class TestGitCommitScoping:
    """_git_commit_block_phase must not leak buildme bookkeeping into user commits."""

    def _init_repo(self, repo: Path):
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@example.com"], cwd=repo, check=True
        )
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
        (config.change_dir / ".board.json").write_text("{}\n")

        sha = _git_commit_block_phase(
            config, "impl", BlockInfo(number=1, name="B", description="d")
        )
        assert sha is not None

        committed = subprocess.run(
            ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        assert "module.py" in committed
        assert "build-progress.md" not in committed
        assert not any(".build-state.json" in f for f in committed)
        assert not any(".board.json" in f for f in committed)

        # The bookkeeping files must remain untracked afterward.
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "build-progress.md" not in tracked
        assert ".build-state.json" not in tracked
        assert ".board.json" not in tracked


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
            change_name="test",
            tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("test", "advanced", 1)
        return config, state, progress, block

    @pytest.mark.asyncio
    async def test_red_pass_stores_evidence(self, setup):
        config, state, progress, block = setup
        ok = AgentResult(success=True, output="Tests written")

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_test_suite",
                return_value=RED_SUITE_RESULT,
            ),
        ):
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

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok,
            ) as tw,
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_test_suite",
                side_effect=lambda *a, **k: next(suite_results),
            ),
        ):
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

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok,
            ) as tw,
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok,
            ) as impl,
            patch(
                "build_pipeline.tdd_engine.run_test_suite", return_value=(0, "5 passed")
            ),
        ):
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
        suite_results = iter(
            [
                (2, "ERROR collecting tests/test_x.py\nSyntaxError: invalid syntax"),
                RED_SUITE_RESULT,
            ]
        )

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok,
            ) as tw,
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_test_suite",
                side_effect=lambda *a, **k: next(suite_results),
            ),
        ):
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        assert "fix_tests" in tw.call_args_list[1].kwargs["block_description"]

    @pytest.mark.asyncio
    async def test_no_test_command_skips_red_gate(self, setup):
        config, state, progress, block = setup
        config.test_command = ""
        ok = AgentResult(success=True, output="Tests written")

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch("build_pipeline.tdd_engine.run_test_suite") as rts,
        ):
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        rts.assert_not_called()
        assert block.red_evidence == ""


class TestNarrowTestCommandInTheBlockLoop:
    """Inside a block's TDD loop, RED and the block's agents use the narrow
    command (the block's own test files plus the test file named after each
    changed source file); the integration gate keeps the whole command."""

    WHOLE = "pytest -xvs"

    @pytest.fixture(autouse=True)
    def _quality(self, quality_audit_overturns):
        yield

    @pytest.fixture
    def setup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        project_dir = tmp_path / "project"
        (project_dir / "pkg").mkdir(parents=True)
        (project_dir / "tests").mkdir()
        (project_dir / "pkg" / "widget.py").write_text("x = 1\n")
        (project_dir / "tests" / "test_widget.py").write_text("def test_w(): pass\n")
        (project_dir / "tests" / "test_unrelated.py").write_text("def test_u(): pass\n")
        (project_dir / "specs" / "changes" / "test").mkdir(parents=True)
        _git_init_project(project_dir)

        config = BuildConfig(
            project_dir=project_dir,
            change_name="test",
            tier="advanced",
            test_command=self.WHOLE,
            config_dir=tmp_path / "config",
            core_memory_files=[],
            stack_memory_files=[],
            additional_memory_files=[],
        )
        config.specs_dir = project_dir / "specs"
        block = BlockInfo(number=1, name="Block1", description="Test block")
        state = BuildState(
            change_name="test",
            tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("test", "advanced", 1)
        return config, state, progress, block

    @staticmethod
    def _writes(project_dir: Path, rel: str, content: str):
        def _run(*_a, **_kw):
            (project_dir / rel).write_text(content)
            return AgentResult(success=True, output="done")

        return _run

    async def _run_block(
        self, config, state, progress, block, suite_results=None, test_writer=None
    ):
        suite_calls: list[str] = []
        gate_calls: list[str] = []
        results = list(suite_results or [])

        def _suite(command, _cwd, *_a, **_k):
            suite_calls.append(command)
            result = results.pop(0) if results else RED_SUITE_RESULT
            return result[0], result[1].replace("tests/test_x.py", "tests/test_new.py")

        def _gate(command, _cwd, **_kw):
            gate_calls.append(command)
            return GateResult(passed=True, reason="green")

        p = config.project_dir
        writer = test_writer or self._writes(
            p, "tests/test_new.py", "def test_n(): assert 0\n"
        )
        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                side_effect=writer,
            ) as tw,
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                side_effect=self._writes(p, "pkg/widget.py", "x = 2\n"),
            ) as impl,
            patch(
                "build_pipeline.tdd_engine.run_refactorer",
                new_callable=AsyncMock,
                return_value=AgentResult(success=True, output="nothing"),
            ) as ref,
            patch("build_pipeline.tdd_engine.run_test_suite", side_effect=_suite),
            patch("build_pipeline.tdd_engine.integration_gate", side_effect=_gate),
        ):
            ok = await run_block_tdd(block, config, state, progress)
        return ok, tw, impl, ref, suite_calls, gate_calls

    @pytest.mark.asyncio
    async def test_red_and_agents_use_the_narrow_command(self, setup):
        config, state, progress, block = setup
        ok, tw, impl, ref, suite_calls, gate_calls = await self._run_block(
            config, state, progress, block
        )

        assert ok is True
        # RED runs only the block's own new test file.
        assert suite_calls == [f"{self.WHOLE} tests/test_new.py"]
        # The test writer starts before the block changed anything: the whole
        # command, with how to run only the test files it writes.
        tw_text = tw.call_args.kwargs["test_command"]
        assert tw_text.splitlines()[0] == self.WHOLE
        assert "test_<module>.py" in tw_text
        # The implementer works against the block's tests (GREEN while it
        # works), and is told to add the test file named after each module it
        # changes, which its command cannot know yet.
        impl_text = impl.call_args.kwargs["test_command"]
        assert impl_text.splitlines()[0] == f"{self.WHOLE} tests/test_new.py"
        assert "test_<module>.py" in impl_text
        # After pkg/widget.py changed, tests/test_widget.py joins; test_unrelated never does.
        assert (
            ref.call_args.kwargs["test_command"].splitlines()[0]
            == f"{self.WHOLE} tests/test_new.py tests/test_widget.py"
        )
        # The refactor's verification is a gate: whole command.
        assert gate_calls == [self.WHOLE]

    @pytest.mark.asyncio
    async def test_red_retry_gets_the_narrow_command_and_reruns_with_its_new_tests(
        self, setup
    ):
        config, state, progress, block = setup
        p = config.project_dir
        files = iter(
            [
                ("tests/test_new.py", "def test_n(): pass\n"),
                ("tests/test_new2.py", "def test_m(): assert 0\n"),
            ]
        )

        def _writer(*_a, **_kw):
            rel, content = next(files)
            (p / rel).write_text(content)
            return AgentResult(success=True, output="done")

        ok, tw, _impl, _ref, suite_calls, _gates = await self._run_block(
            config,
            state,
            progress,
            block,
            suite_results=[(0, "1 passed in 0.01s"), RED_SUITE_RESULT],
            test_writer=_writer,
        )

        assert ok is True
        assert len(tw.call_args_list) == 2
        retry_text = tw.call_args_list[1].kwargs["test_command"]
        assert retry_text.splitlines()[0] == f"{self.WHOLE} tests/test_new.py"
        assert suite_calls == [
            f"{self.WHOLE} tests/test_new.py",
            f"{self.WHOLE} tests/test_new.py tests/test_new2.py",
        ]

    @pytest.mark.asyncio
    async def test_test_quality_retry_gets_the_narrow_command(self, setup):
        config, state, progress, block = setup
        scans = iter(
            [
                GateResult(passed=False, reason="weak"),
                GateResult(passed=True, reason="ok"),
            ]
        )
        with (
            patch(
                "build_pipeline.tdd_engine.run_test_quality_check",
                side_effect=lambda *_a, **_k: next(scans),
            ),
            patch(
                "build_pipeline.tdd_engine._audit_test_quality",
                new_callable=AsyncMock,
                return_value=QualityAudit(passed=False),
            ),
        ):
            ok, tw, _impl, _ref, _suite, _gates = await self._run_block(
                config, state, progress, block
            )

        assert ok is True
        assert len(tw.call_args_list) == 2
        assert (
            tw.call_args_list[1].kwargs["test_command"].splitlines()[0]
            == f"{self.WHOLE} tests/test_new.py"
        )

    @pytest.mark.asyncio
    async def test_narrow_tests_false_keeps_the_whole_command(self, setup):
        config, state, progress, block = setup
        config.narrow_tests = False
        ok, _tw, impl, ref, suite_calls, _gates = await self._run_block(
            config, state, progress, block
        )

        assert ok is True
        assert suite_calls == [self.WHOLE]
        assert impl.call_args.kwargs["test_command"] == self.WHOLE
        assert ref.call_args.kwargs["test_command"] == self.WHOLE

    @pytest.mark.asyncio
    async def test_integration_gate_and_its_fixer_use_the_whole_command(self, setup):
        config, state, progress, block = setup
        block.baseline_commit = git_ops.rev_parse_head(config.project_dir)
        (config.project_dir / "tests" / "test_new.py").write_text(
            "def test_n(): pass\n"
        )
        gate_calls: list[str] = []

        def _gate(command, _cwd, **_kw):
            gate_calls.append(command)
            return GateResult(
                passed=False, reason="red", metadata={"stdout": "1 failed"}
            )

        with (
            patch("build_pipeline.tdd_engine.integration_gate", side_effect=_gate),
            patch(
                "build_pipeline.tdd_engine.run_integration_fix",
                new_callable=AsyncMock,
                return_value=AgentResult(success=True, output="tried"),
            ) as fixer,
        ):
            ok = await _run_integration_and_review(block, config, state, progress)

        assert ok is False
        assert gate_calls and set(gate_calls) == {self.WHOLE}
        assert {c.kwargs["test_command"] for c in fixer.call_args_list} == {self.WHOLE}


class TestEvidenceBasedReview:
    """The per-block quality review must see the block's actual diff."""

    @pytest.fixture(autouse=True)
    def _red(self, red_gate_passes, quality_audit_overturns):
        yield

    def _init_empty_repo(self, repo: Path) -> None:
        """git init with committer identity but zero commits."""
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@example.com"], cwd=repo, check=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)

    def _init_repo(self, repo: Path) -> str:
        self._init_empty_repo(repo)
        (repo / "module.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "module.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
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
        subprocess.run(
            ["git", "commit", "-aqm", "impl(block-1)"],
            cwd=config.project_dir,
            check=True,
        )

        block = BlockInfo(
            number=1,
            name="Feature",
            description="Add y",
            satisfies=["feat"],
            baseline_commit=baseline,
        )
        review = ReviewResult(
            scores=[ReviewScore(dimension="Q", weight=2, score=4, evidence="e")]
        )
        with patch(
            "build_pipeline.tdd_engine.run_code_review",
            new_callable=AsyncMock,
            return_value=review,
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

    @pytest.mark.asyncio
    async def test_quality_review_threads_scenarios_and_evidence(self, tmp_path):
        """The reviewer gets the Satisfies scenarios verbatim and the block's
        RED/GREEN evidence as an unverified implementer report."""
        config = self._make_config(tmp_path)
        baseline = self._init_repo(config.project_dir)
        req_dir = config.change_dir / "requirements"
        req_dir.mkdir()
        (req_dir / "feat.md").write_text(
            "## Scenario: add y\nWHEN y is requested THEN y == 2\n"
        )

        block = BlockInfo(
            number=1,
            name="Feature",
            description="Add y",
            satisfies=["feat"],
            baseline_commit=baseline,
            red_evidence="FAILED test_y - AssertionError",
            green_evidence="2 passed in 0.1s",
        )
        review = ReviewResult(
            scores=[ReviewScore(dimension="Q", weight=2, score=4, evidence="e")]
        )
        with patch(
            "build_pipeline.tdd_engine.run_code_review",
            new_callable=AsyncMock,
            return_value=review,
        ) as mock_review:
            await _run_quality_review(block, config)

        spec_context = mock_review.call_args.kwargs["spec_context"]
        assert "WHEN y is requested THEN y == 2" in spec_context  # verbatim scenario
        report = mock_review.call_args.kwargs["implementer_report"]
        assert "FAILED test_y - AssertionError" in report
        assert "2 passed in 0.1s" in report
        assert "RED evidence" in report and "GREEN evidence" in report

    def test_capture_block_diff_from_baseline(self, tmp_path):
        """Diff spans baseline → working tree: commits AND uncommitted edits."""
        config = self._make_config(tmp_path)
        baseline = self._init_repo(config.project_dir)

        (config.project_dir / "module.py").write_text("x = 1\ny = 2\n")
        subprocess.run(
            ["git", "commit", "-aqm", "impl"], cwd=config.project_dir, check=True
        )
        (config.project_dir / "module.py").write_text(
            "x = 1\ny = 2\nz = 3  # uncommitted fix\n"
        )

        block = BlockInfo(number=1, name="B", description="d", baseline_commit=baseline)
        diff = _capture_block_diff(config, block)
        assert "y = 2" in diff
        assert "z = 3  # uncommitted fix" in diff

    def test_capture_block_diff_shows_new_untracked_files(self, tmp_path):
        """A file created after the last phase commit (by a refactor or fix
        agent) is committed with the block, so the reviewer must see it while
        it is still untracked. Bookkeeping, ignored files and the round records
        stay out: they are never committed with the block, or are the review's
        own output."""
        config = self._make_config(tmp_path)
        baseline = self._init_repo(config.project_dir)
        project = config.project_dir
        (project / ".gitignore").write_text("ignored.log\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=project, check=True)
        subprocess.run(["git", "commit", "-qm", "ignore"], cwd=project, check=True)
        (project / "pkg").mkdir()
        (project / "pkg" / "helpers.py").write_text("def helper():\n    return 7\n")
        (project / "ignored.log").write_text("SECRET LOG LINE\n")
        config.state_file_path().write_text('{"state": "STATE FILE"}\n')
        (project / "build-progress.md").write_text("PROGRESS LINE\n")
        reviews = config.change_dir / "reviews"
        reviews.mkdir()
        (reviews / "block-01-round-1.json").write_text('{"round": "ROUND RECORD"}\n')

        block = BlockInfo(number=1, name="B", description="d", baseline_commit=baseline)
        diff = _capture_block_diff(config, block)
        assert "pkg/helpers.py" in diff
        assert "+    return 7" in diff
        assert "SECRET LOG LINE" not in diff
        assert "STATE FILE" not in diff
        assert "PROGRESS LINE" not in diff
        assert "ROUND RECORD" not in diff

    def test_capture_build_diff_shows_new_untracked_files(self, tmp_path):
        """The whole-build diff the docs agent gets includes a file the build
        created and has not committed yet, as the per-block diff does."""
        config = self._make_config(tmp_path)
        baseline = self._init_repo(config.project_dir)
        (config.project_dir / "fresh.py").write_text("FRESH = 1\n")
        block = BlockInfo(number=1, name="B", description="d", baseline_commit=baseline)
        state = BuildState(
            change_name="feat", tier="advanced", tdd=TDDState(blocks=[block])
        )
        diff = capture_build_diff(config, state)
        assert "fresh.py" in diff and "+FRESH = 1" in diff

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
            change_name="feat",
            tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("feat", "advanced", 1)

        ok = AgentResult(success=True, output="done")
        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
        ):
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
            change_name="feat",
            tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("feat", "advanced", 1)

        ok = AgentResult(success=True, output="done")
        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
        ):
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
        subprocess.run(
            ["git", "commit", "-qm", "impl(block-1)"],
            cwd=config.project_dir,
            check=True,
        )

        block = BlockInfo(
            number=1, name="B", description="d", baseline_commit=EMPTY_TREE_SHA
        )
        diff = _capture_block_diff(config, block)
        assert "x = 1  # block-1 work" in diff

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "resume_status", [BlockStatus.IMPLEMENTING, BlockStatus.REVIEWING]
    )
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
        subprocess.run(
            ["git", "commit", "-qm", "impl(block-1)"],
            cwd=config.project_dir,
            check=True,
        )

        block = BlockInfo(number=1, name="B", description="d", status=resume_status)
        assert block.baseline_commit is None
        state = BuildState(
            change_name="feat",
            tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("feat", "advanced", 1)

        ok = AgentResult(success=True, output="done")
        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            caplog.at_level("WARNING"),
        ):
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

        block = BlockInfo(
            number=1, name="B", description="d", status=BlockStatus.TESTING
        )
        state = BuildState(
            change_name="feat",
            tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("feat", "advanced", 1)

        ok = AgentResult(success=True, output="done")
        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
        ):
            assert await run_block_tdd(block, config, state, progress)

        assert block.baseline_commit == head


class TestExitCodes:
    def test_exit_codes_defined(self):
        assert EXIT_SUCCESS == 0
        assert EXIT_BUILD_FAILURE == 1
        assert EXIT_AGENT_TIMEOUT == 4


@pytest.fixture
def tdd_setup(tmp_path):
    """A minimal one-block change, ready for run_tdd_engine."""
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
        config_dir=tmp_path / "config",
        core_memory_files=[],
        stack_memory_files=[],
        additional_memory_files=[],
    )
    config.specs_dir = specs_root

    args = MagicMock()
    args.block = None
    _git_init_project(project_dir)

    return config, args


class TestRunTDDEngineEntryPoint:
    """Test the main run_tdd_engine entry point with mocked dependencies."""

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
        with patch(
            "build_pipeline.tdd_engine.run_test_writer",
            new_callable=AsyncMock,
            return_value=ok_result,
        ):
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

        with (
            patch.dict(os.environ, {"BUILDME_TRUST_REPO": "1"}),
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=timeout_result,
            ),
        ):
            result = await run_tdd_engine(config, args)

        assert result == EXIT_AGENT_TIMEOUT

    @pytest.mark.asyncio
    async def test_agent_plain_failure_returns_build_failure(self, tdd_setup):
        """A non-timeout agent failure returns EXIT_BUILD_FAILURE, not EXIT_AGENT_TIMEOUT."""
        config, args = tdd_setup
        fail_result = AgentResult(success=False, error="agent crashed")

        with (
            patch.dict(os.environ, {"BUILDME_TRUST_REPO": "1"}),
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=fail_result,
            ),
        ):
            result = await run_tdd_engine(config, args)

        assert result == EXIT_BUILD_FAILURE

    @pytest.mark.asyncio
    async def test_a_rerun_after_a_timeout_that_fails_its_block_is_a_build_failure(
        self, tdd_setup
    ):
        """A block that timed out once and then fails on its own work when the
        build is re-run is a build failure: the earlier timeout must not stick."""
        config, args = tdd_setup
        timeout_result = AgentResult(
            success=False, error="Agent timed out after 1200s", timed_out=True
        )
        blocked = AgentResult(
            success=True, output="Cannot be done.\n\nSTATUS: BLOCKED\n"
        )

        with (
            patch.dict(os.environ, {"BUILDME_TRUST_REPO": "1"}),
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                side_effect=[timeout_result, blocked],
            ),
        ):
            assert await run_tdd_engine(config, args) == EXIT_AGENT_TIMEOUT
            assert await run_tdd_engine(config, args) == EXIT_BUILD_FAILURE

        block = BuildState.load(config.state_file_path()).tdd.blocks[0]
        assert block.timed_out is False
        assert block.resumable is False


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
            tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("test", "advanced", 1)
        return config, state, progress, block

    @pytest.mark.asyncio
    async def test_static_pass_skips_auditor(self, setup):
        config, state, progress, block = setup
        ok = AgentResult(success=True, output=GOOD_TESTS_OUTPUT)

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine._audit_test_quality", new_callable=AsyncMock
            ) as audit,
        ):
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        audit.assert_not_called()

    @pytest.mark.asyncio
    async def test_auditor_overturns_static_scan(self, setup):
        config, state, progress, block = setup
        ok = AgentResult(success=True, output=WEAK_TESTS_OUTPUT)

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok,
            ) as tw,
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine._audit_test_quality",
                new_callable=AsyncMock,
                return_value=QualityAudit(passed=True),
            ),
        ):
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        assert tw.call_count == 1  # no retry when the auditor overturns

    @pytest.mark.asyncio
    async def test_confirmed_weak_retries_then_passes(self, setup):
        config, state, progress, block = setup
        confirm = QualityAudit(
            passed=False,
            weak_count=1,
            total_tests=1,
            weak_tests=[
                WeakTestFinding(
                    file="test_x.py",
                    test_name="test_something",
                    pattern="assert True",
                    suggestion="assert the actual behavior",
                )
            ],
        )
        tw_results = [
            AgentResult(success=True, output=WEAK_TESTS_OUTPUT),
            AgentResult(success=True, output=GOOD_TESTS_OUTPUT),
        ]
        ok = AgentResult(success=True, output="done")

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                side_effect=tw_results,
            ) as tw,
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine._audit_test_quality",
                new_callable=AsyncMock,
                return_value=confirm,
            ) as audit,
        ):
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        assert tw.call_count == 2
        retry_desc = tw.call_args_list[1].kwargs["block_description"]
        assert "Test Quality Failure" in retry_desc
        assert (
            "assert the actual behavior" in retry_desc
        )  # findings carried into the retry
        audit.assert_called_once()  # rescan passed, no re-audit needed

    @pytest.mark.asyncio
    async def test_still_weak_after_retry_fails_block(self, setup):
        config, state, progress, block = setup
        weak = AgentResult(success=True, output=WEAK_TESTS_OUTPUT)
        confirm = QualityAudit(passed=False, weak_count=1, total_tests=1)

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=weak,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock
            ) as impl,
            patch(
                "build_pipeline.tdd_engine._audit_test_quality",
                new_callable=AsyncMock,
                return_value=confirm,
            ) as audit,
        ):
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

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=weak,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock
            ) as impl,
            patch(
                "build_pipeline.tdd_engine._audit_test_quality",
                new_callable=AsyncMock,
                side_effect=RuntimeError("SDK down"),
            ),
        ):
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
            project_dir=project_dir,
            change_name="test",
            tier="advanced",
            test_command="",  # integration gate skips — review loop is the subject
            config_dir=tmp_path / "config",
            core_memory_files=[],
            stack_memory_files=[],
            additional_memory_files=[],
        )
        config.specs_dir = specs_root

        block = BlockInfo(
            number=1, name="Block1", description="d", status=BlockStatus.IMPLEMENTING
        )
        state = BuildState(
            change_name="test",
            tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("test", "advanced", 1)
        return config, state, progress, block

    def _failing_review(self):
        return ReviewResult(
            scores=[ReviewScore(dimension="Q", weight=2, score=2, evidence="weak")]
        )

    def _passing_review(self):
        return ReviewResult(
            scores=[ReviewScore(dimension="Q", weight=2, score=5, evidence="solid")]
        )

    @pytest.mark.asyncio
    async def test_exhaustion_fails_block(self, setup, capsys):
        config, state, progress, block = setup
        fix = AgentResult(success=True, output="fixed")

        with (
            patch(
                "build_pipeline.tdd_engine._run_quality_review",
                new_callable=AsyncMock,
                return_value=self._failing_review(),
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=fix,
            ) as fixer,
        ):
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

        with (
            patch(
                "build_pipeline.tdd_engine._run_quality_review",
                new_callable=AsyncMock,
                return_value=self._failing_review(),
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=fix,
            ),
        ):
            result = await _run_integration_and_review(block, config, state, progress)

        assert result is True
        assert block.status == BlockStatus.DONE
        assert "REVIEW_EXHAUSTED" not in capsys.readouterr().out
        assert "--lenient-review" in progress.path.read_text()

    @pytest.mark.asyncio
    async def test_passing_review_marks_done(self, setup, capsys):
        config, state, progress, block = setup

        with (
            patch(
                "build_pipeline.tdd_engine._run_quality_review",
                new_callable=AsyncMock,
                return_value=self._passing_review(),
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock
            ) as fixer,
        ):
            result = await _run_integration_and_review(block, config, state, progress)

        assert result is True
        assert block.status == BlockStatus.DONE
        assert "COMPLETE" in capsys.readouterr().out
        fixer.assert_not_called()
        # GREEN evidence is recorded even on the no-test-command path.
        assert block.green_evidence
        assert "GREEN evidence" in progress.path.read_text()

    @pytest.mark.asyncio
    async def test_review_exhaustion_is_not_resumable(self, setup):
        """The block's own work failed review: a re-run has nothing to get past."""
        config, state, progress, block = setup
        with (
            patch(
                "build_pipeline.tdd_engine._run_quality_review",
                new_callable=AsyncMock,
                return_value=self._failing_review(),
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=AgentResult(success=True, output="fixed"),
            ),
        ):
            assert (
                await _run_integration_and_review(block, config, state, progress)
                is False
            )
        assert block.status == BlockStatus.FAILED
        assert block.resumable is False

    @pytest.mark.asyncio
    async def test_review_exhaustion_with_no_fix_agent_completing_is_resumable(
        self, setup
    ):
        """Review fixes that all errored never tried the fix: a re-run may."""
        config, state, progress, block = setup
        with (
            patch(
                "build_pipeline.tdd_engine._run_quality_review",
                new_callable=AsyncMock,
                return_value=self._failing_review(),
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=AgentResult(success=False, error="CLI exited 1"),
            ),
        ):
            assert (
                await _run_integration_and_review(block, config, state, progress)
                is False
            )
        assert block.status == BlockStatus.FAILED
        assert block.resumable is True

    @pytest.mark.asyncio
    async def test_a_review_call_that_raises_stops_the_block_resumably(self, setup):
        config, state, progress, block = setup
        with patch(
            "build_pipeline.tdd_engine._run_quality_review",
            new_callable=AsyncMock,
            side_effect=RuntimeError("model unavailable"),
        ):
            assert (
                await _run_integration_and_review(block, config, state, progress)
                is False
            )
        assert block.status == BlockStatus.FAILED
        assert block.resumable is True
        assert block.timed_out is False

    @pytest.mark.asyncio
    async def test_a_budget_stop_in_review_stops_the_block_resumably(self, setup):
        """A re-run with a raised ceiling gets past a budget stop."""
        config, state, progress, block = setup

        class _Usage:
            input_tokens, output_tokens = 500, 0
            cache_read_tokens = cache_creation_tokens = 0
            cost_usd = 0.0

        budget_mod.reset()
        try:
            budget_mod.configure(max_tokens=100)
            budget_mod.record(_Usage(), label="spec")
            with patch(
                "build_pipeline.tdd_engine._run_quality_review", new_callable=AsyncMock
            ) as review:
                assert (
                    await _run_integration_and_review(block, config, state, progress)
                    is False
                )
        finally:
            budget_mod.reset()
        review.assert_not_awaited()
        assert block.status == BlockStatus.FAILED
        assert block.resumable is True


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


class TestIntegrationFixCircuitBreaker:
    """3 fix attempts: two scoped, then an escalated architecture-questioning
    attempt on the review model; exhaustion → FAILED + diagnosis in progress."""

    @pytest.fixture
    def setup(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        specs_root = project_dir / "specs"
        (specs_root / "changes" / "test").mkdir(parents=True)

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
        config.model = "claude-opus-x"
        config.review_model = "claude-stronger-y"

        block = BlockInfo(
            number=1, name="Block1", description="d", status=BlockStatus.IMPLEMENTING
        )
        state = BuildState(
            change_name="test",
            tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("test", "advanced", 1)
        return config, state, progress, block

    @pytest.mark.asyncio
    async def test_exhaustion_escalates_then_fails_with_diagnosis(self, setup):
        config, state, progress, block = setup
        failing_gate = GateResult(
            passed=False,
            reason="2 tests failing",
            metadata={"stdout": "FAILED test_a", "stderr": ""},
        )
        fix = AgentResult(
            success=True, output="DIAGNOSIS: circular import between a and b"
        )

        with (
            patch(
                "build_pipeline.tdd_engine.integration_gate", return_value=failing_gate
            ),
            patch(
                "build_pipeline.tdd_engine.run_integration_fix",
                new_callable=AsyncMock,
                return_value=fix,
            ) as fixer,
        ):
            result = await _run_integration_and_review(block, config, state, progress)

        assert result is False
        assert block.status == BlockStatus.FAILED
        assert fixer.call_count == 3
        # Attempts 1-2 scoped on the build model; attempt 3 escalated on review_model.
        assert fixer.call_args_list[0].kwargs["escalate"] is False
        assert fixer.call_args_list[0].kwargs["model"] == "claude-opus-x"
        assert fixer.call_args_list[1].kwargs["escalate"] is False
        assert fixer.call_args_list[2].kwargs["escalate"] is True
        assert fixer.call_args_list[2].kwargs["model"] == "claude-stronger-y"
        # Diagnosis lands in build-progress.md with the fix agent's report.
        progress_text = progress.path.read_text()
        assert "DIAGNOSIS (Block1)" in progress_text
        assert "circular import between a and b" in progress_text
        assert "ESCALATED" in progress_text
        # Every fix agent ran and the suite still fails: the block's own work.
        assert block.resumable is False

    @pytest.mark.asyncio
    async def test_exhaustion_with_no_fix_agent_completing_is_resumable(self, setup):
        """Fix agents that all errored never tried the fix: a re-run may."""
        config, state, progress, block = setup
        failing_gate = GateResult(passed=False, reason="failing", metadata={})
        errored = AgentResult(success=False, error="CLI exited 1")

        with (
            patch(
                "build_pipeline.tdd_engine.integration_gate", return_value=failing_gate
            ),
            patch(
                "build_pipeline.tdd_engine.run_integration_fix",
                new_callable=AsyncMock,
                return_value=errored,
            ),
        ):
            assert (
                await _run_integration_and_review(block, config, state, progress)
                is False
            )

        assert block.status == BlockStatus.FAILED
        assert block.resumable is True

    @pytest.mark.asyncio
    async def test_escalation_falls_back_to_build_model(self, setup):
        config, state, progress, block = setup
        config.review_model = None
        failing_gate = GateResult(passed=False, reason="failing", metadata={})
        fix = AgentResult(success=True, output="tried")

        with (
            patch(
                "build_pipeline.tdd_engine.integration_gate", return_value=failing_gate
            ),
            patch(
                "build_pipeline.tdd_engine.run_integration_fix",
                new_callable=AsyncMock,
                return_value=fix,
            ) as fixer,
        ):
            result = await _run_integration_and_review(block, config, state, progress)

        assert result is False
        assert fixer.call_args_list[2].kwargs["model"] == "claude-opus-x"

    @pytest.mark.asyncio
    async def test_fix_success_stops_before_escalation(self, setup):
        config, state, progress, block = setup
        gates = iter(
            [
                GateResult(
                    passed=False, reason="failing", metadata={"output": "FAILED"}
                ),
                GateResult(
                    passed=True, reason="all green", metadata={"stdout": "3 passed"}
                ),
            ]
        )
        fix = AgentResult(success=True, output="fixed the import")
        review = ReviewResult(
            scores=[ReviewScore(dimension="Q", weight=2, score=5, evidence="e")]
        )

        with (
            patch(
                "build_pipeline.tdd_engine.integration_gate",
                side_effect=lambda *a, **k: next(gates),
            ),
            patch(
                "build_pipeline.tdd_engine.run_integration_fix",
                new_callable=AsyncMock,
                return_value=fix,
            ) as fixer,
            patch(
                "build_pipeline.tdd_engine._run_quality_review",
                new_callable=AsyncMock,
                return_value=review,
            ),
        ):
            result = await _run_integration_and_review(block, config, state, progress)

        assert result is True
        assert block.status == BlockStatus.DONE
        assert fixer.call_count == 1
        assert fixer.call_args.kwargs["escalate"] is False


class TestIntegrationFixPrompts:
    """The fix prompts embed the systematic-debugging protocol; escalation
    switches to the question-the-architecture variant."""

    @pytest.mark.asyncio
    async def test_scoped_prompt_has_debug_protocol(self):
        from build_pipeline.llm_steps.tdd_steps import run_integration_fix

        ok = AgentResult(success=True, output="done")
        with patch(
            "build_pipeline.llm_steps.tdd_steps.agent_call",
            new_callable=AsyncMock,
            return_value=ok,
        ) as call:
            await run_integration_fix("FAILED test_a", "pytest", "ctx")
        prompt = call.call_args.args[0]
        assert "Debugging Protocol" in prompt
        assert "ONE hypothesis" in prompt
        assert "Reproduce" in prompt
        assert "one variable at a time" in prompt
        assert "question the block's" not in prompt  # scoped, not escalated

    @pytest.mark.asyncio
    async def test_escalated_prompt_questions_architecture(self):
        from build_pipeline.llm_steps.tdd_steps import run_integration_fix

        ok = AgentResult(success=True, output="done")
        with patch(
            "build_pipeline.llm_steps.tdd_steps.agent_call",
            new_callable=AsyncMock,
            return_value=ok,
        ) as call:
            await run_integration_fix("FAILED test_a", "pytest", "ctx", escalate=True)
        prompt = call.call_args.args[0]
        assert "Debugging Protocol" in prompt  # protocol carries over
        assert "DIAGNOSIS:" in prompt
        assert "restructure the block's implementation" in prompt


class TestAgentStatusEnforcement:
    """An agent that reports NEEDS_CONTEXT/BLOCKED fails the block instead of
    the pipeline proceeding on an admitted non-result (Item 7)."""

    @pytest.fixture(autouse=True)
    def _red(self, red_gate_passes, quality_audit_overturns):
        yield

    @pytest.fixture
    def setup(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        specs_root = project_dir / "specs"
        (specs_root / "changes" / "test").mkdir(parents=True)

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
            tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress_path = tmp_path / "progress.md"
        progress = ProgressWriter(progress_path)
        progress.initialize("test", "advanced", 1)
        return config, state, progress, block, progress_path

    @pytest.mark.asyncio
    async def test_test_writer_needs_context_fails_block(self, setup):
        config, state, progress, block, progress_path = setup
        blocked = AgentResult(
            success=True,
            output="Spec names Widget.render() which does not exist.\n\nSTATUS: NEEDS_CONTEXT\n",
        )

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=blocked,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock
            ) as impl,
        ):
            result = await run_block_tdd(block, config, state, progress)

        assert result is False
        # The pipeline never proceeded to implementation on a non-result
        impl.assert_not_called()
        assert state.tdd.blocks[0].status == BlockStatus.FAILED
        # The agent's stated reason reaches the operator's progress file
        assert "Widget.render()" in progress_path.read_text()

    @pytest.mark.asyncio
    async def test_implementer_blocked_fails_block(self, setup):
        config, state, progress, block, progress_path = setup
        tests_ok = AgentResult(success=True, output="STATUS: DONE\nTESTS_RUN: pytest\n")
        blocked = AgentResult(
            success=True,
            output="Tests demand a sync API the design forbids.\n\nSTATUS: BLOCKED\n",
        )

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=tests_ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=blocked,
            ),
        ):
            result = await run_block_tdd(block, config, state, progress)

        assert result is False
        assert state.tdd.blocks[0].status == BlockStatus.FAILED
        assert "sync API the design forbids" in progress_path.read_text()

    @pytest.mark.asyncio
    async def test_done_with_concerns_proceeds(self, setup):
        """A flagged-but-complete result is not a stop condition."""
        config, state, progress, block, _ = setup
        ok = AgentResult(
            success=True,
            output="STATUS: DONE_WITH_CONCERNS\nTESTS_RUN: pytest\nMock is thin.\n",
        )

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok,
            ) as impl,
        ):
            result = await run_block_tdd(block, config, state, progress)

        assert result is True
        impl.assert_called_once()


class TestReadBlockTestFiles:
    """The quality gate scans real test source, not the agent's prose report."""

    @pytest.fixture
    def repo(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        subprocess.run(["git", "init", "-q", "."], cwd=project, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=project, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=project, check=True)
        (project / "README.md").write_text("start\n")
        subprocess.run(["git", "add", "-A"], cwd=project, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=project, check=True)
        baseline = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        specs_root = project / "specs"
        (specs_root / "changes" / "c").mkdir(parents=True)
        config = BuildConfig(
            project_dir=project,
            change_name="c",
            tier="advanced",
            config_dir=tmp_path / "config",
            core_memory_files=[],
            stack_memory_files=[],
            additional_memory_files=[],
        )
        config.specs_dir = specs_root
        block = BlockInfo(number=1, name="B", description="d", baseline_commit=baseline)
        return project, config, block

    def test_reads_test_files_changed_since_baseline(self, repo):
        project, config, block = repo
        tests_dir = project / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_adder.py").write_text(
            "def test_add():\n    assert add(2, 3) == 5\n"
        )
        subprocess.run(["git", "add", "-A"], cwd=project, check=True)
        subprocess.run(["git", "commit", "-qm", "tests"], cwd=project, check=True)

        content = _read_block_test_files(config, block)
        assert "def test_add()" in content
        assert "assert add(2, 3) == 5" in content

    def test_ignores_non_test_files(self, repo):
        project, config, block = repo
        (project / "adder.py").write_text("def add(a, b):\n    return a + b\n")
        subprocess.run(["git", "add", "-A"], cwd=project, check=True)
        subprocess.run(["git", "commit", "-qm", "impl"], cwd=project, check=True)

        assert _read_block_test_files(config, block) == ""

    def test_returns_empty_when_nothing_changed(self, repo):
        _, config, block = repo
        assert _read_block_test_files(config, block) == ""

    def test_returns_empty_outside_a_git_repo(self, tmp_path):
        project = tmp_path / "bare"
        project.mkdir()
        specs_root = project / "specs"
        (specs_root / "changes" / "c").mkdir(parents=True)
        config = BuildConfig(
            project_dir=project,
            change_name="c",
            tier="advanced",
            config_dir=tmp_path / "config",
            core_memory_files=[],
            stack_memory_files=[],
            additional_memory_files=[],
        )
        config.specs_dir = specs_root
        block = BlockInfo(number=1, name="B", description="d")
        assert _read_block_test_files(config, block) == ""

    def test_agent_report_alone_would_fail_the_static_scan(self, repo):
        """Why this helper exists: a prose report contains no test functions,
        so scanning it fails every block on "No test functions found"."""
        _, config, _ = repo
        report = (
            "I wrote 18 tests in tests/test_adder.py covering the spec.\nSTATUS: DONE\n"
        )
        scan = run_test_quality_check(report, "specs", config)
        assert not scan.passed
        assert scan.metadata["total_tests"] == 0


class TestSnapshotTestFiles:
    def test_hashes_content_not_mtime(self, tmp_path):
        """A checkout or a formatter moves mtimes without moving the bar."""
        (tmp_path / "tests").mkdir()
        f = tmp_path / "tests" / "test_a.py"
        f.write_text("def test_x(): assert 1")
        config = BuildConfig(project_dir=tmp_path)
        block = BlockInfo(number=1, name="B", description="d")
        with patch(
            "build_pipeline.tdd_engine._list_block_test_files",
            return_value=["tests/test_a.py"],
        ):
            first = _snapshot_test_files(config, block)
            os.utime(f, (0, 0))
            second = _snapshot_test_files(config, block)
        assert first == second
        assert list(first) == ["tests/test_a.py"]

    def test_content_change_moves_the_hash(self, tmp_path):
        (tmp_path / "tests").mkdir()
        f = tmp_path / "tests" / "test_a.py"
        f.write_text("def test_x(): assert 1")
        config = BuildConfig(project_dir=tmp_path)
        block = BlockInfo(number=1, name="B", description="d")
        with patch(
            "build_pipeline.tdd_engine._list_block_test_files",
            return_value=["tests/test_a.py"],
        ):
            before = _snapshot_test_files(config, block)
            f.write_text("def test_x(): assert True  # weakened")
            after = _snapshot_test_files(config, block)
        assert before != after

    def test_unreadable_file_is_omitted_not_fatal(self, tmp_path):
        config = BuildConfig(project_dir=tmp_path)
        block = BlockInfo(number=1, name="B", description="d")
        with patch(
            "build_pipeline.tdd_engine._list_block_test_files",
            return_value=["tests/gone.py"],
        ):
            assert _snapshot_test_files(config, block) == {}


class TestTestPathPatterns:
    """The integrity regex must cover every language buildme builds for.

    A Python-only pattern makes the gate silently no-op on a Swift or TS repo:
    zero paths matched → empty snapshot → "not checked" → tests freely editable.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "tests/test_a.py",
            "src/a_test.py",
            "tests/conftest.py",
            "internal/foo_test.go",
            "AppTests/ParserTests.swift",
            "Sources/ThingTest.swift",
            "src/a.test.ts",
            "src/a.spec.tsx",
            "src/a.test.mjs",
            "tests/fixtures/data.json",
            "__tests__/helper.js",
            "Tests/Support/Stub.swift",
        ],
    )
    def test_matches_test_paths(self, path):
        assert _TEST_PATH_RE.search(path)

    @pytest.mark.parametrize(
        "path",
        [
            "src/main.py",
            "src/latest.py",
            "Sources/Parser.swift",
            "src/protest.go",
            "src/a.ts",
            "docs/testing.md",
        ],
    )
    def test_ignores_production_paths(self, path):
        assert not _TEST_PATH_RE.search(path)

    def test_scan_regex_stays_python_only(self):
        """`_TEST_FILE_RE` feeds the `def test_*` scan — widening it breaks Swift."""
        assert _TEST_FILE_RE.search("tests/test_a.py")
        assert not _TEST_FILE_RE.search("AppTests/ParserTests.swift")
        assert not _TEST_FILE_RE.search("src/a.test.ts")


class TestEnforceTestIntegrity:
    def _fixtures(self, tmp_path, before, after, report=""):
        config = BuildConfig(project_dir=tmp_path)
        # specs_dir does not follow project_dir — without this the checkpoint
        # written by a flagged change lands in the repo, not in tmp_path.
        config.specs_dir = tmp_path / "specs"
        block = BlockInfo(number=1, name="B", description="d")
        block.test_file_hashes = before
        block.implementer_report = report
        state = BuildState(
            change_name="c", tier="standard", tdd=TDDState(blocks=[block])
        )
        return config, block, state

    def _run(self, config, block, state, after, window="implement", report=None):
        # `report` defaults to the block's accumulated report only because most
        # cases exercise the implement window, where they are the same string.
        if report is None:
            report = block.implementer_report
        with patch(
            "build_pipeline.tdd_engine._snapshot_test_files", return_value=after
        ):
            return _enforce_test_integrity(
                block,
                config,
                state,
                ProgressWriter(config.progress_file_path()),
                0,
                window,
                report,
            )

    def test_unchanged_tests_pass(self, tmp_path):
        config, block, state = self._fixtures(tmp_path, {"t.py": "h"}, None)
        assert self._run(config, block, state, {"t.py": "h"}) is True

    def test_silent_mutation_fails_the_block(self, tmp_path):
        config, block, state = self._fixtures(tmp_path, {"t.py": "h"}, None)
        assert self._run(config, block, state, {"t.py": "MUTATED"}) is False

    def test_declared_change_passes_and_records_the_claim(self, tmp_path):
        config, block, state = self._fixtures(
            tmp_path,
            {"t.py": "h"},
            None,
            report="TEST CHANGE REQUIRED: the assertion contradicted the spec",
        )
        assert self._run(config, block, state, {"t.py": "changed"}) is True
        assert block.test_change_claims == ["the assertion contradicted the spec"]

    def test_declaration_rebaselines_so_it_excuses_only_itself(self, tmp_path):
        """One declared change must not excuse every later silent one."""
        config, block, state = self._fixtures(
            tmp_path,
            {"t.py": "h"},
            None,
            report="TEST CHANGE REQUIRED: legit",
        )
        self._run(config, block, state, {"t.py": "v2"})
        assert block.test_file_hashes == {"t.py": "v2"}
        # A later SILENT change is measured from the re-baselined state. Clear
        # the report so it is no longer declared.
        block.implementer_report = ""
        assert (
            self._run(config, block, state, {"t.py": "v3"}, window="review-fix 1")
            is False
        )

    def test_declaration_does_not_carry_across_windows(self, tmp_path):
        """An implement-phase declaration must not authorize a review-fix mutation.

        `block.implementer_report` accumulates every agent's output, so passing
        it at the later window would find the earlier declaration and pass a
        silent mutation. Each window sees only its own agent's report.
        """
        config, block, state = self._fixtures(
            tmp_path,
            {"t.py": "h"},
            None,
            report="TEST CHANGE REQUIRED: legit",
        )
        assert self._run(config, block, state, {"t.py": "v2"}) is True
        # The accumulation still carries the implement-phase declaration...
        block.implementer_report += "\n\n[review-fix 1]\nfixed the thing"
        assert "TEST CHANGE REQUIRED" in block.implementer_report
        # ...but the review-fix window is judged on the fix agent's report only.
        assert (
            self._run(
                config,
                block,
                state,
                {"t.py": "v3"},
                window="review-fix 1",
                report="fixed the thing",
            )
            is False
        )

    def test_unavailable_snapshot_reports_not_checked_rather_than_failing(
        self, tmp_path
    ):
        """A git hiccup must not read as 'the agent deleted every test file'."""
        config, block, state = self._fixtures(tmp_path, {"t.py": "h"}, None)
        assert self._run(config, block, state, None) is False
        # And it must not re-baseline away the frozen bar.
        assert block.test_file_hashes == {"t.py": "h"}


class TestAdjudicateReviewFindings:
    def _inputs(self):
        review = ReviewResult(
            scores=[ReviewScore(dimension="Q", weight=2, score=4, evidence="e")],
            findings=[
                ReviewFinding(claim="real defect", file_path="a.py", line=1),
                ReviewFinding(claim="reviewer misread it", file_path="b.py", line=2),
            ],
        )
        block = BlockInfo(number=1, name="B", description="d")
        return review, block

    @pytest.mark.asyncio
    async def test_rejected_findings_never_reach_the_fix_loop(self):
        review, block = self._inputs()
        config = BuildConfig(model="m")
        adj = FindingAdjudication(
            verdicts=[
                FindingVerdict(index=0, accepted=True, reason="the diff shows it"),
                FindingVerdict(
                    index=1, accepted=False, reason="already handled elsewhere"
                ),
            ]
        )
        with patch(
            "build_pipeline.tdd_engine.llm_call_structured",
            new_callable=AsyncMock,
            return_value=adj,
        ):
            out = await _adjudicate_review_findings(
                review, block, config, "diff", "ctx"
            )
        assert [f.claim for f in out.findings] == ["real defect"]
        assert [f.claim for f in out.rejected_findings] == ["reviewer misread it"]
        assert "already handled elsewhere" in out.rejected_findings[0].evidence

    @pytest.mark.asyncio
    async def test_adjudicator_failure_is_fail_open(self):
        """Dropping findings when the judge is unavailable would weaken review
        exactly when something is already going wrong."""
        review, block = self._inputs()
        config = BuildConfig(model="m")
        with patch(
            "build_pipeline.tdd_engine.llm_call_structured",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            out = await _adjudicate_review_findings(
                review, block, config, "diff", "ctx"
            )
        assert len(out.findings) == 2

    @pytest.mark.asyncio
    async def test_disabled_drops_findings_rather_than_applying_them_blind(self):
        review, block = self._inputs()
        config = BuildConfig(model="m")
        config.adjudicate_findings = False
        with patch(
            "build_pipeline.tdd_engine.llm_call_structured", new_callable=AsyncMock
        ) as call:
            out = await _adjudicate_review_findings(
                review, block, config, "diff", "ctx"
            )
        call.assert_not_awaited()
        assert out.findings == []
        assert len(out.rejected_findings) == 2

    @pytest.mark.asyncio
    async def test_nothing_to_adjudicate_skips_the_call(self):
        config = BuildConfig(model="m")
        block = BlockInfo(number=1, name="B", description="d")
        review = ReviewResult(scores=[])
        with patch(
            "build_pipeline.tdd_engine.llm_call_structured", new_callable=AsyncMock
        ) as call:
            out = await _adjudicate_review_findings(
                review, block, config, "diff", "ctx"
            )
        call.assert_not_awaited()
        assert out.findings == []


# A suite command that actually inspects the tree: green until "broken"
# appears in module.py. A static "false" can no longer stand in for "the
# refactor broke the suite" — `_run_refactor_all` proves the tree green
# BEFORE checkpointing it, so a command that is red regardless of tree
# content trips that pre-gate and the agent never runs.
SUITE_RED_IF_BROKEN = (
    f"{sys.executable} -c "
    "\"import sys; sys.exit('broken' in open('module.py').read())\""
)


class TestWholeChangeRefactor:
    """One conservative pass over the finished change, between the last block
    and the entry-point check — with the same harness as the per-block step."""

    @pytest.fixture(autouse=True)
    def _clean_budget(self, monkeypatch):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        budget_mod.reset()
        yield
        budget_mod.reset()

    @pytest.fixture
    def setup(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        change_dir = project_dir / "specs" / "changes" / "feat"
        change_dir.mkdir(parents=True)
        (change_dir / "design.md").write_text("# Design")
        (change_dir / "rubric.md").write_text("# Rubric")
        (project_dir / "module.py").write_text("x = 1\n")
        baseline = _git_init_project(project_dir)

        # The build's own work, committed after the baseline: a test file (so
        # there is something for the integrity gate to hash) and a source file.
        (project_dir / "tests").mkdir()
        (project_dir / TEST_FILE_REL).write_text(ORIGINAL_TEST_SOURCE)
        (project_dir / "module.py").write_text("def add(a, b):\n    return a + b\n")
        subprocess.run(["git", "add", "-A"], cwd=project_dir, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "impl(block-1): B"], cwd=project_dir, check=True
        )

        config = BuildConfig(
            project_dir=project_dir,
            change_name="feat",
            tier="advanced",
            test_command="true",
            config_dir=tmp_path / "config",
            core_memory_files=[],
            stack_memory_files=[],
            additional_memory_files=[],
        )
        config.specs_dir = project_dir / "specs"

        block = BlockInfo(
            number=1,
            name="B",
            description="d",
            baseline_commit=baseline,
            status=BlockStatus.DONE,
        )
        state = BuildState(
            change_name="feat",
            tier="advanced",
            state_file=str(config.state_file_path()),
            tdd=TDDState(blocks=[block], current_block=1),
        )
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("feat", "advanced", 1)
        return config, state, progress

    @staticmethod
    def _agent_writing(project_dir: Path, rel: str, content: str, output: str = "done"):
        def _run(*_a, **_kw):
            path = project_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            return AgentResult(success=True, output=output)

        return _run

    @pytest.mark.asyncio
    async def test_verified_pass_is_committed(self, setup):
        config, state, progress = setup
        agent = self._agent_writing(
            config.project_dir,
            "module.py",
            "def add(a: int, b: int) -> int:\n    return a + b\n",
        )

        with patch(
            "build_pipeline.tdd_engine.run_refactor_all",
            new_callable=AsyncMock,
            side_effect=agent,
        ):
            assert await _run_refactor_all(config, state, progress) is True

        assert (
            _git_log_subjects(config.project_dir)[0]
            == "refactor: simplify across blocks"
        )
        assert state.tdd.refactor_all_done is True

    @pytest.mark.asyncio
    async def test_red_suite_reverts_and_the_build_continues(self, setup):
        config, state, progress = setup
        config.test_command = SUITE_RED_IF_BROKEN  # green now, red after the agent
        agent = self._agent_writing(
            config.project_dir,
            "module.py",
            "def add(a, b):\n    return a - b  # broken\n",
        )

        with patch(
            "build_pipeline.tdd_engine.run_refactor_all",
            new_callable=AsyncMock,
            side_effect=agent,
        ):
            assert await _run_refactor_all(config, state, progress) is True

        assert (
            config.project_dir / "module.py"
        ).read_text() == "def add(a, b):\n    return a + b\n"
        assert not any(
            s.startswith("refactor:") for s in _git_log_subjects(config.project_dir)
        )
        assert state.tdd.refactor_all_done is True

    @pytest.mark.asyncio
    async def test_test_file_mutation_reverts(self, setup):
        """Green suite, mutated tests — reverted, with no escape hatch even
        when the agent declares one."""
        config, state, progress = setup
        agent = self._agent_writing(
            config.project_dir,
            TEST_FILE_REL,
            "def test_addition():\n    assert True\n",
            output="TEST CHANGE REQUIRED: it was flaky\n",
        )

        with patch(
            "build_pipeline.tdd_engine.run_refactor_all",
            new_callable=AsyncMock,
            side_effect=agent,
        ):
            assert await _run_refactor_all(config, state, progress) is True

        assert (config.project_dir / TEST_FILE_REL).read_text() == ORIGINAL_TEST_SOURCE
        assert not any(
            s.startswith("refactor:") for s in _git_log_subjects(config.project_dir)
        )

    @pytest.mark.asyncio
    async def test_uncommitted_review_fixes_survive_a_revert(self, setup):
        """The block loop leaves review-fix edits uncommitted. They are
        checkpointed before the refactor runs, so discarding a failed refactor
        cannot take the build's own fixes with it."""
        config, state, progress = setup
        config.test_command = (
            SUITE_RED_IF_BROKEN  # green with the fix, red after the agent
        )
        (config.project_dir / "module.py").write_text(
            "def add(a, b):\n    return a + b  # review fix\n"
        )
        agent = self._agent_writing(
            config.project_dir,
            "module.py",
            "def add(a, b):\n    return a - b  # broken\n",
        )

        with patch(
            "build_pipeline.tdd_engine.run_refactor_all",
            new_callable=AsyncMock,
            side_effect=agent,
        ):
            assert await _run_refactor_all(config, state, progress) is True

        assert "# review fix" in (config.project_dir / "module.py").read_text()

    @pytest.mark.asyncio
    async def test_failed_checkpoint_commit_preserves_work_and_blocks_the_pass(
        self, setup
    ):
        """`_git_commit_tree` returns None for "commit failed" too (failing
        hook, missing identity) — with the dirty work possibly all staged.
        HEAD is then NOT a faithful snapshot, and trusting it as the rewind
        target would let a later failed verification reset over the
        uncommitted review fixes. No trustworthy rewind target → no refactor
        pass, and nothing is reset."""
        config, state, progress = setup
        (config.project_dir / "module.py").write_text(
            "def add(a, b):\n    return a + b  # review fix\n"
        )
        hook = config.project_dir / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=config.project_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        with patch(
            "build_pipeline.tdd_engine.run_refactor_all", new_callable=AsyncMock
        ) as agent:
            with pytest.raises(git_ops.GitLifecycleError, match="Commit failed"):
                await _run_refactor_all(config, state, progress)

        agent.assert_not_awaited()
        # The uncommitted review fix survived, and HEAD never moved.
        assert "# review fix" in (config.project_dir / "module.py").read_text()
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=config.project_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert head_after == head_before
        assert state.tdd.refactor_all_done is False

    @pytest.mark.asyncio
    async def test_red_tree_on_resume_is_not_checkpointed_as_a_baseline(self, setup):
        """Crash simulation: a prior pass's agent edited files, the process
        died before verification, and resume re-enters with refactor_all_done
        still False. The suite is red with the partial edits in place — they
        must not be swept into the checkpoint commit (the rewind baseline),
        no agent may run on top of them, and they must not be reset over
        either (whose edits are dirty is unknowable)."""
        config, state, progress = setup
        config.test_command = SUITE_RED_IF_BROKEN
        (config.project_dir / "module.py").write_text(
            "def add(a, b):\n    return a - b  # broken, half-refactored\n"
        )
        subjects_before = _git_log_subjects(config.project_dir)

        with patch(
            "build_pipeline.tdd_engine.run_refactor_all", new_callable=AsyncMock
        ) as agent:
            assert await _run_refactor_all(config, state, progress) is False

        agent.assert_not_awaited()
        # No checkpoint (or any other) commit swept the breakage in...
        assert _git_log_subjects(config.project_dir) == subjects_before
        # ...and the dirty edits were not destroyed either.
        assert "half-refactored" in (config.project_dir / "module.py").read_text()
        assert state.tdd.refactor_all_done is False

    @pytest.mark.asyncio
    async def test_already_done_is_not_re_run_on_resume(self, setup):
        config, state, progress = setup
        state.tdd.refactor_all_done = True

        with patch(
            "build_pipeline.tdd_engine.run_refactor_all", new_callable=AsyncMock
        ) as agent:
            assert await _run_refactor_all(config, state, progress) is True

        agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_agent_failure_is_non_fatal(self, setup):
        config, state, progress = setup
        fail = AgentResult(success=False, error="crashed")

        with patch(
            "build_pipeline.tdd_engine.run_refactor_all",
            new_callable=AsyncMock,
            return_value=fail,
        ):
            assert await _run_refactor_all(config, state, progress) is True

        assert state.tdd.refactor_all_done is True

    @pytest.mark.parametrize(
        "fail",
        [
            AgentResult(success=False, error="crashed"),
            AgentResult(success=False, timed_out=True),
        ],
        ids=["error", "timeout"],
    )
    @pytest.mark.asyncio
    async def test_failed_agent_partial_edits_are_rolled_back(self, setup, fail):
        """An agent that wrote files and then failed or timed out leaves
        unverified edits behind. The step is marked done, so a resume never
        re-runs it — left in place, those edits would make every later step
        that needs a committed tree (MUTATION) refuse on every resume. They
        are rolled back to the pre-refactor checkpoint, which still holds the
        build's own uncommitted review fixes."""
        config, state, progress = setup
        (config.project_dir / "module.py").write_text(
            "def add(a, b):\n    return a + b  # review fix\n"
        )

        def _partial_then_fail(*_a, **_kw):
            (config.project_dir / "module.py").write_text(
                "def add(a, b):\n    return (a +\n"
            )
            (config.project_dir / "helpers.py").write_text("# half-written\n")
            return fail

        with patch(
            "build_pipeline.tdd_engine.run_refactor_all",
            new_callable=AsyncMock,
            side_effect=_partial_then_fail,
        ):
            assert await _run_refactor_all(config, state, progress) is True

        assert (
            config.project_dir / "module.py"
        ).read_text() == "def add(a, b):\n    return a + b  # review fix\n"
        assert not (config.project_dir / "helpers.py").exists()
        assert git_ops.tree_is_clean(config.project_dir)
        assert not any(
            s.startswith("refactor:") for s in _git_log_subjects(config.project_dir)
        )
        assert state.tdd.refactor_all_done is True

    @pytest.mark.asyncio
    async def test_exhausted_budget_stops_before_the_agent(self, setup):
        config, state, progress = setup

        class _Usage:
            input_tokens, output_tokens = 500, 0
            cache_read_tokens = cache_creation_tokens = 0
            cost_usd = 0.0

        budget_mod.configure(max_tokens=100)
        budget_mod.record(_Usage(), label="spec")

        with patch(
            "build_pipeline.tdd_engine.run_refactor_all", new_callable=AsyncMock
        ) as agent:
            assert await _run_refactor_all(config, state, progress) is False

        agent.assert_not_awaited()
        assert state.tdd.refactor_all_done is False

    @pytest.mark.asyncio
    async def test_snapshot_is_none_when_any_block_cannot_be_listed(self, setup):
        """An unobtainable snapshot means 'not checked', never 'all deleted'."""
        config, state, progress = setup
        with patch("build_pipeline.tdd_engine._snapshot_test_files", return_value=None):
            assert _snapshot_all_test_files(config, state) is None


class TestRefactorAllOrdering:
    """The whole-change pass runs after the last block and before the
    entry-point check. No whole-change review follows it: that is
    /multiplai-dev:review's job, on the PR."""

    @pytest.mark.asyncio
    async def test_refactor_all_runs_before_the_entry_point_check(self, tdd_setup):
        config, args = tdd_setup
        config.gates.e2e_test_entry_point_check = True
        calls: list[str] = []

        async def _refactor_all(*_a, **_kw):
            calls.append("refactor_all")
            return True

        def _entry_point(*_a, **_kw):
            calls.append("e2e_check")
            return GateResult(passed=True, reason="ok")

        ok = AgentResult(success=True, output="done")
        with (
            patch.dict(os.environ, {"BUILDME_TRUST_REPO": "1"}),
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_test_suite",
                return_value=RED_SUITE_RESULT,
            ),
            patch(
                "build_pipeline.tdd_engine._audit_test_quality",
                new_callable=AsyncMock,
                return_value=QualityAudit(passed=True),
            ),
            patch(
                "build_pipeline.tdd_engine._run_quality_review",
                new_callable=AsyncMock,
                return_value=ReviewResult(
                    scores=[ReviewScore(dimension="Q", weight=2, score=5, evidence="e")]
                ),
            ),
            patch(
                "build_pipeline.tdd_engine._adjudicate_review_findings",
                new_callable=AsyncMock,
                side_effect=lambda review, *a, **k: review,
            ),
            patch(
                "build_pipeline.tdd_engine._run_refactor_all", side_effect=_refactor_all
            ),
            patch(
                "build_pipeline.tdd_engine._verify_entry_point",
                side_effect=_entry_point,
            ),
        ):
            result = await run_tdd_engine(config, args)

        assert result == EXIT_SUCCESS
        assert calls == ["refactor_all", "e2e_check"]

    @pytest.mark.asyncio
    async def test_budget_stop_in_refactor_all_fails_the_build(self, tdd_setup):
        config, args = tdd_setup
        ok = AgentResult(success=True, output="done")

        async def _refactor_all(*_a, **_kw):
            return False

        with (
            patch.dict(os.environ, {"BUILDME_TRUST_REPO": "1"}),
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_test_suite",
                return_value=RED_SUITE_RESULT,
            ),
            patch(
                "build_pipeline.tdd_engine._audit_test_quality",
                new_callable=AsyncMock,
                return_value=QualityAudit(passed=True),
            ),
            patch(
                "build_pipeline.tdd_engine._run_quality_review",
                new_callable=AsyncMock,
                return_value=ReviewResult(
                    scores=[ReviewScore(dimension="Q", weight=2, score=5, evidence="e")]
                ),
            ),
            patch(
                "build_pipeline.tdd_engine._adjudicate_review_findings",
                new_callable=AsyncMock,
                side_effect=lambda review, *a, **k: review,
            ),
            patch(
                "build_pipeline.tdd_engine._run_refactor_all", side_effect=_refactor_all
            ),
            patch("build_pipeline.tdd_engine._verify_entry_point") as entry_point,
        ):
            result = await run_tdd_engine(config, args)

        assert result == EXIT_BUILD_FAILURE
        entry_point.assert_not_called()


class TestCheckpointLifecycleOwnership:
    """Who owns the checkpoint on success: the engine when it IS the build
    (`python -m build_pipeline tdd`), the orchestrator when the engine is a
    sub-phase.

    Deleting the checkpoint as a sub-phase defeated the orchestrator's reload
    guard (`if state_path.exists()`), which then rewrote a stale in-memory
    state with `tdd = None`: a crash in DOCS_UPDATE/RESPEC/PUBLISH resumed at
    tdd_build with no block state and re-ran the whole TDD build.
    """

    @staticmethod
    def _patches():
        ok = AgentResult(success=True, output="done")
        return [
            patch.dict(os.environ, {"BUILDME_TRUST_REPO": "1"}),
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_test_suite",
                return_value=RED_SUITE_RESULT,
            ),
            patch(
                "build_pipeline.tdd_engine._audit_test_quality",
                new_callable=AsyncMock,
                return_value=QualityAudit(passed=True),
            ),
            patch(
                "build_pipeline.tdd_engine._run_quality_review",
                new_callable=AsyncMock,
                return_value=ReviewResult(
                    scores=[ReviewScore(dimension="Q", weight=2, score=5, evidence="e")]
                ),
            ),
            patch(
                "build_pipeline.tdd_engine._adjudicate_review_findings",
                new_callable=AsyncMock,
                side_effect=lambda review, *a, **k: review,
            ),
            patch(
                "build_pipeline.tdd_engine._run_refactor_all",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ]

    async def _run(self, config, args, **kwargs):
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)
            return await run_tdd_engine(config, args, **kwargs)

    @pytest.mark.asyncio
    async def test_sub_phase_run_leaves_the_checkpoint_for_the_orchestrator(
        self, tdd_setup
    ):
        config, args = tdd_setup
        state_path = config.state_file_path()

        assert await self._run(config, args, standalone=False) == EXIT_SUCCESS

        assert state_path.exists(), (
            "sub-phase run deleted the orchestrator's checkpoint"
        )
        state = BuildState.load(state_path)
        assert state.tdd is not None, "checkpoint lost its TDD sub-state"
        assert all(b.status == BlockStatus.DONE for b in state.tdd.blocks)
        # The orchestrator owns phase advancement: the pointer must not have
        # jumped past TDD_BUILD, or DOCS_UPDATE/RESPEC/PUBLISH would be skipped.
        assert state.phase == BuildPhase.TDD_BUILD
        for later in (BuildPhase.DOCS_UPDATE, BuildPhase.RESPEC, BuildPhase.PUBLISH):
            assert not state.is_phase_complete(later)

    @pytest.mark.asyncio
    async def test_standalone_run_keeps_its_checkpoint_as_completed(self, tdd_setup):
        """The `python -m build_pipeline tdd` entry point is the build: on
        success the checkpoint becomes `.build-state.complete.json`, at
        TDD_BUILD (not a published build)."""
        config, args = tdd_setup

        assert await self._run(config, args) == EXIT_SUCCESS
        assert not config.state_file_path().exists()
        kept = config.state_file_path().with_name(".build-state.complete.json")
        assert BuildState.load(kept).phase == BuildPhase.TDD_BUILD

    @pytest.mark.asyncio
    async def test_a_post_tdd_resume_does_not_re_run_the_build(self, tdd_setup):
        """The consequence of the bug, asserted end to end: after the sub-phase
        run, a fresh engine invocation (as a crashed DOCS_UPDATE would produce)
        finds every block DONE and spawns no agent."""
        config, args = tdd_setup
        assert await self._run(config, args, standalone=False) == EXIT_SUCCESS

        with (
            patch.dict(os.environ, {"BUILDME_TRUST_REPO": "1"}),
            patch(
                "build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock
            ) as writer,
            patch(
                "build_pipeline.tdd_engine._run_refactor_all",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            assert await run_tdd_engine(config, args, standalone=False) == EXIT_SUCCESS

        writer.assert_not_awaited()


class TestBudgetCircuitBreaker:
    """The engine stops on a spent budget at the block boundary."""

    @pytest.fixture(autouse=True)
    def _clean_budget(self, monkeypatch):
        # The baseline gate runs the repo's test_command, which needs the trust
        # opt-in; without it these tests would fail for the wrong reason.
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        budget_mod.reset()
        yield
        budget_mod.reset()

    @pytest.mark.asyncio
    async def test_exhausted_budget_stops_before_the_first_block(self, tdd_setup):
        config, args = tdd_setup
        config.budget_max_tokens = 100

        class _Usage:
            input_tokens, output_tokens = 500, 0
            cache_read_tokens = cache_creation_tokens = 0
            cost_usd = 0.0

        # Spend it during the baseline phase, before any block starts.
        budget_mod.configure(max_tokens=100)
        budget_mod.record(_Usage(), label="spec")

        with patch(
            "build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock
        ) as writer:
            result = await run_tdd_engine(config, args, standalone=False)

        assert result == EXIT_BUILD_FAILURE
        # Stopped at the boundary — no agent was spawned for the block.
        writer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_spend_survives_a_resume(self, tdd_setup):
        """A fresh budget on resume would make the ceiling unenforceable."""
        config, args = tdd_setup
        config.budget_max_tokens = 100

        class _Usage:
            input_tokens, output_tokens = 500, 0
            cache_read_tokens = cache_creation_tokens = 0
            cost_usd = 0.0

        budget_mod.configure(max_tokens=100)
        budget_mod.record(_Usage(), label="spec")
        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock):
            await run_tdd_engine(config, args, standalone=False)
        assert config.state_file_path().exists()

        # Simulate a new process picking the build back up.
        budget_mod.reset()
        with patch(
            "build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock
        ) as writer:
            result = await run_tdd_engine(config, args)
        assert result == EXIT_BUILD_FAILURE
        writer.assert_not_awaited()
        assert budget_mod.get_budget().total_tokens == 500

    @pytest.mark.asyncio
    async def test_no_ceiling_configured_never_stops_a_block(self, tdd_setup):
        config, args = tdd_setup  # budget_max_tokens defaults to None

        class _Usage:
            input_tokens, output_tokens = 10_000_000, 0
            cache_read_tokens = cache_creation_tokens = 0
            cost_usd = 0.0

        budget_mod.record(_Usage())
        fail = AgentResult(success=False, error="stop here")
        with patch(
            "build_pipeline.tdd_engine.run_test_writer",
            new_callable=AsyncMock,
            return_value=fail,
        ) as writer:
            await run_tdd_engine(config, args)
        # Reached the block: the budget did not intercept it.
        writer.assert_awaited()


CONTRADICTION_REPORT = """\
Implemented the uploader against the failing tests.

STATUS: DONE
TESTS_RUN: pytest -xvs
GREEN: 12 passed in 1.2s
FILES: uploader.py
SURPRISES: The storage client raises on timeout; the design says it returns
an error code. The block only works if the caller catches the exception.
SPEC_IMPACT: contradicts
"""

CLARIFY_TEST_REPORT = """\
STATUS: DONE
TESTS_RUN: pytest -xvs
FILES: tests/test_uploader.py
TEST_COUNT: 6
SURPRISES: The spec never says what happens on an empty file; I pinned it to
a validation error.
SPEC_IMPACT: clarify
"""

CLEAN_REPORT = "STATUS: DONE\nFILES: uploader.py\nSURPRISES: none\nSPEC_IMPACT: none\n"


class TestImplementationNotesLoop:
    """B3: agents' SURPRISES/SPEC_IMPACT reports become durable notes, and a
    contradiction is surfaced (and optionally halts the build)."""

    @pytest.fixture(autouse=True)
    def _red(self, red_gate_passes, quality_audit_overturns):
        yield

    @pytest.fixture
    def setup(self, tmp_path):
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

        block = BlockInfo(number=1, name="Uploader", description="Upload block")
        state = BuildState(
            change_name="test",
            tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(project_dir / "build-progress.md")
        progress.initialize("test", "advanced", 1)
        return config, state, progress, block

    @staticmethod
    def _agents(test_output, impl_output):
        return (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=AgentResult(success=True, output=test_output),
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=AgentResult(success=True, output=impl_output),
            ),
        )

    @pytest.mark.asyncio
    async def test_contradiction_note_is_recorded_and_warned_about(self, setup):
        """Criterion 8 (default config): the note lands in
        implementation-notes.md, on the block, and as a warning in
        build-progress.md — and the build keeps going."""
        config, state, progress, block = setup
        tw, impl = self._agents("STATUS: DONE\nTEST_COUNT: 6\n", CONTRADICTION_REPORT)

        with tw, impl:
            result = await run_block_tdd(block, config, state, progress)

        assert result is True  # halt_on_contradiction defaults to False
        notes_file = config.change_dir / "implementation-notes.md"
        assert notes_file.exists()
        notes_text = notes_file.read_text()
        assert "SPEC_IMPACT: contradicts" in notes_text
        assert "raises on timeout" in notes_text
        assert "## Block 1 — Uploader (implementer)" in notes_text

        assert len(block.notes) == 1
        assert block.notes[0].contradicts
        assert block.notes[0].role == "implementer"

        progress_text = progress.path.read_text()
        assert "WARNING — SPEC_IMPACT: contradicts" in progress_text
        assert "raises on timeout" in progress_text

    @pytest.mark.asyncio
    async def test_halt_on_contradiction_stops_the_build_with_a_diagnosis(self, setup):
        """Criterion 8 (respec.halt_on_contradiction: true)."""
        config, state, progress, block = setup
        config.gates.respec_halt_on_contradiction = True
        tw, impl = self._agents("STATUS: DONE\nTEST_COUNT: 6\n", CONTRADICTION_REPORT)

        with tw, impl:
            result = await run_block_tdd(block, config, state, progress)

        assert result is False
        assert block.status == BlockStatus.FAILED
        progress_text = progress.path.read_text()
        assert "DIAGNOSIS (Uploader)" in progress_text
        assert "SPEC_IMPACT: contradicts" in progress_text
        assert "halt_on_contradiction" in progress_text
        # The note is still recorded — a halted build keeps its learning.
        assert (config.change_dir / "implementation-notes.md").exists()

    @pytest.mark.asyncio
    async def test_notes_persist_on_state_for_resume(self, setup):
        config, state, progress, block = setup
        tw, impl = self._agents(CLARIFY_TEST_REPORT, CONTRADICTION_REPORT)

        with tw, impl:
            assert await run_block_tdd(block, config, state, progress) is True

        reloaded = BuildState.load(config.state_file_path())
        roles = [(n.role, n.spec_impact) for n in reloaded.tdd.blocks[0].notes]
        assert roles == [("test_writer", "clarify"), ("implementer", "contradicts")]

    @pytest.mark.asyncio
    async def test_note_is_on_disk_before_a_later_crash(self, setup):
        """Appended as the build runs: the test_writer's note survives an
        implementer failure that ends the block."""
        config, state, progress, block = setup

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=AgentResult(success=True, output=CLARIFY_TEST_REPORT),
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=AgentResult(success=False, error="agent crashed"),
            ),
        ):
            result = await run_block_tdd(block, config, state, progress)

        assert result is False
        notes_text = (config.change_dir / "implementation-notes.md").read_text()
        assert "## Block 1 — Uploader (test_writer)" in notes_text
        assert "SPEC_IMPACT: clarify" in notes_text
        assert "empty file" in notes_text

    @pytest.mark.asyncio
    async def test_clean_reports_record_no_notes(self, setup):
        config, state, progress, block = setup
        tw, impl = self._agents(CLEAN_REPORT, CLEAN_REPORT)

        with tw, impl:
            assert await run_block_tdd(block, config, state, progress) is True

        assert not (config.change_dir / "implementation-notes.md").exists()
        assert block.notes == []

    def test_resume_does_not_duplicate_an_identical_note(self, setup):
        """A resumed mid-block run re-parses the same agent report — the note
        must land once on the block and once in implementation-notes.md."""
        from build_pipeline.tdd_engine import _record_implementation_note

        config, state, progress, block = setup
        for _ in range(2):
            assert (
                _record_implementation_note(
                    block,
                    config,
                    state,
                    progress,
                    "implementer",
                    CONTRADICTION_REPORT,
                )
                is True
            )

        assert len(block.notes) == 1
        notes_text = (config.change_dir / "implementation-notes.md").read_text()
        assert notes_text.count("## Block 1 — Uploader (implementer)") == 1

    def test_distinct_notes_are_both_recorded(self, setup):
        from build_pipeline.tdd_engine import _record_implementation_note

        config, state, progress, block = setup
        _record_implementation_note(
            block,
            config,
            state,
            progress,
            "test_writer",
            CLARIFY_TEST_REPORT,
        )
        _record_implementation_note(
            block,
            config,
            state,
            progress,
            "implementer",
            CONTRADICTION_REPORT,
        )
        assert len(block.notes) == 2

    def test_duplicate_contradiction_still_halts_when_configured(self, setup):
        """Dedupe skips the recording, never the halt — a resume must not
        sneak past halt_on_contradiction."""
        from build_pipeline.tdd_engine import _record_implementation_note

        config, state, progress, block = setup
        config.gates.respec_halt_on_contradiction = True
        first = _record_implementation_note(
            block,
            config,
            state,
            progress,
            "implementer",
            CONTRADICTION_REPORT,
        )
        second = _record_implementation_note(
            block,
            config,
            state,
            progress,
            "implementer",
            CONTRADICTION_REPORT,
        )
        assert first is False
        assert second is False
        assert len(block.notes) == 1


class TestReportContractSlots:
    """3.1 — the two new REQUIRED slots are in every agent report contract."""

    def test_implementer_prompts_declare_both_slots(self):
        from build_pipeline.prompts.implementation import (
            IMPLEMENTER_PROMPT_CLEAN,
            IMPLEMENTER_PROMPT_MINIMUM,
        )

        for prompt in (IMPLEMENTER_PROMPT_CLEAN, IMPLEMENTER_PROMPT_MINIMUM):
            assert "SURPRISES:" in prompt
            assert "SPEC_IMPACT: <none | clarify | contradicts>" in prompt

    def test_test_writer_prompt_declares_both_slots(self):
        from build_pipeline.prompts.test_writing import TEST_WRITER_PROMPT

        assert "SURPRISES:" in TEST_WRITER_PROMPT
        assert "SPEC_IMPACT: <none | clarify | contradicts>" in TEST_WRITER_PROMPT

    def test_prompts_explain_contradicts_without_discouraging_reports(self):
        """Positive recipe: the prompt says when to use each value and that
        nothing is auto-applied — no instruction that suppresses a finding."""
        from build_pipeline.prompts.implementation import IMPLEMENTER_PROMPT_CLEAN

        assert "contradicts" in IMPLEMENTER_PROMPT_CLEAN
        assert "implementation-notes.md" in IMPLEMENTER_PROMPT_CLEAN


class TestMutationStepPlacement:
    """The MUTATION sub-step runs right after the whole-change refactor, and a
    failed mutation step stops the build there."""

    @pytest.mark.asyncio
    async def test_mutation_runs_right_after_refactor_all(self, tdd_setup):
        config, args = tdd_setup
        calls: list[str] = []

        async def _refactor_all(*_a, **_kw):
            calls.append("refactor_all")
            return True

        async def _mutation(*_a, **_kw):
            calls.append("mutation")
            return False

        ok = AgentResult(success=True, output="done")
        with (
            patch.dict(os.environ, {"BUILDME_TRUST_REPO": "1"}),
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=ok,
            ),
            patch(
                "build_pipeline.tdd_engine.run_test_suite",
                return_value=RED_SUITE_RESULT,
            ),
            patch(
                "build_pipeline.tdd_engine._audit_test_quality",
                new_callable=AsyncMock,
                return_value=QualityAudit(passed=True),
            ),
            patch(
                "build_pipeline.tdd_engine._run_quality_review",
                new_callable=AsyncMock,
                return_value=ReviewResult(
                    scores=[ReviewScore(dimension="Q", weight=2, score=5, evidence="e")]
                ),
            ),
            patch(
                "build_pipeline.tdd_engine._adjudicate_review_findings",
                new_callable=AsyncMock,
                side_effect=lambda review, *a, **k: review,
            ),
            patch(
                "build_pipeline.tdd_engine._run_refactor_all", side_effect=_refactor_all
            ),
            patch("build_pipeline.tdd_engine._run_mutation", side_effect=_mutation),
            patch("build_pipeline.tdd_engine._verify_entry_point") as e2e,
        ):
            result = await run_tdd_engine(config, args)

        assert result == EXIT_BUILD_FAILURE
        assert calls == ["refactor_all", "mutation"]
        e2e.assert_not_called()

    @pytest.mark.asyncio
    async def test_pre_mutation_checkpoint_resumes_into_the_mutation_step(
        self, tdd_setup
    ):
        """A checkpoint written before the MUTATION step existed — every block
        done, refactor_all_done true, no mutation_done key — resumes straight
        into the mutation step: no agent runs, the refactor pass is not
        repeated."""
        config, args = tdd_setup
        fixture = Path(__file__).parent / "fixtures" / "build-state-pre-mutation.json"
        config.state_file_path().write_text(fixture.read_text())
        seen: list[bool] = []

        async def _mutation(_config, state, _progress):
            seen.append(state.tdd.mutation_done)
            return False

        with (
            patch.dict(os.environ, {"BUILDME_TRUST_REPO": "1"}),
            patch(
                "build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock
            ) as tw,
            patch(
                "build_pipeline.tdd_engine.run_refactor_all", new_callable=AsyncMock
            ) as ra,
            patch("build_pipeline.tdd_engine._run_mutation", side_effect=_mutation),
        ):
            result = await run_tdd_engine(config, args)

        assert result == EXIT_BUILD_FAILURE
        assert seen == [False]
        tw.assert_not_awaited()
        ra.assert_not_awaited()


def test_nested_test_evidence_uses_project_relative_paths(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    project = tmp_path / "services" / "api"
    project.mkdir(parents=True)
    path = project / "tests" / "test_with spaces.py"
    path.parent.mkdir()
    path.write_text("def test_it():\n    assert False\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    baseline = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    path.write_text("def test_it():\n    assert True\n")
    config = BuildConfig(project_dir=project)
    block = BlockInfo(
        number=1,
        name="nested",
        description="Read nested tests",
        baseline_commit=baseline,
    )
    assert "assert True" in _read_block_test_files(config, block)
    assert set(_snapshot_test_files(config, block)) == {"tests/test_with spaces.py"}


def test_nested_review_diffs_have_project_relative_headers(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    project = tmp_path / "services/api"
    project.mkdir(parents=True)
    (project / "app.py").write_text("value = 1\n")
    (tmp_path / "sibling.txt").write_text("untouched\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    (project / "app.py").write_text("value = 2\n")
    (tmp_path / "sibling.txt").write_text("sibling secret\n")
    config = BuildConfig(project_dir=project)
    block = BlockInfo(number=1, name="nested", description="change value")
    state = BuildState(
        change_name="nested", tier="standard", tdd=TDDState(blocks=[block])
    )
    for diff in (_capture_block_diff(config, block), capture_build_diff(config, state)):
        assert "--- a/app.py" in diff
        assert "+++ b/app.py" in diff
        assert "services/api" not in diff
        assert "sibling" not in diff


# --- RED_EXPECTED: the test writer's statement of which tests must fail ---

RX_TEST = "def test_add():\n    from module import add\n    assert add(2, 3) == 5\n"
RX_RED = (
    1,
    "FAILED tests/test_new.py::test_add - AssertionError: not implemented\n1 failed",
)
RX_OK = AgentResult(success=True, output="STATUS: DONE")


def _rx_report(slot: str | None) -> str:
    lines = [
        "STATUS: DONE",
        "TESTS_RUN: pytest",
        "FILES: tests/test_new.py",
        "TEST_COUNT: 1",
    ]
    if slot is not None:
        lines.append(f"RED_EXPECTED: {slot}")
    return "\n".join(lines + ["SURPRISES: none", "SPEC_IMPACT: none"]) + "\n"


class _RxStop(Exception):
    """Stands in for the process being killed at that point."""


class TestRedExpected:
    """`block.red_expected` records the test writer's `RED_EXPECTED:` slot and
    the check against the RED run, and opens `block.red_evidence`. Agents are
    faked; git and the files are real."""

    @pytest.fixture(autouse=True)
    def _agents(self):
        from build_pipeline.models import GateResult

        with (
            patch(
                "build_pipeline.tdd_engine.integration_gate",
                return_value=GateResult(passed=True, reason="fixture checks pass"),
            ),
            patch(
                "build_pipeline.tdd_engine.run_refactorer",
                new_callable=AsyncMock,
                return_value=RX_OK,
            ),
            patch(
                "build_pipeline.tdd_engine._audit_test_quality",
                new_callable=AsyncMock,
                return_value=QualityAudit(passed=True),
            ),
        ):
            yield

    @staticmethod
    def _project(root: Path, monkeypatch, test_command: str = "pytest"):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        project = root / "project"
        (project / "specs" / "changes" / "test").mkdir(parents=True)
        (project / "specs" / "changes" / "test" / "design.md").write_text("# Design")
        (project / "module.py").write_text(
            "def add(a, b):\n    raise NotImplementedError\n"
        )
        (project / "tests").mkdir()
        _git_init_project(project)
        config = BuildConfig(
            project_dir=project,
            change_name="test",
            tier="advanced",
            test_command=test_command,
            config_dir=root / "config",
            core_memory_files=[],
            stack_memory_files=[],
            additional_memory_files=[],
        )
        config.specs_dir = project / "specs"
        block = BlockInfo(number=1, name="Add", description="Add two numbers.")
        state = BuildState(
            change_name="test",
            tier="advanced",
            state_file=str(root / "state.json"),
            tdd=TDDState(blocks=[block]),
        )
        progress = ProgressWriter(root / "progress.md")
        progress.initialize("test", "advanced", 1)
        return config, state, progress, block

    @staticmethod
    def _writer(project: Path, report: str):
        async def writer(**_kw):
            (project / "tests" / "test_new.py").write_text(RX_TEST)
            return AgentResult(success=True, output=report)

        return writer

    @staticmethod
    def _reload(config):
        state = BuildState.load(config.state_file_path())
        return state, state.tdd.blocks[state.tdd.current_block]

    async def _run(
        self, config, state, progress, block, report, suite=RX_RED, implementer=None
    ):
        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                side_effect=self._writer(config.project_dir, report),
            ),
            patch("build_pipeline.tdd_engine.run_test_suite", return_value=suite),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                **(
                    {"side_effect": implementer}
                    if implementer
                    else {"new_callable": AsyncMock, "return_value": RX_OK}
                ),
            ),
        ):
            return await run_block_tdd(block, config, state, progress)

    async def _unstopped(self, root: Path, monkeypatch, report: str):
        config, state, progress, block = self._project(root, monkeypatch)
        assert await self._run(config, state, progress, block, report) is True
        return config, block

    @staticmethod
    def _same_apart_from_red_expected(
        resumed, resumed_config, unstopped, unstopped_config
    ):
        """Everything a resumed run leaves equals the unstopped run's, except
        `red_expected` and the lines of `red_evidence` it opens."""
        assert resumed.status == unstopped.status
        assert resumed.test_file_hashes == unstopped.test_file_hashes
        assert resumed.red_evidence.removeprefix(
            resumed.red_expected
        ) == unstopped.red_evidence.removeprefix(unstopped.red_expected)
        assert _git_log_subjects(resumed_config.project_dir) == _git_log_subjects(
            unstopped_config.project_dir
        )

    # --- one engine test per result ---

    @pytest.mark.asyncio
    async def test_red_expected_named_tests_failed(self, tmp_path, monkeypatch):
        config, state, progress, block = self._project(tmp_path, monkeypatch)
        report = _rx_report(
            "tests/test_new.py::test_add — add() raises NotImplementedError"
        )
        assert await self._run(config, state, progress, block, report) is True
        assert block.red_expected == (
            "RED_EXPECTED: tests/test_new.py::test_add — add() raises "
            "NotImplementedError\nRED_EXPECTED result: named tests failed"
        )
        assert block.red_evidence.startswith(block.red_expected + "\n")
        assert (
            "RED_EXPECTED result: named tests failed"
            in (tmp_path / "progress.md").read_text()
        )
        assert self._reload(config)[1].red_expected == block.red_expected

    @pytest.mark.asyncio
    async def test_red_expected_named_tests_not_among_the_failures(
        self, tmp_path, monkeypatch
    ):
        config, state, progress, block = self._project(tmp_path, monkeypatch)
        report = _rx_report(
            "tests/test_new.py::test_add, tests/test_new.py::test_sub — no add() or sub()"
        )
        assert await self._run(config, state, progress, block, report) is True
        assert block.red_expected.splitlines()[-1] == (
            "RED_EXPECTED result: named tests not among the failures: tests/test_new.py::test_sub"
        )
        assert block.red_evidence.startswith(block.red_expected + "\n")

    @pytest.mark.asyncio
    async def test_red_expected_not_stated(self, tmp_path, monkeypatch):
        config, state, progress, block = self._project(tmp_path, monkeypatch)
        assert await self._run(config, state, progress, block, _rx_report(None)) is True
        assert block.red_expected == "RED_EXPECTED result: not stated"
        assert block.red_evidence.startswith(block.red_expected + "\n")

    @pytest.mark.asyncio
    async def test_red_expected_cannot_check_for_this_runner(
        self, tmp_path, monkeypatch
    ):
        config, state, progress, block = self._project(
            tmp_path, monkeypatch, test_command="make test"
        )
        report = _rx_report(
            "tests/test_new.py::test_add — add() raises NotImplementedError"
        )
        assert (
            await self._run(
                config,
                state,
                progress,
                block,
                report,
                suite=(2, "make: *** [test] Error 1"),
            )
            is True
        )
        assert (
            block.red_expected.splitlines()[-1]
            == "RED_EXPECTED result: cannot check for this runner"
        )
        assert block.red_evidence.startswith(block.red_expected + "\n")

    @pytest.mark.asyncio
    async def test_red_expected_comes_from_the_retry_after_a_failed_red_gate(
        self, tmp_path, monkeypatch
    ):
        config, state, progress, block = self._project(tmp_path, monkeypatch)
        first = self._writer(
            config.project_dir, _rx_report("tests/test_new.py::test_old — stale")
        )
        retry = self._writer(
            config.project_dir, _rx_report("tests/test_new.py::test_add — no add()")
        )
        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                side_effect=[await first(), await retry()],
            ),
            patch(
                "build_pipeline.tdd_engine.run_test_suite",
                side_effect=[(0, "1 passed"), RX_RED],
            ),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=RX_OK,
            ),
        ):
            assert await run_block_tdd(block, config, state, progress) is True
        assert block.red_expected == (
            "RED_EXPECTED: tests/test_new.py::test_add — no add()\n"
            "RED_EXPECTED result: named tests failed"
        )

    # --- stop and resume rows S1-S3 ---

    @pytest.mark.asyncio
    async def test_red_expected_resume_s1_stop_before_red_gate_after_the_test_commit(
        self, tmp_path, monkeypatch
    ):
        """S1: stopped after the test writer returned and its tests were
        committed, before red_gate ran. Resume does not run the test writer
        again, so its report is unavailable: `not stated`."""
        report = _rx_report("tests/test_new.py::test_add — no add()")
        u_config, unstopped = await self._unstopped(
            tmp_path / "unstopped", monkeypatch, report
        )

        config, state, progress, block = self._project(
            tmp_path / "stopped", monkeypatch
        )
        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                side_effect=self._writer(config.project_dir, report),
            ),
            patch("build_pipeline.tdd_engine.run_test_suite", side_effect=_RxStop),
        ):
            with pytest.raises(_RxStop):
                await run_block_tdd(block, config, state, progress)
        state, block = self._reload(config)
        assert block.test_commit and not block.red_evidence and not block.red_expected

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=RX_OK,
            ) as tw,
            patch("build_pipeline.tdd_engine.run_test_suite", return_value=RX_RED),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=RX_OK,
            ),
        ):
            assert await run_block_tdd(block, config, state, progress) is True
        tw.assert_not_awaited()
        assert block.red_expected == "RED_EXPECTED result: not stated"
        assert unstopped.red_expected.endswith(
            "RED_EXPECTED result: named tests failed"
        )
        self._same_apart_from_red_expected(block, config, unstopped, u_config)

    @pytest.mark.asyncio
    async def test_red_expected_resume_s1_stop_before_the_test_commit(
        self, tmp_path, monkeypatch
    ):
        """S1: stopped after the test writer returned, before its tests were
        committed. Resume runs the test writer again, so its report is there
        and `red_expected` equals the unstopped run's."""
        report = _rx_report("tests/test_new.py::test_add — no add()")
        u_config, unstopped = await self._unstopped(
            tmp_path / "unstopped", monkeypatch, report
        )

        config, state, progress, block = self._project(
            tmp_path / "stopped", monkeypatch
        )
        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                side_effect=self._writer(config.project_dir, report),
            ),
            patch(
                "build_pipeline.tdd_engine._git_commit_block_phase", side_effect=_RxStop
            ),
        ):
            with pytest.raises(_RxStop):
                await run_block_tdd(block, config, state, progress)
        state, block = self._reload(config)
        assert not block.test_commit and not block.red_expected

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                side_effect=self._writer(config.project_dir, report),
            ) as tw,
            patch("build_pipeline.tdd_engine.run_test_suite", return_value=RX_RED),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=RX_OK,
            ),
        ):
            assert await run_block_tdd(block, config, state, progress) is True
        assert tw.call_count == 1
        assert block.red_expected == unstopped.red_expected
        self._same_apart_from_red_expected(block, config, unstopped, u_config)

    @pytest.mark.asyncio
    async def test_red_expected_resume_s2_stop_after_red_gate_before_the_checkpoint(
        self, tmp_path, monkeypatch
    ):
        """S2: stopped after red_gate passed, before the checkpoint that saves
        red_evidence. Neither red_evidence nor red_expected is on disk; resume
        runs the RED gate again with no report: `not stated`."""
        report = _rx_report("tests/test_new.py::test_add — no add()")
        u_config, unstopped = await self._unstopped(
            tmp_path / "unstopped", monkeypatch, report
        )

        config, state, progress, block = self._project(
            tmp_path / "stopped", monkeypatch
        )
        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                side_effect=self._writer(config.project_dir, report),
            ),
            patch("build_pipeline.tdd_engine.run_test_suite", return_value=RX_RED),
            patch(
                "build_pipeline.tdd_engine._snapshot_test_files", side_effect=_RxStop
            ),
        ):
            with pytest.raises(_RxStop):
                await run_block_tdd(block, config, state, progress)
        assert block.red_expected and block.red_evidence, "the gate passed in memory"
        state, block = self._reload(config)
        assert block.test_commit and not block.red_evidence and not block.red_expected

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=RX_OK,
            ) as tw,
            patch("build_pipeline.tdd_engine.run_test_suite", return_value=RX_RED),
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=RX_OK,
            ),
        ):
            assert await run_block_tdd(block, config, state, progress) is True
        tw.assert_not_awaited()
        assert block.red_expected == "RED_EXPECTED result: not stated"
        self._same_apart_from_red_expected(block, config, unstopped, u_config)

    @pytest.mark.asyncio
    async def test_red_expected_resume_s3_stop_after_the_checkpoint(
        self, tmp_path, monkeypatch
    ):
        """S3: stopped after the checkpoint. The block skips to the
        implementer and keeps `red_expected`."""
        from build_pipeline.gates import red_gate

        report = _rx_report("tests/test_new.py::test_add — no add()")
        u_config, unstopped = await self._unstopped(
            tmp_path / "unstopped", monkeypatch, report
        )

        config, state, progress, block = self._project(
            tmp_path / "stopped", monkeypatch
        )
        with pytest.raises(_RxStop):
            await self._run(config, state, progress, block, report, implementer=_RxStop)
        state, block = self._reload(config)
        assert block.red_expected == unstopped.red_expected
        assert block.red_evidence.startswith(block.red_expected + "\n")

        with (
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=RX_OK,
            ) as tw,
            patch("build_pipeline.tdd_engine.run_test_suite", return_value=RX_RED),
            patch("build_pipeline.tdd_engine.red_gate", wraps=red_gate) as gate,
            patch(
                "build_pipeline.tdd_engine.run_implementer",
                new_callable=AsyncMock,
                return_value=RX_OK,
            ),
        ):
            assert await run_block_tdd(block, config, state, progress) is True
        tw.assert_not_awaited()
        gate.assert_not_called()
        assert block.red_expected == unstopped.red_expected
        assert block.red_evidence == unstopped.red_evidence
        self._same_apart_from_red_expected(block, config, unstopped, u_config)
