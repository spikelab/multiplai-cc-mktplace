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
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from build_pipeline import mutation
from build_pipeline.config import BuildConfig, MutationSettings, _load_mutation_settings
from build_pipeline.models import AgentResult, BlockInfo, BlockStatus, MutationResult, SurvivingMutant
from build_pipeline.progress import ProgressWriter
from build_pipeline.state import BuildState, TDDState
from build_pipeline.tdd_engine import _run_mutation


# --- helpers ---------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, check=True).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init"], cwd=str(path),
                   capture_output=True, check=True)
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _dump_line(job_id, path, line, operator, worker, test=None, output="", diff=None):
    item = {"job_id": job_id, "mutations": [{
        "module_path": path, "operator_name": operator, "occurrence": 0,
        "start_pos": [line, 4], "end_pos": [line, 5], "operator_args": {},
        "definition_name": "f"}]}
    result = None if worker is None else {
        "worker_outcome": worker, "output": output, "test_outcome": test, "diff": diff}
    return json.dumps([item, result])


DUMP = "\n".join([
    _dump_line("a1", "src/calc.py", 2, "core/ReplaceBinaryOperator_Add_Sub", "skipped",
               output="Filtered git"),
    _dump_line("k1", "src/calc.py", 10, "core/ReplaceComparisonOperator_Lt_Gt", "normal", "killed",
               output="1 failed"),
    _dump_line("k2", "src/calc.py", 12, "core/AddNot", "normal", "killed", output="timeout"),
    _dump_line("k3", "src/calc.py", 12, "core/NumberReplacer", "normal", "killed", output="1 failed"),
    _dump_line("s1", "src/calc.py", 10, "core/ReplaceComparisonOperator_Lt_LtE", "normal",
               "survived", output="3 passed", diff="-    if x < lo:\n+    if x <= lo:"),
    _dump_line("e1", "src/calc.py", 14, "core/ZeroIterationForLoop", "exception", "incompetent"),
])


@pytest.fixture
def project(tmp_path):
    """A committed Python project with one pre-build commit (the baseline) and
    one build commit that adds a function and its test."""
    repo = _repo(tmp_path / "project")
    (repo / "pyproject.toml").write_text("[project]\nname = 'calc'\nversion = '0.1.0'\n")
    (repo / "src").mkdir()
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    (repo / ".gitignore").write_text(".build-state.json\nbuild-progress.md\n")
    baseline = _commit_all(repo, "chore: init")
    with (repo / "src" / "calc.py").open("a") as f:
        f.write("\n\ndef clamp(x, lo, hi):\n    if x < lo:\n        return lo\n    return x\n")
    (repo / "tests" / "test_clamp.py").write_text(
        "from src.calc import clamp\n\n\ndef test_clamp():\n    assert clamp(-1, 0, 9) == 0\n")
    specs = repo / "specs" / "changes" / "feat"
    specs.mkdir(parents=True)
    (specs / "tasks.md").write_text("## 1. Clamp\n")
    _commit_all(repo, "feat: clamp")
    return repo, baseline


def _setup(project, *, stack="pyproject", test_command="true", min_score=0.8,
           mutation_done=False):
    repo, baseline = project
    config = BuildConfig(
        project_dir=repo, change_name="feat", tier="advanced", mode="only",
        test_command=test_command, config_dir=repo / ".cfg",
        core_memory_files=[], stack_memory_files=[], additional_memory_files=[],
    )
    config.specs_dir = repo / "specs"
    config.stack = stack
    config.mutation = MutationSettings(min_score=min_score)
    state = BuildState(change_name="feat", mode="only", tier="advanced")
    state.tdd = TDDState(
        blocks=[BlockInfo(number=1, name="Clamp", description="clamp",
                          status=BlockStatus.DONE, baseline_commit=baseline)],
        current_block=1, refactor_all_done=True, mutation_done=mutation_done,
    )
    state.state_file = str(config.state_file_path())
    progress = ProgressWriter(repo / "build-progress.md")
    return config, state, progress


def _result(score_killed: int, survived: int, files=("src/calc.py",)) -> MutationResult:
    judged = score_killed + survived
    return MutationResult(
        ok=True, tool_version=mutation.COSMIC_RAY_VERSION, files=list(files),
        killed=score_killed, survived=survived,
        score=(score_killed / judged) if judged else None,
        survivors=[SurvivingMutant(path="src/calc.py", line=5, operator="core/AddNot",
                                   mutant_id=f"s{i}", diff="-a\n+b") for i in range(survived)],
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
            "src/calc.py", 10, "core/ReplaceComparisonOperator_Lt_LtE", "s1")
        assert "x <= lo" in s.diff

    def test_timeouts_count_as_killed(self):
        dump = _dump_line("t", "a.py", 1, "core/AddNot", "normal", "killed", output="timeout")
        r = mutation.parse_dump(dump)
        assert (r.killed, r.timeouts, r.score) == (1, 1, 1.0)

    def test_no_judged_mutant_gives_no_score(self):
        dump = "\n".join([
            _dump_line("a", "a.py", 1, "core/AddNot", "skipped", output="Filtered git"),
            _dump_line("b", "a.py", 2, "core/AddNot", None),
        ])
        r = mutation.parse_dump(dump)
        assert r.score is None and r.survivors == []

    def test_pending_jobs_lists_only_jobs_without_a_result(self):
        jobs = mutation.pending_jobs("\n".join([
            _dump_line("a", "a.py", 1, "core/AddNot", "skipped"),
            _dump_line("b", "a.py", 2, "core/AddNot", None),
        ]))
        assert [j for j, _ in jobs] == ["b"]

    def test_sample_is_deterministic_and_capped(self):
        jobs = [(f"id{i}", f"key{i}") for i in range(10)]
        dropped = mutation.sample_out(jobs, 4)
        assert len(dropped) == 6
        # Same mutations under fresh job ids → the same mutations are dropped.
        renamed = [(f"new{i}", f"key{i}") for i in range(10)]
        assert [d.replace("id", "") for d in dropped] == [
            d.replace("new", "") for d in mutation.sample_out(renamed, 4)]
        assert mutation.sample_out(jobs, 10) == []


class TestConfig:
    def test_defaults(self):
        s = _load_mutation_settings(None)
        assert (s.enabled, s.min_score, s.timeout, s.max_mutants) == (True, 0.8, 30.0, 300)

    def test_reads_valid_values(self):
        s = _load_mutation_settings(
            {"enabled": False, "min_score": 0.6, "timeout": 12, "max_mutants": 50})
        assert (s.enabled, s.min_score, s.timeout, s.max_mutants) == (False, 0.6, 12.0, 50)

    @pytest.mark.parametrize("raw", [
        {"min_score": 1.5}, {"min_score": "high"}, {"timeout": 0}, {"timeout": True},
        {"max_mutants": -1}, {"max_mutants": 2.5}, {"enabled": "maybe"},
    ])
    def test_bad_values_keep_the_default_and_warn(self, raw, caplog):
        s = _load_mutation_settings(raw)
        assert s == MutationSettings()
        assert "mutation" in caplog.text

    def test_specs_config_yaml_is_read(self, tmp_path):
        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "config.yaml").write_text("mutation:\n  min_score: 0.7\n  max_mutants: 20\n")
        config = BuildConfig(project_dir=tmp_path, config_dir=tmp_path / "c")
        config.specs_dir = specs
        config._load_specs_config()
        assert config.mutation.min_score == 0.7
        assert config.mutation.max_mutants == 20


class TestCosmicRayConfig:
    def test_test_command_runs_with_buildmes_path_and_branch_is_the_sha(self):
        with patch.dict(os.environ, {"PATH": "/project/.venv/bin:/usr/bin"}):
            text = mutation.cosmic_ray_config(["src/calc.py"], "python -m pytest -q", 30, "abc123")
        assert 'module-path = ["src/calc.py"]' in text
        assert 'branch = "abc123"' in text
        assert "env PATH=/project/.venv/bin:/usr/bin python -m pytest -q" in text


# --- applicability ---------------------------------------------------------

class TestApplies:
    def test_python_and_enabled(self):
        config = BuildConfig(stack="pyproject", test_command="pytest")
        assert mutation.mutation_applies(config) == (True, "")

    def test_non_python_stack_does_not_apply(self):
        ok, why = mutation.mutation_applies(BuildConfig(stack="package", test_command="npm test"))
        assert ok is False and "Python" in why

    def test_disabled_does_not_apply(self):
        config = BuildConfig(stack="pyproject", test_command="pytest")
        config.mutation = MutationSettings(enabled=False)
        ok, why = mutation.mutation_applies(config)
        assert ok is False and "enabled" in why

    def test_changed_source_files_drops_tests(self, project):
        repo, baseline = project
        assert mutation.changed_source_files(repo, baseline) == ["src/calc.py"]


# --- run_mutation (subprocess boundary stubbed) ----------------------------

class TestRunMutation:
    def _stub_tool(self, dump=DUMP, on_exec=None):
        calls: list[list[str]] = []

        def _tool(argv, cwd, timeout):
            calls.append(argv)
            cmd = argv[len(mutation._UVX):]
            if cmd[:2] == ["cosmic-ray", "dump"]:
                # First dump: everything pending; second: the finished session.
                return (DUMP_PENDING if sum(
                    1 for c in calls if c[len(mutation._UVX):][:2] == ["cosmic-ray", "dump"]) == 1
                    else dump)
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
        steps = [c[len(mutation._UVX):][:2] for c in calls]
        assert steps == [["cosmic-ray", "init"], ["cr-filter-git", "--config"],
                         ["cosmic-ray", "dump"], ["cosmic-ray", "baseline"],
                         ["cosmic-ray", "exec"], ["cosmic-ray", "dump"]]
        assert all(c[:3] == ["uvx", "--from", "cosmic-ray==8.7.0"] for c in calls)
        assert r.ok and r.score == pytest.approx(0.75) and r.candidates == 2

    def test_dirty_tree_after_the_run_is_discarded(self, project):
        config, _, _ = _setup(project)
        repo, baseline = project
        head = _git(repo, "rev-parse", "HEAD")

        def _leave_mutant(cwd):
            (Path(cwd) / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n")

        _, tool = self._stub_tool(on_exec=_leave_mutant)
        with patch("build_pipeline.mutation._run_tool", side_effect=tool), \
             patch("build_pipeline.mutation.git_ops.discard_to",
                   wraps=mutation.git_ops.discard_to) as discard:
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
        with patch("build_pipeline.mutation._run_tool",
                   side_effect=mutation._ToolError("cosmic-ray baseline exited 1: red")):
            r = mutation.run_mutation(config, baseline, ["src/calc.py"])
        assert r.ok is False and "baseline" in r.error and r.score is None


DUMP_PENDING = "\n".join([
    _dump_line("a1", "src/calc.py", 2, "core/ReplaceBinaryOperator_Add_Sub", "skipped",
               output="Filtered git"),
    _dump_line("k1", "src/calc.py", 10, "core/ReplaceComparisonOperator_Lt_Gt", None),
    _dump_line("s1", "src/calc.py", 10, "core/ReplaceComparisonOperator_Lt_LtE", None),
])


# --- the engine step -------------------------------------------------------

class TestRunMutationStep:
    @pytest.mark.asyncio
    async def test_non_python_stack_skips_without_running_the_tool(self, project):
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
        with patch("build_pipeline.mutation._run_tool") as tool, \
             patch("build_pipeline.tdd_engine.run_mutation") as run:
            assert await _run_mutation(config, state, progress) is True
        tool.assert_not_called()
        run.assert_not_called()

    @pytest.mark.asyncio
    async def test_below_min_score_fails_and_leaves_the_flag_unset(self, project):
        config, state, progress = _setup(project, min_score=0.8)
        with patch("build_pipeline.tdd_engine.run_mutation", return_value=_result(7, 3)), \
             patch("build_pipeline.tdd_engine.run_mutation_test_writer", new_callable=AsyncMock,
                   return_value=AgentResult(success=True, output="nothing to add")):
            assert await _run_mutation(config, state, progress) is False
        assert state.tdd.mutation_done is False
        report = (config.change_dir / "mutation.md").read_text()
        assert "FAILED" in report and "0.70" in report
        assert "FAIL" in (project[0] / "build-progress.md").read_text()

    @pytest.mark.asyncio
    async def test_at_min_score_passes_and_sets_the_flag(self, project):
        config, state, progress = _setup(project, min_score=0.8)
        with patch("build_pipeline.tdd_engine.run_mutation", return_value=_result(8, 0)):
            assert await _run_mutation(config, state, progress) is True
        assert state.tdd.mutation_done is True
        assert BuildState.load(config.state_file_path()).tdd.mutation_done is True
        report = (config.change_dir / "mutation.md").read_text()
        assert "PASSED" in report and "cosmic-ray 8.7.0" in report and "`src/calc.py`" in report
        assert _git(project[0], "log", "-1", "--format=%s") == "test(mutation): record mutation score"

    @pytest.mark.asyncio
    async def test_survivor_round_that_edits_source_fails_the_step(self, project):
        config, state, progress = _setup(project)
        repo = project[0]

        async def _agent(**_kw):
            (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 0\n")
            (repo / "tests" / "test_more.py").write_text("def test_x():\n    assert 1 == 1\n")
            return AgentResult(success=True, output="STATUS: DONE")

        with patch("build_pipeline.tdd_engine.run_mutation", return_value=_result(9, 1)) as run, \
             patch("build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent):
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
            (repo / "tests" / "test_more.py").write_text("def test_x():\n    assert 1 == 2\n")
            return AgentResult(success=True, output="STATUS: DONE")

        red = MagicMock(passed=False, reason="1 failed")
        with patch("build_pipeline.tdd_engine.run_mutation", return_value=_result(9, 1)) as run, \
             patch("build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent), \
             patch("build_pipeline.tdd_engine.integration_gate", return_value=red):
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
            (repo / "tests" / "test_more.py").write_text("def test_x():\n    assert 1 == 1\n")
            return AgentResult(success=True, output="STATUS: DONE")

        with patch("build_pipeline.tdd_engine.run_mutation",
                   side_effect=[_result(7, 3), _result(10, 0)]) as run, \
             patch("build_pipeline.tdd_engine.run_mutation_test_writer", side_effect=_agent):
            assert await _run_mutation(config, state, progress) is True
        assert run.call_count == 2
        subjects = _git(repo, "log", "-2", "--format=%s").splitlines()
        assert subjects == ["test(mutation): record mutation score",
                            "test(mutation): kill surviving mutants"]
        report = (config.change_dir / "mutation.md").read_text()
        assert "0.70" in report and "1.00" in report

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
        with patch("build_pipeline.tdd_engine.run_mutation", return_value=_result(9, 1)), \
             patch("build_pipeline.tdd_engine.budget_mod.check",
                   side_effect=BudgetExceededError("over", diagnosis="d")), \
             patch("build_pipeline.tdd_engine.run_mutation_test_writer", agent):
            assert await _run_mutation(config, state, progress) is False
        agent.assert_not_awaited()
        assert state.tdd.mutation_done is False


# --- the real tool ---------------------------------------------------------

@pytest.mark.skipif(shutil.which("cosmic-ray") is None or shutil.which("uvx") is None,
                    reason="cosmic-ray is not installed")
def test_real_cosmic_ray_scores_only_the_changed_lines(tmp_path):
    """Runs cosmic-ray for real on a two-commit fixture: mutants on the
    baseline's lines are filtered out, the changed lines are scored, and the
    tree is clean afterwards."""
    repo = _repo(tmp_path / "real")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "test_calc.py").write_text(
        "from calc import add, clamp\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n"
        "def test_clamp():\n    assert clamp(-1, 0, 9) == 0\n    assert clamp(5, 0, 9) == 5\n")
    baseline = _commit_all(repo, "init")
    with (repo / "calc.py").open("a") as f:
        f.write("\n\ndef clamp(x, lo, hi):\n    if x < lo:\n        return lo\n    return x\n")
    _commit_all(repo, "clamp")
    pytest_bin = shutil.which("pytest")
    if pytest_bin is None:
        pytest.skip("pytest is not on PATH for the fixture's test command")
    config = BuildConfig(project_dir=repo, stack="pyproject",
                         test_command=f"{pytest_bin} -q -x -p no:cacheprovider",
                         config_dir=tmp_path / "c")
    r = mutation.run_mutation(config, baseline, ["calc.py"])
    assert r.ok, r.error
    assert r.candidates > 0 and r.killed + r.survived > 0
    assert all(s.line >= 4 for s in r.survivors)
    assert _git(repo, "status", "--porcelain") == ""
