"""Stop and resume rows S1-S12 of plan-buildme-test-writer-turns-2026-10-17.md:
a test writer that runs out of turns keeps its tests, a retry is told about its
drafts, a resumed block skips the work it already finished, a capped mutation
round keeps tests that pass its checks, and a resume skips the interview.

Agents are faked; git and the files are real.
"""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from build_pipeline.config import BuildConfig
from build_pipeline.models import BlockInfo, BlockStatus
from build_pipeline.progress import ProgressWriter
from build_pipeline.sdk import AgentResult
from build_pipeline.state import BuildState, TDDState
from build_pipeline.tdd_engine import run_block_tdd

# What agent_call returns when the Claude CLI stops the agent at its turn cap.
TURN_LIMIT = AgentResult(success=False, output="Let me check one more file",
                         error="Reached maximum number of turns (30)\nCLI stderr:\n")
OK = AgentResult(success=True, output="STATUS: DONE")
RED = (1, "FAILED tests/test_new.py::test_add - AssertionError: not implemented")
NEW_TEST = "def test_add():\n    from module import add\n    assert add(2, 3) == 5\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def _agents():
    """No real refactorer, and the test-quality auditor accepts the tests."""
    from build_pipeline.models import TestQualityAudit
    with patch("build_pipeline.tdd_engine.run_refactorer", new_callable=AsyncMock, return_value=OK), \
         patch("build_pipeline.tdd_engine._audit_test_quality", new_callable=AsyncMock,
               return_value=TestQualityAudit(passed=True)):
        yield


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    project = tmp_path / "project"
    (project / "specs" / "changes" / "test").mkdir(parents=True)
    (project / "specs" / "changes" / "test" / "design.md").write_text("# Design")
    (project / "module.py").write_text("def add(a, b):\n    raise NotImplementedError\n")
    (project / "tests").mkdir()
    (project / "tests" / "test_old.py").write_text("def test_old():\n    assert 1 == 1\n")
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.email", "t@example.com")
    _git(project, "config", "user.name", "t")
    _git(project, "config", "commit.gpgsign", "false")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")

    config = BuildConfig(project_dir=project, change_name="test", tier="advanced", test_command="pytest",
                         config_dir=tmp_path / "config", core_memory_files=[], stack_memory_files=[],
                         additional_memory_files=[])
    config.specs_dir = project / "specs"
    block = BlockInfo(number=1, name="Add", description="Add two numbers.")
    state = BuildState(change_name="test", mode="only", tier="advanced",
                       state_file=str(tmp_path / "state.json"), tdd=TDDState(blocks=[block]))
    progress = ProgressWriter(tmp_path / "progress.md")
    progress.initialize("test", "only", "advanced", 1)
    return config, state, progress, block


def _writes(project: Path, files: dict[str, str], result: AgentResult):
    async def writer(**_kw):
        for rel, text in files.items():
            (project / rel).write_text(text)
        return result
    return writer


class TestTurnLimit:
    @pytest.mark.asyncio
    async def test_s1_turn_limit_with_test_files_goes_on_to_the_gates(self, setup, tmp_path):
        config, state, progress, block = setup
        project = config.project_dir
        writer = _writes(project, {"tests/test_new.py": NEW_TEST}, TURN_LIMIT)
        with patch("build_pipeline.tdd_engine.run_test_writer", side_effect=writer), \
             patch("build_pipeline.tdd_engine.run_test_suite", return_value=RED) as suite, \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock,
                   return_value=OK) as implementer:
            assert await run_block_tdd(block, config, state, progress) is True

        assert suite.called, "the RED gate ran"
        implementer.assert_awaited_once()
        assert block.test_commit and "tests/test_new.py" in _git(project, "show", "--name-only", block.test_commit)
        assert block.red_evidence and "tests/test_new.py" in block.test_file_hashes
        assert [n.role for n in block.notes] == ["test_writer"]
        assert "did not report" in block.notes[0].surprises
        log = (tmp_path / "progress.md").read_text()
        assert "ran out of turns" in log
        assert "the turn cap" in implementer.await_args.kwargs["failing_tests"]

    @pytest.mark.asyncio
    async def test_s1_a_changed_tracked_test_file_counts(self, setup):
        config, state, progress, block = setup
        writer = _writes(config.project_dir, {"tests/test_old.py": NEW_TEST}, TURN_LIMIT)
        with patch("build_pipeline.tdd_engine.run_test_writer", side_effect=writer), \
             patch("build_pipeline.tdd_engine.run_test_suite", return_value=RED), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=OK):
            assert await run_block_tdd(block, config, state, progress) is True
        assert block.test_commit

    @pytest.mark.asyncio
    async def test_s2_turn_limit_with_no_test_file_fails_the_block(self, setup):
        config, state, progress, block = setup
        writer = _writes(config.project_dir, {"module.py": "x = 2\n"}, TURN_LIMIT)
        with patch("build_pipeline.tdd_engine.run_test_writer", side_effect=writer), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock) as implementer:
            assert await run_block_tdd(block, config, state, progress) is False
        implementer.assert_not_awaited()
        assert block.status == BlockStatus.FAILED and block.resumable is True
        assert block.test_commit is None

    @pytest.mark.asyncio
    async def test_any_other_failure_with_test_files_still_fails_the_block(self, setup):
        config, state, progress, block = setup
        crashed = AgentResult(success=False, error="CLI exited 1")
        writer = _writes(config.project_dir, {"tests/test_new.py": NEW_TEST}, crashed)
        with patch("build_pipeline.tdd_engine.run_test_writer", side_effect=writer), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock) as implementer:
            assert await run_block_tdd(block, config, state, progress) is False
        implementer.assert_not_awaited()
        assert block.status == BlockStatus.FAILED


def _reload(config, state):
    """The state a resumed run reads back from the checkpoint."""
    state = BuildState.load(config.state_file_path())
    return state, state.tdd.blocks[state.tdd.current_block]


class TestDrafts:
    @pytest.mark.asyncio
    async def test_s3_a_killed_test_writer_s_files_are_named_to_the_retry_as_drafts(self, setup):
        config, state, progress, block = setup
        timed_out = AgentResult(success=False, error="Agent timed out after 1200s", timed_out=True)
        writer = _writes(config.project_dir, {"tests/test_new.py": NEW_TEST}, timed_out)
        with patch("build_pipeline.tdd_engine.run_test_writer", side_effect=writer):
            assert await run_block_tdd(block, config, state, progress) is False
        assert block.status == BlockStatus.FAILED and block.test_commit is None

        state, block = _reload(config, state)
        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=OK) as tw, \
             patch("build_pipeline.tdd_engine.run_test_suite", return_value=RED), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=OK):
            assert await run_block_tdd(block, config, state, progress) is True
        description = tw.await_args_list[0].kwargs["block_description"]
        drafts = description.split("## Drafts from an earlier attempt", 1)[1]
        assert "They are your own drafts" in drafts
        assert "`tests/test_new.py` (new file, 3 lines)" in drafts

    @pytest.mark.asyncio
    async def test_a_changed_tracked_test_file_is_listed_with_its_line_counts(self, setup):
        config, state, progress, block = setup
        (config.project_dir / "tests" / "test_old.py").write_text(NEW_TEST)
        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock,
                   return_value=AgentResult(success=False, error="crash")) as tw:
            await run_block_tdd(block, config, state, progress)
        assert "`tests/test_old.py` (+3 -2)" in tw.await_args.kwargs["block_description"]

    @pytest.mark.asyncio
    async def test_no_drafts_section_without_uncommitted_test_files(self, setup):
        config, state, progress, block = setup
        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock,
                   return_value=AgentResult(success=False, error="crash")) as tw:
            await run_block_tdd(block, config, state, progress)
        assert tw.await_args.kwargs["block_description"] == block.description


class Stop(Exception):
    """Stands in for the process being killed at that point."""


def _skipped_gates():
    return (patch("build_pipeline.tdd_engine._enforce_test_quality", new_callable=AsyncMock, return_value=True),
            patch("build_pipeline.tdd_engine._enforce_red_gate", new_callable=AsyncMock, return_value=True))


async def _stop_after_red(config, state, progress, block):
    """Run the block until its RED gate passed, then stop as the implementer starts."""
    writer = _writes(config.project_dir, {"tests/test_new.py": NEW_TEST}, OK)
    with patch("build_pipeline.tdd_engine.run_test_writer", side_effect=writer), \
         patch("build_pipeline.tdd_engine.run_test_suite", return_value=RED), \
         patch("build_pipeline.tdd_engine.run_implementer", side_effect=Stop):
        with pytest.raises(Stop):
            await run_block_tdd(block, config, state, progress)
    return _reload(config, state)


class TestResume:
    @pytest.mark.asyncio
    async def test_s4_tests_committed_before_the_quality_gate_skip_only_the_test_writer(self, setup, caplog):
        caplog.set_level("INFO", logger="build_pipeline.tdd_engine")
        config, state, progress, block = setup
        writer = _writes(config.project_dir, {"tests/test_new.py": NEW_TEST}, OK)
        with patch("build_pipeline.tdd_engine.run_test_writer", side_effect=writer), \
             patch("build_pipeline.tdd_engine._enforce_test_quality", side_effect=Stop):
            with pytest.raises(Stop):
                await run_block_tdd(block, config, state, progress)
        state, block = _reload(config, state)
        assert block.test_commit and not block.red_evidence

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=OK) as tw, \
             patch("build_pipeline.tdd_engine.run_test_suite", return_value=RED) as suite, \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=OK) as impl:
            assert await run_block_tdd(block, config, state, progress) is True
        tw.assert_not_awaited()
        assert suite.called and block.red_evidence
        assert "tests/test_new.py" in block.test_file_hashes
        impl.assert_awaited_once()
        assert "SKIP block=1 phase=TEST_WRITE reason=tests-committed" in caplog.text

    @pytest.mark.asyncio
    async def test_s5_resume_after_red_skips_test_writer(self, setup, caplog):
        caplog.set_level("INFO", logger="build_pipeline.tdd_engine")
        config, state, progress, block = setup
        state, block = await _stop_after_red(config, state, progress, block)
        assert block.test_commit and block.red_evidence and block.test_file_hashes
        quality, red = _skipped_gates()
        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=OK) as tw, \
             quality as q, red as r, \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=OK) as impl:
            assert await run_block_tdd(block, config, state, progress) is True
        tw.assert_not_awaited()
        q.assert_not_awaited()
        r.assert_not_awaited()
        assert "AssertionError: not implemented" in impl.await_args.kwargs["failing_tests"]
        assert "SKIP block=1 phase=TEST_WRITE reason=tests-committed" in caplog.text

    @pytest.mark.asyncio
    async def test_s6_a_failed_implementer_is_run_again_without_the_test_writer(self, setup):
        config, state, progress, block = setup
        writer = _writes(config.project_dir, {"tests/test_new.py": NEW_TEST}, OK)
        broke = _writes(config.project_dir, {"module.py": "def add(a, b):\n    return 0\n"},
                        AgentResult(success=False, error="CLI exited 1"))
        with patch("build_pipeline.tdd_engine.run_test_writer", side_effect=writer), \
             patch("build_pipeline.tdd_engine.run_test_suite", return_value=RED), \
             patch("build_pipeline.tdd_engine.run_implementer", side_effect=broke):
            assert await run_block_tdd(block, config, state, progress) is False
        state, block = _reload(config, state)
        assert block.status == BlockStatus.FAILED

        quality, red = _skipped_gates()
        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=OK) as tw, \
             quality as q, red as r, \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=OK) as impl:
            assert await run_block_tdd(block, config, state, progress) is True
        tw.assert_not_awaited()
        q.assert_not_awaited()
        r.assert_not_awaited()
        impl.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_s7_a_test_file_edited_before_the_resume_re_runs_the_gates(self, setup):
        config, state, progress, block = setup
        state, block = await _stop_after_red(config, state, progress, block)
        before = dict(block.test_file_hashes)
        with (config.project_dir / "tests" / "test_new.py").open("a") as f:
            f.write("\n\ndef test_add_zero():\n    from module import add\n    assert add(0, 0) == 0\n")

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=OK) as tw, \
             patch("build_pipeline.tdd_engine.run_test_suite", return_value=RED) as suite, \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=OK):
            assert await run_block_tdd(block, config, state, progress) is True
        tw.assert_not_awaited()
        assert suite.called
        assert block.test_file_hashes != before

    @pytest.mark.asyncio
    async def test_s8_a_test_commit_no_longer_on_head_runs_the_test_writer_with_drafts(self, setup):
        config, state, progress, block = setup
        state, block = await _stop_after_red(config, state, progress, block)
        _git(config.project_dir, "reset", "-q", "--mixed", block.baseline_commit)

        with patch("build_pipeline.tdd_engine.run_test_writer", new_callable=AsyncMock, return_value=OK) as tw, \
             patch("build_pipeline.tdd_engine.run_test_suite", return_value=RED), \
             patch("build_pipeline.tdd_engine.run_implementer", new_callable=AsyncMock, return_value=OK):
            assert await run_block_tdd(block, config, state, progress) is True
        tw.assert_awaited()
        assert "`tests/test_new.py` (new file" in tw.await_args_list[0].kwargs["block_description"]
