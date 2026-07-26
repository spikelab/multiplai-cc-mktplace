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
        assert names.index("design_audit") < names.index("prototype")
        assert names.index("prototype") < names.index("review")
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


class TestPrototypePhase:
    """Done-means #5 (orchestrator half): --no-prototype skips the phase and
    logs the reason; an applicable change runs it."""

    def _args(self, tmp_path, change):
        import argparse
        return argparse.Namespace(
            mode="only",
            change=change,
            project_dir=str(tmp_path),
            auto=True,
            spec_only=True,
            skip_research=True,
            interview_summary="",
            research_path="",
            context_files=[],
            session_id="",
        )

    def _config(self, tmp_path, change, prototype_mode, proposal):
        config = BuildConfig(
            project_dir=tmp_path, change_name=change, mode="only",
            auto=True, spec_only=True, skip_research=True,
        )
        config.specs_dir = tmp_path / "specs"
        config.prototype_mode = prototype_mode
        cm = ChangeManager(config.specs_dir)
        cm.init_specs()
        cm.create_change(change)
        (config.change_dir / "proposal.md").write_text(proposal)
        (config.change_dir / "design.md").write_text("## Decisions\nNone.")
        state = BuildState(
            change_name=change, mode="only", tier="advanced",
            phase=BuildPhase.DESIGN_AUDIT,
            state_file=str(config.state_file_path()),
        )
        state.checkpoint(config.state_file_path())
        return config

    FRONTEND_PROPOSAL = (
        "## Why\nUsers need a settings page.\n\n"
        "## What Changes\nA React component renders the form in the browser "
        "with CSS; the UI adds a button and a page layout in the DOM.\n"
    )
    BACKEND_PROPOSAL = (
        "## Why\nThe queue drops jobs.\n\n"
        "## What Changes\nAdd a retry column to the jobs database table, a "
        "migration, and a celery worker calling the internal API endpoint.\n"
    )

    @pytest.mark.asyncio
    async def test_no_prototype_skips_the_phase_and_logs_the_reason(
        self, tmp_path, caplog, monkeypatch,
    ):
        from unittest.mock import AsyncMock, patch
        from build_pipeline.orchestrator import run_orchestrator

        config = self._config(tmp_path, "skip-proto", "false", self.FRONTEND_PROPOSAL)
        mock_prototype = AsyncMock()
        with caplog.at_level("INFO"), \
                patch("build_pipeline.llm_steps.spec_steps.run_design_audit",
                      AsyncMock(return_value=[])), \
                patch("build_pipeline.llm_steps.prototype_steps.run_prototype",
                      mock_prototype):
            result = await run_orchestrator(config, self._args(tmp_path, "skip-proto"))

        assert result == 0
        mock_prototype.assert_not_awaited()
        assert not config.prototype_dir.exists()
        skips = [r.getMessage() for r in caplog.records
                 if "SKIP phase=PROTOTYPE" in r.getMessage()]
        assert skips, "the skip must be logged with its reason"
        assert "--no-prototype" in skips[0]

    @pytest.mark.asyncio
    async def test_applicable_change_runs_the_prototype_and_feedback(self, tmp_path):
        from unittest.mock import AsyncMock, patch
        from build_pipeline.models import GateResult
        from build_pipeline.orchestrator import run_orchestrator

        config = self._config(tmp_path, "run-proto", "auto", self.FRONTEND_PROPOSAL)
        mock_prototype = AsyncMock(return_value=GateResult(passed=True, reason="ok"))
        mock_feedback = AsyncMock(return_value=2)
        with patch("build_pipeline.llm_steps.spec_steps.run_design_audit",
                   AsyncMock(return_value=[])), \
                patch("build_pipeline.llm_steps.prototype_steps.run_prototype",
                      mock_prototype), \
                patch("build_pipeline.llm_steps.prototype_steps.apply_prototype_findings",
                      mock_feedback):
            result = await run_orchestrator(config, self._args(tmp_path, "run-proto"))

        assert result == 0
        assert mock_prototype.await_count == 1
        assert mock_feedback.await_count == 1

    @pytest.mark.asyncio
    async def test_auto_mode_skips_plain_backend_change(self, tmp_path):
        from unittest.mock import AsyncMock, patch
        from build_pipeline.orchestrator import run_orchestrator

        config = self._config(tmp_path, "auto-skip", "auto", self.BACKEND_PROPOSAL)
        mock_prototype = AsyncMock()
        with patch("build_pipeline.llm_steps.spec_steps.run_design_audit",
                   AsyncMock(return_value=[])), \
                patch("build_pipeline.llm_steps.prototype_steps.run_prototype",
                      mock_prototype):
            result = await run_orchestrator(config, self._args(tmp_path, "auto-skip"))

        assert result == 0
        mock_prototype.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prototype_failure_does_not_fail_the_build(self, tmp_path):
        from unittest.mock import AsyncMock, patch
        from build_pipeline.models import GateResult
        from build_pipeline.orchestrator import run_orchestrator

        config = self._config(tmp_path, "fail-proto", "true", self.FRONTEND_PROPOSAL)
        with patch("build_pipeline.llm_steps.spec_steps.run_design_audit",
                   AsyncMock(return_value=[])), \
                patch("build_pipeline.llm_steps.prototype_steps.run_prototype",
                      AsyncMock(return_value=GateResult(
                          passed=False, reason="no artifact file"))):
            result = await run_orchestrator(config, self._args(tmp_path, "fail-proto"))

        assert result == 0
        progress = config.progress_file_path()
        assert progress.exists()
        assert "no artifact file" in progress.read_text()

    @pytest.mark.asyncio
    async def test_review_checkpoint_prints_file_url(self, tmp_path, capsys):
        """Done-means 2.6: the container cannot serve the user's localhost, so
        the review checkpoint hands over a file:// path on the shared mount."""
        from unittest.mock import AsyncMock, patch
        from build_pipeline.models import GateResult
        from build_pipeline.orchestrator import run_orchestrator

        config = self._config(tmp_path, "url-proto", "true", self.FRONTEND_PROPOSAL)
        config.auto = False
        config.spec_only = False
        args = self._args(tmp_path, "url-proto")
        args.auto = False
        args.spec_only = False

        async def _write_prototype(cfg):
            cfg.prototype_dir.mkdir(parents=True, exist_ok=True)
            (cfg.prototype_dir / "mockup.html").write_text("<html></html>")
            (cfg.prototype_dir / "NOTES.md").write_text("PROVES: x\n")
            return GateResult(passed=True, reason="ok")

        with patch("build_pipeline.llm_steps.spec_steps.run_design_audit",
                   AsyncMock(return_value=[])), \
                patch("build_pipeline.llm_steps.prototype_steps.run_prototype",
                      AsyncMock(side_effect=_write_prototype)), \
                patch("build_pipeline.llm_steps.prototype_steps.apply_prototype_findings",
                      AsyncMock(return_value=0)), \
                patch("build_pipeline.tdd_engine.run_tdd_engine",
                      AsyncMock(return_value=0)):
            result = await run_orchestrator(config, args)

        assert result == 0
        out = capsys.readouterr().out
        assert f"PROTOTYPE:file://{config.prototype_dir.resolve()}/mockup.html" in out
        assert "PROTOTYPE_NOTES:file://" in out


class TestPrototypeDecision:
    def test_flag_off_wins(self, tmp_path):
        from build_pipeline.orchestrator import prototype_decision
        config = BuildConfig(project_dir=tmp_path, change_name="c")
        config.specs_dir = tmp_path / "specs"
        config.prototype_mode = "false"
        should, reason = prototype_decision(config)
        assert should is False
        assert "--no-prototype" in reason

    def test_flag_on_wins_for_backend_change(self, tmp_path):
        from build_pipeline.orchestrator import prototype_decision
        config = BuildConfig(project_dir=tmp_path, change_name="c")
        config.specs_dir = tmp_path / "specs"
        config.prototype_mode = "true"
        should, reason = prototype_decision(config)
        assert should is True
        assert "forced on" in reason

    def test_auto_defers_to_the_applicability_rule(self, tmp_path):
        from build_pipeline.orchestrator import prototype_decision
        config = BuildConfig(project_dir=tmp_path, change_name="c")
        config.specs_dir = tmp_path / "specs"
        config.prototype_mode = "auto"
        config.change_dir.mkdir(parents=True, exist_ok=True)
        (config.change_dir / "proposal.md").write_text(
            "A React UI component with CSS in the browser DOM page layout."
        )
        should, reason = prototype_decision(config)
        assert should is True
        assert reason.startswith("auto:")
