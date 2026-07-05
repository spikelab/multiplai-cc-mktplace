"""Tests for the build orchestrator — mode detection, phase sequencing."""

import pytest

from build_pipeline.orchestrator import _run_bootstrap
from build_pipeline.config import BuildConfig
from build_pipeline.state import BuildState
from build_pipeline.models import BuildPhase
from build_pipeline.change_manager import ChangeManager


class TestPhaseOrdering:
    def test_phase_enum_order(self):
        """Verify phases are ordered correctly for is_phase_complete checks."""
        phases = list(BuildPhase)
        names = [p.value for p in phases]
        assert names.index("init") < names.index("bootstrap")
        assert names.index("bootstrap") < names.index("research")
        assert names.index("research") < names.index("spec_generation")
        assert names.index("spec_generation") < names.index("design_audit")
        assert names.index("design_audit") < names.index("review")
        assert names.index("review") < names.index("tdd_build")
        assert names.index("tdd_build") < names.index("complete")


class TestBootstrap:
    @pytest.mark.asyncio
    async def test_creates_git_and_specs(self, tmp_path):
        config = BuildConfig(
            project_dir=tmp_path,
            change_name="test-change",
        )
        config.specs_dir = tmp_path / "specs"
        state = BuildState(
            change_name="test-change", mode="scratch", tier="advanced",
            state_file=str(tmp_path / "state.json"),
        )
        cm = ChangeManager(config.specs_dir)
        state_path = tmp_path / "state.json"

        await _run_bootstrap(config, state, cm, state_path)

        assert (tmp_path / ".git").exists()
        assert (tmp_path / "specs" / "changes").exists()
        assert (tmp_path / "specs" / "changes" / "test-change").exists()
        assert state.bootstrap_done
        assert state.phase == BuildPhase.BOOTSTRAP


class TestModeDetection:
    def test_scratch_mode(self):
        config = BuildConfig(mode="scratch")
        assert config.mode == "scratch"

    def test_brief_mode(self):
        config = BuildConfig(mode="brief")
        assert config.mode == "brief"

    def test_only_mode(self):
        config = BuildConfig(mode="only")
        assert config.mode == "only"


class TestArchivePhase:
    """The orchestrator should archive the change at the end when --auto,
    and leave it in place (awaiting manual archive) otherwise."""

    @pytest.mark.asyncio
    async def test_auto_mode_archives_change(self, tmp_path):
        """With --auto, the change directory is moved to archive/ at the end."""
        import argparse
        from build_pipeline.orchestrator import run_orchestrator

        # Pre-build a minimal completed state so the orchestrator skips straight to archive
        config = BuildConfig(
            project_dir=tmp_path,
            change_name="test-archive",
            auto=True,
        )
        config.specs_dir = tmp_path / "specs"
        cm = ChangeManager(config.specs_dir)
        cm.init_specs()
        cm.create_change("test-archive")

        # Write minimal artifacts so the change looks complete
        change_dir = config.change_dir
        (change_dir / "proposal.md").write_text("## Why\ntest")

        # Create a state file showing TDD_BUILD complete
        state = BuildState(
            change_name="test-archive",
            mode="scratch",
            tier="advanced",
            phase=BuildPhase.COMPLETE,
            state_file=str(config.state_file_path()),
        )
        state.checkpoint(config.state_file_path())

        # Stub out phases that would otherwise make LLM calls — state already
        # marks them complete via is_phase_complete, so we only need the
        # orchestrator to reach the final archive block.
        args = argparse.Namespace(
            mode="only",
            change="test-archive",
            project_dir=str(tmp_path),
            auto=True,
            spec_only=False,
            skip_research=True,
            interview_summary="",
            research_path="",
            context_files=[],
            session_id="",
        )

        result = await run_orchestrator(config, args)

        assert result == 0
        # Change directory should be moved to archive
        assert not change_dir.exists()
        archive_root = config.specs_dir / "archive"
        archived_dirs = list(archive_root.glob("*-test-archive"))
        assert len(archived_dirs) == 1, f"Expected 1 archive entry, got {archived_dirs}"

    @pytest.mark.asyncio
    async def test_non_auto_mode_leaves_change_in_place(self, tmp_path):
        """Without --auto, the change stays in changes/ for manual archive."""
        import argparse
        from build_pipeline.orchestrator import run_orchestrator

        config = BuildConfig(
            project_dir=tmp_path,
            change_name="test-manual",
            auto=False,
        )
        config.specs_dir = tmp_path / "specs"
        cm = ChangeManager(config.specs_dir)
        cm.init_specs()
        cm.create_change("test-manual")

        change_dir = config.change_dir
        (change_dir / "proposal.md").write_text("## Why\ntest")

        state = BuildState(
            change_name="test-manual",
            mode="scratch",
            tier="advanced",
            phase=BuildPhase.COMPLETE,
            state_file=str(config.state_file_path()),
        )
        state.checkpoint(config.state_file_path())

        args = argparse.Namespace(
            mode="only",
            change="test-manual",
            project_dir=str(tmp_path),
            auto=False,
            spec_only=False,
            skip_research=True,
            interview_summary="",
            research_path="",
            context_files=[],
            session_id="",
        )

        result = await run_orchestrator(config, args)

        assert result == 0
        # Change should still be in place
        assert change_dir.exists()
        assert (change_dir / "proposal.md").exists()
