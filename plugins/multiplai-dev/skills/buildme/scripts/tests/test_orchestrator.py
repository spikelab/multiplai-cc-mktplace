"""Tests for the build orchestrator — mode detection, phase sequencing."""

import argparse
import subprocess
from pathlib import Path

import pytest

from build_pipeline.orchestrator import (
    _pr_title_body,
    _run_bootstrap,
    _run_publish,
    run_orchestrator,
)
from build_pipeline.config import BuildConfig, GitToggles
from build_pipeline.progress import ProgressWriter
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
        # --no-worktree: the pre-git-lifecycle path, which `git init`s a
        # brand-new project directory.
        config = BuildConfig(
            project_dir=tmp_path,
            change_name="test-change",
            git=GitToggles(worktree=False),
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
    async def test_resume_after_feedback_checkpoint_skips_the_rerun(self, tmp_path):
        """A crash between the prototype_done checkpoint and
        advance_to(PROTOTYPE) must not re-run the expensive agent on resume —
        the artifact exists and its findings were already applied."""
        from unittest.mock import AsyncMock, patch
        from build_pipeline.orchestrator import run_orchestrator
        from build_pipeline.state import SpecGenState

        config = self._config(tmp_path, "resume-proto", "true", self.FRONTEND_PROPOSAL)
        state = BuildState.load(config.state_file_path())
        state.spec_gen = SpecGenState(prototype_done=True)
        state.checkpoint(config.state_file_path())

        mock_prototype = AsyncMock()
        mock_feedback = AsyncMock()
        with patch("build_pipeline.llm_steps.spec_steps.run_design_audit",
                   AsyncMock(return_value=[])), \
                patch("build_pipeline.llm_steps.prototype_steps.run_prototype",
                      mock_prototype), \
                patch("build_pipeline.llm_steps.prototype_steps.apply_prototype_findings",
                      mock_feedback):
            result = await run_orchestrator(config, self._args(tmp_path, "resume-proto"))

        assert result == 0
        mock_prototype.assert_not_awaited()
        mock_feedback.assert_not_awaited()

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


class TestRespecPhaseWiring:
    """The RESPEC phase runs after TDD_BUILD, is skipped on resume once done,
    and never fails the build."""

    def test_respec_sits_between_tdd_build_and_complete(self):
        names = [p.value for p in BuildPhase]
        assert names.index("tdd_build") < names.index("respec")
        assert names.index("respec") < names.index("complete")

    @pytest.mark.asyncio
    async def test_orchestrator_runs_respec_before_archiving(self, tmp_path):
        from unittest.mock import AsyncMock, patch
        from argparse import Namespace
        from build_pipeline.orchestrator import run_orchestrator

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        config = BuildConfig(
            project_dir=project_dir, change_name="feat", mode="only", auto=False,
            config_dir=tmp_path / "config",
        )
        config.specs_dir = project_dir / "specs"
        config.change_dir.mkdir(parents=True)

        with patch("build_pipeline.tdd_engine.run_tdd_engine",
                   new_callable=AsyncMock, return_value=0), \
             patch("build_pipeline.llm_steps.spec_steps.run_design_audit",
                   new_callable=AsyncMock, return_value=[]), \
             patch("build_pipeline.llm_steps.respec_steps.run_respec_audit",
                   new_callable=AsyncMock,
                   return_value=config.change_dir / "respec.md") as mock_respec:
            rc = await run_orchestrator(config, Namespace(interview_summary=""))

        assert rc == 0
        mock_respec.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_respec_failure_does_not_fail_the_build(self, tmp_path):
        from unittest.mock import AsyncMock, patch
        from argparse import Namespace
        from build_pipeline.orchestrator import run_orchestrator

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        config = BuildConfig(
            project_dir=project_dir, change_name="feat", mode="only", auto=False,
            config_dir=tmp_path / "config",
        )
        config.specs_dir = project_dir / "specs"
        config.change_dir.mkdir(parents=True)

        with patch("build_pipeline.tdd_engine.run_tdd_engine",
                   new_callable=AsyncMock, return_value=0), \
             patch("build_pipeline.llm_steps.spec_steps.run_design_audit",
                   new_callable=AsyncMock, return_value=[]), \
             patch("build_pipeline.llm_steps.respec_steps.run_respec_audit",
                   new_callable=AsyncMock, side_effect=RuntimeError("model down")):
            rc = await run_orchestrator(config, Namespace(interview_summary=""))

        assert rc == 0


class TestDocsUpdatePhaseWiring:
    """DOCS_UPDATE runs between TDD_BUILD and RESPEC, always on, and never
    fails the build."""

    def test_docs_update_sits_between_tdd_build_and_respec(self):
        names = [p.value for p in BuildPhase]
        assert names.index("tdd_build") < names.index("docs_update")
        assert names.index("docs_update") < names.index("respec")

    @staticmethod
    def _config(tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        config = BuildConfig(
            project_dir=project_dir, change_name="feat", mode="only", auto=False,
            config_dir=tmp_path / "config",
        )
        config.specs_dir = project_dir / "specs"
        config.change_dir.mkdir(parents=True)
        return config

    @pytest.mark.asyncio
    async def test_orchestrator_runs_docs_update_before_respec(self, tmp_path, capsys):
        from unittest.mock import AsyncMock, patch
        from argparse import Namespace
        from build_pipeline.models import GateResult
        from build_pipeline.orchestrator import run_orchestrator

        config = self._config(tmp_path)
        order: list[str] = []

        async def fake_docs(*a, **kw):
            order.append("docs")
            return ["README.md"], GateResult(passed=True, reason="ok")

        async def fake_respec(*a, **kw):
            order.append("respec")
            return config.change_dir / "respec.md"

        with patch("build_pipeline.tdd_engine.run_tdd_engine",
                   new_callable=AsyncMock, return_value=0), \
             patch("build_pipeline.llm_steps.spec_steps.run_design_audit",
                   new_callable=AsyncMock, return_value=[]), \
             patch("build_pipeline.llm_steps.docs_steps.run_docs_update",
                   side_effect=fake_docs), \
             patch("build_pipeline.llm_steps.respec_steps.run_respec_audit",
                   side_effect=fake_respec):
            rc = await run_orchestrator(config, Namespace(interview_summary=""))

        assert rc == 0
        assert order == ["docs", "respec"], "docs must run before the respec pass"
        assert "PHASE:DOCS_UPDATE:COMPLETE" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_docs_failure_does_not_fail_the_build(self, tmp_path):
        from unittest.mock import AsyncMock, patch
        from argparse import Namespace
        from build_pipeline.orchestrator import run_orchestrator

        config = self._config(tmp_path)

        with patch("build_pipeline.tdd_engine.run_tdd_engine",
                   new_callable=AsyncMock, return_value=0), \
             patch("build_pipeline.llm_steps.spec_steps.run_design_audit",
                   new_callable=AsyncMock, return_value=[]), \
             patch("build_pipeline.llm_steps.docs_steps.run_docs_update",
                   new_callable=AsyncMock, side_effect=RuntimeError("model down")), \
             patch("build_pipeline.llm_steps.respec_steps.run_respec_audit",
                   new_callable=AsyncMock, return_value=None):
            rc = await run_orchestrator(config, Namespace(interview_summary=""))

        assert rc == 0

    @pytest.mark.asyncio
    async def test_warning_reaches_stdout(self, tmp_path, capsys):
        from unittest.mock import AsyncMock, patch
        from argparse import Namespace
        from build_pipeline.models import GateResult
        from build_pipeline.orchestrator import run_orchestrator

        config = self._config(tmp_path)
        warning = GateResult(
            passed=True, action="docs_may_be_stale",
            reason="1 source file(s) changed and CHANGELOG.md exists",
        )

        with patch("build_pipeline.tdd_engine.run_tdd_engine",
                   new_callable=AsyncMock, return_value=0), \
             patch("build_pipeline.llm_steps.spec_steps.run_design_audit",
                   new_callable=AsyncMock, return_value=[]), \
             patch("build_pipeline.llm_steps.docs_steps.run_docs_update",
                   new_callable=AsyncMock, return_value=([], warning)), \
             patch("build_pipeline.llm_steps.respec_steps.run_respec_audit",
                   new_callable=AsyncMock, return_value=None):
            rc = await run_orchestrator(config, Namespace(interview_summary=""))

        assert rc == 0
        out = capsys.readouterr().out
        assert "DOCS_WARNING:1 source file(s) changed" in out

    @pytest.mark.asyncio
    async def test_phase_records_the_warning_in_the_progress_file(self, tmp_path):
        """A successful publish deletes the progress file, so the warning is
        asserted against the phase helper rather than a whole run."""
        from unittest.mock import AsyncMock, patch
        from build_pipeline.models import GateResult
        from build_pipeline.orchestrator import _run_docs_update_phase

        config = self._config(tmp_path)
        state = BuildState(change_name="feat", mode="only", tier="advanced")
        progress = ProgressWriter(config.progress_file_path())
        warning = GateResult(
            passed=True, action="docs_may_be_stale", reason="CHANGELOG.md may be stale",
        )

        with patch("build_pipeline.llm_steps.docs_steps.run_docs_update",
                   new_callable=AsyncMock, return_value=([], warning)):
            await _run_docs_update_phase(config, state, progress)

        text = config.progress_file_path().read_text()
        assert "WARNING: CHANGELOG.md may be stale" in text
        assert state.docs_impact == []

    @pytest.mark.asyncio
    async def test_phase_records_what_it_updated_on_the_state(self, tmp_path):
        from unittest.mock import AsyncMock, patch
        from build_pipeline.models import GateResult
        from build_pipeline.orchestrator import _run_docs_update_phase

        config = self._config(tmp_path)
        state = BuildState(change_name="feat", mode="only", tier="advanced")
        progress = ProgressWriter(config.progress_file_path())

        with patch("build_pipeline.llm_steps.docs_steps.run_docs_update",
                   new_callable=AsyncMock,
                   return_value=(["README.md"], GateResult(passed=True, reason="ok"))):
            await _run_docs_update_phase(config, state, progress)

        assert state.docs_impact == ["README.md"]
        assert "Updated: README.md" in config.progress_file_path().read_text()

    def test_reported_paths_outside_the_project_are_never_staged(self, tmp_path):
        """The DOCS_IMPACT list is agent-supplied and becomes `git add` argv."""
        from build_pipeline.orchestrator import _docs_paths

        project = tmp_path / "project"
        (project / "docs").mkdir(parents=True)
        (project / "README.md").write_text("r")
        (project / "docs" / "usage.md").write_text("u")
        (tmp_path / "outside.md").write_text("o")
        config = BuildConfig(project_dir=project, change_name="c")

        assert _docs_paths(config, ["README.md", "docs/usage.md"]) == [
            "README.md", "docs/usage.md",
        ]
        assert _docs_paths(config, ["../outside.md"]) == []
        assert _docs_paths(config, [str(tmp_path / "outside.md")]) == []
        assert _docs_paths(config, ["does-not-exist.md"]) == []
        assert _docs_paths(config, [":(exclude)README.md"]) == []
        assert _docs_paths(config, ["README.md", "./README.md"]) == ["README.md"]


class TestDocsUpdateInThePRBody:
    def test_pr_body_names_the_documents_the_build_updated(self, tmp_path):
        from build_pipeline.orchestrator import _pr_title_body

        config = BuildConfig(project_dir=tmp_path, change_name="c")
        config.specs_dir = tmp_path / "specs"
        config.change_dir.mkdir(parents=True)
        state = BuildState(change_name="c", mode="only", tier="advanced")
        state.docs_impact = ["README.md", "CHANGELOG.md"]

        _, body = _pr_title_body(config, state)
        assert "## Documentation" in body
        assert "`README.md`" in body
        assert "`CHANGELOG.md`" in body

    def test_pr_body_stays_silent_when_no_document_changed(self, tmp_path):
        from build_pipeline.orchestrator import _pr_title_body

        config = BuildConfig(project_dir=tmp_path, change_name="c")
        config.specs_dir = tmp_path / "specs"
        config.change_dir.mkdir(parents=True)
        state = BuildState(change_name="c", mode="only", tier="advanced")

        _, body = _pr_title_body(config, state)
        assert "## Documentation" not in body


# --- Git lifecycle (work item 4) -----------------------------------------

from build_pipeline import git_ops
from build_pipeline.git_ops import GitLifecycleError, GitResult
from tests.test_git_ops import _git, make_bare_origin, make_repo


def _args(tmp_path, change, **over):
    base = dict(
        mode="only",
        change=change,
        project_dir=str(tmp_path),
        auto=False,
        spec_only=False,
        skip_research=True,
        interview_summary="",
        research_path="",
        context_files=[],
        session_id="",
    )
    base.update(over)
    return argparse.Namespace(**base)


def _worktree_count(repo: Path) -> int:
    return len(git_ops.worktree_paths(repo))


class TestBootstrapWorktree:
    """Criterion 10 — the build runs in its own worktree on its own branch and
    the caller's checkout is never written to."""

    @pytest.mark.asyncio
    async def test_bootstrap_creates_worktree_and_leaves_source_clean(
        self, tmp_path, monkeypatch
    ):
        workspace = tmp_path / "ws"
        monkeypatch.setenv("WORKSPACE", str(workspace))
        repo = make_repo(tmp_path / "proj")

        config = BuildConfig(project_dir=repo, change_name="my-change")
        config.specs_dir = repo / "specs"
        state = BuildState(
            change_name="my-change", mode="scratch", tier="advanced",
            state_file=str(config.state_file_path()),
        )
        cm = ChangeManager(config.specs_dir)

        await _run_bootstrap(config, state, cm, config.state_file_path())

        expected_wt = workspace / ".worktrees" / "buildme-my-change"
        assert state.worktree_path == str(expected_wt)
        assert state.branch == "buildme/my-change"
        assert state.source_repo == str(repo.resolve())
        assert expected_wt.is_dir()
        assert git_ops.current_branch(expected_wt) == "buildme/my-change"

        # Config re-bound: every later phase writes inside the worktree
        assert config.project_dir == expected_wt
        assert config.specs_dir == expected_wt / "specs"
        assert config.pipeline_branch == "buildme/my-change"
        assert (expected_wt / "specs" / "changes" / "my-change").is_dir()

        # The caller's checkout was never written to
        assert not (repo / "specs").exists()
        status = subprocess.run(
            ["git", "status", "--short"], cwd=str(repo), capture_output=True, text=True
        )
        assert status.stdout.strip() == ""
        assert git_ops.current_branch(repo) == "main"

    @pytest.mark.asyncio
    async def test_refuses_non_repo_when_worktree_requested(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        project = tmp_path / "plain"
        project.mkdir()
        config = BuildConfig(project_dir=project, change_name="c")
        config.specs_dir = project / "specs"
        state = BuildState(change_name="c", mode="scratch", tier="advanced")
        cm = ChangeManager(config.specs_dir)
        with pytest.raises(GitLifecycleError) as exc:
            await _run_bootstrap(config, state, cm, config.state_file_path())
        assert "--no-worktree" in str(exc.value)
        assert not (project / ".git").exists(), "must not git init behind the refusal"

    @pytest.mark.asyncio
    async def test_refuses_dirty_source_repo(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        (repo / "README.md").write_text("uncommitted\n")
        config = BuildConfig(project_dir=repo, change_name="c")
        config.specs_dir = repo / "specs"
        state = BuildState(change_name="c", mode="scratch", tier="advanced")
        cm = ChangeManager(config.specs_dir)
        with pytest.raises(GitLifecycleError) as exc:
            await _run_bootstrap(config, state, cm, config.state_file_path())
        assert "uncommitted changes" in str(exc.value)


class TestNoWorktreeIsUnchangedBehavior:
    """Criterion 11 — `--no-worktree --no-push --no-pr` reproduces the
    pre-change pipeline: same files, same branch, no extra worktree, no
    publish output."""

    @pytest.mark.asyncio
    async def test_bootstrap_file_set_and_branch_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        config = BuildConfig(
            project_dir=repo,
            change_name="inplace",
            git=GitToggles(worktree=False, push=False, pr="none"),
        )
        config.specs_dir = repo / "specs"
        state = BuildState(change_name="inplace", mode="scratch", tier="advanced",
                           state_file=str(config.state_file_path()))
        cm = ChangeManager(config.specs_dir)

        await _run_bootstrap(config, state, cm, config.state_file_path())

        # No re-binding, no worktree, no branch
        assert config.project_dir == repo
        assert config.pipeline_branch is None
        assert state.branch is None and state.worktree_path is None
        assert _worktree_count(repo) == 1
        assert git_ops.current_branch(repo) == "main"

        # Exactly the pre-change on-disk footprint: specs/ under the project dir
        created = sorted(
            str(p.relative_to(repo))
            for p in repo.rglob("*")
            if ".git" not in p.parts and p.is_file()
        )
        assert created == [
            "README.md",
            "specs/changes/inplace/.build-state.json",
            "specs/changes/inplace/.change.yaml",
            "specs/config.yaml",
        ]

    @pytest.mark.asyncio
    async def test_publish_is_skipped_without_a_pipeline_branch(self, tmp_path, capsys):
        repo = make_repo(tmp_path / "proj")
        config = BuildConfig(
            project_dir=repo, change_name="inplace",
            git=GitToggles(worktree=False, push=False, pr="none"),
        )
        config.specs_dir = repo / "specs"
        state = BuildState(change_name="inplace", mode="scratch", tier="advanced")
        progress = ProgressWriter(config.progress_file_path())
        assert _run_publish(config, state, progress) is True
        out = capsys.readouterr().out
        assert "PUSHED:" not in out and "PR:" not in out
        assert not config.progress_file_path().exists()


class TestResumeRebinding:
    """Criterion 12 — a resume re-binds to the existing worktree and never
    creates a second one."""

    @pytest.mark.asyncio
    async def test_resume_creates_no_second_worktree(self, tmp_path, monkeypatch):
        workspace = tmp_path / "ws"
        monkeypatch.setenv("WORKSPACE", str(workspace))
        repo = make_repo(tmp_path / "proj")

        # First run: bootstrap creates the worktree
        config1 = BuildConfig(project_dir=repo, change_name="resumed")
        config1.specs_dir = repo / "specs"
        state = BuildState(change_name="resumed", mode="scratch", tier="advanced",
                           state_file=str(config1.state_file_path()))
        cm = ChangeManager(config1.specs_dir)
        await _run_bootstrap(config1, state, cm, config1.state_file_path())
        worktree = Path(state.worktree_path)

        before = _worktree_count(repo)
        assert before == 2

        # Mark it far enough along that a resume skips every producing phase
        state.phase = BuildPhase.PUBLISH
        state.checkpoint(config1.state_file_path())

        # Second run: a fresh config pointed at the SOURCE repo, as a resuming
        # session would construct it
        config2 = BuildConfig(
            project_dir=repo, change_name="resumed", mode="only",
            git=GitToggles(worktree=True, push=False, pr="none"),
        )
        config2.specs_dir = repo / "specs"
        result = await run_orchestrator(config2, _args(repo, "resumed"))

        assert result == 0
        assert _worktree_count(repo) == before, "a resume must not create a second worktree"
        assert config2.project_dir == worktree
        assert config2.pipeline_branch == "buildme/resumed"


class TestPublishFailureIsNonFatal:
    """Criterion 13 — a failing `gh` leaves the build successful, the branch
    and worktree intact, and the exact manual commands in build-progress.md."""

    @pytest.mark.asyncio
    async def test_gh_failure_keeps_build_successful(self, tmp_path, monkeypatch, capsys):
        workspace = tmp_path / "ws"
        monkeypatch.setenv("WORKSPACE", str(workspace))
        repo = make_repo(tmp_path / "proj")
        make_bare_origin(repo, tmp_path / "origin.git")

        config = BuildConfig(project_dir=repo, change_name="ghfail")
        config.specs_dir = repo / "specs"
        state = BuildState(change_name="ghfail", mode="scratch", tier="advanced",
                           state_file=str(config.state_file_path()))
        cm = ChangeManager(config.specs_dir)
        await _run_bootstrap(config, state, cm, config.state_file_path())
        worktree = Path(state.worktree_path)
        state.phase = BuildPhase.PUBLISH
        state.checkpoint(config.state_file_path())

        monkeypatch.setattr(
            git_ops, "_run_gh",
            lambda argv, cwd=None, timeout=None: GitResult(
                argv, 1, "", "gh: To get started with GitHub CLI, please run: gh auth login"
            ),
        )

        config2 = BuildConfig(project_dir=repo, change_name="ghfail", mode="only")
        config2.specs_dir = repo / "specs"
        result = await run_orchestrator(config2, _args(repo, "ghfail"))

        assert result == 0, "a gh failure must not fail the build"
        out = capsys.readouterr().out
        assert "RESULT:SUCCESS" in out
        assert "PHASE:PUBLISH:FAILED:pr" in out

        # Branch and worktree survive
        assert worktree.is_dir()
        assert git_ops.branch_exists(repo, "buildme/ghfail")

        # The push half did succeed, so only the PR is outstanding
        progress_text = (worktree / "build-progress.md").read_text()
        assert "gh auth login" in progress_text
        assert "git push -u origin buildme/ghfail" in progress_text
        assert "gh pr create --title 'buildme: ghfail'" in progress_text

    @pytest.mark.asyncio
    async def test_no_remote_reports_manual_commands_and_invents_nothing(
        self, tmp_path, monkeypatch, capsys
    ):
        workspace = tmp_path / "ws"
        monkeypatch.setenv("WORKSPACE", str(workspace))
        repo = make_repo(tmp_path / "proj")  # deliberately no origin

        config = BuildConfig(project_dir=repo, change_name="noremote")
        config.specs_dir = repo / "specs"
        state = BuildState(change_name="noremote", mode="scratch", tier="advanced",
                           state_file=str(config.state_file_path()))
        cm = ChangeManager(config.specs_dir)
        await _run_bootstrap(config, state, cm, config.state_file_path())
        worktree = Path(state.worktree_path)
        progress = ProgressWriter(config.progress_file_path())

        assert _run_publish(config, state, progress) is False
        out = capsys.readouterr().out
        assert "PHASE:PUBLISH:FAILED:no-remote" in out
        text = (worktree / "build-progress.md").read_text()
        assert "No 'origin' remote" in text
        assert "git push -u origin buildme/noremote" in text
        assert git_ops.has_remote(worktree) is False

    @pytest.mark.asyncio
    async def test_successful_publish_prints_pr_url_and_records_it(
        self, tmp_path, monkeypatch, capsys
    ):
        workspace = tmp_path / "ws"
        monkeypatch.setenv("WORKSPACE", str(workspace))
        repo = make_repo(tmp_path / "proj")
        make_bare_origin(repo, tmp_path / "origin.git")

        config = BuildConfig(project_dir=repo, change_name="happy")
        config.specs_dir = repo / "specs"
        state = BuildState(change_name="happy", mode="scratch", tier="advanced",
                           state_file=str(config.state_file_path()))
        cm = ChangeManager(config.specs_dir)
        await _run_bootstrap(config, state, cm, config.state_file_path())
        progress = ProgressWriter(config.progress_file_path())

        captured = {}

        def fake_gh(argv, cwd=None, timeout=None):
            captured["argv"] = argv
            return GitResult(argv, 0, "https://github.com/o/r/pull/9\n", "")

        monkeypatch.setattr(git_ops, "_run_gh", fake_gh)
        assert _run_publish(config, state, progress) is True
        assert state.pr_url == "https://github.com/o/r/pull/9"
        out = capsys.readouterr().out
        assert "PR:https://github.com/o/r/pull/9" in out
        assert "PUSHED:buildme/happy" in out
        assert "--draft" in captured["argv"], "PRs are draft by default"

    @pytest.mark.asyncio
    async def test_no_push_skips_publish_entirely(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        config = BuildConfig(
            project_dir=repo, change_name="nopush",
            git=GitToggles(worktree=True, push=False, pr="draft"),
        )
        config.specs_dir = repo / "specs"
        state = BuildState(change_name="nopush", mode="scratch", tier="advanced",
                           state_file=str(config.state_file_path()))
        cm = ChangeManager(config.specs_dir)
        await _run_bootstrap(config, state, cm, config.state_file_path())
        progress = ProgressWriter(config.progress_file_path())
        assert _run_publish(config, state, progress) is True
        assert "PHASE:PUBLISH:SKIPPED:buildme/nopush" in capsys.readouterr().out


async def _bootstrapped_auto_build(tmp_path, change: str, why: str):
    """A worktree build parked at PUBLISH with a proposal carrying a Why."""
    repo = make_repo(tmp_path / "proj")
    make_bare_origin(repo, tmp_path / "origin.git")
    config = BuildConfig(project_dir=repo, change_name=change, auto=True)
    config.specs_dir = repo / "specs"
    state = BuildState(change_name=change, mode="scratch", tier="advanced",
                       state_file=str(config.state_file_path()))
    cm = ChangeManager(config.specs_dir)
    await _run_bootstrap(config, state, cm, config.state_file_path())
    (config.change_dir / "proposal.md").write_text(
        f"# P\n\n## Why\n\n{why}\n\n## Impact\n\nnone\n"
    )
    (config.change_dir / "respec.md").write_text("delta")
    # In a real run earlier phases wrote the board card; it travels with the
    # archive move, which is what keeps board.record from resurrecting an
    # empty changes/<name>/ during publish.
    (config.change_dir / ".board.json").write_text("{}\n")
    state.phase = BuildPhase.PUBLISH
    state.checkpoint(config.state_file_path())
    return repo, state


class TestAutoPublishPRBody:
    """The --auto archive moves proposal.md before publish; the PR body must
    read the documents through the archived location, not the vacated
    change_dir (which would lose the Why and every artifact link)."""

    @pytest.mark.asyncio
    async def test_auto_pr_body_survives_the_archive_move(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo, _ = await _bootstrapped_auto_build(
            tmp_path, "autobody", "Because the queue drops jobs."
        )

        seen: dict = {}

        def fake_gh(argv, cwd=None, timeout=None):
            seen["argv"] = argv
            return GitResult(argv, 0, "https://github.com/o/r/pull/3\n", "")

        monkeypatch.setattr(git_ops, "_run_gh", fake_gh)
        config2 = BuildConfig(project_dir=repo, change_name="autobody",
                              mode="only", auto=True)
        config2.specs_dir = repo / "specs"
        result = await run_orchestrator(config2, _args(repo, "autobody", auto=True))

        assert result == 0
        body = seen["argv"][seen["argv"].index("--body") + 1]
        assert "Because the queue drops jobs." in body
        assert "respec.md" in body
        assert "specs/archive/" in body, "links must point at the archived layout"
        assert not config2.change_dir.exists()  # archived
        # Successful publish drops the checkpoint, even from its archived home.
        assert list((config2.specs_dir / "archive").glob("*/.build-state.json")) == []


class TestPublishFailureRecovery:
    """A failed publish must leave a resumable PUBLISH checkpoint (build still
    exits 0) so a re-run adopts the worktree and retries, instead of
    hard-failing on the branch collision with an impossible 'Resume it'."""

    @pytest.mark.asyncio
    async def test_auto_publish_failure_then_rerun_succeeds(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo, state = await _bootstrapped_auto_build(tmp_path, "pubfail", "Why text.")
        worktree = Path(state.worktree_path)

        monkeypatch.setattr(
            git_ops, "_run_gh",
            lambda argv, cwd=None, timeout=None: GitResult(argv, 1, "", "gh: boom"),
        )
        config2 = BuildConfig(project_dir=repo, change_name="pubfail",
                              mode="only", auto=True)
        config2.specs_dir = repo / "specs"
        result = await run_orchestrator(config2, _args(repo, "pubfail", auto=True))

        assert result == 0, "publish failure stays non-fatal for the build result"
        assert "PHASE:PUBLISH:FAILED:pr" in capsys.readouterr().out

        # The checkpoint traveled with the archive move and survived at PUBLISH.
        archived_states = list(
            (config2.specs_dir / "archive").glob("*-pubfail/.build-state.json")
        )
        assert len(archived_states) == 1
        resumed = BuildState.load(archived_states[0])
        assert resumed.phase == BuildPhase.PUBLISH
        assert resumed.branch == "buildme/pubfail"

        # Re-run with gh healthy: adopts the same worktree, retries, cleans up.
        before = _worktree_count(repo)
        monkeypatch.setattr(
            git_ops, "_run_gh",
            lambda argv, cwd=None, timeout=None: GitResult(
                argv, 0, "https://github.com/o/r/pull/8\n", ""
            ),
        )
        config3 = BuildConfig(project_dir=repo, change_name="pubfail",
                              mode="only", auto=True)
        config3.specs_dir = repo / "specs"
        result = await run_orchestrator(config3, _args(repo, "pubfail", auto=True))

        assert result == 0
        out = capsys.readouterr().out
        assert "PR:https://github.com/o/r/pull/8" in out
        assert _worktree_count(repo) == before, "the re-run must not mint a second worktree"
        assert not archived_states[0].exists(), "checkpoint cleaned after success"
        # Bookkeeping was never tracked, so the worktree ends clean — no
        # uncommitted deletion left behind by the cleanup.
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=str(worktree), capture_output=True, text=True,
        )
        assert status.stdout.strip() == ""

    @pytest.mark.asyncio
    async def test_pr_url_is_persisted_to_the_checkpoint(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        make_bare_origin(repo, tmp_path / "origin.git")
        config = BuildConfig(project_dir=repo, change_name="prurl")
        config.specs_dir = repo / "specs"
        state = BuildState(change_name="prurl", mode="scratch", tier="advanced",
                           state_file=str(config.state_file_path()))
        cm = ChangeManager(config.specs_dir)
        await _run_bootstrap(config, state, cm, config.state_file_path())
        progress = ProgressWriter(config.progress_file_path())

        monkeypatch.setattr(
            git_ops, "_run_gh",
            lambda argv, cwd=None, timeout=None: GitResult(
                argv, 0, "https://github.com/o/r/pull/9\n", ""
            ),
        )
        state_path = config.state_file_path()
        assert _run_publish(config, state, progress, state_path=state_path) is True
        saved = BuildState.load(state_path)
        assert saved.pr_url == "https://github.com/o/r/pull/9"


class TestNestedProjectDir:
    """A project_dir nested inside a larger repo must build at the SAME
    subdirectory inside the worktree — not silently relocate to the repo root."""

    def _nested_repo(self, tmp_path):
        repo = make_repo(tmp_path / "mono")
        pkg = repo / "packages" / "api"
        pkg.mkdir(parents=True)
        (pkg / "keep.txt").write_text("k\n")
        _git(repo, "add", "packages/api/keep.txt")
        _git(repo, "commit", "-m", "chore: nested pkg")
        return repo, pkg

    @pytest.mark.asyncio
    async def test_bootstrap_rebinds_to_same_subdir_inside_worktree(
        self, tmp_path, monkeypatch
    ):
        workspace = tmp_path / "ws"
        monkeypatch.setenv("WORKSPACE", str(workspace))
        repo, pkg = self._nested_repo(tmp_path)

        config = BuildConfig(project_dir=pkg, change_name="nested")
        config.specs_dir = pkg / "specs"
        state = BuildState(change_name="nested", mode="scratch", tier="advanced",
                           state_file=str(config.state_file_path()))
        cm = ChangeManager(config.specs_dir)
        await _run_bootstrap(config, state, cm, config.state_file_path())

        wt = workspace / ".worktrees" / "buildme-nested"
        assert state.worktree_path == str(wt)
        assert config.project_dir == wt / "packages" / "api"
        assert config.specs_dir == wt / "packages" / "api" / "specs"
        assert (wt / "packages" / "api" / "specs" / "changes" / "nested").is_dir()
        assert not (wt / "specs").exists(), "must not build at the repo root"
        assert not (pkg / "specs").exists(), "source checkout untouched"

    @pytest.mark.asyncio
    async def test_resume_rebinds_nested_project_to_same_subdir(
        self, tmp_path, monkeypatch
    ):
        workspace = tmp_path / "ws"
        monkeypatch.setenv("WORKSPACE", str(workspace))
        repo, pkg = self._nested_repo(tmp_path)

        config1 = BuildConfig(project_dir=pkg, change_name="nested")
        config1.specs_dir = pkg / "specs"
        state = BuildState(change_name="nested", mode="scratch", tier="advanced",
                           state_file=str(config1.state_file_path()))
        cm = ChangeManager(config1.specs_dir)
        await _run_bootstrap(config1, state, cm, config1.state_file_path())
        wt = Path(state.worktree_path)
        state.phase = BuildPhase.PUBLISH
        state.checkpoint(config1.state_file_path())

        config2 = BuildConfig(
            project_dir=pkg, change_name="nested", mode="only",
            git=GitToggles(worktree=True, push=False, pr="none"),
        )
        config2.specs_dir = pkg / "specs"
        result = await run_orchestrator(config2, _args(pkg, "nested"))

        assert result == 0
        assert config2.project_dir == wt / "packages" / "api"
        assert _worktree_count(repo) == 2, "resume must adopt, not create"


class TestPRBody:
    def test_body_carries_why_blocks_and_worktree_note(self, tmp_path):
        repo = make_repo(tmp_path / "proj")
        config = BuildConfig(project_dir=repo, change_name="bodytest")
        config.specs_dir = repo / "specs"
        config.change_dir.mkdir(parents=True)
        (config.change_dir / "proposal.md").write_text(
            "# P\n\n## Why\n\nBecause the thing is broken.\n\n## Impact\n\nnone\n"
        )
        (config.change_dir / "respec.md").write_text("delta")
        from build_pipeline.models import BlockInfo
        from build_pipeline.state import TDDState

        state = BuildState(
            change_name="bodytest", mode="scratch", tier="advanced",
            worktree_path=str(repo),
            tdd=TDDState(blocks=[BlockInfo(number=1, name="First", description="d")]),
        )
        title, body = _pr_title_body(config, state)
        assert title == "buildme: bodytest"
        assert "Because the thing is broken." in body
        assert "Block 1: First" in body
        assert "respec.md" in body
        assert "unknowns.md" not in body  # absent artifacts are not linked
        assert "calling session's decision" in body
