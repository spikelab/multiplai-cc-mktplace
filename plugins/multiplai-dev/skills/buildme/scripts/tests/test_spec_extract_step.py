"""PR 4 of the reverse-specs plan: buildme runs spec-extract inside CODEBASE_ANALYSIS.

`orchestrator._run_spec_extract_step` launches `spec_extract_pipeline run ... --apply`
through `git_ops._run_spec_extract_skill`, which every test here patches: no test
starts a real spec-extract. Rows S1, S3 and S5 of the plan's stop-and-resume table
each stop the step, resume it from the saved checkpoint, and compare with a run
that never stopped.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
from pathlib import Path

import pytest

from build_pipeline import git_ops, orchestrator
from build_pipeline.change_manager import ChangeManager
from build_pipeline.config import BuildConfig, GitToggles
from build_pipeline.models import BuildPhase
from build_pipeline.orchestrator import _run_spec_extract_step, run_orchestrator
from build_pipeline.progress import ProgressWriter
from build_pipeline.state import BuildState, SpecGenState

CAPABILITY = "## ADDED Requirements\n\n### Requirement: Billing totals\n\n#### Scenario: sum\n" \
             "- **WHEN** two lines are billed\n- **THEN** the total is their sum\n" \
             "- **CITES** `app.py:1-2`\n- **EVIDENCE** read from code only\n"


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def _repo(root: Path) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    (root / "app.py").write_text("def total(a, b):\n    return a + b\n")
    _git(root, "add", "app.py")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _config(project: Path, *, branch: str | None = "buildme/c", **kw) -> BuildConfig:
    config = BuildConfig(project_dir=project, change_name="c", **kw)
    config.specs_dir = project / "specs"
    ChangeManager(config.specs_dir).create_change("c")
    (config.specs_dir / "registry").mkdir(exist_ok=True)
    config.pipeline_branch = branch
    return config


def _state(config: BuildConfig) -> BuildState:
    state = BuildState(change_name="c", mode="scratch", tier="advanced", phase=BuildPhase.RESEARCH,
                       bootstrap_done=True, state_file=str(config.state_file_path()))
    state.checkpoint(config.state_file_path())
    return state


def _progress(tmp_path: Path) -> ProgressWriter:
    return ProgressWriter(tmp_path / "progress.md")


class FakeSpecExtract:
    """`git_ops._run_spec_extract_skill`: records each argv and answers from *steps*,
    one per call (the last repeats). A step is an exit code, an exception to raise,
    or a callable run first (it may write files) whose return is the exit code."""

    def __init__(self, *steps, stdout: str = "", stderr: str = "spec-extract: refused"):
        self.steps = list(steps) or [0]
        self.calls: list[list[str]] = []
        self.stdout, self.stderr = stdout, stderr

    def __call__(self, argv, cwd=None, timeout=None):
        self.calls.append(list(argv))
        step = self.steps[min(len(self.calls), len(self.steps)) - 1]
        if isinstance(step, BaseException):
            raise step
        code = step() if callable(step) else step
        return git_ops.GitResult(list(argv), code, self.stdout if code == 0 else "",
                                 "" if code == 0 else self.stderr)


@pytest.fixture(autouse=True)
def trusted(monkeypatch):
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")


def _apply_into(config: BuildConfig):
    """What `--apply` leaves: one new registry file, uncommitted."""
    def write():
        registry = config.specs_dir / "registry"
        registry.mkdir(parents=True, exist_ok=True)
        (registry / "billing.md").write_text(CAPABILITY)
        return 0
    return write


def _flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


# --- config ------------------------------------------------------------------------------------


class TestConfigKeys:
    def _load(self, tmp_path, yaml_text: str) -> BuildConfig:
        (tmp_path / "specs").mkdir(exist_ok=True)
        (tmp_path / "specs" / "config.yaml").write_text(yaml_text)
        config = BuildConfig(project_dir=tmp_path)
        config.specs_dir = tmp_path / "specs"
        config._load_specs_config()
        return config

    def test_defaults_are_on_and_80_usd(self, tmp_path):
        c = self._load(tmp_path, "context: x\n")
        assert (c.spec_extract, c.spec_extract_max_usd) == (True, 80.0)

    def test_values(self, tmp_path):
        c = self._load(tmp_path, "spec_extract: false\nspec_extract_max_usd: 12.5\n")
        assert (c.spec_extract, c.spec_extract_max_usd) == (False, 12.5)

    def test_invalid_values_keep_the_defaults(self, tmp_path, caplog):
        c = self._load(tmp_path, "spec_extract: 'no thanks'\nspec_extract_max_usd: 0\n")
        assert (c.spec_extract, c.spec_extract_max_usd) == (True, 80.0)
        assert sum("Invalid spec_extract" in r.getMessage() for r in caplog.records) == 2

    def test_a_boolean_is_not_a_spend_cap(self, tmp_path):
        assert self._load(tmp_path, "spec_extract_max_usd: true\n").spec_extract_max_usd == 80.0


# --- the command -------------------------------------------------------------------------------


class TestArgv:
    def test_runs_spec_extract_at_the_saved_head_with_apply(self, tmp_path, monkeypatch):
        project = _repo(tmp_path / "proj")
        config = _config(project, spec_extract_max_usd=25.0)
        state = _state(config)
        fake = FakeSpecExtract(0)
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill", fake)

        assert _run_spec_extract_step(config, state, _progress(tmp_path)) is None

        [argv] = fake.calls
        head = _git(project, "rev-parse", "HEAD")
        assert argv[:3] == ["uv", "run", "--directory"]
        assert Path(argv[3]) == Path(orchestrator.__file__).resolve().parents[3] / "spec-extract" / "scripts"
        assert argv[4:8] == ["python", "-m", "spec_extract_pipeline", "run"]
        assert _flag(argv, "--tree") == head
        assert Path(_flag(argv, "--repo")).resolve() == project.resolve()
        assert "--path" not in argv
        assert _flag(argv, "--max-usd") == "25"
        assert "--trust-repo" in argv and "--apply" in argv
        assert _flag(argv, "--specs-dir") == str(config.specs_dir)

    @pytest.mark.parametrize("cap, passed", [(1234.567, "1234.567"), (1234567.0, "1234567"), (0.25, "0.25")])
    def test_the_spend_cap_is_passed_exactly(self, cap, passed, tmp_path, monkeypatch):
        config = _config(_repo(tmp_path / "proj"), spec_extract_max_usd=cap)
        fake = FakeSpecExtract(0)
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill", fake)
        assert _run_spec_extract_step(config, _state(config), _progress(tmp_path)) is None
        assert _flag(fake.calls[0], "--max-usd") == passed

    def test_a_subdirectory_project_reads_only_its_path(self, tmp_path, monkeypatch):
        """spec-extract puts `--repo`'s own prefix in front of `--path`
        (`target.tree_root`), so the subdirectory goes in `--path` and `--repo`
        is the top of the checkout: the path is never doubled."""
        top = _repo(tmp_path / "mono")
        project = top / "packages" / "api"
        project.mkdir(parents=True)
        (project / "svc.py").write_text("def f():\n    return 1\n")
        _git(top, "add", ".")
        _git(top, "commit", "-q", "-m", "api")
        config = _config(project)
        state = _state(config)
        fake = FakeSpecExtract(0)
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill", fake)

        _run_spec_extract_step(config, state, _progress(tmp_path))

        [argv] = fake.calls
        assert Path(_flag(argv, "--repo")).resolve() == top.resolve()
        assert _flag(argv, "--path") == "packages/api"
        assert _flag(argv, "--tree") == _git(top, "rev-parse", "HEAD")

    def test_the_seam_runs_the_launcher_with_the_four_hour_limit(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(git_ops, "_run_skill",
                            lambda argv, cwd, timeout: seen.update(argv=argv, timeout=timeout))
        git_ops._run_spec_extract_skill(["uv"], cwd=None)
        assert seen == {"argv": ["uv"], "timeout": 4 * 3600}


class TestSkips:
    def _skipped(self, config, state, tmp_path, monkeypatch, caplog, reason):
        fake = FakeSpecExtract(0)
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill", fake)
        with caplog.at_level(logging.INFO):
            assert _run_spec_extract_step(config, state, _progress(tmp_path)) is None
        assert fake.calls == []
        assert any(f"SKIP phase=CODEBASE_ANALYSIS step=spec-extract reason={reason}" in r.getMessage()
                   for r in caplog.records)
        assert state.spec_gen is None or state.spec_gen.spec_extract_sha is None

    def test_only_mode(self, tmp_path, monkeypatch, caplog):
        config = _config(_repo(tmp_path / "p"), mode="only")
        self._skipped(config, _state(config), tmp_path, monkeypatch, caplog, "only-mode")

    def test_switched_off(self, tmp_path, monkeypatch, caplog):
        config = _config(_repo(tmp_path / "p"), spec_extract=False)
        self._skipped(config, _state(config), tmp_path, monkeypatch, caplog, "spec_extract-false")

    def test_untrusted_repo(self, tmp_path, monkeypatch, caplog):
        monkeypatch.delenv("BUILDME_TRUST_REPO")
        config = _config(_repo(tmp_path / "p"))
        self._skipped(config, _state(config), tmp_path, monkeypatch, caplog, "repo-not-trusted")

    def test_a_registry_that_already_holds_a_capability(self, tmp_path, monkeypatch, caplog):
        config = _config(_repo(tmp_path / "p"))
        (config.specs_dir / "registry" / "auth.md").write_text(CAPABILITY)
        self._skipped(config, _state(config), tmp_path, monkeypatch, caplog, "registry-not-empty")

    def test_a_project_with_no_commit(self, tmp_path, monkeypatch, caplog):
        project = tmp_path / "p"
        project.mkdir()
        _git(project, "init", "-q")
        config = _config(project)
        self._skipped(config, _state(config), tmp_path, monkeypatch, caplog, "no-git-commit")


class TestExits:
    def test_exit_0_commits_the_registry_and_prints_the_baseline(self, tmp_path, monkeypatch, capsys):
        project = _repo(tmp_path / "p")
        config = _config(project)
        state = _state(config)
        fake = FakeSpecExtract(_apply_into(config), stdout="SPEC_EXTRACT:RENDERED 1 x\nSPEC_EXTRACT:APPLIED 1\n")
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill", fake)

        assert _run_spec_extract_step(config, state, _progress(tmp_path)) is None

        sha12 = state.spec_gen.spec_extract_sha[:12]
        assert _git(project, "log", "-1", "--format=%s") == \
            f"docs(specs): baseline registry from spec-extract at {sha12}"
        assert _git(project, "show", "HEAD:specs/registry/billing.md") + "\n" == CAPABILITY
        assert _git(project, "status", "--porcelain", "--", "specs/registry") == ""
        assert "SPEC_EXTRACT:BASELINE 1" in capsys.readouterr().out

    def test_exit_5_returns_1(self, tmp_path, monkeypatch, capsys):
        config = _config(_repo(tmp_path / "p"))
        state = _state(config)
        fake = FakeSpecExtract(5, stderr="spec-extract: another run holds /x/.locks/tree.lock (pid 7)")
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill", fake)

        assert _run_spec_extract_step(config, state, _progress(tmp_path)) == 1
        out = capsys.readouterr().out
        assert "SPEC_EXTRACT:RUNNING" in out and "pid 7" in out

    @pytest.mark.parametrize("code", [1, 2, 3, 4])
    def test_another_exit_with_a_clean_registry_goes_on_without_a_baseline(
            self, code, tmp_path, monkeypatch, capsys):
        """Row S7."""
        project = _repo(tmp_path / "p")
        config = _config(project)
        state = _state(config)
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill", FakeSpecExtract(code, stderr="a\nlast words"))
        before = _git(project, "rev-parse", "HEAD")

        assert _run_spec_extract_step(config, state, _progress(tmp_path)) is None

        assert f"SPEC_EXTRACT:NOT_RUN exit={code} last words" in capsys.readouterr().out
        assert _git(project, "rev-parse", "HEAD") == before

    def test_a_missing_registry_counts_as_clean(self, tmp_path, monkeypatch, capsys):
        config = _config(_repo(tmp_path / "p"))
        state = _state(config)
        (config.specs_dir / "registry").rmdir()
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill", FakeSpecExtract(1))
        assert _run_spec_extract_step(config, state, _progress(tmp_path)) is None
        assert "SPEC_EXTRACT:NOT_RUN exit=1" in capsys.readouterr().out

    def test_another_exit_with_a_dirty_registry_returns_1(self, tmp_path, monkeypatch, capsys):
        """Row S6: the user's uncommitted edits under the registry stop the build."""
        project = _repo(tmp_path / "p")
        config = _config(project)
        state = _state(config)
        state.spec_gen = SpecGenState()
        state.spec_gen.spec_extract_sha = _git(project, "rev-parse", "HEAD")
        (config.specs_dir / "registry" / "mine.md").write_text("my edit\n")
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill", FakeSpecExtract(2))

        assert _run_spec_extract_step(config, state, _progress(tmp_path)) == 1

        out = capsys.readouterr()
        text = out.out + out.err
        assert "mine.md" in text and "spec_extract: false" in text
        assert (config.specs_dir / "registry" / "mine.md").read_text() == "my edit\n"


# --- the block -----------------------------------------------------------------------------------


class TestInTheBlock:
    """The step runs inside CODEBASE_ANALYSIS, after the analysis and before the phase advances."""

    @pytest.fixture(autouse=True)
    def _one_change(self, monkeypatch):
        from unittest.mock import AsyncMock

        from build_pipeline.epic import split as epic_split

        answer = epic_split.EpicSplitAnswer(single=True, reason="one change")
        monkeypatch.setattr(epic_split, "epic_split", AsyncMock(return_value=answer))

    def _run(self, tmp_path, fake, monkeypatch):
        from unittest.mock import AsyncMock, patch

        project = _repo(tmp_path / "p")
        config = BuildConfig(project_dir=project, change_name="c", mode="scratch", auto=True,
                             spec_only=True, skip_research=True, git=GitToggles(worktree=False))
        config.specs_dir = project / "specs"
        config.prototype_mode = "false"
        cm = ChangeManager(config.specs_dir)
        cm.init_specs()
        cm.create_change("c")
        (config.change_dir / "proposal.md").write_text("## Why\nA thing.\n")
        _state(config)
        args = argparse.Namespace(mode="scratch", change="c", project_dir=str(project), auto=True,
                                  spec_only=True, skip_research=True, interview_summary="",
                                  research_path="", context_files=[], session_id="")
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill", fake)
        with patch("build_pipeline.llm_steps.spec_steps.run_codebase_analysis",
                   AsyncMock(return_value="## Architecture\nx")), \
                patch("build_pipeline.spec_generator.run_spec_generator", AsyncMock(return_value=0)), \
                patch("build_pipeline.llm_steps.spec_steps.run_design_audit", AsyncMock(return_value=[])):
            import asyncio
            rc = asyncio.run(run_orchestrator(config, args))
        return rc, config

    def test_exit_5_stops_the_build_without_advancing_the_phase(self, tmp_path, monkeypatch, capsys):
        """Row S2."""
        rc, config = self._run(tmp_path, FakeSpecExtract(5), monkeypatch)
        assert rc == 1
        state = BuildState.load(config.state_file_path())
        assert not state.is_phase_complete(BuildPhase.CODEBASE_ANALYSIS)
        assert state.spec_gen.spec_extract_sha
        assert "PHASE:CODEBASE_ANALYSIS:COMPLETE" not in capsys.readouterr().out

    def test_a_clean_failure_completes_the_phase(self, tmp_path, monkeypatch, capsys):
        rc, config = self._run(tmp_path, FakeSpecExtract(2), monkeypatch)
        assert rc == 0
        out = capsys.readouterr().out
        assert "SPEC_EXTRACT:NOT_RUN exit=2" in out
        assert out.index("SPEC_EXTRACT:NOT_RUN") < out.index("PHASE:CODEBASE_ANALYSIS:COMPLETE")


# --- stop and resume (rows S1, S3, S5) -----------------------------------------------------------


def _resume(config: BuildConfig, tmp_path: Path):
    """A new buildme process: the state comes back from its checkpoint."""
    return _run_spec_extract_step(config, BuildState.load(config.state_file_path()), _progress(tmp_path))


class TestStopAndResume:
    def _reference(self, tmp_path, monkeypatch, steps=(0,)):
        project = _repo(tmp_path / "ref")
        config = _config(project)
        fake = FakeSpecExtract(*steps)
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill", fake)
        assert _run_spec_extract_step(config, _state(config), _progress(tmp_path)) is None
        return project, fake

    def _argv_in(self, argv: list[str], project: Path) -> list[str]:
        """*argv* with the fixture's own directory and HEAD written as `<p>` and `<head>`,
        so the commands of two fixture repos compare."""
        head = _git(project, "rev-parse", "HEAD")
        return [a.replace(str(project), "<p>").replace(head, "<head>") for a in argv]

    def test_S1_a_stopped_launch_is_resumed_at_the_saved_commit_in_the_same_folder(
            self, tmp_path, monkeypatch):
        """Row S1, stopped twice: each resume launches the same command, so spec-extract
        resumes its own folder and pays only for what had not returned."""
        ref_project, ref = self._reference(tmp_path, monkeypatch)
        project = _repo(tmp_path / "p")
        config = _config(project)
        fake = FakeSpecExtract(KeyboardInterrupt(), KeyboardInterrupt(), 0)
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill", fake)

        with pytest.raises(KeyboardInterrupt):
            _run_spec_extract_step(config, _state(config), _progress(tmp_path))
        with pytest.raises(KeyboardInterrupt):
            _resume(config, tmp_path)
        assert _resume(config, tmp_path) is None

        head = _git(project, "rev-parse", "HEAD")
        assert [_flag(a, "--tree") for a in fake.calls] == [head] * 3
        assert len({tuple(a) for a in fake.calls}) == 1
        assert self._argv_in(fake.calls[-1], project) == self._argv_in(ref.calls[0], ref_project)
        assert BuildState.load(config.state_file_path()).spec_gen.spec_extract_sha == head

    def test_S5_a_commit_added_before_the_resume_does_not_move_the_target(self, tmp_path, monkeypatch):
        """Row S5: the resume uses the saved commit, never the new HEAD, so it reaches
        the folder the stopped run paid into instead of starting a second one."""
        project = _repo(tmp_path / "p")
        config = _config(project)
        first = _git(project, "rev-parse", "HEAD")
        fake = FakeSpecExtract(KeyboardInterrupt(), 0)
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill", fake)

        with pytest.raises(KeyboardInterrupt):
            _run_spec_extract_step(config, _state(config), _progress(tmp_path))
        (project / "more.py").write_text("def g():\n    return 2\n")
        _git(project, "add", "more.py")
        _git(project, "commit", "-q", "-m", "more")
        assert _resume(config, tmp_path) is None

        assert [_flag(a, "--tree") for a in fake.calls] == [first, first]
        assert fake.calls[0] == fake.calls[1]

    def test_S3_files_applied_but_not_committed_are_committed_on_resume(self, tmp_path, monkeypatch, capsys):
        """Row S3: the stop came after spec-extract applied its files and before
        `commit_stage`. The non-empty registry does not skip the step (the commit is
        saved), and the resume commits exactly what a run that never stopped commits."""
        ref_project = _repo(tmp_path / "ref")
        ref_config = _config(ref_project)
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill",
                            FakeSpecExtract(_apply_into(ref_config), stdout="SPEC_EXTRACT:APPLIED 1\n"))
        assert _run_spec_extract_step(ref_config, _state(ref_config), _progress(tmp_path)) is None

        project = _repo(tmp_path / "p")
        config = _config(project)
        fake = FakeSpecExtract(_apply_into(config), stdout="SPEC_EXTRACT:APPLIED 1\n")
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill", fake)
        real_commit = git_ops.commit_stage

        def stop(*a, **k):
            raise KeyboardInterrupt

        monkeypatch.setattr(git_ops, "commit_stage", stop)
        with pytest.raises(KeyboardInterrupt):
            _run_spec_extract_step(config, _state(config), _progress(tmp_path))
        assert _git(project, "status", "--porcelain", "--", "specs/registry") != ""
        monkeypatch.setattr(git_ops, "commit_stage", real_commit)
        assert _resume(config, tmp_path) is None

        assert len(fake.calls) == 2 and fake.calls[0] == fake.calls[1]
        for repo in (project, ref_project):
            assert _git(repo, "status", "--porcelain", "--", "specs/registry") == ""
        assert _git(project, "ls-tree", "-r", "HEAD", "--", "specs/registry") == \
            _git(ref_project, "ls-tree", "-r", "HEAD", "--", "specs/registry")
        for repo in (project, ref_project):
            assert _git(repo, "log", "--format=%s", "--", "specs/registry") == \
                "docs(specs): baseline registry from spec-extract at " + _git(repo, "rev-parse", "HEAD~1")[:12]
        assert (project / "specs/registry/billing.md").read_bytes() == \
            (ref_project / "specs/registry/billing.md").read_bytes()


# --- review round 1 of #345 ----------------------------------------------------------------------


class TestTheBaselineMarkerSaysWhetherItWasCommitted:
    """`commit_stage` commits nothing without a pipeline branch (`--no-worktree`) and
    returns None when the commit fails; the marker then says the files are uncommitted."""

    def test_no_pipeline_branch_leaves_the_files_uncommitted_and_says_so(self, tmp_path, monkeypatch, capsys):
        project = _repo(tmp_path / "p")
        config = _config(project, branch=None)
        before = _git(project, "rev-parse", "HEAD")
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill",
                            FakeSpecExtract(_apply_into(config), stdout="SPEC_EXTRACT:APPLIED 1\n"))

        assert _run_spec_extract_step(config, _state(config), _progress(tmp_path)) is None

        assert _git(project, "rev-parse", "HEAD") == before
        assert (config.specs_dir / "registry" / "billing.md").read_text() == CAPABILITY
        out = capsys.readouterr().out
        assert "SPEC_EXTRACT:BASELINE 1 uncommitted" in out

    def test_a_failed_commit_says_so(self, tmp_path, monkeypatch, capsys):
        config = _config(_repo(tmp_path / "p"))
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill",
                            FakeSpecExtract(_apply_into(config), stdout="SPEC_EXTRACT:APPLIED 1\n"))
        monkeypatch.setattr(git_ops, "commit_stage", lambda *a, **k: None)

        assert _run_spec_extract_step(config, _state(config), _progress(tmp_path)) is None

        assert "SPEC_EXTRACT:BASELINE 1 uncommitted" in capsys.readouterr().out

    def test_nothing_left_to_commit_is_the_plain_marker(self, tmp_path, monkeypatch, capsys):
        """Row S4: the files were committed before the stop; the resume applies none."""
        project = _repo(tmp_path / "p")
        config = _config(project)
        state = _state(config)
        state.spec_gen = SpecGenState()
        state.spec_gen.spec_extract_sha = _git(project, "rev-parse", "HEAD")
        (config.specs_dir / "registry" / "billing.md").write_text(CAPABILITY)
        _git(project, "add", "specs")
        _git(project, "commit", "-q", "-m", "baseline")
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill", FakeSpecExtract(0, stdout="SPEC_EXTRACT:APPLIED 0\n"))

        assert _run_spec_extract_step(config, state, _progress(tmp_path)) is None

        lines = capsys.readouterr().out.splitlines()
        assert "SPEC_EXTRACT:BASELINE 0" in lines

    def test_no_applied_line_counts_0(self, tmp_path, monkeypatch, capsys):
        config = _config(_repo(tmp_path / "p"))
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill", FakeSpecExtract(0, stdout="something else\n"))
        assert _run_spec_extract_step(config, _state(config), _progress(tmp_path)) is None
        assert "SPEC_EXTRACT:BASELINE 0" in capsys.readouterr().out.splitlines()

    def test_an_empty_stderr_reports_the_last_stdout_line(self, tmp_path, monkeypatch, capsys):
        config = _config(_repo(tmp_path / "p"))
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill", lambda argv, cwd=None, timeout=None:
                            git_ops.GitResult(list(argv), 2, "first\nSPEC_EXTRACT:REFUSED here\n", ""))
        assert _run_spec_extract_step(config, _state(config), _progress(tmp_path)) is None
        assert "SPEC_EXTRACT:NOT_RUN exit=2 SPEC_EXTRACT:REFUSED here" in capsys.readouterr().out


def test_a_relative_specs_dir_is_passed_absolute(tmp_path, monkeypatch):
    """`uv run --directory` runs spec-extract in its own scripts directory, so a
    relative `--specs-dir` would name a directory there."""
    project = _repo(tmp_path / "p")
    config = _config(project)
    monkeypatch.chdir(project)
    config.specs_dir = Path("specs")
    fake = FakeSpecExtract(1)
    monkeypatch.setattr(git_ops, "_run_spec_extract_skill", fake)
    _run_spec_extract_step(config, _state(config), _progress(tmp_path))
    assert _flag(fake.calls[0], "--specs-dir") == str((project / "specs").resolve())


class TestEpicChildren:
    """An epic's child skips spec-extract: the parent ran it, and the child branches
    from the parent's registry commit (below); siblings at one commit would also
    race for spec-extract's lock."""

    def test_a_child_build_skips_spec_extract(self, tmp_path, monkeypatch, caplog):
        from build_pipeline.epic import CHILD_ENV

        monkeypatch.setenv(CHILD_ENV, "1")
        config = _config(_repo(tmp_path / "p"))
        TestSkips()._skipped(config, _state(config), tmp_path, monkeypatch, caplog, "epic-child")

    def test_the_coordinator_marks_each_child_it_starts(self, tmp_path, monkeypatch):
        from build_pipeline.epic import CHILD_ENV, coordinator

        seen = {}

        class Popen:
            def __init__(self, argv, **kw):
                seen.update(kw)

        monkeypatch.delenv(CHILD_ENV, raising=False)
        monkeypatch.setattr(coordinator.subprocess, "Popen", Popen)
        coordinator._spawn(["python", "-m", "build_pipeline"], tmp_path / "log")
        assert seen["env"][CHILD_ENV] == "1"
        assert seen["env"]["PATH"] == os.environ["PATH"]


class TestEpicChildrenBranchFromTheRegistryCommit:
    """Spike, 2026-10-14 (run-decisions-2026-10-11.md): a child with no dependency
    branches from the parent's commit that holds `specs/registry`, so the registry
    lands on main with the first merged child PR; a child that waits on a
    dependency branches from origin's default branch after that merge."""

    def _epic(self, tmp_path, monkeypatch):
        """An approved epic (api, ui; docs waits on both) whose parent committed a
        baseline registry after the commit its spec-extract step read. The
        registry commit is on the parent's branch only, not on origin."""
        from build_pipeline.config import EpicSettings

        from .epic_row_helpers import THREE, CallLog, Fakes, approve, make_build, make_project, use_workspace

        monkeypatch.delenv("BUILDME_TRUST_REPO")      # the parent's own step is not under test
        use_workspace(monkeypatch, tmp_path)
        project = make_project(tmp_path)
        fakes = Fakes(tmp_path, monkeypatch)
        config = make_build(project)
        config.epic = EpicSettings(max_parallel=2)
        approve(config, THREE, CallLog(tmp_path / "split.log"))
        state = BuildState.load(config.state_file_path())
        state.spec_gen = SpecGenState(spec_extract_sha=_git(project, "rev-parse", "HEAD"))
        state.checkpoint(config.state_file_path())
        (project / "specs" / "registry").mkdir(parents=True, exist_ok=True)
        (project / "specs" / "registry" / "billing.md").write_text(CAPABILITY)
        _git(project, "add", "specs/registry/billing.md")
        _git(project, "commit", "-q", "-m", "docs(specs): baseline registry from spec-extract")
        return config, fakes, _git(project, "rev-parse", "HEAD")

    def test_a_child_with_no_dependency_branches_from_the_registry_commit(self, tmp_path, monkeypatch):
        from .epic_row_helpers import run_coordinator

        config, _, registry = self._epic(tmp_path, monkeypatch)
        assert run_coordinator(config) == 3
        for cid in ("api", "ui"):
            assert (tmp_path / "bases" / cid).read_text() == registry
        assert not (tmp_path / "bases" / "docs").exists()

    def test_a_child_waiting_on_a_dependency_branches_from_origin_after_the_merge(self, tmp_path, monkeypatch):
        from .epic_row_helpers import pr_url, run_coordinator

        config, fakes, registry = self._epic(tmp_path, monkeypatch)
        assert run_coordinator(config) == 3
        fakes.set_pr(pr_url("e-api"), "MERGED")
        fakes.set_pr(pr_url("e-ui"), "MERGED")
        run_coordinator(config)
        origin = _git(tmp_path / "o" / "r.git", "rev-parse", "main")
        assert (tmp_path / "bases" / "docs").read_text() == origin != registry

    def test_a_child_in_another_repository_branches_from_its_origin(self, tmp_path, monkeypatch):
        import asyncio

        from build_pipeline.epic import coordinator, store

        from .epic_row_helpers import make_project

        config, _, registry = self._epic(tmp_path, monkeypatch)
        state = BuildState.load(config.state_file_path())
        epic_dir = Path(state.epic_dir)
        coord = coordinator.Coordinator(config, state, None, epic_dir, store.load_epic(epic_dir))
        other = make_project(tmp_path, "q", "q")
        c = coord.children["api"]
        c.repo, c.project, c.root = "../q", other, other
        assert asyncio.run(coord._base(c)) == _git(other, "rev-parse", "HEAD") != registry

    def test_no_registry_commit_keeps_origin(self, tmp_path, monkeypatch):
        """The parent's spec-extract step committed nothing (`NOT_RUN`, `--no-worktree`,
        a failed commit): every child branches from origin's default branch."""
        import asyncio

        from build_pipeline.epic import coordinator, store

        config, _, _ = self._epic(tmp_path, monkeypatch)
        project = Path(config.project_dir)
        _git(project, "reset", "-q", "--hard", "HEAD~1")
        state = BuildState.load(config.state_file_path())
        epic_dir = Path(state.epic_dir)
        coord = coordinator.Coordinator(config, state, None, epic_dir, store.load_epic(epic_dir))
        assert asyncio.run(coord._base(coord.children["api"])) == _git(project, "rev-parse", "origin/main")


class TestTheAnalysisIsNotPaidTwice:
    """Rows S1, S2 and S6 re-run the step; the codebase analysis before it already
    ran and wrote its report, so the re-run does not ask its agents again."""

    def _setup(self, tmp_path):
        project = _repo(tmp_path / "p")
        config = BuildConfig(project_dir=project, change_name="c", mode="scratch", auto=True,
                             spec_only=True, skip_research=True, git=GitToggles(worktree=False))
        config.specs_dir = project / "specs"
        config.prototype_mode = "false"
        cm = ChangeManager(config.specs_dir)
        cm.init_specs()
        cm.create_change("c")
        (config.change_dir / "proposal.md").write_text("## Why\nA thing.\n")
        _state(config)
        args = argparse.Namespace(mode="scratch", change="c", project_dir=str(project), auto=True,
                                  spec_only=True, skip_research=True, interview_summary="",
                                  research_path="", context_files=[], session_id="")
        return config, args

    def _runs(self, config, args, monkeypatch, analysis, *steps, runs: int = 1) -> list:
        """*runs* buildme processes on one project, each answering spec-extract from
        *steps* in turn; *analysis* counts the codebase analysis's calls."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        from build_pipeline.epic import split as epic_split

        answer = epic_split.EpicSplitAnswer(single=True, reason="one change")
        monkeypatch.setattr(epic_split, "epic_split", AsyncMock(return_value=answer))
        monkeypatch.setattr(git_ops, "_run_spec_extract_skill", FakeSpecExtract(*steps))
        codes = []
        with patch("build_pipeline.llm_steps.spec_steps.run_codebase_analysis", analysis), \
                patch("build_pipeline.spec_generator.run_spec_generator", AsyncMock(return_value=0)), \
                patch("build_pipeline.llm_steps.spec_steps.run_design_audit", AsyncMock(return_value=[])):
            for _ in range(runs):
                codes.append(asyncio.run(run_orchestrator(config, args)))
        return codes

    @staticmethod
    def _analysis():
        from unittest.mock import AsyncMock

        return AsyncMock(return_value="## Architecture\nx")

    def _reference(self, tmp_path, monkeypatch):
        """A run that never stopped."""
        config, args = self._setup(tmp_path / "ref")
        analysis = self._analysis()
        assert self._runs(config, args, monkeypatch, analysis, 0) == [0]
        return analysis, config

    @pytest.mark.parametrize("first", [5, "dirty"])
    def test_S2_and_S6_the_re_run_asks_the_analysis_once(self, first, tmp_path, monkeypatch):
        ref_analysis, ref_config = self._reference(tmp_path, monkeypatch)
        config, args = self._setup(tmp_path / "p")
        mine = config.specs_dir / "registry" / "mine.md"

        def user_edit():
            mine.parent.mkdir(parents=True, exist_ok=True)
            mine.write_text("my edit\n")
            return 2

        analysis = self._analysis()
        assert self._runs(config, args, monkeypatch, analysis, 5 if first == 5 else user_edit) == [1]
        if first == "dirty":
            mine.unlink()
        assert self._runs(config, args, monkeypatch, analysis, 0) == [0]

        assert analysis.await_count == ref_analysis.await_count == 1
        assert config.codebase_analysis_path.read_text() == ref_config.codebase_analysis_path.read_text()

    def test_an_analysis_that_failed_is_asked_again_on_the_re_run(self, tmp_path, monkeypatch):
        """No report was saved, so the re-run retries the analysis, as before the step."""
        from unittest.mock import AsyncMock

        config, args = self._setup(tmp_path / "p")
        analysis = AsyncMock(side_effect=[RuntimeError("model down"), "## Architecture\nx"])
        assert self._runs(config, args, monkeypatch, analysis, 5) == [1]
        assert self._runs(config, args, monkeypatch, analysis, 0) == [0]
        assert analysis.await_count == 2
        assert config.codebase_analysis_path.read_text() == "## Architecture\nx"

    def test_S1_stopped_twice_during_spec_extract_asks_the_analysis_once(self, tmp_path, monkeypatch):
        ref_analysis, _ = self._reference(tmp_path, monkeypatch)
        config, args = self._setup(tmp_path / "p")
        analysis = self._analysis()
        for _ in range(2):
            with pytest.raises(KeyboardInterrupt):
                self._runs(config, args, monkeypatch, analysis, KeyboardInterrupt())
        assert self._runs(config, args, monkeypatch, analysis, 0) == [0]
        assert analysis.await_count == ref_analysis.await_count == 1


def test_no_test_can_start_a_real_spec_extract():
    """conftest refuses the paid launch unless a test stubs the seam, as it refuses
    the real pr skill and a real walkthrough run."""
    with pytest.raises(AssertionError, match="spec-extract"):
        git_ops._run_spec_extract_skill(["uv", "run", "python", "-m", "spec_extract_pipeline", "run"])


class TestSpend:
    """spec-extract prints `SPEND:<usd>:<tokens>` last, as a walkthrough run does, and
    buildme adds it to its own `SPEND:` line. Only an epic's parent runs it, and an
    epic's `epic.max_usd` counts only its children's records, so it is not in that."""

    @pytest.fixture(autouse=True)
    def _fresh_total(self, monkeypatch):
        from build_pipeline import budget

        monkeypatch.setattr(budget, "_process", budget.ProcessSpend())

    ARGV = ["uv", "run", "--directory", "/s", "python", "-m", "spec_extract_pipeline", "run", "--tree", "abc"]

    def test_its_spend_line_is_added(self):
        from build_pipeline import budget

        git_ops._count_skill_spend(self.ARGV, "SPEC_EXTRACT:APPLIED 2\nSPEND:12.500000:3400\n")
        assert budget.spend_marker() == "SPEND:12.500000:3400"

    def test_no_spend_line_makes_the_total_incomplete(self):
        from build_pipeline import budget

        git_ops._count_skill_spend(self.ARGV, None)
        assert budget.spend_marker() == "SPEND:0.000000:0:incomplete"
