"""`tdd.setup_command`: a project's own command, run once per buildme process
before its first test run (plan-buildme-epic-speedups-2026-11-03.md, item 5;
item 8's `run_setup_once` bullet; Stop-and-resume row S16).

Each test starts as a fresh process: `gates._SETUP_DONE` is emptied before and
after it. A setup command here appends a line to a file outside the project's
git tree, so the number of lines is the number of times it ran and the tree
stays clean.
"""

from __future__ import annotations

import argparse
import json
import logging
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from build_pipeline import gates, git_ops, orchestrator
from build_pipeline.config import BuildConfig, GitToggles, parse_setup_command
from build_pipeline.epic.fix import failed_reason, park_fields, park_kind
from build_pipeline.findings import code, loop
from build_pipeline.gates import SETUP_FAILED, TEST_RUNS_DIR, run_setup_once
from build_pipeline.models import (
    AgentResult,
    BlockInfo,
    BlockStatus,
    BuildPhase,
    GateResult,
)
from build_pipeline.spec_generator import reconcile_plan
from build_pipeline.state import BuildState, TDDState, completed_state_path
from build_pipeline.tdd_engine import EXIT_BUILD_FAILURE, run_tdd_engine
from multiplai_core.specs import mark_ready

from tests import test_findings_round as round_fixtures
from tests.test_findings_round import (
    FIX,
    FakeReview,
    FakeTriage,
    _run,
    _Stubs,
    engine_stubs,
)

# test_findings_round's fixtures: a finished, archived build and a fake PR.
built = round_fixtures.built
fake_pr = round_fixtures.fake_pr


@pytest.fixture(autouse=True)
def fresh_process():
    """Each test is a new buildme process: nothing has run its setup yet."""
    gates._SETUP_DONE.clear()
    yield
    gates._SETUP_DONE.clear()


@pytest.fixture
def trust_repo(monkeypatch):
    """Gates that execute the repo's commands require an explicit trust opt-in."""
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")


def _appender(log: Path) -> str:
    """A setup command that appends one line to *log* each time it runs."""
    return f"sh -c {shlex.quote(f'echo ran >> {shlex.quote(str(log))}')}"


def _runs(log: Path) -> int:
    return len(log.read_text().splitlines()) if log.exists() else 0


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


# --- config ------------------------------------------------------------------


def _config_from_yaml(tmp_path: Path, yaml_text: str) -> BuildConfig:
    specs = tmp_path / "specs"
    specs.mkdir(exist_ok=True)
    (specs / "config.yaml").write_text(yaml_text)
    config = BuildConfig(project_dir=tmp_path)
    config.specs_dir = specs
    config._load_specs_config()
    return config


class TestSetupCommandConfig:
    """Item 5: `setup_command: str = ""`, read from `tdd.setup_command`, also in
    the reload after the build moves into its worktree."""

    def test_unset_is_empty(self, tmp_path):
        assert BuildConfig().setup_command == ""
        assert parse_setup_command(None) == ""
        assert (
            _config_from_yaml(tmp_path, "tdd:\n  test_command: pytest\n").setup_command
            == ""
        )

    def test_a_string_is_read_and_stripped(self, tmp_path):
        config = _config_from_yaml(
            tmp_path, "tdd:\n  setup_command: '  make reset-test-db  '\n"
        )
        assert config.setup_command == "make reset-test-db"

    @pytest.mark.parametrize("value", ["12", "true", "[a, b]", "{cmd: x}"])
    def test_a_value_that_is_not_a_string_warns_and_is_empty(
        self, tmp_path, caplog, value
    ):
        with caplog.at_level(logging.WARNING):
            config = _config_from_yaml(tmp_path, f"tdd:\n  setup_command: {value}\n")
        assert config.setup_command == ""
        assert "tdd.setup_command" in caplog.text

    def test_the_worktree_reload_reads_it(self, tmp_path):
        wt = tmp_path / "wt"
        (wt / "specs").mkdir(parents=True)
        (wt / "specs" / "config.yaml").write_text(
            "tdd:\n  setup_command: ./bin/reset-db\n"
        )
        config = BuildConfig(project_dir=tmp_path)
        config.rebind_project_dir(wt)
        config.reload_test_settings()
        assert config.setup_command == "./bin/reset-db"


# --- the gate ------------------------------------------------------------------


class TestRunSetupOnce:
    def test_an_unset_key_runs_nothing(self, tmp_path, trust_repo):
        with patch(
            "build_pipeline.gates.run_test_suite",
            side_effect=AssertionError("ran a command"),
        ):
            for command in ("", "   "):
                result = run_setup_once(command, tmp_path)
                assert result.passed
        assert gates._SETUP_DONE == set()

    def test_it_runs_once_per_process_per_project(self, tmp_path, trust_repo):
        log = tmp_path / "setup.log"
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (tmp_path / "a-link").symlink_to(a)
        command = _appender(log)
        assert run_setup_once(command, a).passed
        assert run_setup_once(command, a).passed
        # The same project by another path is the same project.
        assert run_setup_once(command, tmp_path / "a-link").passed
        assert _runs(log) == 1
        assert run_setup_once(command, b).passed
        assert _runs(log) == 2

    def test_it_runs_from_the_project(self, tmp_path, trust_repo):
        (tmp_path / "proj").mkdir()
        assert run_setup_once("touch made-here", tmp_path / "proj").passed
        assert (tmp_path / "proj" / "made-here").exists()

    def test_an_untrusted_repo_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BUILDME_TRUST_REPO", raising=False)
        log = tmp_path / "setup.log"
        result = run_setup_once(_appender(log), tmp_path)
        assert not result.passed
        assert result.reason.startswith(f"{SETUP_FAILED}: ")
        assert "not trusted" in result.reason
        assert _runs(log) == 0
        assert gates._SETUP_DONE == set()

    def test_a_failure_names_the_exit_code_and_saves_its_output(
        self, tmp_path, trust_repo
    ):
        change = tmp_path / "change"
        result = run_setup_once(
            "sh -c 'echo db is gone; exit 3'", tmp_path, change_dir=change
        )
        assert not result.passed
        assert result.reason == f"{SETUP_FAILED}: exit 3"
        logs = list((change / TEST_RUNS_DIR).glob("*-setup.log"))
        assert len(logs) == 1
        assert result.metadata["output_file"] == str(logs[0])
        assert "db is gone" in logs[0].read_text()

    def test_a_command_that_leaves_the_tree_dirty_fails(self, tmp_path, trust_repo):
        """The tests would pass on files the push never sends."""
        run = lambda *a: subprocess.run(
            a, cwd=tmp_path, check=True, capture_output=True
        )
        run("git", "init", "-q")
        (tmp_path / "uv.lock").write_text("old\n")
        run("git", "add", "-A")
        run("git", "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-qm", "init")
        result = run_setup_once("sh -c 'echo new > uv.lock'", tmp_path)
        assert not result.passed
        assert result.reason == (
            f"{SETUP_FAILED}: it left uncommitted changes in the project"
        )
        assert gates._SETUP_DONE == set()

    def test_a_command_that_cannot_run_names_the_runner_reason(
        self, tmp_path, trust_repo
    ):
        result = run_setup_once("no-such-binary-xyz --reset", tmp_path)
        assert not result.passed
        assert result.reason.startswith(f"{SETUP_FAILED}: ")
        assert result.reason != f"{SETUP_FAILED}: exit None"

    def test_it_uses_the_test_timeout(self, tmp_path, trust_repo):
        seen = {}

        def runner(command, project_dir, timeout, output):
            seen["timeout"] = timeout
            return 0, ""

        with patch("build_pipeline.gates.run_test_suite", side_effect=runner):
            assert run_setup_once("x", tmp_path, timeout=1800).passed
        assert seen["timeout"] == 1800

    def test_a_failed_command_runs_again_when_asked_again(self, tmp_path, trust_repo):
        log = tmp_path / "setup.log"
        flag = tmp_path / "ok"
        command = f"sh -c {shlex.quote(f'echo ran >> {log}; test -e {flag}')}"
        assert not run_setup_once(command, tmp_path).passed
        flag.touch()
        assert run_setup_once(command, tmp_path).passed
        assert run_setup_once(command, tmp_path).passed
        assert _runs(log) == 2

    def test_a_failure_reason_is_classified_setup_command(self, tmp_path, trust_repo):
        reason = run_setup_once("false", tmp_path).reason
        assert park_kind(reason) == ("environment", "setup-command")


# --- the TDD engine ---------------------------------------------------------------


def _engine_project(base: Path) -> tuple[BuildConfig, MagicMock]:
    """test_tdd_engine's `tdd_setup`: a one-block change under *base*."""
    project_dir = base / "project"
    project_dir.mkdir(parents=True)
    specs_root = project_dir / "specs"
    change_dir = specs_root / "changes" / "feat"
    change_dir.mkdir(parents=True)
    (change_dir / "tasks.md").write_text(
        "## 1. Setup\n\nInit the project.\n\nSatisfies: setup\n"
    )
    config = BuildConfig(
        project_dir=project_dir,
        change_name="feat",
        tier="advanced",
        test_command="true",
        config_dir=base / "config",
        core_memory_files=[],
        stack_memory_files=[],
        additional_memory_files=[],
    )
    config.specs_dir = specs_root
    args = MagicMock()
    args.block = None
    _git(project_dir, "init", "-q")
    _git(project_dir, "add", "-A")
    _git(project_dir, "commit", "-qm", "init")
    return config, args


WRITER_FAILS = AgentResult(success=False, error="stop after the baseline")


class Killed(BaseException):
    """The process dies here (SIGKILL / Ctrl-C between two steps)."""


class TestEngine:
    @pytest.mark.asyncio
    async def test_it_runs_once_per_process_across_two_engine_runs(
        self, tmp_path, trust_repo
    ):
        log = tmp_path / "setup.log"
        config, args = _engine_project(tmp_path)
        config.setup_command = _appender(log)
        with patch(
            "build_pipeline.tdd_engine.run_test_writer",
            new_callable=AsyncMock,
            return_value=WRITER_FAILS,
        ) as writer:
            await run_tdd_engine(config, args)
            await run_tdd_engine(config, args)
        assert writer.await_count == 2  # both runs got past the setup
        assert _runs(log) == 1

    @pytest.mark.asyncio
    async def test_it_runs_before_the_baseline(self, tmp_path, trust_repo):
        log = tmp_path / "setup.log"
        config, args = _engine_project(tmp_path)
        config.setup_command = _appender(log)
        config.test_command = f"test -e {log}"
        with patch(
            "build_pipeline.tdd_engine.run_test_writer",
            new_callable=AsyncMock,
            return_value=WRITER_FAILS,
        ):
            await run_tdd_engine(config, args)
        saved = BuildState.load(config.state_file_path())
        assert saved.tdd.baseline_tests_pass is True

    @pytest.mark.asyncio
    async def test_a_failure_stops_the_engine_at_setup(
        self, tmp_path, trust_repo, caplog
    ):
        config, args = _engine_project(tmp_path)
        config.setup_command = "sh -c 'exit 7'"
        baseline = Mock(side_effect=AssertionError("ran the baseline"))
        writer = AsyncMock(side_effect=AssertionError("ran a block"))
        with (
            caplog.at_level(logging.ERROR),
            patch("build_pipeline.tdd_engine.baseline_test_gate", baseline),
            patch("build_pipeline.tdd_engine.run_test_writer", writer),
        ):
            result = await run_tdd_engine(config, args)
        assert result == EXIT_BUILD_FAILURE
        assert f"STOP phase=SETUP reason={SETUP_FAILED}: exit 7" in caplog.text
        assert "SETUP\nFAILED: tdd.setup_command failed: exit 7" in (
            config.progress_file_path().read_text()
        )
        saved = BuildState.load(config.state_file_path())
        assert saved.tdd.baseline_failure == f"{SETUP_FAILED}: exit 7"
        assert saved.tdd.baseline_tests_pass is False

    @pytest.mark.asyncio
    async def test_a_later_pass_clears_the_saved_setup_failure(
        self, tmp_path, trust_repo
    ):
        config, args = _engine_project(tmp_path)
        config.setup_command = "false"
        assert await run_tdd_engine(config, args) == EXIT_BUILD_FAILURE
        config.setup_command = "true"
        with patch(
            "build_pipeline.tdd_engine.run_test_writer",
            new_callable=AsyncMock,
            return_value=WRITER_FAILS,
        ):
            await run_tdd_engine(config, args)
        saved = BuildState.load(config.state_file_path())
        assert saved.tdd.baseline_failure == ""
        assert saved.tdd.baseline_tests_pass is True

    @pytest.mark.asyncio
    async def test_an_unset_key_runs_only_the_test_command(self, tmp_path, trust_repo):
        config, args = _engine_project(tmp_path)
        ran: list[str] = []
        real = gates.run_test_suite

        def record(command, *a, **kw):
            ran.append(command)
            return real(command, *a, **kw)

        with (
            patch("build_pipeline.gates.run_test_suite", side_effect=record),
            patch(
                "build_pipeline.tdd_engine.run_test_writer",
                new_callable=AsyncMock,
                return_value=WRITER_FAILS,
            ),
        ):
            await run_tdd_engine(config, args)
        assert ran and set(ran) == {"true"}

    @pytest.mark.asyncio
    async def test_s16_killed_after_setup_ran_before_the_first_test_run(
        self, tmp_path, trust_repo
    ):
        """S16: the next process runs `setup_command` again; buildme saves
        nothing for it. Resumed, the checkpoint matches a run that never
        stopped."""
        log = tmp_path / "setup.log"
        config, args = _engine_project(tmp_path / "stopped")
        config.setup_command = _appender(log)
        with (
            patch(
                "build_pipeline.tdd_engine.baseline_test_gate",
                side_effect=Killed(),
            ),
            pytest.raises(Killed),
        ):
            await run_tdd_engine(config, args)
        assert _runs(log) == 1
        path = config.state_file_path()
        stopped = path.read_text()
        assert "setup_command" not in stopped
        assert BuildState.load(path).tdd.baseline_failure == ""
        assert BuildState.load(path).tdd.baseline_tests_pass is False

        gates._SETUP_DONE.clear()  # the next process
        with patch(
            "build_pipeline.tdd_engine.run_test_writer",
            new_callable=AsyncMock,
            return_value=WRITER_FAILS,
        ):
            await run_tdd_engine(config, args)
        assert _runs(log) == 2

        clean_log = tmp_path / "clean-setup.log"
        clean, clean_args = _engine_project(tmp_path / "clean")
        clean.setup_command = _appender(clean_log)
        gates._SETUP_DONE.clear()
        with patch(
            "build_pipeline.tdd_engine.run_test_writer",
            new_callable=AsyncMock,
            return_value=WRITER_FAILS,
        ):
            await run_tdd_engine(clean, clean_args)
        assert _runs(clean_log) == 1

        def comparable(p: Path) -> dict:
            tdd = json.loads(p.read_text())["tdd"]
            return {
                "baseline_tests_pass": tdd["baseline_tests_pass"],
                "baseline_failure": tdd.get("baseline_failure", ""),
                "current_block": tdd["current_block"],
                "blocks": [(b["number"], b["status"]) for b in tdd["blocks"]],
            }

        assert comparable(path) == comparable(clean.state_file_path())
        assert _git(config.project_dir, "log", "--format=%s") == _git(
            clean.project_dir, "log", "--format=%s"
        )
        assert _git(config.project_dir, "status", "--porcelain") == _git(
            clean.project_dir, "status", "--porcelain"
        )


# --- the fixer (findings round) ------------------------------------------------------


class TestFixer:
    @pytest.mark.asyncio
    async def test_a_failure_parks_the_fixer_as_setup_command(self, built, fake_pr):
        repo, change, config = built
        config.setup_command = "sh -c 'exit 5'"
        fake_pr.add_thread("alice", "add subtracts", fid="ab12")
        run, lines = _run(
            config,
            change,
            triage_call=FakeTriage({"add subtracts": FIX}),
            review=FakeReview(fake_pr),
        )
        with _Stubs(engine_stubs(repo)):
            assert await loop.run_findings(run) == 1, lines
        assert lines[-1] == f"FINDINGS:FAILED:code:{SETUP_FAILED}: exit 5"
        assert fake_pr.replies() == []
        why = failed_reason("\n".join(lines))
        fields = park_fields(why)
        assert fields["park_kind"] == "environment"
        assert fields["park_cause"] == "setup-command"

    @pytest.mark.asyncio
    async def test_publish_runs_setup_before_final_verification(
        self, built, fake_pr, tmp_path
    ):
        repo, change, config = built
        log = tmp_path / "setup.log"
        config.setup_command = _appender(log)
        fake_pr.add_thread("alice", "add subtracts", fid="ab12")
        order: list[str] = []
        real_setup, real_final = gates.run_setup_once, gates.final_verification

        def setup(*a, **kw):
            order.append("setup")
            return real_setup(*a, **kw)

        def final(*a, **kw):
            order.append("final")
            return real_final(*a, **kw)

        run, lines = _run(
            config,
            change,
            triage_call=FakeTriage({"add subtracts": FIX}),
            review=FakeReview(fake_pr),
        )
        with (
            _Stubs(engine_stubs(repo)),
            patch("build_pipeline.gates.run_setup_once", side_effect=setup),
            patch("build_pipeline.gates.final_verification", side_effect=final),
            patch.object(code, "reused_end_suite", return_value=None),
        ):
            assert await loop.run_findings(run) == 0, lines
        assert order == ["setup", "final"]
        assert _runs(log) == 1  # the engine ran it; publish found it done

    @pytest.mark.asyncio
    async def test_publish_reusing_the_end_of_build_pass_runs_no_setup(
        self, built, fake_pr
    ):
        repo, change, config = built
        config.setup_command = "true"
        fake_pr.add_thread("alice", "add subtracts", fid="ab12")
        reused = GateResult(
            passed=True, reason="reused", metadata={"reused_from": "a" * 40}
        )
        run, lines = _run(
            config,
            change,
            triage_call=FakeTriage({"add subtracts": FIX}),
            review=FakeReview(fake_pr),
        )
        with (
            _Stubs(engine_stubs(repo)),
            patch(
                "build_pipeline.gates.run_setup_once",
                side_effect=AssertionError("setup before a reused pass"),
            ),
            patch(
                "build_pipeline.gates.final_verification",
                side_effect=AssertionError("final verification ran"),
            ),
            patch.object(code, "reused_end_suite", return_value=reused),
        ):
            assert await loop.run_findings(run) == 0, lines


# --- the orchestrator ---------------------------------------------------------------


@pytest.fixture
def accepted_build(make_accepted_build):
    return make_accepted_build()


@pytest.fixture
def make_accepted_build(tmp_path, monkeypatch):
    """A factory for test_completion_contract's `accepted_build`, with the
    project under tmp_path/project so the setup command's log sits outside its
    tree; *setup_command* (default: append to that log) is in the committed
    specs/config.yaml, since the plan revision covers the file."""
    return lambda setup_command=None: _accepted_build(
        tmp_path, monkeypatch, setup_command
    )


def _accepted_build(tmp_path, monkeypatch, setup_command):
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    root = tmp_path / "project"
    root.mkdir()
    log = tmp_path / "setup.log"
    _git(root, "init", "-qb", "buildme/contract")
    config = BuildConfig(
        project_dir=root,
        change_name="contract",
        auto=False,
        git=GitToggles(worktree=False, push=True, pr="none"),
        test_command=(
            f'{sys.executable} -c "import pathlib; from app import value; '
            f"assert value == 1; assert pathlib.Path('{log}').exists()\""
        ),
    )
    config.setup_command = setup_command or _appender(log)
    config.specs_dir = root / "specs"
    change = config.change_dir
    (change / "requirements").mkdir(parents=True)
    for name, content in {
        "proposal.md": "## Capabilities\n- `value`\n",
        "requirements/value.md": "### Requirement: Value\n#### Scenario: Read\nWHEN read\nTHEN returns one\n",
        "implementation-plan.md": "# Design\nA constant",
        "use-cases.md": "# Use cases\nRead one",
        "unknowns.md": "# Unknowns\nNone",
        "tasks.md": "## 1. Value\nReturn one",
        "rubric.md": "# Rubric\nCorrect value",
    }.items():
        (change / name).write_text(content)
    (config.specs_dir / "config.yaml").write_text(
        f"tdd:\n  setup_command: {json.dumps(config.setup_command)}\n"
    )
    (root / "app.py").write_text("value = 1\n")
    mark_ready(change)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "accepted implementation")
    config.pipeline_branch = "buildme/contract"
    state = BuildState(
        change_name="contract",
        tier="advanced",
        phase=BuildPhase.PUBLISH,
        branch=config.pipeline_branch,
        worktree_path=str(root),
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
    state.spec_gen.record_step("plan_review", "passed")
    state.checkpoint(config.state_file_path())
    args = argparse.Namespace(
        change="contract",
        project_dir=str(root),
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
    return config, args, push, log


class TestOrchestrator:
    @pytest.mark.asyncio
    async def test_setup_runs_before_final_verification(self, accepted_build):
        """The test command asserts the setup's log exists: it passes only
        when the setup ran first."""
        config, args, push, log = accepted_build
        assert await orchestrator.run_orchestrator(config, args) == 0
        assert _runs(log) == 1
        done = BuildState.load(completed_state_path(config.state_file_path()))
        assert done.verification["passed"]
        push.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_setup_failure_blocks_verification_and_push(
        self, make_accepted_build, monkeypatch
    ):
        config, args, push, _ = make_accepted_build("sh -c 'exit 4'")
        monkeypatch.setattr(
            "build_pipeline.gates.final_verification",
            Mock(side_effect=AssertionError("verified after a failed setup")),
        )
        assert await orchestrator.run_orchestrator(config, args) == 1
        push.assert_not_called()
        saved = BuildState.load(config.state_file_path())
        assert saved.verification["passed"] is False
        assert saved.verification["reason"] == f"{SETUP_FAILED}: exit 4"

    @pytest.mark.asyncio
    async def test_a_recorded_verification_runs_no_setup(self, accepted_build):
        """A publish retry whose inputs are unchanged reuses the verification:
        the next process runs no setup, because no test run happens."""
        config, args, push, log = accepted_build
        push.return_value = git_ops.GitResult([], 1, "", "offline")
        assert await orchestrator.run_orchestrator(config, args) == 0
        assert _runs(log) == 1
        gates._SETUP_DONE.clear()  # the next process
        push.return_value = git_ops.GitResult([], 0, "", "")
        assert await orchestrator.run_orchestrator(config, args) == 0
        assert _runs(log) == 1

    @pytest.mark.asyncio
    async def test_a_reused_end_of_build_pass_runs_no_setup(
        self, accepted_build, monkeypatch
    ):
        config, args, push, log = accepted_build

        def reused(cfg, state, spec_dirs, current):
            gate = GateResult(
                passed=True,
                reason="reused",
                metadata={"inputs": current, "reused_from": "a" * 40},
            )
            return gate, 1

        monkeypatch.setattr(orchestrator, "_reused_end_suite", reused)
        monkeypatch.setattr(
            "build_pipeline.gates.final_verification",
            Mock(side_effect=AssertionError("final verification ran")),
        )
        assert await orchestrator.run_orchestrator(config, args) == 0
        assert _runs(log) == 0
        push.assert_called_once()
