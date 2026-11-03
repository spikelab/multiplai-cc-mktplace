"""The end-of-build run keeps the whole-change refactor when the tests that
failed with it still fail without it (item 7, E, of
plan-buildme-epic-speedups-2026-11-03.md; its item 8 bullet "E, engine"; and
rows S22-S26 of its Stop and resume table).

The tests run the real `_run_end_suite` against a real git repo whose whole
test command is real pytest: `tests/test_a.py::test_one` fails while
`broken.flag` exists. The history is init, the block's commit P and the
whole-change refactor R. In "innocent" repos P adds `broken.flag`, so the
test fails with or without R; in "guilty" repos R adds it. Only the model
calls are replaced; the end fixer deletes `broken.flag`. Each `test_s<N>_...`
stops at row S<N>'s point, resumes, and compares the result with a run that
never stopped, except where the row's "Resume does" says otherwise.
"""

import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from build_pipeline import gates, git_ops, tdd_engine
from build_pipeline.config import BuildConfig
from build_pipeline.gates import RunnerFailed
from build_pipeline.models import AgentResult, BlockInfo, BlockStatus
from build_pipeline.progress import ProgressWriter
from build_pipeline.state import BuildState, TDDState
from build_pipeline.tdd_engine import _run_end_suite

WHOLE = f"{sys.executable} -m pytest -q -p no:cacheprovider"
FAILED_ID = "tests/test_a.py::test_one"
CHECK = f"{WHOLE} {FAILED_ID}"
REFACTOR_SUBJECT = "refactor: simplify across blocks"
FIX_SUBJECT = "fix: end-of-build test suite"

TEST_FILE = """\
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_one():
    assert not (ROOT / "broken.flag").exists()


def test_two():
    pass
"""


class Killed(BaseException):
    """A stop, as SIGKILL would leave it: nothing after the raise runs."""


def _git(root: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(root: Path, subject: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", subject)
    return _git(root, "rev-parse", "HEAD")


def _make(base: Path, *, innocent: bool, command: str = WHOLE):
    project_dir = base / "project"
    change = project_dir / "specs" / "changes" / "test"
    change.mkdir(parents=True)
    (change / "proposal.md").write_text("# test\n")
    (project_dir / "app.py").write_text("x = 1\n")
    (project_dir / "pytest.ini").write_text("[pytest]\n")
    (project_dir / ".gitignore").write_text("__pycache__/\n")
    (project_dir / "tests").mkdir()
    (project_dir / "tests" / "test_a.py").write_text(TEST_FILE)
    _git(project_dir, "init", "-q")
    _git(project_dir, "config", "user.email", "t@example.com")
    _git(project_dir, "config", "user.name", "T")
    baseline = _commit(project_dir, "init")
    (project_dir / "b1.py").write_text("n = 1\n")
    if innocent:
        (project_dir / "broken.flag").write_text("")
    parent = _commit(project_dir, "fix(block-1): B1")
    (project_dir / "app.py").write_text("x = 1  # simplified\n")
    if not innocent:
        (project_dir / "broken.flag").write_text("")
    refactor = _commit(project_dir, REFACTOR_SUBJECT)

    config = BuildConfig(
        project_dir=project_dir,
        change_name="test",
        tier="advanced",
        test_command=command,
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
            refactor_all_commit=refactor,
        ),
    )
    state.checkpoint(config.state_file_path())
    progress = ProgressWriter(config.progress_file_path())
    progress.initialize("test", "advanced", 1)
    return SimpleNamespace(
        dir=project_dir,
        config=config,
        progress=progress,
        parent=parent,
        refactor=refactor,
    )


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    tdd_engine._WHOLE_FALLBACK_WARNED.clear()
    count = iter(range(100))

    def build(**kwargs):
        return _make(tmp_path / f"run{next(count)}", **kwargs)

    return build


class Spy:
    """Records each whole run's result and each command the check runs, and
    runs the real ones."""

    def __init__(self):
        self.gates: list = []
        self.checks: list[str] = []

    def patches(self) -> list:
        def gate(*args, **kwargs):
            result = gates.integration_gate(*args, **kwargs)
            self.gates.append(result)
            return result

        def check(command, *args, **kwargs):
            self.checks.append(command)
            return gates.run_test_suite(command, *args, **kwargs)

        return [
            patch("build_pipeline.tdd_engine.integration_gate", side_effect=gate),
            patch("build_pipeline.tdd_engine.run_test_suite", side_effect=check),
        ]


async def _end(p, *, fixes=(True, True, True), spy: Spy | None = None, **patches):
    """Run the end run in a fresh process (the state as saved on disk), with
    the end fixer stubbed: its *n*th call fixes when `fixes[n-1]`."""
    state = BuildState.load(p.config.state_file_path())
    calls = iter(fixes)

    def integration_fix(**kwargs):
        if next(calls):
            (p.dir / "broken.flag").unlink(missing_ok=True)
        return AgentResult(success=True, output="fixed")

    spy = spy or Spy()
    with ExitStack() as stack:
        fixer = stack.enter_context(
            patch(
                "build_pipeline.tdd_engine.run_integration_fix",
                new_callable=AsyncMock,
                side_effect=integration_fix,
            )
        )
        for each in spy.patches():
            stack.enter_context(each)
        for target, value in patches.items():
            stack.enter_context(patch(f"build_pipeline.tdd_engine.{target}", **value))
        result = await _run_end_suite(p.config, state, p.progress, False)
    return result, SimpleNamespace(fixer=fixer, spy=spy)


def _saved(p) -> TDDState:
    return BuildState.load(p.config.state_file_path()).tdd


def _outcome(p) -> dict:
    """What a resumed run must share with one that never stopped: commit
    subjects, the tracked files, a clean tree and the end run's saved
    fields (the refactor's sha named by role: each repo has its own)."""
    tracked = _git(p.dir, "ls-files").splitlines()
    saved = _saved(p)
    names = {p.refactor: "R", p.parent: "P"}
    return {
        "subjects": _git(p.dir, "log", "--format=%s").splitlines(),
        "files": {name: (p.dir / name).read_text() for name in tracked},
        "dirty": _git(
            p.dir, "status", "--porcelain", "--", ".", *git_ops.BOOKKEEPING_EXCLUDES
        ),
        "tdd": {
            "end_suite_done": saved.end_suite_done,
            "end_suite": saved.end_suite,
            "refactor_all_commit": names.get(
                saved.refactor_all_commit, saved.refactor_all_commit
            ),
        },
    }


def _today(**extra) -> dict:
    """The end run before item 7: no failed-tests command, so no check."""
    return {"failed_tests_command": {"return_value": None}, **extra}


KEPT_SUBJECTS = [FIX_SUBJECT, REFACTOR_SUBJECT, "fix(block-1): B1", "init"]
DISCARDED_SUBJECTS = [FIX_SUBJECT, "fix(block-1): B1", "init"]


@pytest.fixture
def unstopped(make):
    """The innocent repo's run that never stopped: refactor kept, fixed."""

    async def run():
        p = make(innocent=True)
        result, mocks = await _end(p)
        assert result is True and mocks.fixer.await_count == 1
        return _outcome(p)

    return run


@pytest.fixture
def unstopped_guilty(make):
    """The guilty repo's run that never stopped: refactor discarded, green."""

    async def run():
        p = make(innocent=False)
        result, mocks = await _end(p)
        assert result is True
        mocks.fixer.assert_not_awaited()
        return _outcome(p)

    return run


# --- Item 8, "E, engine" -----------------------------------------------------


class TestEngine:
    @pytest.mark.asyncio
    async def test_same_ids_red_keeps_the_refactor(self, make):
        p = make(innocent=True)
        result, mocks = await _end(p)
        assert result is True
        assert mocks.spy.checks == [CHECK]
        # Only the red whole run at R and the green one after the fix: the
        # kept refactor is not run whole again before the fixers.
        assert [g.passed for g in mocks.spy.gates] == [False, True]
        assert _git(p.dir, "log", "--format=%s").splitlines() == KEPT_SUBJECTS
        assert _git(p.dir, "rev-parse", "HEAD~1") == p.refactor
        saved = _saved(p)
        assert saved.end_suite_done is True and saved.end_suite is None
        assert saved.refactor_all_commit == p.refactor
        progress = p.config.progress_file_path().read_text()
        assert (
            "REFACTOR_ALL KEPT — the same tests fail without it (1 of 1); "
            "fixing them with the refactor in place"
        ) in progress
        assert "REVERTED" not in progress

    @pytest.mark.asyncio
    async def test_the_fix_loop_gets_the_original_red_output(self, make):
        p = make(innocent=True)
        _, mocks = await _end(p)
        first = mocks.spy.gates[0]
        kwargs = mocks.fixer.await_args.kwargs
        assert kwargs["failure_output"] == first.metadata["output"]
        assert kwargs["failing_tests"] == [FAILED_ID]

    @pytest.mark.asyncio
    async def test_head_is_r_and_the_checkpoint_moves_there(self, make):
        """Stopped right as the fix loop starts, HEAD is R and the saved end
        run starts at R with the verdict saved."""
        p = make(innocent=True)
        with pytest.raises(Killed):
            await _end(p, run_integration_fix={"side_effect": Killed()})
        assert _git(p.dir, "rev-parse", "HEAD") == p.refactor
        es = _saved(p).end_suite
        assert es.checkpoint == p.refactor
        assert es.refactor_check == {
            "refactor": p.refactor,
            "parent": p.parent,
            "ids": [FAILED_ID],
            "red_log": es.refactor_check["red_log"],
            "verdict": "not-refactor",
        }
        assert Path(es.refactor_check["red_log"]).exists()

    @pytest.mark.asyncio
    async def test_green_discards_the_refactor_and_runs_whole(
        self, make, unstopped_guilty
    ):
        today = make(innocent=False)
        assert (await _end(today, **_today()))[0] is True
        expected = _outcome(today)

        p = make(innocent=False)
        result, mocks = await _end(p)
        assert result is True
        assert mocks.spy.checks == [CHECK]
        assert [g.passed for g in mocks.spy.gates] == [False, True]
        mocks.fixer.assert_not_awaited()
        assert _git(p.dir, "rev-parse", "HEAD") == p.parent
        assert _outcome(p) == expected == await unstopped_guilty()
        assert "REVERTED — whole suite red after it" in (
            p.config.progress_file_path().read_text()
        )

    @pytest.mark.parametrize(
        "runner",
        [
            {"return_value": (None, "Test command failed to run: timed out")},
            {"side_effect": RunnerFailed(CHECK, [], "no tests ran")},
        ],
        ids=["exit-none", "runner-failed"],
    )
    @pytest.mark.asyncio
    async def test_a_check_that_did_not_run_is_as_today(self, make, runner):
        today = make(innocent=True)
        assert (await _end(today, **_today()))[0] is True
        expected = _outcome(today)
        assert expected["subjects"] == DISCARDED_SUBJECTS

        p = make(innocent=True)
        with pytest.raises(Killed):
            await _end(
                p,
                run_test_suite=runner,
                run_integration_fix={"side_effect": Killed()},
            )
        assert _saved(p).end_suite.refactor_check["verdict"] == "refactor"
        result, mocks = await _end(p)
        assert result is True
        assert mocks.spy.checks == []  # a verdict: no second check
        assert _outcome(p) == expected

    @pytest.mark.asyncio
    async def test_red_only_on_other_tests_discards_the_refactor(self, make):
        today = make(innocent=True)
        assert (await _end(today, **_today()))[0] is True
        expected = _outcome(today)

        p = make(innocent=True)
        other = "FAILED tests/test_a.py::test_two - assert False\n1 failed in 0.01s\n"
        with pytest.raises(Killed):
            await _end(
                p,
                run_test_suite={"return_value": (1, other)},
                run_integration_fix={"side_effect": Killed()},
            )
        assert _saved(p).end_suite.refactor_check["verdict"] == "refactor"
        assert "REFACTOR_ALL KEPT" not in p.config.progress_file_path().read_text()
        result, mocks = await _end(p)
        assert result is True
        assert mocks.spy.checks == []
        assert _outcome(p) == expected

    @pytest.mark.asyncio
    async def test_no_command_is_as_today(self, make):
        """A chained `test_command` with no `narrow_commands` cannot be
        narrowed: the refactor is discarded unchecked, as before item 7."""
        chained = f"sh -c '{WHOLE}; exit $?'"
        p = make(innocent=True, command=chained)
        with pytest.raises(Killed):
            await _end(p, run_integration_fix={"side_effect": Killed()})
        saved = _saved(p)
        assert saved.end_suite.refactor_check is None
        assert saved.refactor_all_commit is None
        assert _git(p.dir, "rev-parse", "HEAD") == p.parent
        result, mocks = await _end(p)
        assert result is True
        assert mocks.spy.checks == []
        assert _git(p.dir, "log", "--format=%s").splitlines() == DISCARDED_SUBJECTS

    @pytest.mark.asyncio
    async def test_a_second_red_run_after_a_verdict_starts_no_check(self, make):
        p = make(innocent=True)
        result, mocks = await _end(p, fixes=(False, True))
        assert result is True
        assert mocks.fixer.await_count == 2
        assert [g.passed for g in mocks.spy.gates] == [False, False, True]
        assert mocks.spy.checks == [CHECK]
        assert _git(p.dir, "log", "--format=%s").splitlines() == KEPT_SUBJECTS

    @pytest.mark.asyncio
    async def test_a_saved_verdict_at_r_starts_no_check(self, make):
        """A red whole run at R whose end run already holds a verdict goes
        straight to the fixers."""
        p = make(innocent=True)
        with pytest.raises(Killed):
            await _end(p, run_integration_fix={"side_effect": Killed()})
        result, mocks = await _end(p)
        assert result is True
        assert mocks.spy.checks == []
        assert mocks.fixer.await_count == 1

    @pytest.mark.asyncio
    async def test_git_refusing_the_fast_forward_leaves_it_discarded(self, make):
        p = make(innocent=True)
        with pytest.raises(Killed):
            await _end(
                p,
                **{
                    "git_ops.fast_forward_to": {"return_value": False},
                    "run_integration_fix": {"side_effect": Killed()},
                },
            )
        saved = _saved(p)
        assert saved.end_suite.refactor_check["verdict"] == "refactor"
        assert saved.refactor_all_commit is None
        assert _git(p.dir, "rev-parse", "HEAD") == p.parent

    def test_an_older_checkpoint_loads_refactor_check_as_none(self, make):
        import json

        p = make(innocent=True)
        raw = json.loads(p.config.state_file_path().read_text())
        raw["tdd"]["end_suite"] = {"checkpoint": p.refactor, "attempts": 0}
        p.config.state_file_path().write_text(json.dumps(raw))
        assert _saved(p).end_suite.refactor_check is None


# --- Stop and resume, rows S22-S26 --------------------------------------------


def _nth(n: int, real):
    """A side effect that stops on its *n*th call and runs *real* otherwise."""
    count = {"calls": 0}

    def run(*args, **kwargs):
        count["calls"] += 1
        if count["calls"] == n:
            raise Killed()
        return real(*args, **kwargs)

    return run


def _then_stop(real):
    def run(*args, **kwargs):
        real(*args, **kwargs)
        raise Killed()

    return run


class TestStopAndResume:
    @pytest.mark.asyncio
    async def test_s22_stopped_before_the_discard(self, make, unstopped):
        expected = await unstopped()
        p = make(innocent=True)
        with pytest.raises(Killed):
            await _end(p, **{"git_ops.discard_to": {"side_effect": Killed()}})
        assert _git(p.dir, "rev-parse", "HEAD") == p.refactor
        check = _saved(p).end_suite.refactor_check
        assert check["verdict"] == "" and check["parent"] == p.parent

        result, mocks = await _end(p)
        assert result is True
        # A whole run at R, red, then the check again from step 1.
        assert [g.passed for g in mocks.spy.gates] == [False, True]
        assert mocks.spy.checks == [CHECK]
        assert _outcome(p) == expected

    @pytest.mark.asyncio
    async def test_s23_stopped_during_the_check_innocent(self, make, unstopped):
        expected = await unstopped()
        p = make(innocent=True)
        with pytest.raises(Killed):
            await _end(p, run_test_suite={"side_effect": Killed()})
        assert _git(p.dir, "rev-parse", "HEAD") == p.parent
        assert _saved(p).end_suite.refactor_check["verdict"] == ""

        result, mocks = await _end(p)
        assert result is True
        assert mocks.spy.checks == [CHECK]  # step 3 again, not END_SUITE over
        # Resume then runs whole at R (red, no second check) before fixing.
        assert [g.passed for g in mocks.spy.gates] == [False, True]
        assert mocks.fixer.await_count == 1
        assert _outcome(p) == expected

    @pytest.mark.asyncio
    async def test_s23_stopped_during_the_check_guilty(self, make, unstopped_guilty):
        expected = await unstopped_guilty()
        p = make(innocent=False)
        with pytest.raises(Killed):
            await _end(p, run_test_suite={"side_effect": Killed()})
        assert _git(p.dir, "rev-parse", "HEAD") == p.parent

        result, mocks = await _end(p)
        assert result is True
        assert mocks.spy.checks == [CHECK]
        assert [g.passed for g in mocks.spy.gates] == [True]  # whole at P
        mocks.fixer.assert_not_awaited()
        assert _outcome(p) == expected

    @pytest.mark.asyncio
    async def test_s24_verdict_saved_before_the_fast_forward(self, make, unstopped):
        expected = await unstopped()
        p = make(innocent=True)
        with pytest.raises(Killed):
            await _end(p, **{"git_ops.fast_forward_to": {"side_effect": Killed()}})
        assert _git(p.dir, "rev-parse", "HEAD") == p.parent
        assert _saved(p).end_suite.refactor_check["verdict"] == "not-refactor"

        result, mocks = await _end(p)
        assert result is True
        assert mocks.spy.checks == []
        assert [g.passed for g in mocks.spy.gates] == [False, True]  # whole at R
        assert mocks.fixer.await_count == 1
        assert _outcome(p) == expected

    @pytest.mark.asyncio
    async def test_s24_verdict_saved_after_the_fast_forward(self, make, unstopped):
        expected = await unstopped()
        p = make(innocent=True)
        with pytest.raises(Killed):
            await _end(
                p,
                **{
                    "git_ops.fast_forward_to": {
                        "side_effect": _then_stop(git_ops.fast_forward_to)
                    }
                },
            )
        assert _git(p.dir, "rev-parse", "HEAD") == p.refactor
        assert _saved(p).end_suite.refactor_check["verdict"] == "not-refactor"

        result, mocks = await _end(p)
        assert result is True
        assert mocks.spy.checks == []  # a red run at R goes to the fixers
        assert [g.passed for g in mocks.spy.gates] == [False, True]
        assert mocks.fixer.await_count == 1
        assert _outcome(p) == expected

    @pytest.mark.asyncio
    async def test_s25_verdict_refactor_saved(self, make, unstopped_guilty):
        expected = await unstopped_guilty()
        p = make(innocent=False)
        with pytest.raises(Killed):
            await _end(
                p,
                _start_end_suite={"side_effect": _nth(2, tdd_engine._start_end_suite)},
            )
        assert _git(p.dir, "rev-parse", "HEAD") == p.parent
        saved = _saved(p)
        assert saved.end_suite.refactor_check["verdict"] == "refactor"
        assert saved.refactor_all_commit is None

        result, mocks = await _end(p)
        assert result is True
        assert mocks.spy.checks == []
        assert [g.passed for g in mocks.spy.gates] == [True]  # whole at P
        assert _outcome(p) == expected

    @pytest.mark.parametrize(
        "stop",
        [
            {"run_test_suite": {"side_effect": Killed()}},
            {"git_ops.fast_forward_to": {"side_effect": Killed()}},
        ],
        ids=["undecided", "not-refactor"],
    )
    @pytest.mark.asyncio
    async def test_s26_user_changes_between_stop_and_resume_refuse(
        self, make, capsys, stop
    ):
        p = make(innocent=True)
        with pytest.raises(Killed):
            await _end(p, **stop)
        assert _git(p.dir, "rev-parse", "HEAD") == p.parent
        (p.dir / "app.py").write_text("x = 2  # mine\n")
        before = _saved(p)

        result, mocks = await _end(p)
        assert result is False
        assert mocks.spy.checks == [] and mocks.spy.gates == []
        mocks.fixer.assert_not_awaited()
        assert (
            "did not resume — uncommitted changes in app.py" in capsys.readouterr().out
        )
        assert _saved(p) == before
        assert _git(p.dir, "rev-parse", "HEAD") == p.parent  # nothing moved
        assert (p.dir / "app.py").read_text() == "x = 2  # mine\n"
