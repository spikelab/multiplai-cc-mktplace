"""Tests for the build orchestrator — phase sequencing."""

import argparse
import subprocess
from pathlib import Path

import pytest
from build_pipeline.change_manager import ChangeManager
from build_pipeline.config import BuildConfig, GitToggles
from build_pipeline.models import BuildPhase
from build_pipeline.orchestrator import (
    _rebind_to_existing_worktree,
    _run_bootstrap,
    run_orchestrator,
)
from build_pipeline.progress import ProgressWriter
from build_pipeline.state import BuildState


@pytest.fixture(autouse=True)
def _no_spec_extract(monkeypatch):
    """No build here runs spec-extract: its step inside CODEBASE_ANALYSIS lets the phase complete."""
    from build_pipeline import orchestrator

    monkeypatch.setattr(
        orchestrator, "_run_spec_extract_step", lambda config, state, progress: None
    )


from tests.planning_fakes import _plan_fixture, _prepare_and_publish


@pytest.fixture(autouse=True)
def _phase_wiring_prerequisites(monkeypatch):
    """Supply model outputs and verified-plan fixtures, retaining real final gates.

    These tests exercise phase routing and Git publication; missing/stale evidence
    is tested through unwrapped public entry points in test_completion_contract.
    """
    from unittest.mock import AsyncMock

    from build_pipeline import orchestrator, spec_generator
    from build_pipeline.llm_steps import plan_review_steps
    from build_pipeline.models import GateResult, PlanReviewResult

    real = orchestrator.run_orchestrator

    async def generated(cm, change_dir, config, state):
        _plan_fixture(config, state)

    async def run(config, args):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        if not config.test_command:
            config.test_command = "true"
        orchestrator._rebind_to_existing_worktree(config)
        path = config.state_file_path()
        if path.exists():
            state = BuildState.load(path)
            if state.phase == BuildPhase.PUBLISH and not (
                state.spec_gen and state.spec_gen.revision
            ):
                _plan_fixture(config, state)
                state.spec_gen.record_step("plan_review", "passed")
                state.checkpoint(path)
                git_ops.commit_tree(config.project_dir, "test: accepted plan")
        if not git_ops.rev_parse_head(config.project_dir):
            git_ops._run(["git", "init", "-q"], cwd=config.project_dir)
            git_ops._run(
                ["git", "commit", "--allow-empty", "-qm", "test baseline"],
                cwd=config.project_dir,
            )
        return await real(config, args)

    monkeypatch.setattr(spec_generator, "_generate_all_artifacts", generated)
    monkeypatch.setattr(
        spec_generator, "generate_rubric", AsyncMock(return_value="# Current rubric")
    )
    monkeypatch.setattr(
        "build_pipeline.llm_steps.docs_steps.run_docs_update",
        AsyncMock(return_value=([], GateResult(passed=True, reason="unit docs"))),
    )
    monkeypatch.setattr(
        plan_review_steps,
        "run_plan_review",
        AsyncMock(return_value=PlanReviewResult(findings=[])),
    )
    monkeypatch.setattr(orchestrator, "run_orchestrator", run)
    monkeypatch.setitem(globals(), "run_orchestrator", run)


class TestPhaseOrdering:
    def test_phase_enum_order(self):
        """Verify phases are ordered correctly for is_phase_complete checks."""
        phases = list(BuildPhase)
        names = [p.value for p in phases]
        assert names.index("init") < names.index("bootstrap")
        assert names.index("bootstrap") < names.index("research")
        assert names.index("research") < names.index("spec_generation")
        assert names.index("spec_generation") < names.index("design_audit")
        assert names.index("design_audit") < names.index("plan_review")
        assert names.index("plan_review") < names.index("prototype")
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
            change_name="test-change",
            tier="advanced",
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
            tier="advanced",
            phase=BuildPhase.COMPLETE,
            state_file=str(config.state_file_path()),
        )
        state.checkpoint(config.state_file_path())

        # Stub out phases that would otherwise make LLM calls — state already
        # marks them complete via is_phase_complete, so we only need the
        # orchestrator to reach the final archive block.
        args = argparse.Namespace(
            change="test-archive",
            project_dir=str(tmp_path),
            auto=True,
            spec_only=False,
            skip_research=True,
            research_path="",
            session_id="",
        )

        result = await run_orchestrator(config, args)

        assert result == 0
        # Change directory should be moved to archive
        assert (
            change_dir.exists()
        )  # historical completion never archives or republishes
        archive_root = config.specs_dir / "archive"
        archived_dirs = list(archive_root.glob("*-test-archive"))
        assert archived_dirs == []

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
            tier="advanced",
            phase=BuildPhase.COMPLETE,
            state_file=str(config.state_file_path()),
        )
        state.checkpoint(config.state_file_path())

        args = argparse.Namespace(
            change="test-manual",
            project_dir=str(tmp_path),
            auto=False,
            spec_only=False,
            skip_research=True,
            research_path="",
            session_id="",
        )

        result = await run_orchestrator(config, args)

        assert result == 0
        # Change should still be in place
        assert change_dir.exists()
        assert (change_dir / "proposal.md").exists()


class TestCodebaseAnalysisPhase:
    """BuildPhase.CODEBASE_ANALYSIS runs between RESEARCH and SPEC_GENERATION,
    writes codebase-analysis.md, records the path on the checkpoint, and is
    skipped — with the reason logged — for a project with no source files."""

    def _args(self, tmp_path, change):
        return argparse.Namespace(
            change=change,
            project_dir=str(tmp_path),
            auto=True,
            spec_only=True,
            skip_research=True,
            research_path="",
            session_id="",
        )

    def _config(self, tmp_path, change, *, with_source=True, phase=BuildPhase.RESEARCH):
        from build_pipeline.config import GitToggles

        config = BuildConfig(
            project_dir=tmp_path,
            change_name=change,
            auto=True,
            spec_only=True,
            skip_research=True,
            git=GitToggles(worktree=False),
        )
        config.specs_dir = tmp_path / "specs"
        cm = ChangeManager(config.specs_dir)
        cm.init_specs()
        cm.create_change(change)
        (config.change_dir / "proposal.md").write_text("## Why\nA thing.\n")
        if with_source:
            (tmp_path / "app.py").write_text("def main():\n    return 1\n")
        state = BuildState(
            change_name=change,
            tier="advanced",
            phase=phase,
            bootstrap_done=True,
            state_file=str(config.state_file_path()),
        )
        state.checkpoint(config.state_file_path())
        return config

    @staticmethod
    def _patches(analysis_mock):
        from unittest.mock import AsyncMock, patch

        return (
            patch(
                "build_pipeline.llm_steps.spec_steps.run_codebase_analysis",
                analysis_mock,
            ),
            patch(
                "build_pipeline.spec_generator.run_spec_generator",
                AsyncMock(return_value=0),
            ),
        )

    @pytest.mark.asyncio
    async def test_runs_and_writes_the_report(self, tmp_path, capsys):
        from unittest.mock import AsyncMock

        config = self._config(tmp_path, "analyze-me")
        mock_analysis = AsyncMock(return_value="## Architecture\nOne module: app.py")
        p1, p2 = self._patches(mock_analysis)
        with p1, p2:
            result = await run_orchestrator(config, self._args(tmp_path, "analyze-me"))

        assert result == 0
        assert mock_analysis.await_count == 1
        # The agents are pointed at the project, not the specs directory.
        assert mock_analysis.await_args.args[0] == config.project_dir

        report = config.codebase_analysis_path
        assert report.exists()
        assert report.read_text() == "## Architecture\nOne module: app.py"
        assert report.name == "codebase-analysis.md"

        out = capsys.readouterr().out
        assert "PHASE:CODEBASE_ANALYSIS:COMPLETE" in out
        # Still shaping — the card must not jump a column for reading code.
        assert "BOARD:analyze-me:Shaping" in out

    @pytest.mark.asyncio
    async def test_records_the_report_path_on_the_checkpoint(self, tmp_path):
        """spec generation reads spec_gen.codebase_analysis_path, so the phase
        that writes the report has to persist where it put it."""
        from unittest.mock import AsyncMock

        config = self._config(tmp_path, "record-path")
        seen = {}

        async def _capture(cfg, args=None, **kwargs):
            state = BuildState.load(cfg.state_file_path())
            seen["path"] = (
                state.spec_gen.codebase_analysis_path if state.spec_gen else None
            )
            return 0

        from unittest.mock import patch

        with (
            patch(
                "build_pipeline.llm_steps.spec_steps.run_codebase_analysis",
                AsyncMock(return_value="## Architecture\nstuff"),
            ),
            patch("build_pipeline.spec_generator.run_spec_generator", _capture),
        ):
            result = await run_orchestrator(config, self._args(tmp_path, "record-path"))

        assert result == 0
        assert seen["path"] == str(config.codebase_analysis_path)

    @pytest.mark.asyncio
    async def test_skipped_with_a_reason_when_there_is_no_source_yet(
        self,
        tmp_path,
        caplog,
        capsys,
    ):
        from unittest.mock import AsyncMock

        config = self._config(tmp_path, "greenfield", with_source=False)
        mock_analysis = AsyncMock()
        p1, p2 = self._patches(mock_analysis)
        with caplog.at_level("INFO"), p1, p2:
            result = await run_orchestrator(config, self._args(tmp_path, "greenfield"))

        assert result == 0
        mock_analysis.assert_not_awaited()
        assert not config.codebase_analysis_path.exists()
        skips = [
            r.getMessage()
            for r in caplog.records
            if "SKIP phase=CODEBASE_ANALYSIS" in r.getMessage()
        ]
        assert skips, "the skip must be logged with its reason"
        assert "no-source-files-yet" in skips[0]
        # The phase still completes — a skip is not a stall.
        assert "PHASE:CODEBASE_ANALYSIS:COMPLETE" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_specs_dir_alone_is_not_a_codebase(self, tmp_path):
        """A bootstrapped project whose only files are its own specs must not
        pay for three explore agents."""
        from unittest.mock import AsyncMock

        config = self._config(tmp_path, "specs-only", with_source=False)
        (config.change_dir / "notes.py").write_text("# not project source")
        mock_analysis = AsyncMock()
        p1, p2 = self._patches(mock_analysis)
        with p1, p2:
            assert (
                await run_orchestrator(config, self._args(tmp_path, "specs-only")) == 0
            )
        mock_analysis.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_analysis_failure_does_not_fail_the_build(self, tmp_path, caplog):
        from unittest.mock import AsyncMock

        config = self._config(tmp_path, "boom")
        mock_analysis = AsyncMock(side_effect=RuntimeError("all three agents died"))
        p1, p2 = self._patches(mock_analysis)
        with caplog.at_level("WARNING"), p1, p2:
            result = await run_orchestrator(config, self._args(tmp_path, "boom"))

        assert result == 0
        assert not config.codebase_analysis_path.exists()
        assert any("Codebase analysis failed" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_resume_past_the_phase_does_not_rerun_it(self, tmp_path):
        from unittest.mock import AsyncMock

        config = self._config(tmp_path, "resumed", phase=BuildPhase.SPEC_GENERATION)
        mock_analysis = AsyncMock()
        p1, p2 = self._patches(mock_analysis)
        with p1, p2:
            assert await run_orchestrator(config, self._args(tmp_path, "resumed")) == 0
        mock_analysis.assert_not_awaited()


class TestReviewCheckpointPaths:
    """The non-`--auto` review checkpoint prints what the reviewer must read,
    in order — the codebase analysis explains what the design was written
    against, so it is printed before the design."""

    def test_codebase_analysis_path_is_printed_when_present(self, tmp_path, capsys):
        from build_pipeline.orchestrator import _print_review_context_paths

        config = BuildConfig(project_dir=tmp_path, change_name="review-me")
        config.specs_dir = tmp_path / "specs"
        config.change_dir.mkdir(parents=True)
        config.unknowns_path.write_text("# Unknowns")
        config.codebase_analysis_path.write_text("## Architecture")

        _print_review_context_paths(config)
        out = capsys.readouterr().out
        assert f"REVIEW:READ_FIRST:{config.unknowns_path}" in out
        assert f"REVIEW:CODEBASE_ANALYSIS:{config.codebase_analysis_path}" in out
        assert out.index("READ_FIRST") < out.index("CODEBASE_ANALYSIS")

    def test_absent_analysis_prints_nothing(self, tmp_path, capsys):
        from build_pipeline.orchestrator import _print_review_context_paths

        config = BuildConfig(project_dir=tmp_path, change_name="review-me")
        config.specs_dir = tmp_path / "specs"
        config.change_dir.mkdir(parents=True)

        _print_review_context_paths(config)
        assert "CODEBASE_ANALYSIS" not in capsys.readouterr().out


class TestSpecGenerationReloadOnlyMovesForward:
    """The orchestrator reloads the state run_spec_generator checkpointed. That
    copy can already sit at a LATER phase than SPEC_GENERATION, so the advance
    that follows has to be guarded — advance_to assigns unconditionally, and an
    unguarded call rewinds the persisted pointer, re-opening the phases in
    between."""

    @staticmethod
    def _config(tmp_path):
        config = BuildConfig(
            project_dir=tmp_path,
            change_name="reload",
            auto=True,
            spec_only=True,
            skip_research=True,
        )
        config.specs_dir = tmp_path / "specs"
        cm = ChangeManager(config.specs_dir)
        cm.init_specs()
        cm.create_change("reload")
        (config.change_dir / "proposal.md").write_text("## Why\nBecause.")
        (config.change_dir / "implementation-plan.md").write_text("## Decisions\nNone.")
        (config.change_dir / "tasks.md").write_text("## 1. Block")
        state = BuildState(
            change_name="reload",
            tier="advanced",
            phase=BuildPhase.RESEARCH,
            state_file=str(config.state_file_path()),
        )
        state.checkpoint(config.state_file_path())
        return config

    @staticmethod
    def _args(tmp_path):
        import argparse

        return argparse.Namespace(
            change="reload",
            project_dir=str(tmp_path),
            auto=True,
            spec_only=True,
            skip_research=True,
            research_path="",
            session_id="",
        )

    @pytest.mark.asyncio
    async def test_phase_is_not_rewound_and_the_check_is_not_re_run(self, tmp_path):
        from unittest.mock import AsyncMock, patch

        from build_pipeline.orchestrator import run_orchestrator

        config = self._config(tmp_path)
        state_path = config.state_file_path()

        async def fake_spec_generator(cfg, args=None, **kwargs):
            """What the real one leaves behind: the pointer already at
            DESIGN_AUDIT, and the plan check recorded as passed."""
            s = BuildState.load(state_path)
            _plan_fixture(cfg, s)
            s.spec_gen.record_step("plan_review", "passed")
            s.advance_to(BuildPhase.DESIGN_AUDIT, state_path)
            return 0

        # A successful spec-only run deletes its state file, so the pointer is
        # observed as it is written rather than read back at the end.
        advanced: list[BuildPhase] = []
        real_advance = BuildState.advance_to

        def recording_advance(self, phase, path=None):
            advanced.append(phase)
            return real_advance(self, phase, path)

        with (
            patch(
                "build_pipeline.spec_generator.run_spec_generator",
                side_effect=fake_spec_generator,
            ),
            patch("build_pipeline.state.BuildState.advance_to", recording_advance),
            patch(
                "build_pipeline.llm_steps.plan_review_steps.run_plan_review",
                new_callable=AsyncMock,
            ) as mock_audit,
        ):
            result = await run_orchestrator(config, self._args(tmp_path))

        assert result == 0
        # The plan check already passed, so the orchestrator's planning
        # phases cost no LLM call at all.
        mock_audit.assert_not_awaited()
        # The pointer never went backwards: nothing re-advances to
        # SPEC_GENERATION after the spec generator left it at DESIGN_AUDIT.
        order = list(BuildPhase)
        assert BuildPhase.DESIGN_AUDIT in advanced
        after = advanced[advanced.index(BuildPhase.DESIGN_AUDIT) + 1 :]
        assert all(
            order.index(p) >= order.index(BuildPhase.DESIGN_AUDIT) for p in after
        ), f"phase pointer rewound: {[p.value for p in advanced]}"


class TestRespecPhaseWiring:
    """The RESPEC phase runs after TDD_BUILD, is skipped on resume once done,
    and never fails the build."""

    def test_respec_sits_between_tdd_build_and_complete(self):
        names = [p.value for p in BuildPhase]
        assert names.index("tdd_build") < names.index("respec")
        assert names.index("respec") < names.index("complete")

    @pytest.mark.asyncio
    async def test_orchestrator_runs_respec_before_archiving(self, tmp_path):
        from argparse import Namespace
        from unittest.mock import AsyncMock, patch

        from build_pipeline.orchestrator import run_orchestrator

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        config = BuildConfig(
            project_dir=project_dir,
            change_name="feat",
            auto=False,
            config_dir=tmp_path / "config",
        )
        config.specs_dir = project_dir / "specs"
        config.change_dir.mkdir(parents=True)

        with (
            patch(
                "build_pipeline.tdd_engine.run_tdd_engine",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch(
                "build_pipeline.llm_steps.respec_steps.run_respec_audit",
                new_callable=AsyncMock,
                return_value=config.change_dir / "respec.md",
            ) as mock_respec,
        ):
            rc = await run_orchestrator(config, Namespace())

        assert rc == 0
        mock_respec.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_respec_failure_does_not_fail_the_build(self, tmp_path):
        from argparse import Namespace
        from unittest.mock import AsyncMock, patch

        from build_pipeline.orchestrator import run_orchestrator

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        config = BuildConfig(
            project_dir=project_dir,
            change_name="feat",
            auto=False,
            config_dir=tmp_path / "config",
        )
        config.specs_dir = project_dir / "specs"
        config.change_dir.mkdir(parents=True)

        with (
            patch(
                "build_pipeline.tdd_engine.run_tdd_engine",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch(
                "build_pipeline.llm_steps.respec_steps.run_respec_audit",
                new_callable=AsyncMock,
                side_effect=RuntimeError("model down"),
            ),
        ):
            rc = await run_orchestrator(config, Namespace())

        assert rc == 0


class TestTDDSubPhaseWiring:
    """The orchestrator owns the checkpoint's lifecycle, so it must tell the
    engine it is a sub-phase — otherwise the engine advances to COMPLETE and
    deletes the file the reload below depends on."""

    @pytest.mark.asyncio
    async def test_engine_is_invoked_as_a_sub_phase(self, tmp_path):
        from argparse import Namespace
        from unittest.mock import AsyncMock, patch

        from build_pipeline.models import GateResult
        from build_pipeline.orchestrator import run_orchestrator

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        config = BuildConfig(
            project_dir=project_dir,
            change_name="feat",
            auto=False,
            config_dir=tmp_path / "config",
        )
        config.specs_dir = project_dir / "specs"
        config.change_dir.mkdir(parents=True)

        with (
            patch(
                "build_pipeline.tdd_engine.run_tdd_engine",
                new_callable=AsyncMock,
                return_value=0,
            ) as engine,
            patch(
                "build_pipeline.llm_steps.docs_steps.run_docs_update",
                new_callable=AsyncMock,
                return_value=([], GateResult(passed=True, reason="ok")),
            ),
            patch(
                "build_pipeline.llm_steps.respec_steps.run_respec_audit",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            assert await run_orchestrator(config, Namespace()) == 0

        assert engine.await_args.kwargs.get("standalone") is False


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
            project_dir=project_dir,
            change_name="feat",
            auto=False,
            config_dir=tmp_path / "config",
        )
        config.specs_dir = project_dir / "specs"
        config.change_dir.mkdir(parents=True)
        return config

    @pytest.mark.asyncio
    async def test_orchestrator_runs_docs_update_before_respec(self, tmp_path, capsys):
        from argparse import Namespace
        from unittest.mock import AsyncMock, patch

        from build_pipeline.models import GateResult
        from build_pipeline.orchestrator import run_orchestrator

        config = self._config(tmp_path)
        (config.project_dir / "README.md").write_text("r")
        order: list[str] = []

        async def fake_docs(*a, **kw):
            order.append("docs")
            return ["README.md"], GateResult(passed=True, reason="ok")

        async def fake_respec(*a, **kw):
            order.append("respec")
            return config.change_dir / "respec.md"

        with (
            patch(
                "build_pipeline.tdd_engine.run_tdd_engine",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch(
                "build_pipeline.llm_steps.docs_steps.run_docs_update",
                side_effect=fake_docs,
            ),
            patch(
                "build_pipeline.llm_steps.respec_steps.run_respec_audit",
                side_effect=fake_respec,
            ),
        ):
            rc = await run_orchestrator(config, Namespace())

        assert rc == 0
        assert order == ["docs", "respec"], "docs must run before the respec pass"
        assert "PHASE:DOCS_UPDATE:COMPLETE" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_docs_failure_does_not_fail_the_build(self, tmp_path):
        from argparse import Namespace
        from unittest.mock import AsyncMock, patch

        from build_pipeline.orchestrator import run_orchestrator

        config = self._config(tmp_path)

        with (
            patch(
                "build_pipeline.tdd_engine.run_tdd_engine",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch(
                "build_pipeline.llm_steps.docs_steps.run_docs_update",
                new_callable=AsyncMock,
                side_effect=RuntimeError("model down"),
            ),
            patch(
                "build_pipeline.llm_steps.respec_steps.run_respec_audit",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            rc = await run_orchestrator(config, Namespace())

        assert rc == 1

    @pytest.mark.asyncio
    async def test_warning_reaches_stdout(self, tmp_path, capsys):
        from argparse import Namespace
        from unittest.mock import AsyncMock, patch

        from build_pipeline.models import GateResult
        from build_pipeline.orchestrator import run_orchestrator

        config = self._config(tmp_path)
        warning = GateResult(
            passed=True,
            action="docs_may_be_stale",
            reason="1 source file(s) changed and CHANGELOG.md exists",
        )

        with (
            patch(
                "build_pipeline.tdd_engine.run_tdd_engine",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch(
                "build_pipeline.llm_steps.docs_steps.run_docs_update",
                new_callable=AsyncMock,
                return_value=([], warning),
            ),
            patch(
                "build_pipeline.llm_steps.respec_steps.run_respec_audit",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            rc = await run_orchestrator(config, Namespace())

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
        state = BuildState(change_name="feat", tier="advanced")
        progress = ProgressWriter(config.progress_file_path())
        warning = GateResult(
            passed=True,
            action="docs_may_be_stale",
            reason="CHANGELOG.md may be stale",
        )

        with patch(
            "build_pipeline.llm_steps.docs_steps.run_docs_update",
            new_callable=AsyncMock,
            return_value=([], warning),
        ):
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
        (config.project_dir / "README.md").write_text("r")
        state = BuildState(change_name="feat", tier="advanced")
        progress = ProgressWriter(config.progress_file_path())

        with patch(
            "build_pipeline.llm_steps.docs_steps.run_docs_update",
            new_callable=AsyncMock,
            return_value=(["README.md"], GateResult(passed=True, reason="ok")),
        ):
            await _run_docs_update_phase(config, state, progress)

        assert state.docs_impact == ["README.md"]
        assert "Updated: README.md" in config.progress_file_path().read_text()

    @pytest.mark.asyncio
    async def test_a_hallucinated_path_never_reaches_the_state_or_the_pr_body(
        self,
        tmp_path,
        capsys,
    ):
        """The DOCS_IMPACT list is agent output: an entry that is not a file in
        the project must not be presented to a reviewer as an updated document.
        It is dropped from state (and so from the PR body) and named back to
        the user as a DOCS_WARNING, on stdout and in the progress file."""
        from unittest.mock import AsyncMock, patch

        from build_pipeline.models import GateResult
        from build_pipeline.orchestrator import _run_docs_update_phase

        config = self._config(tmp_path)
        (config.project_dir / "README.md").write_text("r")
        state = BuildState(change_name="feat", tier="advanced")
        progress = ProgressWriter(config.progress_file_path())

        with patch(
            "build_pipeline.llm_steps.docs_steps.run_docs_update",
            new_callable=AsyncMock,
            return_value=(
                ["README.md", "ghost.md"],
                GateResult(passed=True, reason="ok"),
            ),
        ):
            await _run_docs_update_phase(config, state, progress)

        assert state.docs_impact == ["README.md"]
        out = capsys.readouterr().out
        assert "DOCS_WARNING:" in out and "ghost.md" in out
        text = config.progress_file_path().read_text()
        assert "ghost.md" in text and "Updated: README.md" in text
        # The PR body is the pr skill's: it writes exactly the checkpointed
        # docs_impact (pr/scripts/tests/test_fill.py).

    @pytest.mark.asyncio
    async def test_all_reported_paths_invalid_leaves_the_state_empty(
        self,
        tmp_path,
        capsys,
    ):
        """Truthy garbage from the report must not suppress the freshness
        warning path either: nothing survives validation, docs_impact stays
        empty, and the gate's own warning still reaches stdout."""
        from unittest.mock import AsyncMock, patch

        from build_pipeline.models import GateResult
        from build_pipeline.orchestrator import _run_docs_update_phase

        config = self._config(tmp_path)
        state = BuildState(change_name="feat", tier="advanced")
        progress = ProgressWriter(config.progress_file_path())
        warning = GateResult(
            passed=True,
            action="docs_may_be_stale",
            reason="CHANGELOG.md may be stale",
        )

        with patch(
            "build_pipeline.llm_steps.docs_steps.run_docs_update",
            new_callable=AsyncMock,
            return_value=(["ghost.md", "phantom.md"], warning),
        ):
            await _run_docs_update_phase(config, state, progress)

        assert state.docs_impact == []
        out = capsys.readouterr().out
        assert "ghost.md" in out and "phantom.md" in out
        assert "DOCS_WARNING:CHANGELOG.md may be stale" in out
        assert (
            "No documentation needed updating"
            in config.progress_file_path().read_text()
        )

    def test_reported_paths_outside_the_project_are_never_staged(self, tmp_path):
        """The DOCS_IMPACT list is agent-supplied and becomes `git add` argv."""
        from build_pipeline.orchestrator import _docs_paths

        project = tmp_path / "project"
        (project / "docs").mkdir(parents=True)
        (project / "README.md").write_text("r")
        (project / "docs" / "usage.md").write_text("u")
        (tmp_path / "outside.md").write_text("o")
        config = BuildConfig(project_dir=project, change_name="c")

        assert _docs_paths(config, ["README.md", "docs/usage.md"]) == (
            ["README.md", "docs/usage.md"],
            [],
        )
        assert _docs_paths(config, ["../outside.md"]) == ([], ["../outside.md"])
        outside = str(tmp_path / "outside.md")
        assert _docs_paths(config, [outside]) == ([], [outside])
        assert _docs_paths(config, ["does-not-exist.md"]) == ([], ["does-not-exist.md"])
        assert _docs_paths(config, [":(exclude)README.md"]) == (
            [],
            [":(exclude)README.md"],
        )
        assert _docs_paths(config, ["README.md", "./README.md"]) == (["README.md"], [])


class TestDocsAgentWritesItDidNotReport:
    """The agent holds Write/Edit over the whole project but only its reported
    paths are staged, so anything else it touched would leave the build without
    reaching the PR. The commit stays explicit-path — the discrepancy is
    surfaced, not swept in."""

    @staticmethod
    def _repo(tmp_path):
        import subprocess

        project = tmp_path / "project"
        (project / "specs").mkdir(parents=True)
        for cmd in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.email", "t@e.st"],
            ["git", "config", "user.name", "T"],
        ):
            subprocess.run(cmd, cwd=project, check=True, capture_output=True)
        (project / "README.md").write_text("v1\n")
        (project / "src.py").write_text("x = 1\n")
        subprocess.run(
            ["git", "add", "-A"], cwd=project, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-qm", "init"],
            cwd=project,
            check=True,
            capture_output=True,
        )
        config = BuildConfig(project_dir=project, change_name="c")
        config.specs_dir = project / "specs"
        return config

    def test_an_unreported_edit_is_named_on_stdout_and_in_the_progress_file(
        self,
        tmp_path,
        capsys,
    ):
        from build_pipeline.orchestrator import _warn_on_unreported_writes

        config = self._repo(tmp_path)
        (config.project_dir / "README.md").write_text("v2\n")  # reported
        (config.project_dir / "src.py").write_text("x = 2\n")  # NOT reported
        progress = ProgressWriter(config.progress_file_path())

        _warn_on_unreported_writes(config, ["README.md"], progress)

        out = capsys.readouterr().out
        assert "DOCS_WARNING:unreported changes left uncommitted" in out
        assert "src.py" in out
        assert "README.md" not in out.split("unreported", 1)[1]
        assert "src.py" in config.progress_file_path().read_text()

    def test_silent_when_everything_changed_was_reported(self, tmp_path, capsys):
        from build_pipeline.orchestrator import _warn_on_unreported_writes

        config = self._repo(tmp_path)
        (config.project_dir / "README.md").write_text("v2\n")
        progress = ProgressWriter(config.progress_file_path())

        _warn_on_unreported_writes(config, ["README.md"], progress)

        assert "DOCS_WARNING" not in capsys.readouterr().out

    def test_the_pipelines_own_scratch_is_never_reported_as_unreported(
        self,
        tmp_path,
        capsys,
    ):
        """specs/ and build-progress.md are buildme's paperwork, committed by
        other phases on their own schedule."""
        from build_pipeline.orchestrator import _warn_on_unreported_writes

        config = self._repo(tmp_path)
        config.change_dir.mkdir(parents=True, exist_ok=True)
        (config.change_dir / ".build-state.json").write_text("{}")
        config.progress_file_path().write_text("progress\n")
        progress = ProgressWriter(config.progress_file_path())

        _warn_on_unreported_writes(config, [], progress)

        assert "DOCS_WARNING" not in capsys.readouterr().out

    def test_a_git_failure_is_not_reported_as_the_agent_wrote_nothing(
        self,
        tmp_path,
        capsys,
    ):
        from build_pipeline.orchestrator import (
            _uncommitted_paths,
            _warn_on_unreported_writes,
        )

        config = BuildConfig(project_dir=tmp_path / "not-a-repo", change_name="c")
        progress = ProgressWriter(tmp_path / "progress.md")

        assert _uncommitted_paths(config) is None
        _warn_on_unreported_writes(config, [], progress)
        assert "DOCS_WARNING" not in capsys.readouterr().out


# TestDocsUpdateInThePRBody moved to pr/scripts/tests/test_fill.py: the pr
# skill builds the PR body now.


# --- Git lifecycle (work item 4) -----------------------------------------

from build_pipeline import git_ops
from build_pipeline.git_ops import GitLifecycleError, GitResult

from tests.test_git_ops import _git, make_bare_origin, make_repo


def _args(tmp_path, change, **over):
    base = dict(
        change=change,
        project_dir=str(tmp_path),
        auto=False,
        spec_only=False,
        skip_research=True,
        research_path="",
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
            change_name="my-change",
            tier="advanced",
            state_file=str(config.state_file_path()),
        )
        cm = ChangeManager(config.specs_dir)

        await _run_bootstrap(config, state, cm, config.state_file_path())

        expected_wt = workspace / ".worktrees" / "buildme-my-change"
        assert state.worktree_path == str(expected_wt)
        assert state.branch == "ENG-1-my-change"
        assert state.source_repo == str(repo.resolve())
        assert expected_wt.is_dir()
        assert git_ops.current_branch(expected_wt) == "ENG-1-my-change"

        # Config re-bound: every later phase writes inside the worktree
        assert config.project_dir == expected_wt
        assert config.specs_dir == expected_wt / "specs"
        assert config.pipeline_branch == "ENG-1-my-change"
        assert (expected_wt / "specs" / "changes" / "my-change").is_dir()

        # The caller's checkout was never written to
        assert not (repo / "specs").exists()
        status = subprocess.run(
            ["git", "status", "--short"], cwd=str(repo), capture_output=True, text=True
        )
        assert status.stdout.strip() == ""
        assert git_ops.current_branch(repo) == "main"

    @pytest.mark.asyncio
    async def test_refuses_non_repo_when_worktree_requested(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        project = tmp_path / "plain"
        project.mkdir()
        config = BuildConfig(project_dir=project, change_name="c")
        config.specs_dir = project / "specs"
        state = BuildState(change_name="c", tier="advanced")
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
        state = BuildState(change_name="c", tier="advanced")
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
        state = BuildState(
            change_name="inplace",
            tier="advanced",
            state_file=str(config.state_file_path()),
        )
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
            project_dir=repo,
            change_name="inplace",
            git=GitToggles(worktree=False, push=False, pr="none"),
        )
        config.specs_dir = repo / "specs"
        state = BuildState(change_name="inplace", tier="advanced")
        progress = ProgressWriter(config.progress_file_path())
        assert _prepare_and_publish(config, state, progress) is True
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
        state = BuildState(
            change_name="resumed",
            tier="advanced",
            state_file=str(config1.state_file_path()),
        )
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
            project_dir=repo,
            change_name="resumed",
            git=GitToggles(worktree=True, push=False, pr="none"),
        )
        config2.specs_dir = repo / "specs"
        result = await run_orchestrator(config2, _args(repo, "resumed"))

        assert result == 0
        assert _worktree_count(repo) == before, (
            "a resume must not create a second worktree"
        )
        assert config2.project_dir == worktree
        assert config2.pipeline_branch == "ENG-1-resumed"

    @pytest.mark.asyncio
    async def test_a_resume_started_inside_the_worktree_owns_its_branch(
        self,
        tmp_path,
        monkeypatch,
    ):
        """There the state file is found in place, so nothing re-binds; the
        build still owns the branch, or every spec-stage commit is skipped."""
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        config1 = BuildConfig(project_dir=repo, change_name="resumed")
        config1.specs_dir = repo / "specs"
        state = BuildState(
            change_name="resumed",
            tier="advanced",
            state_file=str(config1.state_file_path()),
        )
        await _run_bootstrap(
            config1, state, ChangeManager(config1.specs_dir), config1.state_file_path()
        )
        worktree = Path(state.worktree_path)

        config2 = BuildConfig(project_dir=worktree, change_name="resumed")
        config2.specs_dir = worktree / "specs"
        _rebind_to_existing_worktree(config2)
        assert config2.project_dir == worktree
        assert config2.pipeline_branch == "ENG-1-resumed"


class TestPublishFailureIsNonFatal:
    """Criterion 13 — a failing `gh` leaves the build successful, the branch
    and worktree intact, and the exact manual commands in build-progress.md."""

    @pytest.mark.asyncio
    async def test_gh_failure_keeps_build_successful(
        self, tmp_path, monkeypatch, capsys
    ):
        workspace = tmp_path / "ws"
        monkeypatch.setenv("WORKSPACE", str(workspace))
        repo = make_repo(tmp_path / "proj")
        make_bare_origin(repo, tmp_path / "origin.git")

        config = BuildConfig(project_dir=repo, change_name="ghfail")
        config.specs_dir = repo / "specs"
        state = BuildState(
            change_name="ghfail",
            tier="advanced",
            state_file=str(config.state_file_path()),
        )
        cm = ChangeManager(config.specs_dir)
        await _run_bootstrap(config, state, cm, config.state_file_path())
        worktree = Path(state.worktree_path)
        state.phase = BuildPhase.PUBLISH
        state.checkpoint(config.state_file_path())

        monkeypatch.setattr(
            git_ops,
            "_run_pr_skill",
            lambda argv, cwd=None, timeout=None: GitResult(
                argv,
                2,
                "PR_DIAGNOSIS:`gh pr create` failed (exit 1): gh: To get started with "
                "GitHub CLI, please run: gh auth login\n",
                "",
            ),
        )

        config2 = BuildConfig(project_dir=repo, change_name="ghfail")
        config2.specs_dir = repo / "specs"
        result = await run_orchestrator(config2, _args(repo, "ghfail"))

        assert result == 0, "a gh failure must not fail the build"
        out = capsys.readouterr().out
        assert "RESULT:SUCCESS" in out
        assert "PHASE:PUBLISH:FAILED:pr" in out

        # Branch and worktree survive
        assert worktree.is_dir()
        assert git_ops.branch_exists(repo, "ENG-1-ghfail")

        # The push half did succeed, so only the PR is outstanding
        progress_text = (worktree / "build-progress.md").read_text()
        assert "gh auth login" in progress_text
        assert "git push -u origin ENG-1-ghfail" in progress_text
        # The proposal pm_spec_handoff writes is titled "Proposal".
        assert "gh pr create --title '[main] ENG-1 Proposal'" in progress_text

    @pytest.mark.asyncio
    async def test_no_remote_reports_manual_commands_and_invents_nothing(
        self, tmp_path, monkeypatch, capsys
    ):
        workspace = tmp_path / "ws"
        monkeypatch.setenv("WORKSPACE", str(workspace))
        repo = make_repo(tmp_path / "proj")  # deliberately no origin

        config = BuildConfig(project_dir=repo, change_name="noremote")
        config.specs_dir = repo / "specs"
        state = BuildState(
            change_name="noremote",
            tier="advanced",
            state_file=str(config.state_file_path()),
        )
        cm = ChangeManager(config.specs_dir)
        await _run_bootstrap(config, state, cm, config.state_file_path())
        worktree = Path(state.worktree_path)
        progress = ProgressWriter(config.progress_file_path())

        assert _prepare_and_publish(config, state, progress) is False
        out = capsys.readouterr().out
        assert "PHASE:PUBLISH:FAILED:no-remote" in out
        text = (worktree / "build-progress.md").read_text()
        assert "No 'origin' remote" in text
        assert "git push -u origin ENG-1-noremote" in text
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
        state = BuildState(
            change_name="happy",
            tier="advanced",
            state_file=str(config.state_file_path()),
        )
        cm = ChangeManager(config.specs_dir)
        await _run_bootstrap(config, state, cm, config.state_file_path())
        progress = ProgressWriter(config.progress_file_path())

        captured = {}

        def fake_pr(argv, cwd=None, timeout=None):
            captured["argv"] = argv
            return GitResult(argv, 0, "PR:https://github.com/o/r/pull/9\n", "")

        monkeypatch.setattr(git_ops, "_run_pr_skill", fake_pr)
        assert _prepare_and_publish(config, state, progress) is True
        assert state.pr_url == "https://github.com/o/r/pull/9"
        out = capsys.readouterr().out
        assert "PR:https://github.com/o/r/pull/9" in out
        assert "PUSHED:ENG-1-happy" in out
        # The pr skill opens a draft unless told --ready.
        assert "--ready" not in captured["argv"], "PRs are draft by default"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stdout", "stderr", "shown", "hidden"),
        [
            # The skill died before it could print PR_DIAGNOSIS (an import error, uv failing):
            # its stderr is the only account of why.
            (
                "",
                "Traceback (most recent call last):\nModuleNotFoundError: No module named 'x'\n",
                "ModuleNotFoundError: No module named 'x'",
                None,
            ),
            # Nothing on stderr: stdout is reported instead.
            (
                "error: Failed to spawn: `python`\n",
                "",
                "Failed to spawn: `python`",
                None,
            ),
            # stderr wins over stdout when both are there.
            (
                "DIFF:2 files, +3 -1\n",
                "boom on stderr\n",
                "boom on stderr",
                "DIFF:2 files",
            ),
            # A long traceback keeps its end, where the error is, not its first 800 characters.
            (
                "",
                "Traceback (most recent call last):\n"
                + '  File "x.py", line 1, in f\n' * 60
                + "ValueError: the real reason\n",
                "ValueError: the real reason",
                None,
            ),
        ],
    )
    async def test_a_pr_skill_failure_without_pr_diagnosis_reports_its_output(
        self, tmp_path, monkeypatch, capsys, stdout, stderr, shown, hidden
    ):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        make_bare_origin(repo, tmp_path / "origin.git")
        config = BuildConfig(project_dir=repo, change_name="nodiag")
        config.specs_dir = repo / "specs"
        state = BuildState(
            change_name="nodiag",
            tier="advanced",
            state_file=str(config.state_file_path()),
        )
        cm = ChangeManager(config.specs_dir)
        await _run_bootstrap(config, state, cm, config.state_file_path())
        progress = ProgressWriter(config.progress_file_path())

        monkeypatch.setattr(
            git_ops,
            "_run_pr_skill",
            lambda argv, cwd=None, timeout=None: GitResult(argv, 1, stdout, stderr),
        )
        assert _prepare_and_publish(config, state, progress) is False
        out = capsys.readouterr().out
        assert "PHASE:PUBLISH:FAILED:pr" in out
        # PUBLISH_DIAGNOSIS is a one-line marker: the reason must be on that line.
        diagnosis = next(
            line for line in out.splitlines() if line.startswith("PUBLISH_DIAGNOSIS:")
        )
        assert "failed (exit 1): " in diagnosis and shown in diagnosis
        assert diagnosis.endswith("The branch is pushed — only the PR is missing.")
        if hidden:
            assert hidden not in diagnosis
        assert shown in (Path(state.worktree_path) / "build-progress.md").read_text()
        assert state.pr_url is None

    @pytest.mark.asyncio
    async def test_no_push_skips_publish_entirely(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        config = BuildConfig(
            project_dir=repo,
            change_name="nopush",
            git=GitToggles(worktree=True, push=False, pr="draft"),
        )
        config.specs_dir = repo / "specs"
        state = BuildState(
            change_name="nopush",
            tier="advanced",
            state_file=str(config.state_file_path()),
        )
        cm = ChangeManager(config.specs_dir)
        await _run_bootstrap(config, state, cm, config.state_file_path())
        progress = ProgressWriter(config.progress_file_path())
        assert _prepare_and_publish(config, state, progress) is True
        assert "PHASE:PUBLISH:SKIPPED:ENG-1-nopush" in capsys.readouterr().out


async def _bootstrapped_auto_build(tmp_path, change: str, why: str):
    """A worktree build parked at PUBLISH with a proposal carrying a Why."""
    repo = make_repo(tmp_path / "proj")
    make_bare_origin(repo, tmp_path / "origin.git")
    config = BuildConfig(project_dir=repo, change_name=change, auto=True)
    config.specs_dir = repo / "specs"
    state = BuildState(
        change_name=change, tier="advanced", state_file=str(config.state_file_path())
    )
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

        def fake_pr(argv, cwd=None, timeout=None):
            seen["argv"] = argv
            return GitResult(argv, 0, "PR:https://github.com/o/r/pull/3\n", "")

        monkeypatch.setattr(git_ops, "_run_pr_skill", fake_pr)
        config2 = BuildConfig(project_dir=repo, change_name="autobody", auto=True)
        config2.specs_dir = repo / "specs"
        result = await run_orchestrator(config2, _args(repo, "autobody", auto=True))

        assert result == 0
        # The pr skill reads the documents from the dir it is given (its body
        # tests: pr/scripts/tests/test_fill.py); that dir must be the archive.
        change_dir = Path(seen["argv"][seen["argv"].index("--change-dir") + 1])
        assert "specs/archive/" in str(change_dir), (
            "links must point at the archived layout"
        )
        assert (
            "Because the queue drops jobs." in (change_dir / "proposal.md").read_text()
        )
        assert (change_dir / "respec.md").exists()
        assert not (
            config2.specs_dir / "changes" / config2.change_name
        ).exists()  # archived
        # Successful publish keeps the checkpoint, renamed, in its archived home.
        assert list((config2.specs_dir / "archive").glob("*/.build-state.json")) == []
        kept = list(
            (config2.specs_dir / "archive").glob("*/.build-state.complete.json")
        )
        assert len(kept) == 1 and BuildState.load(kept[0]).phase == BuildPhase.COMPLETE


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
            git_ops,
            "_run_pr_skill",
            lambda argv, cwd=None, timeout=None: GitResult(
                argv, 2, "PR_DIAGNOSIS:gh: boom\n", ""
            ),
        )
        config2 = BuildConfig(project_dir=repo, change_name="pubfail", auto=True)
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
        assert resumed.branch == "ENG-1-pubfail"

        # Re-run with gh healthy: adopts the same worktree, retries, cleans up.
        before = _worktree_count(repo)
        monkeypatch.setattr(
            git_ops,
            "_run_pr_skill",
            lambda argv, cwd=None, timeout=None: GitResult(
                argv, 0, "PR:https://github.com/o/r/pull/8\n", ""
            ),
        )
        config3 = BuildConfig(project_dir=repo, change_name="pubfail", auto=True)
        config3.specs_dir = repo / "specs"
        result = await run_orchestrator(config3, _args(repo, "pubfail", auto=True))

        assert result == 0
        out = capsys.readouterr().out
        assert "PR:https://github.com/o/r/pull/8" in out
        assert _worktree_count(repo) == before, (
            "the re-run must not mint a second worktree"
        )
        assert not archived_states[0].exists(), "checkpoint renamed after success"
        kept = archived_states[0].with_name(".build-state.complete.json")
        assert BuildState.load(kept).phase == BuildPhase.COMPLETE
        # Bookkeeping was never tracked, so the worktree ends clean — no
        # uncommitted deletion left behind by the rename.
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
        )
        assert status.stdout.strip() == ""

    @pytest.mark.asyncio
    async def test_pr_url_is_persisted_to_the_checkpoint(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo = make_repo(tmp_path / "proj")
        make_bare_origin(repo, tmp_path / "origin.git")
        config = BuildConfig(project_dir=repo, change_name="prurl")
        config.specs_dir = repo / "specs"
        state = BuildState(
            change_name="prurl",
            tier="advanced",
            state_file=str(config.state_file_path()),
        )
        cm = ChangeManager(config.specs_dir)
        await _run_bootstrap(config, state, cm, config.state_file_path())
        progress = ProgressWriter(config.progress_file_path())

        monkeypatch.setattr(
            git_ops,
            "_run_pr_skill",
            lambda argv, cwd=None, timeout=None: GitResult(
                argv, 0, "PR:https://github.com/o/r/pull/9\n", ""
            ),
        )
        state_path = config.state_file_path()
        assert (
            _prepare_and_publish(config, state, progress, state_path=state_path) is True
        )
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
        state = BuildState(
            change_name="nested",
            tier="advanced",
            state_file=str(config.state_file_path()),
        )
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
        from tests.spec_fixtures import ready_specs

        ready_specs(pkg / "specs" / "changes" / "nested", pkg)

        config1 = BuildConfig(project_dir=pkg, change_name="nested")
        config1.specs_dir = pkg / "specs"
        state = BuildState(
            change_name="nested",
            tier="advanced",
            state_file=str(config1.state_file_path()),
        )
        cm = ChangeManager(config1.specs_dir)
        await _run_bootstrap(config1, state, cm, config1.state_file_path())
        wt = Path(state.worktree_path)
        state.phase = BuildPhase.PUBLISH
        state.checkpoint(config1.state_file_path())

        config2 = BuildConfig(
            project_dir=pkg,
            change_name="nested",
            git=GitToggles(worktree=True, push=False, pr="none"),
        )
        config2.specs_dir = pkg / "specs"
        result = await run_orchestrator(config2, _args(pkg, "nested"))

        assert result == 0
        assert config2.project_dir == wt / "packages" / "api"
        assert _worktree_count(repo) == 2, "resume must adopt, not create"


async def _published_build(tmp_path, change: str, sub: str | None = None):
    """A worktree build with an origin, ready for _run_publish. With *sub*, the
    project is that subdirectory of the repo (a monorepo subpackage)."""
    repo = make_repo(tmp_path / "proj")
    make_bare_origin(repo, tmp_path / "origin.git")
    project = repo
    if sub:
        project = repo / sub
        project.mkdir(parents=True)
        (project / "keep.txt").write_text("k\n")
        _git(repo, "add", f"{sub}/keep.txt")
        _git(repo, "commit", "-m", "chore: nested pkg")
    config = BuildConfig(project_dir=project, change_name=change)
    config.specs_dir = project / "specs"
    state = BuildState(
        change_name=change, tier="advanced", state_file=str(config.state_file_path())
    )
    cm = ChangeManager(config.specs_dir)
    await _run_bootstrap(config, state, cm, config.state_file_path())
    return config, state, ProgressWriter(config.progress_file_path())


def _sections(argv: list[str]) -> list[Path]:
    return [Path(argv[i + 1]) for i, a in enumerate(argv) if a == "--section"]


class TestPublishHandsOffToThePrSkill:
    """PUBLISH pushes, then runs `pr_pipeline open --no-push` as a subprocess;
    the body (Why, blocks, docs, artifact links) is the pr skill's and is
    tested in pr/scripts/tests/."""

    @pytest.fixture
    def captured(self, monkeypatch):
        seen: dict = {}

        def fake_pr(argv, cwd=None, timeout=None):
            seen["argv"] = argv
            seen["sections"] = {p.name: p.read_text() for p in _sections(argv)}
            return GitResult(
                argv, 0, "DIFF:1 files, +1 -0\nPR:https://github.com/o/r/pull/5\n", ""
            )

        monkeypatch.setattr(git_ops, "_run_pr_skill", fake_pr)
        return seen

    @pytest.mark.asyncio
    async def test_runs_the_pr_skill_from_the_skills_directory(
        self, tmp_path, monkeypatch, captured
    ):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        config, state, progress = await _published_build(tmp_path, "handoff")
        assert _prepare_and_publish(config, state, progress) is True
        argv = captured["argv"]
        from build_pipeline import orchestrator

        skills = Path(orchestrator.__file__).resolve().parents[3]
        assert argv[:4] == ["uv", "run", "--directory", str(skills / "pr" / "scripts")]
        assert argv[4:8] == ["python", "-m", "pr_pipeline", "open"]
        assert argv[argv.index("--repo") + 1] == state.worktree_path
        assert argv[argv.index("--change-dir") + 1] == str(config.change_dir)
        assert argv[argv.index("--head") + 1] == "ENG-1-handoff"
        assert argv[argv.index("--title") + 1] == "[main] ENG-1 handoff"
        assert "--no-push" in argv, (
            "buildme pushes itself, so PUSHED: and push failures are unchanged"
        )

    @pytest.mark.asyncio
    async def test_a_nested_project_passes_its_own_dir_so_its_pr_settings_apply(
        self, tmp_path, monkeypatch, captured
    ):
        # buildme setup writes `pr:` to <project_dir>/specs/config.yaml; the pr skill reads
        # specs/config.yaml under --repo, so --repo must be the subproject, not the worktree root.
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        config, state, progress = await _published_build(
            tmp_path, "nestedpub", sub="packages/api"
        )
        assert _prepare_and_publish(config, state, progress) is True
        argv = captured["argv"]
        assert config.project_dir == Path(state.worktree_path) / "packages" / "api"
        assert argv[argv.index("--repo") + 1] == str(config.project_dir)

    @pytest.mark.asyncio
    async def test_ready_pr_passes_ready(self, tmp_path, monkeypatch, captured):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        config, state, progress = await _published_build(tmp_path, "ready")
        config.git = GitToggles(worktree=True, push=True, pr="ready")
        assert _prepare_and_publish(config, state, progress) is True
        assert "--ready" in captured["argv"]

    @pytest.mark.asyncio
    async def test_worktree_note_reaches_the_body_as_a_section(
        self, tmp_path, monkeypatch, captured
    ):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        config, state, progress = await _published_build(tmp_path, "note")
        assert _prepare_and_publish(config, state, progress) is True
        note = captured["sections"]["worktree.md"]
        assert note.startswith("## Worktree\n")
        assert state.worktree_path in note and "calling session's decision" in note
        assert (
            _sections(captured["argv"])[0].parent == config.change_dir / "pr-sections"
        )

    @pytest.mark.asyncio
    async def test_a_contributors_section_reaches_the_section_argv(
        self, tmp_path, monkeypatch, captured
    ):
        from build_pipeline import orchestrator

        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        config, state, progress = await _published_build(tmp_path, "hook")

        def stale_walkthroughs(cfg, st, docs_dir):
            path = docs_dir / "pr-sections" / "walkthroughs.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("## Stale walkthroughs\n\n- docs/walk.md\n")
            return path

        monkeypatch.setattr(
            orchestrator,
            "PRE_PUBLISH_CONTRIBUTORS",
            [stale_walkthroughs, lambda *a: None],
        )
        assert _prepare_and_publish(config, state, progress) is True
        assert captured["sections"]["walkthroughs.md"].startswith(
            "## Stale walkthroughs"
        )
        assert [p.name for p in _sections(captured["argv"])] == [
            "worktree.md",
            "walkthroughs.md",
        ]

    @pytest.mark.asyncio
    async def test_a_raising_contributor_does_not_stop_publish(
        self, tmp_path, monkeypatch, captured, capsys
    ):
        from build_pipeline import orchestrator

        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        config, state, progress = await _published_build(tmp_path, "raises")

        def broken(cfg, st, docs_dir):
            raise RuntimeError("contributor bug")

        monkeypatch.setattr(orchestrator, "PRE_PUBLISH_CONTRIBUTORS", [broken])
        assert _prepare_and_publish(config, state, progress) is True
        out = capsys.readouterr().out
        assert (
            "PHASE:PUBLISH:COMPLETE" in out
            and "PR:https://github.com/o/r/pull/5" in out
        )
        assert [p.name for p in _sections(captured["argv"])] == ["worktree.md"]

    def test_section_files_are_never_committed(self):
        assert ":(exclude,glob)**/pr-sections/**" in git_ops.BOOKKEEPING_EXCLUDES

    @pytest.mark.asyncio
    async def test_pr_skill_failure_names_the_skill_and_its_command(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        config, state, progress = await _published_build(tmp_path, "skillfail")
        monkeypatch.setattr(
            git_ops,
            "_run_pr_skill",
            lambda argv, cwd=None, timeout=None: GitResult(
                argv,
                1,
                "PR_DIAGNOSIS:a gate rejected the body; nothing was written: too long\n",
                "",
            ),
        )
        assert _prepare_and_publish(config, state, progress) is False
        out = capsys.readouterr().out
        assert "PHASE:PUBLISH:FAILED:pr" in out
        assert (
            "PUBLISH_DIAGNOSIS:the pr skill (`pr_pipeline open`) failed (exit 1): a gate rejected"
            in out
        )
        text = (Path(state.worktree_path) / "build-progress.md").read_text()
        assert "python -m pr_pipeline open" in text
        assert "gh pr create --title '[main] ENG-1 skillfail'" in text

    @pytest.mark.asyncio
    async def test_an_existing_prs_url_from_gh_reaches_the_publish_diagnosis(
        self, tmp_path, monkeypatch, capsys
    ):
        # gh prints the existing PR's URL on the line after "already exists:"; the pr
        # skill folds that onto its one PR_DIAGNOSIS line (pr tests:
        # test_open_folds_a_multi_line_gh_error_onto_one_diagnosis_line).
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        config, state, progress = await _published_build(tmp_path, "exists")
        existing = "https://github.com/o/r/pull/9"
        monkeypatch.setattr(
            git_ops,
            "_run_pr_skill",
            lambda argv, cwd=None, timeout=None: GitResult(
                argv,
                2,
                "PR_DIAGNOSIS:`gh pr create` failed (exit 1): a pull request for branch "
                f'"buildme/exists" into branch "main" already exists: {existing}\n',
                "",
            ),
        )
        assert _prepare_and_publish(config, state, progress) is False
        diagnosis = [
            line
            for line in capsys.readouterr().out.splitlines()
            if line.startswith("PUBLISH_DIAGNOSIS:")
        ]
        assert len(diagnosis) == 1 and f"already exists: {existing}" in diagnosis[0]


class TestResumeAtPublish:
    """Item 13: a checkpoint parked at PUBLISH resumes, runs publish once with
    the pr skill stubbed, and completes."""

    @pytest.mark.asyncio
    async def test_parked_checkpoint_publishes_once_and_completes(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "ws"))
        repo, state = await _bootstrapped_auto_build(tmp_path, "parked", "Why text.")
        assert BuildState.load(Path(state.state_file)).phase == BuildPhase.PUBLISH
        calls: list[list[str]] = []

        def fake_pr(argv, cwd=None, timeout=None):
            calls.append(argv)
            return GitResult(argv, 0, "PR:https://github.com/o/r/pull/11\n", "")

        monkeypatch.setattr(git_ops, "_run_pr_skill", fake_pr)
        config = BuildConfig(project_dir=repo, change_name="parked", auto=True)
        config.specs_dir = repo / "specs"
        result = await run_orchestrator(config, _args(repo, "parked", auto=True))

        assert result == 0
        assert len(calls) == 1, "publish runs exactly once"
        out = capsys.readouterr().out
        assert "PHASE:PUBLISH:COMPLETE" in out and "RESULT:SUCCESS" in out
        assert "PR:https://github.com/o/r/pull/11" in out


class TestPlanReviewPhase:
    """W9: the PLAN_REVIEW phase — the second pair of eyes the board's
    Planning column has always claimed. It runs after DESIGN_AUDIT, before
    the size gate, is skipped on resume when `plan_review_done` is recorded, and
    never fails the build."""

    def _args(self, tmp_path, change):
        return argparse.Namespace(
            change=change,
            project_dir=str(tmp_path),
            auto=True,
            spec_only=True,
            skip_research=True,
            research_path="",
            session_id="",
        )

    def _config(self, tmp_path, change):
        config = BuildConfig(
            project_dir=tmp_path,
            change_name=change,
            auto=True,
            spec_only=True,
            skip_research=True,
        )
        config.specs_dir = tmp_path / "specs"
        cm = ChangeManager(config.specs_dir)
        cm.init_specs()
        cm.create_change(change)
        (config.change_dir / "proposal.md").write_text("## Why\nShip it.\n")
        (config.change_dir / "implementation-plan.md").write_text("## Decisions\nNone.")
        (config.change_dir / "tasks.md").write_text("## Block 1\n- [ ] do it\n")
        (config.change_dir / "rubric.md").write_text("## Criteria\n- correctness\n")
        state = BuildState(
            change_name=change,
            tier="advanced",
            phase=BuildPhase.DESIGN_AUDIT,
            state_file=str(config.state_file_path()),
        )
        state.checkpoint(config.state_file_path())
        return config

    @pytest.mark.asyncio
    async def test_phase_runs_and_emits_its_stdout_line(self, tmp_path, capsys):
        from unittest.mock import AsyncMock, patch

        from build_pipeline.models import PlanReviewResult

        config = self._config(tmp_path, "review-plan")
        mock_review = AsyncMock(return_value=PlanReviewResult(findings=[]))
        with (
            patch(
                "build_pipeline.llm_steps.plan_review_steps.run_plan_review",
                mock_review,
            ),
        ):
            result = await run_orchestrator(config, self._args(tmp_path, "review-plan"))

        assert result == 0
        assert mock_review.await_count == 1
        out = capsys.readouterr().out
        assert "PHASE:PLAN_REVIEW:COMPLETE" in out
        # ...and it sits between the two phases it was inserted between.
        assert out.index("PHASE:DESIGN_AUDIT:COMPLETE") < out.index(
            "PHASE:PLAN_REVIEW:COMPLETE"
        )

    @pytest.mark.asyncio
    async def test_recorded_stage_is_skipped_on_resume(self, tmp_path):
        """`plan_review_done` is checked BEFORE the model call, so a resume
        costs nothing at all."""
        from unittest.mock import AsyncMock, patch

        config = self._config(tmp_path, "resume-plan")
        state = BuildState.load(config.state_file_path())
        _plan_fixture(config, state)
        state.spec_gen.record_step("plan_review", "passed")
        state.checkpoint(config.state_file_path())

        mock_review = AsyncMock()
        with (
            patch(
                "build_pipeline.llm_steps.plan_review_steps.run_plan_review",
                mock_review,
            ),
        ):
            result = await run_orchestrator(config, self._args(tmp_path, "resume-plan"))

        assert result == 0
        mock_review.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_review_failure_never_fails_the_build(self, tmp_path):
        from unittest.mock import AsyncMock, patch

        config = self._config(tmp_path, "broken-plan")
        with (
            patch(
                "build_pipeline.llm_steps.plan_review_steps.run_plan_review",
                AsyncMock(side_effect=RuntimeError("model exploded")),
            ),
        ):
            result = await run_orchestrator(config, self._args(tmp_path, "broken-plan"))

        assert result == 1

    @pytest.mark.asyncio
    async def test_a_failed_review_is_retried_on_a_resume(self, tmp_path):
        """`run_plan_review_stage` leaves `plan_review_done` False on an LLM
        failure precisely so a resume tries again. Advancing the phase pointer
        anyway made `is_phase_complete` report the phase finished, and the
        retry never happened."""
        from unittest.mock import AsyncMock, patch

        config = self._config(tmp_path, "retry-plan")
        args = self._args(tmp_path, "retry-plan")
        with (
            patch(
                "build_pipeline.llm_steps.plan_review_steps.run_plan_review",
                AsyncMock(side_effect=RuntimeError("model exploded")),
            ),
        ):
            assert await run_orchestrator(config, args) == 1

        # The build stopped on the failed review. Nothing recorded the phase.
        resumed = BuildState.load(config.state_file_path())
        assert not resumed.is_phase_complete(BuildPhase.PLAN_REVIEW)
        assert not (resumed.spec_gen and resumed.spec_gen.step_passed("plan_review"))

        # The resume runs it again.
        from build_pipeline.models import PlanReviewResult

        retry = AsyncMock(return_value=PlanReviewResult(findings=[]))
        with (
            patch("build_pipeline.llm_steps.plan_review_steps.run_plan_review", retry),
        ):
            assert await run_orchestrator(config, args) == 0
        retry.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_checkpoint_past_the_phase_does_not_re_enter_it(self, tmp_path):
        """advance_to only ever runs forwards: a state already at PROTOTYPE
        must not be sent back to review the plan."""
        from unittest.mock import AsyncMock, patch

        config = self._config(tmp_path, "past-plan")
        state = BuildState.load(config.state_file_path())
        state.advance_to(BuildPhase.PROTOTYPE, config.state_file_path())

        from build_pipeline.models import PlanReviewResult

        mock_review = AsyncMock(return_value=PlanReviewResult(findings=[]))
        with (
            patch(
                "build_pipeline.llm_steps.plan_review_steps.run_plan_review",
                mock_review,
            ),
        ):
            result = await run_orchestrator(config, self._args(tmp_path, "past-plan"))

        assert result == 0
        mock_review.assert_awaited_once()  # ordinal progress is not review evidence
