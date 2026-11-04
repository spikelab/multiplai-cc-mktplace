"""A block's reviewer judges only that block's work, and a RED gate that a
block's new tests pass names the earlier commits that already built it.

The case behind these tests: block 2's reviewer read a Global Constraint that
block 3 delivers, reported it as Missing, and block 2's fixer built block 3.
Block 3's new tests then passed before its implementation, and the RED gate
retried the test writer on every run.
"""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from build_pipeline.config import BuildConfig
from build_pipeline.gates import GateResult
from build_pipeline.models import AgentResult, BlockInfo, ReviewResult, ReviewScore
from build_pipeline.progress import ProgressWriter
from build_pipeline.prompts.review import CODE_REVIEW_PROMPT, COMPONENT_REVIEW_PROMPT
from build_pipeline.state import BuildState, TDDState
from build_pipeline.tdd_engine import (
    _built_by_earlier_blocks,
    _enforce_red_gate,
    _run_quality_review,
)

SPECS = """# Capability: billing

### Requirement: Profiles belong to the account

#### Scenario: New profile is linked
WHEN a profile is created THEN it has the account

### Requirement: Legacy profiles are refused

#### Scenario: Assigning a legacy-only profile is refused
WHEN a legacy-only profile is assigned THEN the answer is 400
"""

TASKS = """# Tasks

## 1. Profiles belong to the account

Link each profile to its account in `app/models.py`.

Satisfies: billing (Profiles belong to the account: "New profile is linked", "Profile without account is rejected")

## 2. Legacy profiles are refused

`app/views.py` refuses a legacy-only profile with 400.

Satisfies: billing (Legacy profiles are refused: "Assigning a legacy-only profile is refused")
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def project(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    change = repo / "specs" / "changes" / "feat"
    change.mkdir(parents=True)
    (change / "specs.md").write_text(SPECS)
    (change / "tasks.md").write_text(TASKS)
    (change / "rubric.md").write_text("# Rubric\nQuality criteria.")
    (change / "implementation-plan.md").write_text(
        "# Plan\n\n## Global Constraints\n"
        "- A legacy-only assignment is refused with HTTP status 400.\n"
    )
    config = BuildConfig(project_dir=repo, change_name="feat", tier="advanced")
    config.specs_dir = repo / "specs"
    return config


async def _spec_context(config: BuildConfig, block: BlockInfo) -> str:
    review = ReviewResult(
        scores=[ReviewScore(dimension="Q", weight=2, score=4, evidence="e")]
    )
    with patch(
        "build_pipeline.tdd_engine.run_code_review",
        new_callable=AsyncMock,
        return_value=review,
    ) as mock_review:
        await _run_quality_review(block, config)
    return mock_review.call_args.kwargs["spec_context"]


def _block(config: BuildConfig, number: int, baseline: str | None = None) -> BlockInfo:
    from build_pipeline.tdd_engine import parse_blocks

    block = parse_blocks(config.tasks_path)[number - 1]
    block.baseline_commit = baseline
    return block


class TestReviewerScope:
    @pytest.mark.asyncio
    async def test_reviewer_gets_only_the_requirement_its_satisfies_line_names(
        self, project
    ):
        """A Satisfies line with the requirement and scenarios in parentheses
        still reaches the reviewer, cut to the requirement it names."""
        _commit(project.project_dir, {"app/models.py": "x = 1\n"}, "init")
        spec_context = await _spec_context(project, _block(project, 1))

        assert "WHEN a profile is created THEN it has the account" in spec_context
        assert "WHEN a legacy-only profile is assigned" not in spec_context

    @pytest.mark.asyncio
    async def test_reviewer_is_shown_the_blocks_after_this_one(self, project):
        _commit(project.project_dir, {"app/models.py": "x = 1\n"}, "init")
        first = await _spec_context(project, _block(project, 1))
        last = await _spec_context(project, _block(project, 2))

        assert "### Later blocks" in first
        assert "#### Block 2: Legacy profiles are refused" in first
        assert "`app/views.py` refuses a legacy-only profile with 400." in first
        assert "### Later blocks" not in last

    def test_only_the_block_prompt_defines_missing_by_the_later_blocks(self):
        assert "a block listed under Later blocks" in CODE_REVIEW_PROMPT
        assert "a block listed under Later blocks" not in COMPONENT_REVIEW_PROMPT
        assert (
            "spec behavior with no implementation in the diff"
            in COMPONENT_REVIEW_PROMPT
        )


class TestBuiltByEarlierBlocks:
    def _state(self, blocks: list[BlockInfo], tmp_path: Path) -> BuildState:
        return BuildState(
            change_name="feat",
            tier="advanced",
            state_file=str(tmp_path / "state.json"),
            tdd=TDDState(blocks=blocks),
        )

    def test_names_the_earlier_commit_that_changed_a_file_the_task_names(
        self, project, tmp_path
    ):
        repo = project.project_dir
        start = _commit(repo, {"app/models.py": "x = 1\n"}, "init")
        fix = _commit(
            repo,
            {"app/views.py": "refuse = 400\n", "tests/test_views.py": "t = 1\n"},
            "fix(block-1): Profiles belong to the account",
        )
        first, second = _block(project, 1, start), _block(project, 2, fix)

        note = _built_by_earlier_blocks(
            project, self._state([first, second], tmp_path), second
        )

        assert "app/views.py" in note
        assert f"{fix[:7]} fix(block-1): Profiles belong to the account" in note
        assert "tests/test_views.py" not in note

    def test_says_nothing_when_earlier_blocks_left_the_tasks_files_alone(
        self, project, tmp_path
    ):
        repo = project.project_dir
        start = _commit(repo, {"app/models.py": "x = 1\n"}, "init")
        end = _commit(repo, {"app/models.py": "x = 2\n"}, "impl(block-1): link")
        first, second = _block(project, 1, start), _block(project, 2, end)

        assert (
            _built_by_earlier_blocks(
                project, self._state([first, second], tmp_path), second
            )
            == ""
        )

    def test_the_first_block_has_no_earlier_blocks(self, project, tmp_path):
        start = _commit(project.project_dir, {"app/models.py": "x = 1\n"}, "init")
        first = _block(project, 1, start)

        assert (
            _built_by_earlier_blocks(project, self._state([first], tmp_path), first)
            == ""
        )

    @pytest.mark.asyncio
    async def test_red_gate_failure_names_the_earlier_commit(self, project, tmp_path):
        repo = project.project_dir
        project.test_command = "pytest -q"
        start = _commit(repo, {"app/models.py": "x = 1\n"}, "init")
        fix = _commit(repo, {"app/views.py": "refuse = 400\n"}, "fix(block-1): link")
        first, second = _block(project, 1, start), _block(project, 2, fix)
        state = self._state([first, second], tmp_path)
        progress = ProgressWriter(tmp_path / "progress.md")
        progress.initialize("feat", "advanced", 2)
        passed = GateResult(
            passed=False,
            reason="RED gate failed: suite passed BEFORE implementation",
            action="rewrite_tests",
        )

        with (
            patch(
                "build_pipeline.tdd_engine._red_run",
                return_value=(passed, 0, "1 passed", "pytest -q", "pytest"),
            ),
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=AgentResult(success=True, output=""),
            ),
            patch(
                "build_pipeline.tdd_engine._git_commit_block_phase", return_value=None
            ),
        ):
            ok = await _enforce_red_gate(second, project, state, progress, "", "", 1)

        assert ok is False
        log = (tmp_path / "progress.md").read_text()
        assert "FAILED after retry: RED gate failed" in log
        assert f"{fix[:7]} fix(block-1): link" in log
