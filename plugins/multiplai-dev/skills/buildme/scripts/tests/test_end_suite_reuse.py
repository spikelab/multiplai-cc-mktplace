"""D of plan-buildme-epic-speedups-2026-11-03.md: the final verification
reuses the end-of-build whole run when only the docs step's files and the
change's own spec files changed after it passed.

- `gates.reusable_end_suite_pass` decides whether a recorded pass may stand.
- `tdd_engine._run_end_suite` records the pass (`TDDState.end_suite_pass`)
  with `end_suite_done`, in a build or round only.
- `orchestrator.run_orchestrator` reuses it for a build's final check.
- `findings/loop.py` `do_publish` reuses it for a round when nothing changed.

Git and the test commands are real; only model calls, the push and GitHub
are replaced. Each `test_s<n>_...` is row S<n> of that plan's Stop and resume
table: it stops at the row's point or applies its perturbation, resumes, and
compares the result with a run that never stopped.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from build_pipeline import gates, git_ops, orchestrator, tdd_engine
from build_pipeline.config import BuildConfig, GitToggles
from build_pipeline.findings import code, loop, publish
from build_pipeline.models import AgentResult, BlockInfo, BlockStatus, BuildPhase
from build_pipeline.progress import ProgressWriter
from build_pipeline.spec_generator import reconcile_plan
from build_pipeline.state import BuildState, TDDState, completed_state_path
from build_pipeline.tdd_engine import _run_end_suite
from multiplai_core.specs import mark_ready

from tests.test_findings_self_check import PROBLEM, _log, calls
from tests.test_findings_stop_resume_d1 import (
    _assert_like_unstopped,
    _die,
    _killed,
    _Round,
)


class Killed(BaseException):
    """A stop, as SIGKILL would leave it: nothing after the raise runs."""


def _git(root: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(root: Path, files: dict[str, str], subject: str) -> str:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", subject)
    return _git(root, "rev-parse", "HEAD")


class FinalRuns:
    """Wraps `gates.final_verification` (the build and the fixer import it
    from `gates` when they run), recording each call and running the real
    gate."""

    def __init__(self, monkeypatch):
        self.calls: list[str] = []
        real = gates.final_verification

        def run(command, *args, **kwargs):
            self.calls.append(command)
            return real(command, *args, **kwargs)

        monkeypatch.setattr(gates, "final_verification", run)


# --- D, `reusable_end_suite_pass` ----------------------------------------------


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _commit(root, {"app.py": "x = 1\n"}, "init")
    return root


def _record(root: Path, command: str = "pytest", parallel: str = "auto") -> dict:
    return {
        "head": _git(root, "rev-parse", "HEAD"),
        "command": command,
        "parallel": parallel,
    }


def _docs_only(name: str) -> bool:
    return name.startswith("docs/")


class TestReusableEndSuitePass:
    def test_it_returns_the_head_when_only_allowed_paths_changed(self, repo):
        record = _record(repo)
        _commit(repo, {"docs/guide.md": "# Guide\n"}, "docs")
        _commit(repo, {"docs/more.md": "# More\n"}, "more docs")
        assert (
            gates.reusable_end_suite_pass(record, repo, "pytest", "auto", _docs_only)
            == record["head"]
        )

    def test_it_returns_the_head_when_nothing_changed(self, repo):
        record = _record(repo)
        never = Mock(return_value=False)
        assert (
            gates.reusable_end_suite_pass(record, repo, "pytest", "auto", never)
            == record["head"]
        )
        never.assert_not_called()

    def test_bookkeeping_changes_are_not_asked_about(self, repo):
        record = _record(repo)
        _commit(
            repo,
            {"specs/changes/c/.build-state.json": "{}", "build-progress.md": "x\n"},
            "bookkeeping",
        )
        never = Mock(return_value=False)
        assert (
            gates.reusable_end_suite_pass(record, repo, "pytest", "auto", never)
            == record["head"]
        )
        never.assert_not_called()

    def test_one_other_changed_path_is_none(self, repo):
        record = _record(repo)
        _commit(repo, {"docs/guide.md": "# Guide\n", "app.py": "x = 2\n"}, "both")
        assert (
            gates.reusable_end_suite_pass(record, repo, "pytest", "auto", _docs_only)
            is None
        )

    @pytest.mark.parametrize("dirt", ["tracked", "untracked"])
    def test_a_dirty_tree_is_none(self, repo, dirt):
        record = _record(repo)
        _commit(repo, {"docs/guide.md": "# Guide\n"}, "docs")
        (repo / ("app.py" if dirt == "tracked" else "new.py")).write_text("y = 1\n")
        assert (
            gates.reusable_end_suite_pass(record, repo, "pytest", "auto", _docs_only)
            is None
        )

    def test_another_command_is_none(self, repo):
        record = _record(repo, command="pytest -x")
        assert (
            gates.reusable_end_suite_pass(record, repo, "pytest", "auto", _docs_only)
            is None
        )

    def test_another_parallel_is_none(self, repo):
        record = _record(repo, parallel="off")
        assert (
            gates.reusable_end_suite_pass(record, repo, "pytest", "auto", _docs_only)
            is None
        )

    def test_a_head_that_is_not_an_ancestor_is_none(self, repo):
        base = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-qb", "side")
        side = _commit(repo, {"docs/side.md": "side\n"}, "side")
        _git(repo, "checkout", "-q", "-")
        _commit(repo, {"docs/guide.md": "# Guide\n"}, "docs")
        record = {"head": side, "command": "pytest", "parallel": "auto"}
        assert git_ops.is_ancestor_of_head(repo, base)
        assert (
            gates.reusable_end_suite_pass(record, repo, "pytest", "auto", _docs_only)
            is None
        )

    @pytest.mark.parametrize("record", [None, {}])
    def test_no_record_is_none(self, repo, record):
        assert (
            gates.reusable_end_suite_pass(record, repo, "pytest", "auto", _docs_only)
            is None
        )

    @pytest.mark.parametrize("head", ["", "not-a-sha", "0" * 40])
    def test_a_git_error_is_none(self, repo, head):
        record = {"head": head, "command": "pytest", "parallel": "auto"}
        assert (
            gates.reusable_end_suite_pass(record, repo, "pytest", "auto", _docs_only)
            is None
        )

    def test_outside_a_repository_is_none(self, tmp_path):
        record = {"head": "a" * 40, "command": "pytest", "parallel": "auto"}
        assert (
            gates.reusable_end_suite_pass(
                record, tmp_path, "pytest", "auto", lambda _n: True
            )
            is None
        )


# --- D, recording: `tdd_engine._run_end_suite` --------------------------------
#
# The engine fixture of test_whole_suite_end.py, except that the whole test
# command fails while `status.txt` says `red` and the end-of-build fixer
# rewrites it (a fix that deletes a tracked file is its own test below).

WHOLE = (
    f'{sys.executable} -c "import pathlib,sys; '
    f"sys.exit(pathlib.Path('status.txt').read_text().strip() == 'red')\""
)


def _engine_project(base: Path):
    project_dir = base / "project"
    change = project_dir / "specs" / "changes" / "test"
    change.mkdir(parents=True)
    (change / "proposal.md").write_text("# test\n")
    (project_dir / "app.py").write_text("x = 1\n")
    _git(project_dir, "init", "-q")
    _git(project_dir, "config", "user.email", "t@example.com")
    _git(project_dir, "config", "user.name", "T")
    baseline = _commit(project_dir, {}, "init")
    config = BuildConfig(
        project_dir=project_dir,
        change_name="test",
        tier="advanced",
        test_command=WHOLE,
        config_dir=base / "config",
        core_memory_files=[],
        stack_memory_files=[],
        additional_memory_files=[],
    )
    config.specs_dir = project_dir / "specs"
    config.whole_suite = "end"
    config.gates.e2e_test_entry_point_check = False
    state = BuildState(
        change_name="test",
        tier="advanced",
        state_file=str(config.state_file_path()),
        tdd=TDDState(
            blocks=[
                BlockInfo(
                    number=1,
                    name="B1",
                    description="block 1",
                    status=BlockStatus.DONE,
                    baseline_commit=baseline,
                )
            ],
            current_block=1,
            baseline_tests_pass=True,
            whole_suite_deferred=True,
            refactor_all_done=True,
        ),
    )
    progress = ProgressWriter(config.progress_file_path())
    progress.initialize("test", "advanced", 1)
    return SimpleNamespace(
        dir=project_dir, config=config, state=state, progress=progress
    )


@pytest.fixture
def end_ready(tmp_path, monkeypatch):
    """Blocks done under `end`, the last one committed; *red* leaves the
    whole command failing until the end-of-build fixer runs."""
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    count = iter(range(100))

    def make(red: bool = True):
        p = _engine_project(tmp_path / f"run{next(count)}")
        files = {"b1.py": "n = 1\n", "status.txt": "red\n" if red else "green\n"}
        _commit(p.dir, files, "fix(block-1): B1")
        p.state.checkpoint(p.config.state_file_path())
        return p

    return make


async def _end(p, *, standalone: bool = False, deletes: str = "", **patches):
    """The end run in a fresh process: the state as saved on disk. The fixer
    also deletes the tracked file *deletes* when one is named."""
    state = BuildState.load(p.config.state_file_path())

    def integration_fix(**kwargs):
        (p.dir / "status.txt").write_text("green\n")
        if deletes:
            (p.dir / deletes).unlink()
        return AgentResult(success=True, output="fixed")

    fixer = AsyncMock(side_effect=integration_fix)
    targets = {
        "run_integration_fix": {"new": fixer},
        "_test_command_since": {"side_effect": lambda config, base: WHOLE},
        **patches,
    }
    started = [patch(f"build_pipeline.tdd_engine.{t}", **v) for t, v in targets.items()]
    for each in started:
        each.start()
    try:
        result = await _run_end_suite(p.config, state, p.progress, standalone)
    finally:
        for each in reversed(started):
            each.stop()
    return result, fixer


def _saved_tdd(p) -> TDDState:
    return BuildState.load(p.config.state_file_path()).tdd


class TestRecording:
    @pytest.mark.asyncio
    async def test_a_pass_with_a_fix_records_the_head_after_the_fix_commit(
        self, end_ready
    ):
        p = end_ready()
        result, fixer = await _end(p)
        assert result is True and fixer.await_count == 1
        head = _git(p.dir, "rev-parse", "HEAD")
        assert _git(p.dir, "log", "-1", "--format=%s") == "fix: end-of-build test suite"
        saved = _saved_tdd(p)
        assert saved.end_suite_done is True
        assert saved.end_suite_pass == {
            "head": head,
            "command": WHOLE,
            "parallel": p.config.parallel_tests,
        }

    @pytest.mark.asyncio
    async def test_a_fix_that_deletes_a_tracked_file_records_the_head(self, end_ready):
        p = end_ready()
        _commit(p.dir, {"obsolete.py": "old = 1\n"}, "obsolete")
        result, fixer = await _end(p, deletes="obsolete.py")
        assert result is True and fixer.await_count == 1
        assert not (p.dir / "obsolete.py").exists()
        assert git_ops.tree_is_clean(p.dir)
        assert _saved_tdd(p).end_suite_pass == {
            "head": _git(p.dir, "rev-parse", "HEAD"),
            "command": WHOLE,
            "parallel": p.config.parallel_tests,
        }

    @pytest.mark.asyncio
    async def test_a_run_that_changes_the_files_it_tests_records_nothing(
        self, end_ready
    ):
        """The passing run rewrites a tracked file as it ends; the fix commit
        then takes that change in, so no run tested what is committed."""
        p = end_ready()
        real = tdd_engine.integration_gate
        runs = []

        def gate(*args, **kwargs):
            result = real(*args, **kwargs)
            runs.append(result.passed)
            if result.passed:
                (p.dir / "status.txt").write_text("green\nrewritten by the run\n")
            return result

        result, _ = await _end(p, integration_gate={"side_effect": gate})
        assert result is True and runs[-1] is True
        assert _saved_tdd(p).end_suite_pass is None

    @pytest.mark.asyncio
    async def test_a_pass_with_no_fix_records_the_current_head(self, end_ready):
        p = end_ready(red=False)
        head = _git(p.dir, "rev-parse", "HEAD")
        result, fixer = await _end(p)
        assert result is True
        fixer.assert_not_awaited()
        assert _git(p.dir, "rev-parse", "HEAD") == head
        assert _saved_tdd(p).end_suite_pass == {
            "head": head,
            "command": WHOLE,
            "parallel": p.config.parallel_tests,
        }

    @pytest.mark.asyncio
    async def test_a_pass_whose_files_changed_across_the_commit_records_nothing(
        self, end_ready
    ):
        """A commit that changes what it commits (a hook that rewrites a file)
        leaves a HEAD that is not the tree the run tested."""
        p = end_ready()
        real_commit = git_ops.commit_tree

        def rewriting_commit(project_dir, *args, **kwargs):
            (Path(project_dir) / "app.py").write_text("x = 2  # rewritten\n")
            return real_commit(project_dir, *args, **kwargs)

        result, _ = await _end(
            p, **{"git_ops.commit_tree": {"side_effect": rewriting_commit}}
        )
        assert result is True
        assert git_ops.tree_is_clean(p.dir)
        saved = _saved_tdd(p)
        assert saved.end_suite_done is True
        assert saved.end_suite_pass is None

    @pytest.mark.asyncio
    async def test_a_tree_left_dirty_after_the_commit_records_nothing(self, end_ready):
        p = end_ready()
        real_commit = git_ops.commit_tree

        def commit_then_dirty(project_dir, *args, **kwargs):
            out = real_commit(project_dir, *args, **kwargs)
            (Path(project_dir) / "stray.py").write_text("y = 1\n")
            return out

        result, _ = await _end(
            p, **{"git_ops.commit_tree": {"side_effect": commit_then_dirty}}
        )
        assert result is True
        assert _saved_tdd(p).end_suite_done is True
        assert _saved_tdd(p).end_suite_pass is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("red", [True, False])
    async def test_a_standalone_run_records_nothing_new(self, end_ready, red):
        p = end_ready(red=red)
        result, _ = await _end(p, standalone=True)
        assert result is True
        saved = _saved_tdd(p)
        assert saved.end_suite_done is True
        assert saved.end_suite_pass is None

    def test_an_older_checkpoint_loads_with_no_pass(self, tmp_path):
        path = tmp_path / ".build-state.json"
        path.write_text(
            json.dumps(
                {
                    "change_name": "c",
                    "tier": "advanced",
                    "tdd": {"blocks": [], "end_suite_done": True},
                }
            )
        )
        assert BuildState.load(path).tdd.end_suite_pass is None

    @pytest.mark.asyncio
    async def test_s17_killed_after_the_fix_commit_before_the_checkpoint(
        self, end_ready
    ):
        """S17: the fix commit is there, neither `end_suite_done` nor
        `end_suite_pass` is saved. The resume runs END_SUITE again at the new
        head, and its pass records `end_suite_pass` there."""
        unstopped = end_ready()
        assert (await _end(unstopped))[0] is True
        expected = _saved_tdd(unstopped)

        p = end_ready()
        real_commit = git_ops.commit_tree

        def commit_then_stop(*args, **kwargs):
            real_commit(*args, **kwargs)
            raise Killed()

        with pytest.raises(Killed):
            await _end(p, **{"git_ops.commit_tree": {"side_effect": commit_then_stop}})
        assert _git(p.dir, "log", "-1", "--format=%s") == "fix: end-of-build test suite"
        stopped = _saved_tdd(p)
        assert stopped.end_suite_done is False
        assert stopped.end_suite_pass is None

        runs = []
        real_gate = gates.integration_gate

        def counted(command, *args, **kwargs):
            runs.append(command)
            return real_gate(command, *args, **kwargs)

        result, fixer = await _end(p, integration_gate={"side_effect": counted})
        assert result is True
        fixer.assert_not_awaited()
        assert runs == [WHOLE]  # END_SUITE ran again, at the new head
        head = _git(p.dir, "rev-parse", "HEAD")
        saved = _saved_tdd(p)
        assert saved.end_suite_pass == {
            "head": head,
            "command": WHOLE,
            "parallel": p.config.parallel_tests,
        }
        # As the unstopped run: same fields, each at its own repo's head.
        assert saved.end_suite_done == expected.end_suite_done
        assert saved.end_suite == expected.end_suite
        assert expected.end_suite_pass["head"] == _git(
            unstopped.dir, "rev-parse", "HEAD"
        )
        assert {k: v for k, v in saved.end_suite_pass.items() if k != "head"} == {
            k: v for k, v in expected.end_suite_pass.items() if k != "head"
        }
        assert _git(p.dir, "log", "--format=%s") == _git(
            unstopped.dir, "log", "--format=%s"
        )


# --- D, orchestrator: the build's final verification ---------------------------

PASSING = f'{sys.executable} -c "from app import value; assert value == 1"'
DOCS = "docs/guide.md"


@pytest.fixture
def build(tmp_path, monkeypatch):
    return _make_build(tmp_path, monkeypatch)


def _make_build(tmp_path, monkeypatch):
    """A build at PUBLISH whose END_SUITE passed at the implementation commit
    P (`TDDState.end_suite_pass`), with nothing published yet. The same
    accepted build as test_completion_contract.py's `accepted_build`."""
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-qb", "buildme/contract")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    config = BuildConfig(
        project_dir=root,
        change_name="contract",
        auto=False,
        git=GitToggles(worktree=False, push=True, pr="none"),
        test_command=PASSING,
    )
    config.specs_dir = root / "specs"
    change = config.change_dir
    (change / "requirements").mkdir(parents=True)
    for name, content in {
        "proposal.md": "## Capabilities\n- `value`\n",
        "requirements/value.md": "### Requirement: Value\n#### Scenario: Read\n"
        "WHEN read\nTHEN returns one\n",
        "implementation-plan.md": "# Design\nA constant",
        "use-cases.md": "# Use cases\nRead one",
        "unknowns.md": "# Unknowns\nNone",
        "tasks.md": "## 1. Value\nReturn one",
        "rubric.md": "# Rubric\nCorrect value",
    }.items():
        (change / name).write_text(content)
    (root / "app.py").write_text("value = 1\n")
    mark_ready(change)
    head = _commit(root, {}, "accepted implementation")
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
            end_suite_done=True,
            end_suite_pass={
                "head": head,
                "command": PASSING,
                "parallel": config.parallel_tests,
            },
        ),
    )
    reconcile_plan(config, state)
    state.spec_gen.record_step("plan_review", "passed")
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
    section = root / "specs" / "changes" / "contract" / "pr-sections" / "extra.md"

    def contributor(cfg, st, docs_dir):
        section.parent.mkdir(parents=True, exist_ok=True)
        section.write_text("## Extra\n")
        return section

    contributors = Mock(side_effect=contributor, __name__="contributor")
    monkeypatch.setattr(orchestrator, "PRE_PUBLISH_CONTRIBUTORS", [contributors])
    monkeypatch.setattr(git_ops, "has_remote", lambda *a: True)
    push = Mock(return_value=git_ops.GitResult([], 0, "", ""))
    monkeypatch.setattr(git_ops, "push_branch", push)
    finals = FinalRuns(monkeypatch)

    def save(*, docs: bool = True) -> None:
        """Commit the docs step's file and respec.md after P, as the steps
        between END_SUITE and PUBLISH would, and checkpoint."""
        if docs:
            _commit(
                root,
                {DOCS: "# Guide\n", "specs/changes/contract/respec.md": "# Respec\n"},
                "docs: update for contract",
            )
            state.docs_impact = [DOCS]
        state.checkpoint(config.state_file_path())

    return SimpleNamespace(
        dir=root,
        config=config,
        state=state,
        args=args,
        push=push,
        finals=finals,
        contributor=contributors,
        section=section,
        pass_head=head,
        save=save,
    )


def _completed(b) -> BuildState:
    return BuildState.load(completed_state_path(b.config.state_file_path()))


def _build_outcome(b) -> dict:
    """What a resumed build shares with one that never stopped."""
    done = _completed(b)
    v = done.verification
    return {
        "subjects": _git(b.dir, "log", "--format=%s").splitlines(),
        "phase": done.phase,
        "publication": done.publication,
        "passed": v["passed"],
        "reused": (v.get("metadata") or {}).get("reused_from") == b.pass_head,
        "reason": v["reason"].replace(b.pass_head[:8], "<P8>"),
        "sections": [Path(s).name for s in done.publish_sections],
        "final_runs": len(b.finals.calls),
        "pushes": b.push.call_count,
    }


class TestOrchestrator:
    @pytest.mark.asyncio
    async def test_only_docs_and_respec_changed_reuses_the_pass(self, build):
        build.save()
        assert await orchestrator.run_orchestrator(build.config, build.args) == 0
        assert build.finals.calls == []  # no test run
        build.push.assert_called_once()
        done = _completed(build)
        v = done.verification
        assert v["passed"] is True
        assert v["metadata"]["reused_from"] == build.pass_head
        assert v["reason"] == (
            f"reused the end-of-build run at {build.pass_head[:8]}: only docs "
            "and spec files changed since"
        )
        assert v["metadata"]["inputs"] == gates.verification_inputs(
            build.config.test_command,
            build.dir,
            parallel=build.config.parallel_tests,
            plan_revision=done.spec_gen.revision,
        )
        # The publish sections are still built.
        build.contributor.assert_called_once()
        assert [Path(s).name for s in done.publish_sections] == [
            "worktree.md",
            "extra.md",
        ]
        progress = build.config.progress_file_path().read_text()
        assert (
            f"VERIFY REUSED — the whole test command passed at "
            f"{build.pass_head[:8]} (end of build); since then only 2 docs/spec "
            "files changed"
        ) in progress

    @pytest.mark.asyncio
    async def test_nothing_changed_since_the_pass_reuses_it(self, build):
        build.save(docs=False)
        assert await orchestrator.run_orchestrator(build.config, build.args) == 0
        assert build.finals.calls == []
        assert _completed(build).verification["metadata"]["reused_from"] == (
            build.pass_head
        )
        assert "since then only 0 docs/spec files changed" in (
            build.config.progress_file_path().read_text()
        )

    @pytest.mark.asyncio
    async def test_one_code_file_changed_runs_final_verification(self, build):
        build.save()
        _commit(build.dir, {"helper.py": "y = 2\n"}, "late code")
        assert await orchestrator.run_orchestrator(build.config, build.args) == 0
        assert build.finals.calls == [PASSING]
        v = _completed(build).verification
        assert v["passed"] is True
        assert "reused_from" not in v["metadata"]
        build.contributor.assert_called_once()
        assert "VERIFY REUSED" not in build.config.progress_file_path().read_text()

    @pytest.mark.asyncio
    async def test_no_record_runs_final_verification(self, build):
        build.state.tdd.end_suite_pass = None
        build.save()
        assert await orchestrator.run_orchestrator(build.config, build.args) == 0
        assert build.finals.calls == [PASSING]

    @pytest.mark.parametrize(
        "rel", ["specs/config.yaml", "specs/registry/value.md", "docs/other.md"]
    )
    def test_a_spec_path_outside_the_change_dir_is_not_allowed(self, build, rel):
        """The departure from the plan: only the change dir (and the docs
        step's own files) count; `specs/config.yaml` can change how tests run,
        and `specs/registry/` is written by the archive. (The orchestrator
        itself refuses both earlier, in `assert_plan_current`, so this asks
        `_reused_end_suite` directly.)"""
        build.save()
        _commit(build.dir, {rel: "x: 1\n"}, "outside")
        current = gates.verification_inputs(
            build.config.test_command, build.dir, parallel=build.config.parallel_tests
        )
        change = build.config.change_dir
        assert (
            orchestrator._reused_end_suite(
                build.config, build.state, [change, change], current
            )
            is None
        )

    def test_a_source_file_the_docs_step_reported_is_not_allowed(self, build):
        """The docs agent can write any file: a reported path counts only
        when it is a document."""
        build.save()
        build.state.docs_impact = ["app.py"]
        _commit(build.dir, {"app.py": "x = 2\n"}, "docs step edited code")
        current = gates.verification_inputs(
            build.config.test_command, build.dir, parallel=build.config.parallel_tests
        )
        change = build.config.change_dir
        assert (
            orchestrator._reused_end_suite(
                build.config, build.state, [change, change], current
            )
            is None
        )

    def test_unknown_inputs_are_no_reuse(self, build):
        build.save()
        change = build.config.change_dir
        assert (
            orchestrator._reused_end_suite(
                build.config, build.state, [change, change], None
            )
            is None
        )

    def test_a_move_of_the_change_dir_is_allowed_on_both_sides(self, build):
        """The archive move: paths under the change dir before the move and
        after it both count as the change's own spec files."""
        build.save()
        old = build.config.change_dir
        new = build.config.specs_dir / "archive" / "2026-11-03-contract"
        new.parent.mkdir(parents=True)
        _git(build.dir, "mv", str(old), str(new))
        _git(build.dir, "commit", "-qm", "chore(specs): archive contract")
        current = gates.verification_inputs(
            build.config.test_command, build.dir, parallel=build.config.parallel_tests
        )
        reused = orchestrator._reused_end_suite(
            build.config, build.state, [old, new], current
        )
        assert reused is not None
        gate, changed = reused
        assert gate.passed and gate.metadata["reused_from"] == build.pass_head
        assert changed > 2  # the docs, respec.md and each moved spec file
        # With only the old location allowed, the moved files are not.
        assert (
            orchestrator._reused_end_suite(build.config, build.state, [old], current)
            is None
        )

    @pytest.mark.asyncio
    async def test_a_reused_record_is_a_recorded_pass_for_the_fixer(self, build):
        """Item 2 reads reused records too: `recorded_pass` accepts the
        reused `verification` as it would a run."""
        build.save()
        assert await orchestrator.run_orchestrator(build.config, build.args) == 0
        assert build.finals.calls == []
        found = code.recorded_pass(build.config.change_dir, build.dir, build.config)
        assert found == (
            _git(build.dir, "rev-parse", "HEAD"),
            ".build-state.complete.json",
        )

    @pytest.mark.asyncio
    async def test_s18_killed_after_the_reused_verification_before_publishing(
        self, build, tmp_path, monkeypatch
    ):
        """S18: the reused `verification` is checkpointed with `reused_from`
        and the current inputs; the resume's `verified` check matches, so it
        publishes with no test run."""
        build.save()
        real_publish = orchestrator._run_publish
        stop = Mock(side_effect=Killed())
        monkeypatch.setattr(orchestrator, "_run_publish", stop)
        with pytest.raises(Killed):
            await orchestrator.run_orchestrator(build.config, build.args)
        saved = BuildState.load(build.config.state_file_path())
        assert saved.verification["metadata"]["reused_from"] == build.pass_head
        assert saved.verification["metadata"]["inputs"] == gates.verification_inputs(
            build.config.test_command,
            build.dir,
            parallel=build.config.parallel_tests,
            plan_revision=saved.spec_gen.revision,
        )
        build.push.assert_not_called()

        monkeypatch.setattr(orchestrator, "_run_publish", real_publish)
        assert await orchestrator.run_orchestrator(build.config, build.args) == 0
        assert build.finals.calls == []
        build.contributor.assert_called_once()  # not built again on resume
        outcome = _build_outcome(build)
        assert outcome["pushes"] == 1
        assert outcome["reused"] is True
        assert outcome == await _unstopped_build_outcome(tmp_path, monkeypatch)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("perturbation", ["user-edit", "review-fix"])
    async def test_s19_a_path_outside_the_allowed_set_changed_after_the_pass(
        self, build, perturbation
    ):
        """S19: `reusable_end_suite_pass` returns None and
        `final_verification` runs as today."""
        build.save()
        rel, text = {
            "user-edit": ("app.py", "value = 1  # edited\n"),
            "review-fix": ("helper.py", "def helper():\n    return 1\n"),
        }[perturbation]
        _commit(build.dir, {rel: text}, perturbation)
        assert await orchestrator.run_orchestrator(build.config, build.args) == 0
        assert build.finals.calls == [PASSING]
        v = _completed(build).verification
        assert v["passed"] is True and "reused_from" not in v["metadata"]

    @pytest.mark.asyncio
    async def test_s19_an_auto_archive_that_writes_the_registry_runs_the_check(
        self, build
    ):
        """S19's archive move under `--auto`: it writes `specs/registry/`,
        which is outside the allowed set."""
        build.config.auto = True
        build.save()
        assert await orchestrator.run_orchestrator(build.config, build.args) == 0
        changed = _git(build.dir, "diff", "--name-only", build.pass_head, "HEAD")
        assert "specs/registry/value.md" in changed.splitlines()
        assert build.finals.calls == [PASSING]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("setting", ["command", "parallel"])
    async def test_s20_test_command_or_parallel_changed_between_stop_and_resume(
        self, build, setting
    ):
        """S20: the saved pass has the old values: no reuse, the final run
        happens."""
        build.save()
        if setting == "command":
            build.config.test_command = PASSING.replace("== 1", "== 1 and True")
        else:
            build.config.parallel_tests = (
                "off" if build.config.parallel_tests == "auto" else "auto"
            )
        assert await orchestrator.run_orchestrator(build.config, build.args) == 0
        assert build.finals.calls == [build.config.test_command]
        v = _completed(build).verification
        assert v["passed"] is True and "reused_from" not in v["metadata"]


async def _unstopped_build_outcome(tmp_path, monkeypatch) -> dict:
    """The `build` fixture's run with no stop, in its own directory."""
    with monkeypatch.context() as m:
        (tmp_path / "unstopped").mkdir()
        b = _make_build(tmp_path / "unstopped", m)
        b.save()
        assert await orchestrator.run_orchestrator(b.config, b.args) == 0
        return _build_outcome(b)


# --- D, fixer: `findings/loop.py` `do_publish` ---------------------------------
#
# The findings round of test_findings_stop_resume_d1.py (`_Round`): one code
# fix, a check agent answering "" (nothing) or PROBLEM (one more block). The
# round's engine runs under `per_block` there, so its END_SUITE never runs;
# `_PassRound` saves the `end_suite_pass` that END_SUITE would have saved, at
# the head the first engine run returned at. A self-check block runs the
# engine again with `end_suite_done` already set, so the pass stays there.


class _PassRound(_Round):
    def __init__(self, base: Path, monkeypatch, report: str = ""):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        super().__init__(base, monkeypatch, report)

    def spies(self):
        log = self.log
        real_engine = tdd_engine.run_tdd_engine
        real_final = gates.final_verification

        async def engine(config, args, **kw):
            code_ = await real_engine(config, args, **kw)
            state = BuildState.load(config.state_file_path())
            if code_ == 0 and state.tdd and state.tdd.end_suite_pass is None:
                state.tdd.end_suite_done = True
                state.tdd.end_suite_pass = {
                    "head": git_ops.rev_parse_head(config.project_dir),
                    "command": config.test_command,
                    "parallel": config.parallel_tests,
                }
                state.checkpoint(config.state_file_path())
                _log(log, "end_suite_pass", "")
            return code_

        def final(command, *a, **kw):
            _log(log, "final", command)
            return real_final(command, *a, **kw)

        return [
            patch("build_pipeline.tdd_engine.run_tdd_engine", side_effect=engine),
            patch("build_pipeline.gates.final_verification", side_effect=final),
        ]

    def run(self, *, patches=(), **kw) -> int:
        return super().run(patches=[*self.spies(), *patches], **kw)

    def finals(self) -> int:
        return sum(1 for c in calls(self.log) if c[0] == "final")

    def verification(self) -> dict:
        return BuildState.load(self.change / loop.STATE_FILE).verification


class TestFixer:
    def test_nothing_changed_after_the_pass_runs_no_test(self, tmp_path, monkeypatch):
        r = _PassRound(tmp_path / "r", monkeypatch)
        assert r.run() == 0, r.lines
        assert ("end_suite_pass", "") in calls(r.log)
        assert r.finals() == 0
        v = r.verification()
        recorded = BuildState.load(r.change / code.engine_state_name(1))
        assert v["passed"] is True
        assert v["metadata"]["reused_from"] == recorded.tdd.end_suite_pass["head"]
        assert v["metadata"]["reused_from"] == _git(r.repo, "rev-parse", "HEAD")
        assert v["reason"].endswith(": nothing changed since")
        assert ("push",) == r.writes()[0][:1]

    def test_a_self_check_commit_after_the_pass_runs_final_verification(
        self, tmp_path, monkeypatch
    ):
        r = _PassRound(tmp_path / "r", monkeypatch, PROBLEM)
        assert r.run() == 0, r.lines
        recorded = BuildState.load(r.change / code.engine_state_name(1))
        head = recorded.tdd.end_suite_pass["head"]
        assert head != _git(r.repo, "rev-parse", "HEAD")  # the check's block
        assert r.finals() == 1
        v = r.verification()
        assert v["passed"] is True and "reused_from" not in v["metadata"]

    def test_no_engine_checkpoint_gives_no_reuse(self, tmp_path, monkeypatch):
        r = _PassRound(tmp_path / "r", monkeypatch)
        assert (
            code.reused_end_suite(None, r.repo, r.config) is None
            and code.reused_end_suite(r.change / "missing.json", r.repo, r.config)
            is None
        )

    def test_s21_killed_after_the_reused_verification_before_publish_round(
        self, tmp_path, monkeypatch
    ):
        """S21: the reused record is in `.findings-state.json`; `do_publish`
        runs again, the same check gives the same answer, no test runs, and
        it publishes as a round that never stopped."""
        clean = _PassRound(tmp_path / "clean", monkeypatch)
        assert clean.run() == 0, clean.lines
        assert clean.finals() == 0

        stopped = _PassRound(tmp_path / "stopped", monkeypatch)
        stop = patch.object(
            publish, "publish_round", side_effect=lambda *a, **k: _die()
        )
        _killed(lambda: stopped.run(patches=[stop]))
        saved = stopped.verification()
        assert saved["passed"] is True
        assert saved["metadata"]["reused_from"] == _git(
            stopped.repo, "rev-parse", "HEAD"
        )
        state = BuildState.load(stopped.change / loop.STATE_FILE)
        rnd = state.findings.rounds[-1]
        assert "publish" in rnd.started and "publish" not in rnd.finished
        assert stopped.pr.events == []  # nothing pushed or replied

        assert stopped.run() == 0, stopped.lines
        assert stopped.finals() == 0
        _assert_like_unstopped(stopped, clean)
