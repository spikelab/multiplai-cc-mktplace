"""Tests for the MUTATION sub-step: `build_pipeline.mutation` and the engine's
`_run_mutation`.

cosmic-ray is stubbed at the subprocess boundary (`mutation._run_tool`) or one
level up (`tdd_engine.run_mutation`); git is real, in a temp repo, because the
step's safety rests on git (commit before the run, discard after). One test
runs the real tool and is skipped unless `cosmic-ray` is on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from build_pipeline import mutation
from build_pipeline.config import BuildConfig, MutationSettings, _load_mutation_settings
from build_pipeline.models import (
    AgentResult,
    BlockInfo,
    BlockStatus,
    MutationResult,
    SurvivingMutant,
)
from build_pipeline.progress import ProgressWriter
from build_pipeline.state import BuildState, TDDState
from build_pipeline.tdd_engine import _run_mutation

# --- helpers ---------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init"],
        cwd=str(path),
        capture_output=True,
        check=True,
    )
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _dump_line(job_id, path, line, operator, worker, test=None, output="", diff=None):
    item = {
        "job_id": job_id,
        "mutations": [
            {
                "module_path": path,
                "operator_name": operator,
                "occurrence": 0,
                "start_pos": [line, 4],
                "end_pos": [line, 5],
                "operator_args": {},
                "definition_name": "f",
            }
        ],
    }
    result = (
        None
        if worker is None
        else {
            "worker_outcome": worker,
            "output": output,
            "test_outcome": test,
            "diff": diff,
        }
    )
    return json.dumps([item, result])


DUMP = "\n".join(
    [
        _dump_line(
            "a1",
            "src/calc.py",
            2,
            "core/ReplaceBinaryOperator_Add_Sub",
            "skipped",
            output="Filtered git",
        ),
        _dump_line(
            "k1",
            "src/calc.py",
            10,
            "core/ReplaceComparisonOperator_Lt_Gt",
            "normal",
            "killed",
            output="1 failed",
        ),
        _dump_line(
            "k2", "src/calc.py", 12, "core/AddNot", "normal", "killed", output="timeout"
        ),
        _dump_line(
            "k3",
            "src/calc.py",
            12,
            "core/NumberReplacer",
            "normal",
            "killed",
            output="1 failed",
        ),
        _dump_line(
            "s1",
            "src/calc.py",
            10,
            "core/ReplaceComparisonOperator_Lt_LtE",
            "normal",
            "survived",
            output="3 passed",
            diff="-    if x < lo:\n+    if x <= lo:",
        ),
        _dump_line(
            "e1",
            "src/calc.py",
            14,
            "core/ZeroIterationForLoop",
            "exception",
            "incompetent",
        ),
    ]
)


@pytest.fixture
def project(tmp_path):
    """A committed Python project with one pre-build commit (the baseline) and
    one build commit that adds a function and its test."""
    repo = _repo(tmp_path / "project")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'calc'\nversion = '0.1.0'\n"
    )
    (repo / "src").mkdir()
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    (repo / ".gitignore").write_text(".build-state.json\nbuild-progress.md\n")
    baseline = _commit_all(repo, "chore: init")
    with (repo / "src" / "calc.py").open("a") as f:
        f.write(
            "\n\ndef clamp(x, lo, hi):\n    if x < lo:\n        return lo\n    return x\n"
        )
    (repo / "tests" / "test_clamp.py").write_text(
        "from src.calc import clamp\n\n\ndef test_clamp():\n    assert clamp(-1, 0, 9) == 0\n"
    )
    specs = repo / "specs" / "changes" / "feat"
    specs.mkdir(parents=True)
    (specs / "tasks.md").write_text("## 1. Clamp\n")
    _commit_all(repo, "feat: clamp")
    return repo, baseline


def _setup(
    project,
    *,
    stack="pyproject",
    test_command="true",
    min_score=0.8,
    mutation_done=False,
):
    repo, baseline = project
    config = BuildConfig(
        project_dir=repo,
        change_name="feat",
        tier="advanced",
        test_command=test_command,
        config_dir=repo / ".cfg",
        core_memory_files=[],
        stack_memory_files=[],
        additional_memory_files=[],
    )
    config.specs_dir = repo / "specs"
    config.stack = stack
    config.mutation = MutationSettings(min_score=min_score)
    state = BuildState(change_name="feat", tier="advanced")
    state.tdd = TDDState(
        blocks=[
            BlockInfo(
                number=1,
                name="Clamp",
                description="clamp",
                status=BlockStatus.DONE,
                baseline_commit=baseline,
            )
        ],
        current_block=1,
        refactor_all_done=True,
        mutation_done=mutation_done,
    )
    state.state_file = str(config.state_file_path())
    progress = ProgressWriter(repo / "build-progress.md")
    return config, state, progress


def _result(score_killed: int, survived: int, files=("src/calc.py",)) -> MutationResult:
    judged = score_killed + survived
    return MutationResult(
        ok=True,
        tool_version=mutation.COSMIC_RAY_VERSION,
        files=list(files),
        killed=score_killed,
        survived=survived,
        score=(score_killed / judged) if judged else None,
        survivors=[
            SurvivingMutant(
                path="src/calc.py",
                line=5,
                operator="core/AddNot",
                mutant_id=f"s{i}",
                diff="-a\n+b",
            )
            for i in range(survived)
        ],
    )


@pytest.fixture(autouse=True)
def _trusted():
    with patch.dict(os.environ, {"BUILDME_TRUST_REPO": "1"}):
        yield


# --- parsing ---------------------------------------------------------------


class TestParseDump:
    def test_counts_survivors_killed_timeouts_and_score(self):
        r = mutation.parse_dump(DUMP)
        assert r.ok is True
        assert (r.killed, r.survived, r.timeouts, r.errors) == (3, 1, 1, 1)
        # killed / (killed + survived); skipped and errored jobs left out
        assert r.score == pytest.approx(3 / 4)

    def test_survivor_carries_file_line_operator_id_and_diff(self):
        [s] = mutation.parse_dump(DUMP).survivors
        assert (s.path, s.line, s.operator, s.mutant_id) == (
            "src/calc.py",
            10,
            "core/ReplaceComparisonOperator_Lt_LtE",
            "s1",
        )
        assert "x <= lo" in s.diff

    def test_timeouts_count_as_killed(self):
        dump = _dump_line(
            "t", "a.py", 1, "core/AddNot", "normal", "killed", output="timeout"
        )
        r = mutation.parse_dump(dump)
        assert (r.killed, r.timeouts, r.score) == (1, 1, 1.0)

    def test_no_judged_mutant_gives_no_score(self):
        dump = "\n".join(
            [
                _dump_line(
                    "a", "a.py", 1, "core/AddNot", "skipped", output="Filtered git"
                ),
                _dump_line("b", "a.py", 2, "core/AddNot", None),
            ]
        )
        r = mutation.parse_dump(dump)
        assert r.score is None and r.survivors == []

    def test_pending_jobs_lists_only_jobs_without_a_result(self):
        jobs = mutation.pending_jobs(
            "\n".join(
                [
                    _dump_line("a", "a.py", 1, "core/AddNot", "skipped"),
                    _dump_line("b", "a.py", 2, "core/AddNot", None),
                ]
            )
        )
        assert [j for j, _ in jobs] == ["b"]

    def test_sample_is_deterministic_and_capped(self):
        jobs = [(f"id{i}", f"key{i}") for i in range(10)]
        dropped = mutation.sample_out(jobs, 4)
        assert len(dropped) == 6
        # Same mutations under fresh job ids → the same mutations are dropped.
        renamed = [(f"new{i}", f"key{i}") for i in range(10)]
        assert [d.replace("id", "") for d in dropped] == [
            d.replace("new", "") for d in mutation.sample_out(renamed, 4)
        ]
        assert mutation.sample_out(jobs, 10) == []


class TestConfig:
    def test_defaults(self):
        s = _load_mutation_settings(None)
        assert (s.enabled, s.min_score, s.timeout, s.max_mutants) == (
            True,
            0.8,
            30.0,
            300,
        )

    def test_reads_valid_values(self):
        s = _load_mutation_settings(
            {"enabled": False, "min_score": 0.6, "timeout": 12, "max_mutants": 50}
        )
        assert (s.enabled, s.min_score, s.timeout, s.max_mutants) == (
            False,
            0.6,
            12.0,
            50,
        )

    @pytest.mark.parametrize(
        "raw",
        [
            {"min_score": 1.5},
            {"min_score": "high"},
            {"timeout": 0},
            {"timeout": True},
            {"max_mutants": -1},
            {"max_mutants": 2.5},
            {"enabled": "maybe"},
        ],
    )
    def test_bad_values_keep_the_default_and_warn(self, raw, caplog):
        s = _load_mutation_settings(raw)
        assert s == MutationSettings()
        assert "mutation" in caplog.text

    def test_specs_config_yaml_is_read(self, tmp_path):
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text(
            "mutation:\n  min_score: 0.7\n  max_mutants: 20\n"
        )
        config = BuildConfig(project_dir=tmp_path, config_dir=tmp_path / "c")
        config.specs_dir = specs
        config._load_specs_config()
        assert config.mutation.min_score == 0.7
        assert config.mutation.max_mutants == 20


class TestCosmicRayConfig:
    def test_test_command_runs_with_buildmes_path_and_branch_is_the_sha(self):
        with patch.dict(os.environ, {"PATH": "/project/.venv/bin:/usr/bin"}):
            text = mutation.cosmic_ray_config(
                ["src/calc.py"], "python -m pytest -q", 30, "abc123"
            )
        assert 'module-path = ["src/calc.py"]' in text
        assert 'branch = "abc123"' in text
        assert "env PATH=/project/.venv/bin:/usr/bin python -m pytest -q" in text


# --- applicability ---------------------------------------------------------


class TestApplies:
    def test_python_and_enabled(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        config = BuildConfig(
            project_dir=tmp_path, stack="pyproject", test_command="pytest"
        )
        assert mutation.mutation_applies(config) == (True, "")

    @pytest.mark.parametrize(
        "marker, stack",
        [
            # pytest.ini is checked before pyproject.toml, so a project with both
            # records stack "pytest".
            ("pytest.ini", "pytest"),
            ("setup.py", "setup"),
            ("setup.cfg", ""),
            ("tox.ini", ""),
            ("requirements.txt", ""),
            # tdd.test_command set, so discovery never ran and stack stayed "".
            ("pyproject.toml", ""),
            # memory_files.stacks names the stack freely.
            ("pyproject.toml", "python"),
        ],
    )
    def test_every_python_project_layout_applies(self, tmp_path, marker, stack):
        (tmp_path / marker).write_text("")
        config = BuildConfig(project_dir=tmp_path, stack=stack, test_command="pytest")
        assert mutation.mutation_applies(config) == (True, "")

    def test_non_python_stack_does_not_apply(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        ok, why = mutation.mutation_applies(
            BuildConfig(project_dir=tmp_path, stack="package", test_command="npm test")
        )
        assert ok is False and "Python" in why
        assert "pyproject.toml" in why

    @pytest.mark.parametrize("helper", ["requirements.txt", "setup.cfg", "tox.ini"])
    def test_discovered_non_python_test_command_does_not_apply(self, tmp_path, helper):
        # A Node project with a helper Python file at its root: discovery picks
        # `npm test`, which never runs the mutated Python, so every mutant would
        # survive and fail the build.
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / helper).write_text("")
        config = BuildConfig(project_dir=tmp_path, config_dir=tmp_path / "c")
        config._discover_test_command()
        assert config.test_command == "npm test"
        ok, why = mutation.mutation_applies(config)
        assert ok is False
        assert "npm test" in why and "package.json" in why
        assert "tdd.test_command" in why

    @pytest.mark.parametrize("marker", ["pytest.ini", "pyproject.toml", "setup.py"])
    def test_discovered_python_test_command_applies(self, tmp_path, marker):
        (tmp_path / marker).write_text("")
        (tmp_path / "package.json").write_text("{}")
        config = BuildConfig(project_dir=tmp_path, config_dir=tmp_path / "c")
        config._discover_test_command()
        assert config.test_command == "pytest -xvs"
        assert mutation.mutation_applies(config) == (True, "")

    @pytest.mark.parametrize("helper", ["requirements.txt", "setup.cfg", "tox.ini"])
    def test_persisted_discovered_non_python_command_does_not_apply(
        self, tmp_path, helper
    ):
        # Bootstrap writes the discovered `npm test` into specs/config.yaml as
        # tdd.test_command, so a resume or a later build loads it as configured
        # and discovery never runs. It is still discovery's pick from
        # package.json, and it still never runs the mutated Python.
        import yaml

        (tmp_path / "package.json").write_text("{}")
        (tmp_path / helper).write_text("")
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text(
            yaml.dump(
                {
                    "schema": "spec-driven",
                    "tdd": {"enabled": True, "test_command": "npm test"},
                },
                default_flow_style=False,
            )
        )
        config = BuildConfig(project_dir=tmp_path, config_dir=tmp_path / "c")
        config.specs_dir = specs
        config._load_specs_config()
        config._discover_test_command()
        assert config.test_command == "npm test"
        assert config.test_command_discovered_from == ""
        ok, why = mutation.mutation_applies(config)
        assert ok is False
        assert "npm test" in why and "package.json" in why
        assert "tdd.test_command" in why

    def test_configured_test_command_is_trusted(self, tmp_path):
        # tdd.test_command set in specs/config.yaml: discovery never runs, and
        # the project's own stated command is taken as the one that runs its
        # Python.
        (tmp_path / "requirements.txt").write_text("")
        (tmp_path / "package.json").write_text("{}")
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text("tdd:\n  test_command: uv run pytest -q\n")
        config = BuildConfig(project_dir=tmp_path, config_dir=tmp_path / "c")
        config.specs_dir = specs
        config._load_specs_config()
        config._discover_test_command()
        assert config.test_command == "uv run pytest -q"
        assert mutation.mutation_applies(config) == (True, "")

    def test_disabled_does_not_apply(self):
        config = BuildConfig(stack="pyproject", test_command="pytest")
        config.mutation = MutationSettings(enabled=False)
        ok, why = mutation.mutation_applies(config)
        assert ok is False and "enabled" in why

    def test_changed_source_files_drops_tests(self, project):
        repo, baseline = project
        assert mutation.changed_source_files(repo, baseline) == ["src/calc.py"]

    def test_changed_source_files_drops_files_no_test_imports(self, project):
        # Packaging, task-runner, docs and migration files run outside the
        # test suite: every mutant on them would survive, and the survivor
        # round (tests only) could never kill them.
        repo, baseline = project
        for name in (
            "setup.py",
            "noxfile.py",
            "fabfile.py",
            "manage.py",
            "src/setup.py",
            "docs/conf.py",
            "doc/conf.py",
            "src/app/migrations/0001_initial.py",
            "alembic/versions/abc123_init.py",
            "alembic/env.py",
        ):
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text("x = 1\n")
        # Look-alike names that are ordinary source stay in; a `docs` package
        # below the root is code, not a Sphinx tree.
        for name in (
            "src/docs.py",
            "src/migrations_helper.py",
            "src/docs/render.py",
            "src/setup_helpers.py",
        ):
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text("x = 1\n")
        _commit_all(repo, "chore: packaging and docs")
        assert mutation.changed_source_files(repo, baseline) == [
            "src/calc.py",
            "src/docs.py",
            "src/docs/render.py",
            "src/migrations_helper.py",
            "src/setup_helpers.py",
        ]

    def test_changed_source_files_drops_django_tests_modules(self, project):
        # Django's `startapp` writes each app's tests to `<app>/tests.py`.
        # It is a test module: mutating it would score the tests themselves.
        repo, baseline = project
        (repo / "polls").mkdir()
        (repo / "polls" / "tests.py").write_text("def test_x():\n    assert 1\n")
        (repo / "polls" / "views.py").write_text("x = 1\n")
        _commit_all(repo, "feat: polls app")
        assert mutation.changed_source_files(repo, baseline) == [
            "polls/views.py",
            "src/calc.py",
        ]


# --- run_mutation (subprocess boundary stubbed) ----------------------------


class TestRunMutation:
    def _stub_tool(self, dump=DUMP, on_exec=None):
        calls: list[list[str]] = []

        def _tool(argv, cwd, timeout):
            calls.append(argv)
            cmd = argv[len(mutation._UVX) :]
            if cmd[:2] == ["cosmic-ray", "dump"]:
                # First dump: everything pending; second: the finished session.
                return (
                    DUMP_PENDING
                    if sum(
                        1
                        for c in calls
                        if c[len(mutation._UVX) :][:2] == ["cosmic-ray", "dump"]
                    )
                    == 1
                    else dump
                )
            if cmd[:2] == ["cosmic-ray", "exec"] and on_exec:
                on_exec(cwd)
            return ""

        return calls, _tool

    def test_runs_the_command_sequence_from_the_project_dir(self, project):
        config, _, _ = _setup(project)
        repo, baseline = project
        calls, tool = self._stub_tool()
        with patch("build_pipeline.mutation._run_tool", side_effect=tool):
            r = mutation.run_mutation(config, baseline, ["src/calc.py"])
        steps = [c[len(mutation._UVX) :][:2] for c in calls]
        assert steps == [
            ["cosmic-ray", "init"],
            ["cr-filter-git", "--config"],
            ["cosmic-ray", "dump"],
            ["cosmic-ray", "baseline"],
            ["cosmic-ray", "exec"],
            ["cosmic-ray", "dump"],
        ]
        assert all(c[:3] == ["uvx", "--from", "cosmic-ray==8.7.0"] for c in calls)
        assert r.ok and r.score == pytest.approx(0.75) and r.candidates == 2

    def test_dirty_tree_after_the_run_is_discarded(self, project):
        config, _, _ = _setup(project)
        repo, baseline = project
        head = _git(repo, "rev-parse", "HEAD")

        def _leave_mutant(cwd):
            (Path(cwd) / "src" / "calc.py").write_text(
                "def add(a, b):\n    return a - b\n"
            )

        _, tool = self._stub_tool(on_exec=_leave_mutant)
        with (
            patch("build_pipeline.mutation._run_tool", side_effect=tool),
            patch(
                "build_pipeline.mutation.git_ops.discard_to",
                wraps=mutation.git_ops.discard_to,
            ) as discard,
        ):
            r = mutation.run_mutation(config, baseline, ["src/calc.py"])
        discard.assert_called_once()
        assert discard.call_args.args[1] == head
        assert r.mutant_left_on_disk is True
        assert "return a + b" in (repo / "src" / "calc.py").read_text()
        assert _git(repo, "status", "--porcelain") == ""

    def test_refuses_a_dirty_tree(self, project):
        config, _, _ = _setup(project)
        repo, baseline = project
        (repo / "src" / "calc.py").write_text("# uncommitted\n")
        with patch("build_pipeline.mutation._run_tool") as tool:
            r = mutation.run_mutation(config, baseline, ["src/calc.py"])
        tool.assert_not_called()
        assert r.ok is False and "uncommitted" in r.error

    def test_tool_failure_is_not_a_score(self, project):
        config, _, _ = _setup(project)
        _, baseline = project
        with patch(
            "build_pipeline.mutation._run_tool",
            side_effect=mutation._ToolError("cosmic-ray baseline exited 1: red"),
        ):
            r = mutation.run_mutation(config, baseline, ["src/calc.py"])
        assert r.ok is False and "baseline" in r.error and r.score is None

    def test_baseline_is_not_held_to_the_per_mutant_timeout(self, project):
        """A green suite slower than `mutation.timeout` must not read as a red
        baseline: the baseline runs under its own generous limit, and each
        mutant then gets at least `_BASELINE_FACTOR` times the measured run."""
        import tomllib

        config, _, _ = _setup(project)
        repo, baseline = project
        clock = [1000.0]
        seen: dict[str, float] = {}
        _, stub = self._stub_tool()

        def _tool(argv, cwd, timeout):
            cmd = argv[len(mutation._UVX) :]
            if cmd[:2] in (["cosmic-ray", "baseline"], ["cosmic-ray", "exec"]):
                cfg = tomllib.loads(Path(cmd[2]).read_text())
                seen[cmd[1]] = cfg["cosmic-ray"]["timeout"]
                seen[cmd[1] + "-outer"] = timeout
            if cmd[:2] == ["cosmic-ray", "baseline"]:
                clock[0] += 100.0  # a green suite that takes 100 s
            return stub(argv, cwd, timeout)

        fake_time = MagicMock()
        fake_time.monotonic.side_effect = lambda: clock[0]
        with (
            patch("build_pipeline.mutation._run_tool", side_effect=_tool),
            patch("build_pipeline.mutation.time", fake_time),
        ):
            r = mutation.run_mutation(config, baseline, ["src/calc.py"])
        assert r.ok, r.error
        assert config.mutation.timeout == 30
        assert seen["baseline"] >= mutation._SETUP_TIMEOUT
        assert seen["baseline-outer"] > seen["baseline"]
        assert seen["exec"] == pytest.approx(100.0 * mutation._BASELINE_FACTOR)
        assert r.test_timeout == pytest.approx(seen["exec"])
        report = mutation.render_report(
            first=r, final=r, round_note="", min_score=0.8, passed=True
        )
        assert "Time allowed per test run under a mutant: 300 s" in report

    def test_a_fast_suite_keeps_the_configured_timeout(self, project):
        assert mutation.mutant_timeout(30.0, 0.5) == 30.0
        assert mutation.mutant_timeout(30.0, 20.0) == 20.0 * mutation._BASELINE_FACTOR

    def test_an_interrupt_mid_run_still_discards_the_mutant(self, project):
        """Ctrl-C (KeyboardInterrupt) while cosmic-ray has a mutant on disk:
        the tree is discarded back to HEAD before the interrupt propagates, so
        a resume never sees the mutant as unverified work to commit."""
        config, _, _ = _setup(project)
        repo, baseline = project
        _, stub = self._stub_tool()

        def _tool(argv, cwd, timeout):
            if argv[len(mutation._UVX) :][:2] == ["cosmic-ray", "exec"]:
                (Path(cwd) / "src" / "calc.py").write_text(
                    "def add(a, b):\n    return a - b\n"
                )
                raise KeyboardInterrupt
            return stub(argv, cwd, timeout)

        with patch("build_pipeline.mutation._run_tool", side_effect=_tool):
            with pytest.raises(KeyboardInterrupt):
                mutation.run_mutation(config, baseline, ["src/calc.py"])
        assert "return a + b" in (repo / "src" / "calc.py").read_text()
        assert _git(repo, "status", "--porcelain") == ""

    def test_sigterm_mid_run_discards_the_mutant_and_exits(self, project):
        """SIGTERM's default action ends the process with no cleanup at all,
        leaving the detached cosmic-ray rewriting files. During the run it
        becomes a SystemExit(143) instead, so the tree is restored first; the
        previous handler is back afterwards. The handler is called directly
        rather than by sending a real SIGTERM, which would kill pytest itself
        if the handler were missing."""
        import signal

        config, _, _ = _setup(project)
        repo, baseline = project
        _, stub = self._stub_tool()
        before = signal.getsignal(signal.SIGTERM)
        assert before == signal.SIG_DFL

        def _tool(argv, cwd, timeout):
            if argv[len(mutation._UVX) :][:2] == ["cosmic-ray", "exec"]:
                (Path(cwd) / "src" / "calc.py").write_text(
                    "def add(a, b):\n    return a - b\n"
                )
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler), "no SIGTERM handler during the run"
                handler(signal.SIGTERM, None)
            return stub(argv, cwd, timeout)

        with patch("build_pipeline.mutation._run_tool", side_effect=_tool):
            with pytest.raises(SystemExit) as exc:
                mutation.run_mutation(config, baseline, ["src/calc.py"])
        assert exc.value.code == 128 + signal.SIGTERM
        assert "return a + b" in (repo / "src" / "calc.py").read_text()
        assert _git(repo, "status", "--porcelain") == ""
        assert signal.getsignal(signal.SIGTERM) == before

    def test_a_dirty_tree_that_cannot_be_discarded_is_flagged(self, project):
        config, _, _ = _setup(project)
        _, baseline = project

        def _leave_mutant(cwd):
            (Path(cwd) / "src" / "calc.py").write_text(
                "def add(a, b):\n    return a - b\n"
            )

        _, tool = self._stub_tool(on_exec=_leave_mutant)
        with (
            patch("build_pipeline.mutation._run_tool", side_effect=tool),
            patch("build_pipeline.mutation.git_ops.discard_to", return_value=False),
        ):
            r = mutation.run_mutation(config, baseline, ["src/calc.py"])
        assert r.ok is False and r.discard_failed is True
        assert "could NOT be discarded" in r.error


class TestRunTool:
    def test_missing_uvx_names_uv_and_both_ways_out(self, tmp_path):
        with patch.dict(os.environ, {"PATH": str(tmp_path)}):
            with pytest.raises(mutation._ToolError) as exc:
                mutation._run_tool(
                    [*mutation._UVX, "cosmic-ray", "init", "a", "b"], tmp_path, 10
                )
        msg = str(exc.value)
        assert msg.startswith("could not start uvx")
        assert "install uv" in msg
        assert "mutation.enabled: false" in msg

    def test_tool_that_exits_nonzero_names_the_way_to_turn_it_off(self, tmp_path):
        """uvx starts but cosmic-ray cannot be fetched or run (offline, no
        PyPI access, private index): the error carries the tool's own stderr
        and also says the step can be turned off."""
        uvx = tmp_path / "uvx"
        uvx.write_text(
            "#!/bin/sh\necho 'error: Failed to fetch cosmic-ray' >&2\nexit 2\n"
        )
        uvx.chmod(0o755)
        with patch.dict(os.environ, {"PATH": f"{tmp_path}:/usr/bin:/bin"}):
            with pytest.raises(mutation._ToolError) as exc:
                mutation._run_tool(
                    [*mutation._UVX, "cosmic-ray", "init", "a", "b"], tmp_path, 10
                )
        msg = str(exc.value)
        assert "exited 2" in msg and "Failed to fetch cosmic-ray" in msg
        assert "mutation.enabled: false" in msg

    def test_tool_that_times_out_names_the_way_to_turn_it_off(self, tmp_path):
        uvx = tmp_path / "uvx"
        uvx.write_text("#!/bin/sh\nsleep 30\n")
        uvx.chmod(0o755)
        with patch.dict(os.environ, {"PATH": f"{tmp_path}:/usr/bin:/bin"}):
            with pytest.raises(mutation._ToolError) as exc:
                mutation._run_tool(
                    [*mutation._UVX, "cosmic-ray", "init", "a", "b"], tmp_path, 1
                )
        msg = str(exc.value)
        assert "timed out" in msg
        assert "mutation.enabled: false" in msg

    def test_an_interrupt_kills_the_whole_process_group(self, tmp_path):
        """A KeyboardInterrupt while cosmic-ray runs must not leave its process
        group running: it was started in its own session, so the terminal's
        SIGINT never reaches it, and it would go on rewriting source files."""
        import signal
        import time

        pid_file = tmp_path / "child.pid"
        uvx = tmp_path / "uvx"
        uvx.write_text(f"#!/bin/sh\nsleep 7731 &\necho $! > {pid_file}\nwait\n")
        uvx.chmod(0o755)

        def _interrupt(signum, frame):
            raise KeyboardInterrupt

        def _alive(pid):
            out = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
            ).stdout.strip()
            return bool(out) and not out.startswith("Z")

        old = signal.signal(signal.SIGALRM, _interrupt)
        try:
            signal.setitimer(signal.ITIMER_REAL, 1.0)
            with patch.dict(os.environ, {"PATH": f"{tmp_path}:/usr/bin:/bin"}):
                with pytest.raises(KeyboardInterrupt):
                    mutation._run_tool(
                        [*mutation._UVX, "cosmic-ray", "exec", "a", "b"], tmp_path, 60
                    )
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
        child = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        alive = _alive(child)
        if alive:
            os.kill(child, signal.SIGKILL)
        assert not alive, "cosmic-ray's process group survived the interrupt"

    def test_a_real_ctrl_c_under_asyncio_run_kills_the_group_at_once(self, tmp_path):
        """buildme runs under `asyncio.run`, whose SIGINT handler only cancels
        the main task on the first Ctrl-C and raises nothing. `run_mutation`
        blocks the event loop in `communicate`, so without a handler of its
        own the first Ctrl-C would wait out cosmic-ray, which keeps mutating
        files meanwhile. A real SIGINT is sent here, through asyncio's handler,
        and must end the wait and kill the group well before the child exits."""
        import asyncio
        import signal
        import threading
        import time

        pid_file = tmp_path / "child.pid"
        uvx = tmp_path / "uvx"
        uvx.write_text(f"#!/bin/sh\nsleep 6 &\necho $! > {pid_file}\nwait\n")
        uvx.chmod(0o755)
        outcome = {}

        async def _main():
            assert signal.getsignal(signal.SIGINT) is not signal.default_int_handler, (
                "asyncio.run did not install its SIGINT handler"
            )
            threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGINT)).start()
            started = time.monotonic()
            try:
                with mutation._exit_on_termination():
                    mutation._run_tool(
                        [*mutation._UVX, "cosmic-ray", "exec", "a", "b"], tmp_path, 60
                    )
            except KeyboardInterrupt:
                outcome["raised_in_wait"] = True
                raise
            finally:
                outcome["elapsed"] = time.monotonic() - started

        with patch.dict(os.environ, {"PATH": f"{tmp_path}:/usr/bin:/bin"}):
            with pytest.raises(KeyboardInterrupt):
                asyncio.run(_main())
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        assert outcome.get("raised_in_wait"), "Ctrl-C did not interrupt the wait"
        assert outcome["elapsed"] < 4, (
            f"waited {outcome['elapsed']:.1f}s for cosmic-ray"
        )
        child = int(pid_file.read_text())
        out = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(child)], capture_output=True, text=True
        ).stdout.strip()
        assert not out or out.startswith("Z"), "cosmic-ray's group survived Ctrl-C"

    @staticmethod
    def _uvx_with_own_session_test(tmp_path, pid_file):
        """A `uvx` stub that starts its "test run" the way cosmic-ray 8.7.0's
        `testing.run_tests` does: `Popen(..., start_new_session=True)`, so the
        test command sits in a session of its own, outside cosmic-ray's group."""
        uvx = tmp_path / "uvx"
        uvx.write_text(
            f"#!{sys.executable}\n"
            "import subprocess\n"
            "p = subprocess.Popen(['sleep', '7733'], start_new_session=True)\n"
            f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
            "p.wait()\n"
        )
        uvx.chmod(0o755)

    @staticmethod
    def _gone(pid, wait=5.0):
        import signal
        import time

        def _alive():
            out = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
            ).stdout.strip()
            return bool(out) and not out.startswith("Z")

        deadline = time.monotonic() + wait
        while _alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if _alive():
            os.kill(pid, signal.SIGKILL)
            return False
        return True

    def test_a_timeout_kills_the_test_run_in_its_own_session(self, tmp_path):
        """cosmic-ray puts each test run in a session of its own, so killing
        cosmic-ray's group leaves it running: a looping mutant would run
        forever and could write into the tree after it was restored."""
        pid_file = tmp_path / "test.pid"
        self._uvx_with_own_session_test(tmp_path, pid_file)
        with patch.dict(os.environ, {"PATH": f"{tmp_path}:/usr/bin:/bin"}):
            with pytest.raises(mutation._ToolError):
                mutation._run_tool(
                    [*mutation._UVX, "cosmic-ray", "exec", "a", "b"], tmp_path, 1.5
                )
        assert self._gone(int(pid_file.read_text())), (
            "the in-flight test run survived the timeout"
        )

    @pytest.mark.parametrize("source", ["proc", "ps"])
    def test_process_table_reads_this_process(self, tmp_path, source):
        """The process table comes from /proc on Linux and from `ps` where
        there is no /proc (macOS); both must give pid, ppid and pgid."""
        if source == "proc" and not (mutation._PROC / "self" / "stat").exists():
            pytest.skip("no /proc here")
        with patch.object(
            mutation, "_PROC", mutation._PROC if source == "proc" else tmp_path / "none"
        ):
            table = mutation._process_table()
        assert (os.getpid(), os.getppid(), os.getpgrp()) in table

    def test_an_interrupt_kills_the_test_run_in_its_own_session(self, tmp_path):
        import signal

        pid_file = tmp_path / "test.pid"
        self._uvx_with_own_session_test(tmp_path, pid_file)

        def _interrupt(signum, frame):
            raise KeyboardInterrupt

        old = signal.signal(signal.SIGALRM, _interrupt)
        try:
            signal.setitimer(signal.ITIMER_REAL, 1.5)
            with patch.dict(os.environ, {"PATH": f"{tmp_path}:/usr/bin:/bin"}):
                with pytest.raises(KeyboardInterrupt):
                    mutation._run_tool(
                        [*mutation._UVX, "cosmic-ray", "exec", "a", "b"], tmp_path, 60
                    )
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
        assert self._gone(int(pid_file.read_text())), (
            "the in-flight test run survived the interrupt"
        )


DUMP_PENDING = "\n".join(
    [
        _dump_line(
            "a1",
            "src/calc.py",
            2,
            "core/ReplaceBinaryOperator_Add_Sub",
            "skipped",
            output="Filtered git",
        ),
        _dump_line(
            "k1", "src/calc.py", 10, "core/ReplaceComparisonOperator_Lt_Gt", None
        ),
        _dump_line(
            "s1", "src/calc.py", 10, "core/ReplaceComparisonOperator_Lt_LtE", None
        ),
    ]
)


# --- annotation-only mutants ----------------------------------------------

# store.py from the 2026-10-23 real-model run: every one of its 11 survivors
# was a `|` operator mutant on line 9's parameter annotation, which no test can
# kill because `from __future__ import annotations` never evaluates it.
STORE_PY = """\
from __future__ import annotations

import datetime


def add_task(
    tasks: list[int],
    title: str,
    due: datetime.date | None = None,
) -> int | None:
    count: int | None = len(tasks) | 0
    return count
"""


def _span_line(
    job_id, path, start, end, operator="core/ReplaceBinaryOperator_BitOr_Add"
):
    item = {
        "job_id": job_id,
        "mutations": [
            {
                "module_path": path,
                "operator_name": operator,
                "occurrence": 0,
                "start_pos": list(start),
                "end_pos": list(end),
            }
        ],
    }
    return json.dumps([item, None])


class TestAnnotationMutants:
    def _dump(self):
        return "\n".join(
            [
                # `|` in the parameter annotation on line 9
                _span_line("param", "src/store.py", (9, 23), (9, 24)),
                # `|` in the return annotation on line 10
                _span_line("ret", "src/store.py", (10, 9), (10, 10)),
                # `|` in the local variable annotation on line 11
                _span_line("local", "src/store.py", (11, 15), (11, 16)),
                # `|` in the assigned value on line 11: behaviour, kept
                _span_line("value", "src/store.py", (11, 35), (11, 36)),
                # the default value `None` on line 9: behaviour, kept
                _span_line("default", "src/store.py", (9, 32), (9, 36)),
            ]
        )

    def test_mutants_inside_annotations_are_listed(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "store.py").write_text(STORE_PY)
        ids = mutation.annotation_only_jobs(self._dump(), tmp_path)
        assert sorted(ids) == ["local", "param", "ret"]

    def test_a_file_that_does_not_parse_lists_nothing(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "store.py").write_text("def broken(:\n")
        assert mutation.annotation_only_jobs(self._dump(), tmp_path) == []

    def test_a_missing_file_lists_nothing(self, tmp_path):
        assert mutation.annotation_only_jobs(self._dump(), tmp_path) == []

    def test_columns_count_characters_not_bytes(self, tmp_path):
        (tmp_path / "m.py").write_text(
            "from __future__ import annotations\n\n\n"
            "def f(\u00e9t\u00e9: int | None) -> None:\n    return None\n"
        )
        dump = _span_line("p", "m.py", (4, 15), (4, 16))
        assert mutation.annotation_only_jobs(dump, tmp_path) == ["p"]

    def test_run_skips_annotation_mutants_and_leaves_them_out_of_the_score(
        self, project
    ):
        config, _, _ = _setup(project)
        repo, baseline = project
        (repo / "src" / "store.py").write_text(STORE_PY)
        _commit_all(repo, "store")
        pending = self._dump()
        final = "\n".join(
            json.dumps(
                [
                    json.loads(line)[0],
                    {
                        "worker_outcome": "normal",
                        "output": "1 failed",
                        "test_outcome": "killed",
                        "diff": "",
                    },
                ]
            )
            for line in pending.splitlines()
            if json.loads(line)[0]["job_id"] in ("value", "default")
        )
        calls: list[list[str]] = []
        skipped: list[tuple[list[str], str]] = []

        def _tool(argv, cwd, timeout):
            calls.append(argv)
            cmd = argv[len(mutation._UVX) :]
            if cmd[:2] == ["cosmic-ray", "dump"]:
                dumps = sum(1 for c in calls if c[len(mutation._UVX) :][:2] == cmd[:2])
                return pending if dumps == 1 else final
            if cmd[0] == "python":
                skipped.append((json.loads(Path(cmd[4]).read_text()), cmd[5]))
            return ""

        with patch("build_pipeline.mutation._run_tool", side_effect=_tool):
            r = mutation.run_mutation(config, baseline, ["src/store.py"])
        assert r.ok, r.error
        assert skipped == [
            (["param", "ret", "local"], mutation._ANNOTATION_ONLY),
        ]
        assert (r.killed, r.survived, r.score) == (2, 0, 1.0)
        assert (r.candidates, r.annotation_only, r.sampled_out) == (5, 3, 0)
        report = mutation.render_report(
            first=r, final=r, round_note="", min_score=0.8, passed=True
        )
        assert "3 mutants inside type annotations were not run" in report

    def test_the_sample_is_drawn_after_annotation_mutants_are_dropped(self, project):
        config, _, _ = _setup(project)
        config.mutation.max_mutants = 1
        repo, baseline = project
        (repo / "src" / "store.py").write_text(STORE_PY)
        _commit_all(repo, "store")
        calls: list[list[str]] = []
        skipped: list[list[str]] = []

        def _tool(argv, cwd, timeout):
            calls.append(argv)
            cmd = argv[len(mutation._UVX) :]
            if cmd[:2] == ["cosmic-ray", "dump"]:
                return self._dump()
            if cmd[0] == "python":
                skipped.append(json.loads(Path(cmd[4]).read_text()))
            return ""

        with patch("build_pipeline.mutation._run_tool", side_effect=_tool):
            r = mutation.run_mutation(config, baseline, ["src/store.py"])
        assert r.ok, r.error
        [annotations, sampled] = skipped
        assert sorted(annotations) == ["local", "param", "ret"]
        assert len(sampled) == 1 and sampled[0] in ("value", "default")
        assert (r.candidates, r.annotation_only, r.sampled_out) == (5, 3, 1)


# --- the engine step -------------------------------------------------------


class TestRunMutationStep:
    @pytest.mark.asyncio
    async def test_non_python_stack_skips_without_running_the_tool(self, project):
        repo = project[0]
        (repo / "pyproject.toml").unlink()
        (repo / "package.json").write_text("{}")
        _commit_all(repo, "chore: a node project")
        config, state, progress = _setup(project, stack="package")
        with patch("build_pipeline.tdd_engine.run_mutation") as run:
            assert await _run_mutation(config, state, progress) is True
        run.assert_not_called()
        assert state.tdd.mutation_done is True
        assert "SKIPPED" in (project[0] / "build-progress.md").read_text()

    @pytest.mark.asyncio
    async def test_no_changed_python_file_skips(self, project):
        config, state, progress = _setup(project)
        state.tdd.blocks[0].baseline_commit = _git(project[0], "rev-parse", "HEAD")
        with patch("build_pipeline.tdd_engine.run_mutation") as run:
            assert await _run_mutation(config, state, progress) is True
        run.assert_not_called()
        assert state.tdd.mutation_done is True

    @pytest.mark.asyncio
    async def test_resume_after_a_passed_step_makes_no_cosmic_ray_call(self, project):
        config, state, progress = _setup(project, mutation_done=True)
        with (
            patch("build_pipeline.mutation._run_tool") as tool,
            patch("build_pipeline.tdd_engine.run_mutation") as run,
        ):
            assert await _run_mutation(config, state, progress) is True
        tool.assert_not_called()
        run.assert_not_called()

    @pytest.mark.asyncio
    async def test_below_min_score_fails_and_leaves_the_flag_unset(self, project):
        config, state, progress = _setup(project, min_score=0.8)
        with (
            patch("build_pipeline.tdd_engine.run_mutation", return_value=_result(7, 3)),
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer",
                new_callable=AsyncMock,
                return_value=AgentResult(success=True, output="nothing to add"),
            ),
        ):
            assert await _run_mutation(config, state, progress) is False
        assert state.tdd.mutation_done is False
        report = (config.change_dir / "mutation.md").read_text()
        assert "FAILED" in report and "0.70" in report
        assert "FAIL" in (project[0] / "build-progress.md").read_text()

    @pytest.mark.asyncio
    async def test_at_min_score_passes_and_sets_the_flag(self, project):
        config, state, progress = _setup(project, min_score=0.8)
        with patch(
            "build_pipeline.tdd_engine.run_mutation", return_value=_result(8, 0)
        ):
            assert await _run_mutation(config, state, progress) is True
        assert state.tdd.mutation_done is True
        assert BuildState.load(config.state_file_path()).tdd.mutation_done is True
        report = (config.change_dir / "mutation.md").read_text()
        assert (
            "PASSED" in report
            and "cosmic-ray 8.7.0" in report
            and "`src/calc.py`" in report
        )
        assert (
            _git(project[0], "log", "-1", "--format=%s")
            == "test(mutation): record mutation score"
        )

    @pytest.mark.asyncio
    async def test_no_mutant_generated_passes_with_no_score(self, project):
        """No mutant on the changed lines (nothing errored either): nothing to
        score, so the step passes."""
        config, state, progress = _setup(project)
        with patch(
            "build_pipeline.tdd_engine.run_mutation", return_value=_result(0, 0)
        ):
            assert await _run_mutation(config, state, progress) is True
        assert state.tdd.mutation_done is True
        assert "n/a" in (config.change_dir / "mutation.md").read_text()

    @pytest.mark.asyncio
    async def test_every_mutant_errored_fails_the_step(self, project):
        """Mutants were generated but none could be judged (worker exception,
        abnormal exit, incompetent): cosmic-ray produced no score, which fails
        the build rather than passing as "no mutants ran"."""
        config, state, progress = _setup(project)
        repo = project[0]
        head = _git(repo, "rev-parse", "HEAD")
        errored = _result(0, 0)
        errored.errors = 3
        with (
            patch("build_pipeline.tdd_engine.run_mutation", return_value=errored),
            patch("build_pipeline.tdd_engine.run_mutation_test_writer") as writer,
        ):
            assert await _run_mutation(config, state, progress) is False
        writer.assert_not_called()
        assert state.tdd.mutation_done is False
        assert _git(repo, "rev-parse", "HEAD") == head
        log_text = (repo / "build-progress.md").read_text()
        assert "did not produce a score" in log_text and "PASSED" not in log_text

    @pytest.mark.asyncio
    async def test_survivor_round_gets_the_configured_turn_cap(self, project):
        config, state, progress = _setup(project)
        config.mutation_test_writer_max_turns = 11
        with (
            patch("build_pipeline.tdd_engine.run_mutation", return_value=_result(9, 1)),
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer",
                new_callable=AsyncMock,
                return_value=AgentResult(success=True, output="nothing to add"),
            ) as writer,
        ):
            await _run_mutation(config, state, progress)
        assert writer.await_args.kwargs["max_turns"] == 11

    TURN_LIMIT = AgentResult(
        success=False,
        output="still working",
        error="Reached maximum number of turns (30)\nCLI stderr:\n",
    )

    @pytest.mark.asyncio
    async def test_s10_a_capped_round_with_a_green_suite_is_kept(self, project):
        config, state, progress = _setup(project)
        repo = project[0]

        async def _agent(**_kw):
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 1\n"
            )
            return self.TURN_LIMIT

        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation",
                side_effect=[_result(7, 3), _result(10, 0)],
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is True
        assert run.call_count == 2
        assert "kill surviving mutants" in _git(repo, "log", "--format=%s")
        assert (
            "ran out of turns; its tests passed the round's checks and were kept"
            in (config.change_dir / "mutation.md").read_text()
        )

    @pytest.mark.asyncio
    async def test_s11_a_capped_round_that_edited_a_source_file_is_discarded(
        self, project
    ):
        config, state, progress = _setup(project)
        repo = project[0]

        async def _agent(**_kw):
            (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 0\n")
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 1\n"
            )
            return self.TURN_LIMIT

        with (
            patch("build_pipeline.tdd_engine.run_mutation", return_value=_result(9, 1)),
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is False
        assert "return a + b" in (repo / "src" / "calc.py").read_text()
        assert not (repo / "tests" / "test_more.py").exists()
        assert "src/calc.py" in (config.change_dir / "mutation.md").read_text()

    @pytest.mark.asyncio
    async def test_s11_a_capped_round_with_a_red_suite_is_discarded(self, project):
        config, state, progress = _setup(project, test_command="false")
        repo = project[0]

        async def _agent(**_kw):
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 2\n"
            )
            return self.TURN_LIMIT

        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation", return_value=_result(9, 1)
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is True
        assert run.call_count == 1
        assert not (repo / "tests" / "test_more.py").exists()
        assert "kill surviving mutants" not in _git(repo, "log", "--format=%s")
        assert "left the suite red" in (config.change_dir / "mutation.md").read_text()

    @pytest.mark.asyncio
    async def test_survivor_round_that_adds_a_non_test_file_fails_the_step(
        self, project
    ):
        """A new untracked non-test file of any extension (a source module, a
        root pytest.ini that changes how the second run is scored) is a
        non-test edit: the round is discarded and nothing is committed."""
        config, state, progress = _setup(project)
        repo = project[0]

        async def _agent(**_kw):
            (repo / "src" / "helper.py").write_text("X = 1\n")
            (repo / "pytest.ini").write_text("[pytest]\naddopts = -k nothing\n")
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 1\n"
            )
            return AgentResult(success=True, output="STATUS: DONE")

        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation", return_value=_result(9, 1)
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is False
        assert run.call_count == 1
        assert state.tdd.mutation_done is False
        assert "kill surviving mutants" not in _git(repo, "log", "--format=%s")
        assert not (repo / "pytest.ini").exists()
        assert not (repo / "src" / "helper.py").exists()
        report = (config.change_dir / "mutation.md").read_text()
        assert "pytest.ini" in report and "src/helper.py" in report

    @pytest.mark.asyncio
    async def test_survivor_round_that_only_leaves_tool_caches_is_committed(
        self, project
    ):
        """Running the suite writes caches (`__pycache__`, `.pytest_cache`,
        `.coverage`) into a project with no .gitignore for them. Those are not
        edits, so they must not discard a green round."""
        config, state, progress = _setup(project)
        repo = project[0]

        async def _agent(**_kw):
            (repo / "src" / "__pycache__").mkdir()
            (repo / "src" / "__pycache__" / "calc.cpython-312.pyc").write_bytes(b"\0")
            (repo / ".pytest_cache" / "v").mkdir(parents=True)
            (repo / ".pytest_cache" / "v" / "lastfailed").write_text("{}")
            (repo / ".coverage").write_text("x")
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 1\n"
            )
            return AgentResult(success=True, output="STATUS: DONE")

        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation",
                side_effect=[_result(7, 3), _result(10, 0)],
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is True
        assert run.call_count == 2

    @pytest.mark.asyncio
    async def test_survivor_round_git_refuses_to_commit_is_discarded(self, project):
        """A pre-commit hook rejects the round's tests: the round is discarded
        and the first-run score stands, rather than the build failing with
        the round's edits left in the tree."""
        repo = project[0]
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(
            "#!/bin/sh\n"
            "git diff --cached --name-only | grep -q test_more.py && exit 1\n"
            "exit 0\n"
        )
        hook.chmod(0o755)
        config, state, progress = _setup(project)

        async def _agent(**_kw):
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 1\n"
            )
            return AgentResult(success=True, output="STATUS: DONE")

        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation",
                side_effect=[_result(7, 3), _result(10, 0)],
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            await _run_mutation(config, state, progress)
        assert run.call_count == 1
        assert not (repo / "tests" / "test_more.py").exists()
        assert "could not be committed and were discarded" in (
            config.change_dir / "mutation.md"
        ).read_text()

    @pytest.mark.asyncio
    async def test_survivor_round_that_rewrites_tracked_tool_caches_is_committed(
        self, project
    ):
        """Block commits run `git add -A`, so in a project whose .gitignore
        does not cover them, `.pyc` and `.coverage` files end up tracked.
        cosmic-ray restoring the source changes its mtime, so the next pytest
        run rewrites the tracked `.pyc` with new bytes. That is not an edit
        and must not discard a green round."""
        repo = project[0]
        (repo / "src" / "__pycache__").mkdir()
        (repo / "src" / "__pycache__" / "calc.cpython-312.pyc").write_bytes(b"old")
        (repo / ".coverage").write_text("old")
        _commit_all(repo, "feat: block commit that swept in caches")
        config, state, progress = _setup(project)

        async def _agent(**_kw):
            (repo / "src" / "__pycache__" / "calc.cpython-312.pyc").write_bytes(b"new")
            (repo / ".coverage").write_text("new")
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 1\n"
            )
            return AgentResult(success=True, output="STATUS: DONE")

        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation",
                side_effect=[_result(7, 3), _result(10, 0)],
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is True
        assert run.call_count == 2
        assert state.tdd.mutation_done is True

    @pytest.mark.asyncio
    async def test_survivor_round_that_rewrites_tracked_suite_reports_is_committed(
        self, project
    ):
        """Block commits run `git add -A`, so a project whose suite writes
        coverage.xml, htmlcov/, a junit report or an *.egg-info (from an
        install) tracks them. The suite rewrites them on every run; that is not
        an edit and must not discard a green round."""
        repo = project[0]
        reports = [
            "coverage.xml",
            "coverage.json",
            "htmlcov/index.html",
            "junit.xml",
            "reports/junit-unit.xml",
            "src/calc.egg-info/PKG-INFO",
            "lcov.info",
        ]
        for name in reports:
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text("old")
        _commit_all(repo, "feat: block commit that swept in suite reports")
        config, state, progress = _setup(project)

        async def _agent(**_kw):
            for name in reports:
                (repo / name).write_text("new")
            (repo / "htmlcov" / "calc_py.html").write_text("new")
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 1\n"
            )
            return AgentResult(success=True, output="STATUS: DONE")

        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation",
                side_effect=[_result(7, 3), _result(10, 0)],
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is True
        assert run.call_count == 2
        assert state.tdd.mutation_done is True

    @staticmethod
    def _suite_that_writes(tmp_path: Path, names: list[str]) -> str:
        """A test command that, like a real suite, writes each of *names* with
        fresh content on every run (a sqlite file, a log, a benchmark, an HTML
        report)."""
        script = tmp_path / "suite.py"
        script.write_text(
            "import pathlib, time\n"
            f"for name in {names!r}:\n"
            "    p = pathlib.Path(name)\n"
            "    p.parent.mkdir(parents=True, exist_ok=True)\n"
            "    p.write_text(str(time.time_ns()))\n"
        )
        return f"{sys.executable} {script}"

    @pytest.mark.asyncio
    async def test_survivor_round_whose_suite_writes_other_files_is_committed(
        self, project, tmp_path
    ):
        """A suite may write any file its project does not ignore: a test.db,
        a *.log, a .benchmarks/ entry, report.html — new ones, or ones a block
        commit already tracks. The agent's own suite runs write them too. They
        are not edits, so they must not discard a green tests-only round."""
        repo = project[0]
        written = ["test.db", "run.log", ".benchmarks/0001.json", "report.html"]
        (repo / "test.db").write_text("old")
        (repo / "run.log").write_text("old")
        _commit_all(repo, "feat: block commit that swept in suite output")
        test_command = self._suite_that_writes(tmp_path, written)
        config, state, progress = _setup(project, test_command=test_command)

        async def _agent(**_kw):
            subprocess.run(test_command.split(), cwd=str(repo), check=True)
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 1\n"
            )
            return AgentResult(success=True, output="STATUS: DONE")

        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation",
                side_effect=[_result(7, 3), _result(10, 0)],
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is True
        assert run.call_count == 2
        assert state.tdd.mutation_done is True
        assert "kill surviving mutants" in _git(repo, "log", "--format=%s")

    @pytest.mark.asyncio
    async def test_source_edit_is_caught_when_the_suite_also_writes_files(
        self, project, tmp_path
    ):
        """The suite's own output is set aside, but a source edit made in the
        same round still fails the step, and only the source file is named."""
        repo = project[0]
        test_command = self._suite_that_writes(tmp_path, ["test.db"])
        config, state, progress = _setup(project, test_command=test_command)

        async def _agent(**_kw):
            subprocess.run(test_command.split(), cwd=str(repo), check=True)
            (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 0\n")
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 1\n"
            )
            return AgentResult(success=True, output="STATUS: DONE")

        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation", return_value=_result(9, 1)
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is False
        assert run.call_count == 1
        assert state.tdd.mutation_done is False
        assert "return a + b" in (repo / "src" / "calc.py").read_text()
        assert "kill surviving mutants" not in _git(repo, "log", "--format=%s")
        report = (config.change_dir / "mutation.md").read_text()
        assert "src/calc.py" in report and "test.db" not in report

    @pytest.mark.asyncio
    async def test_survivor_round_whose_suite_rewrites_a_golden_file_is_committed(
        self, project, tmp_path
    ):
        """A tracked file the full suite rewrites to its committed bytes (a
        golden JSON, a test.db built from fixed fixtures) is the suite's
        output even when it comes out the same as before the round. The
        agent's partial run left other bytes in it; that is not an edit and
        must not discard a green tests-only round."""
        repo = project[0]
        (repo / "golden.json").write_text('{"full": true}')
        _commit_all(repo, "feat: block commit that swept in the golden file")
        script = tmp_path / "suite.py"
        script.write_text(
            "import pathlib\n"
            "pathlib.Path('golden.json').write_text('{\"full\": true}')\n"
        )
        test_command = f"{sys.executable} {script}"
        config, state, progress = _setup(project, test_command=test_command)

        async def _agent(**_kw):
            # What running one test file alone writes there.
            (repo / "golden.json").write_text('{"full": false}')
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 1\n"
            )
            return AgentResult(success=True, output="STATUS: DONE")

        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation",
                side_effect=[_result(7, 3), _result(10, 0)],
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is True
        assert run.call_count == 2
        assert state.tdd.mutation_done is True
        assert "kill surviving mutants" in _git(repo, "log", "--format=%s")
        assert (repo / "golden.json").read_text() == '{"full": true}'

    @pytest.mark.asyncio
    async def test_source_edit_is_caught_when_the_suite_rewrites_a_golden_file(
        self, project, tmp_path
    ):
        """Setting the golden file aside does not let a source edit through."""
        repo = project[0]
        (repo / "golden.json").write_text('{"full": true}')
        _commit_all(repo, "feat: block commit that swept in the golden file")
        script = tmp_path / "suite.py"
        script.write_text(
            "import pathlib\n"
            "pathlib.Path('golden.json').write_text('{\"full\": true}')\n"
        )
        test_command = f"{sys.executable} {script}"
        config, state, progress = _setup(project, test_command=test_command)

        async def _agent(**_kw):
            (repo / "golden.json").write_text('{"full": false}')
            (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 0\n")
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 1\n"
            )
            return AgentResult(success=True, output="STATUS: DONE")

        with (
            patch("build_pipeline.tdd_engine.run_mutation", return_value=_result(9, 1)),
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is False
        assert "return a + b" in (repo / "src" / "calc.py").read_text()
        report = (config.change_dir / "mutation.md").read_text()
        assert "src/calc.py" in report and "golden.json" not in report

    @pytest.mark.asyncio
    async def test_survivor_round_that_edits_a_django_tests_module_is_committed(
        self, project
    ):
        """`<app>/tests.py` is where Django keeps an app's tests; the survivor
        round may edit it."""
        repo = project[0]
        (repo / "polls").mkdir()
        (repo / "polls" / "tests.py").write_text("def test_x():\n    assert 1 == 1\n")
        _commit_all(repo, "feat: polls app")
        config, state, progress = _setup(project)

        async def _agent(**_kw):
            with (repo / "polls" / "tests.py").open("a") as f:
                f.write("\n\ndef test_y():\n    assert 2 == 2\n")
            return AgentResult(success=True, output="STATUS: DONE")

        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation",
                side_effect=[_result(7, 3), _result(10, 0)],
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is True
        assert run.call_count == 2
        assert "kill surviving mutants" in _git(repo, "log", "--format=%s")

    @pytest.mark.asyncio
    async def test_survivor_round_that_adds_a_helper_under_tests_is_committed(
        self, project
    ):
        """Anything under a tests directory is a test, helpers included."""
        config, state, progress = _setup(project)
        repo = project[0]

        async def _agent(**_kw):
            (repo / "tests" / "helpers.py").write_text("LIMIT = 9\n")
            (repo / "tests" / "test_more.py").write_text(
                "from tests.helpers import LIMIT\n\n\ndef test_x():\n    assert LIMIT == 9\n"
            )
            return AgentResult(success=True, output="STATUS: DONE")

        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation",
                side_effect=[_result(7, 3), _result(10, 0)],
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is True
        assert run.call_count == 2

    @pytest.mark.asyncio
    async def test_survivor_round_that_edits_source_fails_the_step(self, project):
        config, state, progress = _setup(project)
        repo = project[0]

        async def _agent(**_kw):
            (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 0\n")
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 1\n"
            )
            return AgentResult(success=True, output="STATUS: DONE")

        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation", return_value=_result(9, 1)
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is False
        assert run.call_count == 1
        assert state.tdd.mutation_done is False
        # The round was discarded: the source edit and the new test are gone.
        assert "return a + b" in (repo / "src" / "calc.py").read_text()
        assert not (repo / "tests" / "test_more.py").exists()
        assert "src/calc.py" in (config.change_dir / "mutation.md").read_text()

    @pytest.mark.asyncio
    async def test_survivor_round_that_leaves_the_suite_red_is_discarded(self, project):
        config, state, progress = _setup(project, min_score=0.5)
        repo = project[0]

        async def _agent(**_kw):
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 2\n"
            )
            return AgentResult(success=True, output="STATUS: DONE")

        red = MagicMock(passed=False, reason="1 failed")
        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation", return_value=_result(9, 1)
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
            patch("build_pipeline.tdd_engine.integration_gate", return_value=red),
        ):
            assert await _run_mutation(config, state, progress) is True
        # No second run: the first-run score (0.90) stands.
        assert run.call_count == 1
        assert not (repo / "tests" / "test_more.py").exists()
        report = (config.change_dir / "mutation.md").read_text()
        assert "red" in report and "0.90" in report

    @pytest.mark.asyncio
    async def test_green_survivor_round_is_committed_and_rescored(self, project):
        config, state, progress = _setup(project)
        repo = project[0]

        async def _agent(**kw):
            assert "src/calc.py:5" in kw["survivors"]
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 1\n"
            )
            return AgentResult(success=True, output="STATUS: DONE")

        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation",
                side_effect=[_result(7, 3), _result(10, 0)],
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is True
        assert run.call_count == 2
        subjects = _git(repo, "log", "-2", "--format=%s").splitlines()
        assert subjects == [
            "test(mutation): record mutation score",
            "test(mutation): kill surviving mutants",
        ]
        report = (config.change_dir / "mutation.md").read_text()
        assert "0.70" in report and "1.00" in report

    @pytest.mark.asyncio
    async def test_a_round_the_agent_committed_itself_is_rescored(self, project):
        """The test writer has Bash: if it commits its own tests, buildme still
        checks them, commits them under its own message, and runs cosmic-ray again."""
        config, state, progress = _setup(project)
        repo = project[0]

        async def _agent(**_kw):
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 1\n"
            )
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "agent: my own commit")
            return AgentResult(success=True, output="STATUS: DONE")

        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation",
                side_effect=[_result(7, 3), _result(10, 0)],
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is True
        assert run.call_count == 2
        subjects = _git(repo, "log", "--format=%s").splitlines()
        assert subjects[:2] == [
            "test(mutation): record mutation score",
            "test(mutation): kill surviving mutants",
        ]
        assert "agent: my own commit" not in subjects
        assert (repo / "tests" / "test_more.py").exists()
        assert "1.00" in (config.change_dir / "mutation.md").read_text()

    @pytest.mark.asyncio
    async def test_a_source_edit_the_agent_committed_itself_is_caught(self, project):
        config, state, progress = _setup(project)
        repo = project[0]
        head = _git(repo, "rev-parse", "HEAD")

        async def _agent(**_kw):
            (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 0\n")
            _git(repo, "commit", "-q", "-am", "agent: sneaky source edit")
            return AgentResult(success=True, output="STATUS: DONE")

        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation", return_value=_result(9, 1)
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is False
        assert run.call_count == 1
        assert state.tdd.mutation_done is False
        assert "return a + b" in (repo / "src" / "calc.py").read_text()
        assert "agent: sneaky source edit" not in _git(repo, "log", "--format=%s")
        assert _git(repo, "rev-parse", "HEAD~1") == head

    @pytest.mark.asyncio
    async def test_a_round_that_moved_head_off_the_checkpoint_fails_the_step(
        self, project
    ):
        """HEAD moved somewhere that does not contain the checkpoint: nothing
        can be unwound safely, so the step fails and commits nothing."""
        config, state, progress = _setup(project)
        repo = project[0]

        async def _agent(**_kw):
            _git(repo, "checkout", "-q", "--detach", "HEAD~1")
            return AgentResult(success=True, output="STATUS: DONE")

        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation", return_value=_result(9, 1)
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is False
        assert run.call_count == 1
        assert state.tdd.mutation_done is False
        assert "test(mutation)" not in _git(repo, "log", "--all", "--format=%s")

    @pytest.mark.asyncio
    async def test_a_round_that_switched_branch_without_committing_fails_the_step(
        self, project
    ):
        """The agent switched to another branch at the same sha and left its
        tests uncommitted. HEAD's sha did not move, but a commit now would land
        on that other branch, never on the build branch: the step fails and
        commits nothing anywhere."""
        config, state, progress = _setup(project)
        repo = project[0]
        branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")

        async def _agent(**_kw):
            _git(repo, "switch", "-q", "-c", "scratch")
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 1\n"
            )
            return AgentResult(success=True, output="STATUS: DONE")

        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation", return_value=_result(9, 1)
            ) as run,
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is False
        assert run.call_count == 1
        assert state.tdd.mutation_done is False
        assert "test(mutation)" not in _git(repo, "log", "--all", "--format=%s")
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "scratch"
        assert branch != "scratch"

    @pytest.mark.asyncio
    async def test_uncommitted_edits_at_entry_are_not_committed_or_mutated(
        self, project
    ):
        """Edits the whole-change refactor left uncommitted (verification failed
        and the discard was refused, or a red suite skipped its checkpoint) are
        unverified: the step must not commit them onto the branch or score them."""
        config, state, progress = _setup(project)
        repo = project[0]
        head = _git(repo, "rev-parse", "HEAD")
        (repo / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a - b  # unverified\n"
        )
        with patch("build_pipeline.tdd_engine.run_mutation") as run:
            assert await _run_mutation(config, state, progress) is False
        run.assert_not_called()
        assert state.tdd.mutation_done is False
        assert _git(repo, "rev-parse", "HEAD") == head
        assert "unverified" in (repo / "src" / "calc.py").read_text()
        assert "uncommitted" in (repo / "build-progress.md").read_text()

    # --- a discard that fails must fail the step, and commit nothing ------

    def _no_discard(self):
        return patch("build_pipeline.tdd_engine.git_ops.discard_to", return_value=False)

    def _assert_nothing_committed_after(self, repo, head_before):
        # The pre-mutation checkpoint is a no-op on the committed fixture, so
        # HEAD must not have moved, and no report commit swept the tree in.
        assert _git(repo, "rev-parse", "HEAD") == head_before
        assert "test(mutation)" not in _git(repo, "log", "--format=%s")

    @pytest.mark.asyncio
    async def test_failed_agent_whose_edits_cannot_be_discarded_fails_the_step(
        self, project
    ):
        config, state, progress = _setup(project, min_score=0.5)
        repo = project[0]
        head = _git(repo, "rev-parse", "HEAD")

        async def _agent(**_kw):
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 2\n"
            )
            return AgentResult(success=False, error="crashed")

        with (
            patch("build_pipeline.tdd_engine.run_mutation", return_value=_result(9, 1)),
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
            self._no_discard(),
        ):
            assert await _run_mutation(config, state, progress) is False
        assert state.tdd.mutation_done is False
        self._assert_nothing_committed_after(repo, head)
        assert "NOT discarded" in (repo / "build-progress.md").read_text()

    @pytest.mark.asyncio
    async def test_red_round_that_cannot_be_discarded_fails_the_step(self, project):
        config, state, progress = _setup(project, min_score=0.5)
        repo = project[0]
        head = _git(repo, "rev-parse", "HEAD")

        async def _agent(**_kw):
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 2\n"
            )
            return AgentResult(success=True, output="STATUS: DONE")

        red = MagicMock(passed=False, reason="1 failed")
        with (
            patch("build_pipeline.tdd_engine.run_mutation", return_value=_result(9, 1)),
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
            patch("build_pipeline.tdd_engine.integration_gate", return_value=red),
            self._no_discard(),
        ):
            assert await _run_mutation(config, state, progress) is False
        assert state.tdd.mutation_done is False
        self._assert_nothing_committed_after(repo, head)

    @pytest.mark.asyncio
    async def test_source_edit_that_cannot_be_discarded_fails_without_committing(
        self, project
    ):
        config, state, progress = _setup(project)
        repo = project[0]
        head = _git(repo, "rev-parse", "HEAD")

        async def _agent(**_kw):
            (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 0\n")
            return AgentResult(success=True, output="STATUS: DONE")

        with (
            patch("build_pipeline.tdd_engine.run_mutation", return_value=_result(9, 1)),
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
            self._no_discard(),
        ):
            assert await _run_mutation(config, state, progress) is False
        assert state.tdd.mutation_done is False
        self._assert_nothing_committed_after(repo, head)

    @pytest.mark.asyncio
    async def test_second_run_that_left_a_mutant_fails_even_above_min_score(
        self, project
    ):
        config, state, progress = _setup(project, min_score=0.5)
        repo = project[0]

        async def _agent(**_kw):
            (repo / "tests" / "test_more.py").write_text(
                "def test_x():\n    assert 1 == 1\n"
            )
            return AgentResult(success=True, output="STATUS: DONE")

        stuck = MutationResult(
            ok=False,
            discard_failed=True,
            mutant_left_on_disk=True,
            error="a mutated file was left in the tree and could NOT be discarded",
        )

        def _second():
            (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
            return stuck

        runs = iter([lambda: _result(9, 1), _second])
        with (
            patch(
                "build_pipeline.tdd_engine.run_mutation",
                side_effect=lambda *a, **k: next(runs)(),
            ),
            patch(
                "build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent
            ),
        ):
            assert await _run_mutation(config, state, progress) is False
        assert state.tdd.mutation_done is False
        # The round's tests were committed; the mutant and the report were not.
        assert (
            _git(repo, "log", "-1", "--format=%s")
            == "test(mutation): kill surviving mutants"
        )
        assert "return a - b" in (repo / "src" / "calc.py").read_text()
        assert _git(repo, "status", "--porcelain").strip() != ""

    @pytest.mark.asyncio
    async def test_tool_error_fails_the_step(self, project):
        config, state, progress = _setup(project)
        bad = MutationResult(ok=False, error="uvx not found")
        with patch("build_pipeline.tdd_engine.run_mutation", return_value=bad):
            assert await _run_mutation(config, state, progress) is False
        assert state.tdd.mutation_done is False

    @pytest.mark.asyncio
    async def test_budget_breaker_stops_before_the_agent(self, project):
        from build_pipeline.budget import BudgetExceededError

        config, state, progress = _setup(project)
        agent = AsyncMock()
        with (
            patch("build_pipeline.tdd_engine.run_mutation", return_value=_result(9, 1)),
            patch(
                "build_pipeline.tdd_engine.budget_mod.check",
                side_effect=BudgetExceededError("over", diagnosis="d"),
            ),
            patch("build_pipeline.tdd_engine.run_mutation_test_writer", agent),
        ):
            assert await _run_mutation(config, state, progress) is False
        agent.assert_not_awaited()
        assert state.tdd.mutation_done is False


# --- the real tool ---------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("cosmic-ray") is None or shutil.which("uvx") is None,
    reason="cosmic-ray is not installed",
)
def test_real_cosmic_ray_scores_only_the_changed_lines(tmp_path):
    """Runs cosmic-ray for real on a two-commit fixture: mutants on the
    baseline's lines are filtered out, the changed lines are scored, and the
    tree is clean afterwards."""
    repo = _repo(tmp_path / "real")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "test_calc.py").write_text(
        "from calc import add, clamp\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n"
        "def test_clamp():\n    assert clamp(-1, 0, 9) == 0\n    assert clamp(5, 0, 9) == 5\n"
    )
    baseline = _commit_all(repo, "init")
    with (repo / "calc.py").open("a") as f:
        f.write(
            "\n\ndef clamp(x, lo, hi):\n    if x < lo:\n        return lo\n    return x\n"
        )
    _commit_all(repo, "clamp")
    pytest_bin = shutil.which("pytest")
    if pytest_bin is None:
        pytest.skip("pytest is not on PATH for the fixture's test command")
    config = BuildConfig(
        project_dir=repo,
        stack="pyproject",
        test_command=f"{pytest_bin} -q -x -p no:cacheprovider",
        config_dir=tmp_path / "c",
    )
    r = mutation.run_mutation(config, baseline, ["calc.py"])
    assert r.ok, r.error
    assert r.candidates > 0 and r.killed + r.survived > 0
    assert all(s.line >= 4 for s in r.survivors)
    assert _git(repo, "status", "--porcelain") == ""


def test_cosmic_test_command_honors_cd_and_literal_arguments(tmp_path):
    import shlex
    import subprocess
    import sys
    import tomllib

    (tmp_path / "api").mkdir()
    command = f'cd api && {sys.executable} -c "from pathlib import Path; import sys; assert Path.cwd().name == chr(97)+chr(112)+chr(105); assert sys.argv[1] == chr(35)" #'
    text = mutation.cosmic_ray_config(
        ["app.py"], command, 30, "abc123", project_dir=tmp_path
    )
    argv = shlex.split(tomllib.loads(text)["cosmic-ray"]["test-command"])
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
