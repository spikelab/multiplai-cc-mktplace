"""`tdd.whole_suite: end` runs the whole test command once after the last
block; every failed whole run saves its output (G23); no agent is told to run
the whole suite (G31); the weak-test scan counts Jest tests and records the
auditor's reason (G32).

The tests run the real engine against a real git repo whose whole test
command fails while `broken.flag` exists, and whose narrowed command (the
block's or the change's tests) fails while `block.flag` exists. Only the
model calls are replaced. Each `test_S<n>_...` is row S<n> of the Stop and
resume table in plan-buildme-full-suite-once-2026-10-28.md: it stops the
step at the row's point, resumes, and compares the result with a run that
never stopped.
"""

import json
import re
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from build_pipeline import gates, git_ops, tdd_engine
from build_pipeline.config import BuildConfig
from build_pipeline.gates import (
    TEST_RUNS_DIR,
    failing_tests,
    final_verification,
    integration_gate,
)
from build_pipeline.models import (
    AgentResult,
    BlockInfo,
    BlockStatus,
    FindingAdjudication,
    PriorAssessment,
    ReviewFinding,
    ReviewResult,
    ReviewScore,
    TestQualityAudit,
)
from build_pipeline.narrow_tests import agent_test_command_multi
from build_pipeline.progress import ProgressWriter
from build_pipeline.state import BuildState, TDDState
from build_pipeline.tdd_engine import (
    EXIT_BUILD_FAILURE,
    EXIT_SUCCESS,
    _run_end_suite,
    run_tdd_engine,
    run_test_quality_check,
)


def _flag_command(flag: str) -> str:
    return (
        f'{sys.executable} -c "import pathlib,sys; '
        f"sys.exit(pathlib.Path('{flag}').exists())\""
    )


WHOLE = _flag_command("broken.flag")
NARROW = _flag_command("block.flag")


# "lock" as a word in a file name or state key, not inside "block".
LOCK = re.compile(r"(^|[._-])lock($|[._-])", re.IGNORECASE)


class Killed(BaseException):
    """A stop, as SIGKILL would leave it: nothing after the raise runs."""


def _git(root: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _make(base: Path, *, blocks: int = 1, whole_suite: str = "end"):
    project_dir = base / "project"
    change = project_dir / "specs" / "changes" / "test"
    change.mkdir(parents=True)
    # Tracked, as the spec stages leave it, so `.buildme-test-runs/` is an
    # untracked folder inside a tracked one.
    (change / "proposal.md").write_text("# test\n")
    (project_dir / "app.py").write_text("x = 1\n")
    _git(project_dir, "init", "-q")
    _git(project_dir, "config", "user.email", "t@example.com")
    _git(project_dir, "config", "user.name", "T")
    _git(project_dir, "add", "-A")
    _git(project_dir, "commit", "-qm", "init")
    baseline = _git(project_dir, "rev-parse", "HEAD")
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
    config.whole_suite = whole_suite
    config.gates.e2e_test_entry_point_check = False
    state = BuildState(
        change_name="test",
        tier="advanced",
        state_file=str(config.state_file_path()),
        tdd=TDDState(
            blocks=[
                BlockInfo(
                    number=n,
                    name=f"B{n}",
                    description=f"block {n}",
                    status=BlockStatus.IMPLEMENTING,
                    baseline_commit=baseline,
                )
                for n in range(1, blocks + 1)
            ],
            baseline_tests_pass=True,
        ),
    )
    state.checkpoint(config.state_file_path())
    progress = ProgressWriter(config.progress_file_path())
    progress.initialize("test", "advanced", blocks)
    return SimpleNamespace(
        dir=project_dir, config=config, state=state, progress=progress
    )


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    tdd_engine._WHOLE_FALLBACK_WARNED.clear()
    count = iter(range(100))

    def build(**kwargs):
        return _make(tmp_path / f"run{next(count)}", **kwargs)

    return build


def _review(score: int, findings=()) -> ReviewResult:
    return ReviewResult(
        scores=[ReviewScore(dimension="Q", weight=2, score=score, evidence="e")],
        findings=list(findings),
    )


def _finding() -> ReviewFinding:
    return ReviewFinding(
        claim="off-by-one",
        severity="Major",
        file_path="app.py",
        line=1,
        evidence="x = 1",
    )


def _round_with_fixes():
    """A failing review, a passing one that keeps the finding open (so a fix
    on pass runs), and a passing one."""
    still = _review(5).model_copy(
        update={
            "prior_assessments": [
                PriorAssessment(finding_id="B01-R1-F1", status="still_present")
            ]
        }
    )
    return [_review(2, [_finding()]), still, _review(5)]


class Recorder:
    """Records every test command the engine runs through the three gates it
    imports, then runs the real gate."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def patches(self) -> list:
        def wrap(kind, real):
            def run(command, *args, **kwargs):
                self.calls.append((kind, command))
                return real(command, *args, **kwargs)

            return run

        return [
            patch(
                "build_pipeline.tdd_engine.integration_gate",
                side_effect=wrap("integration", gates.integration_gate),
            ),
            patch(
                "build_pipeline.tdd_engine.baseline_test_gate",
                side_effect=wrap("baseline", gates.baseline_test_gate),
            ),
            patch(
                "build_pipeline.tdd_engine.final_verification",
                side_effect=wrap("final", gates.final_verification),
            ),
        ]

    def commands(self) -> list[str]:
        return [command for _, command in self.calls]


def _agents(
    p,
    reviews=None,
    *,
    narrow: str = NARROW,
    block_writes: dict[str, str] | None = None,
    end_fix: bool = True,
    refactor_writes: dict[str, str] | None = None,
    recorder: Recorder | None = None,
):
    """Patch the model calls and the narrowing. Returns (stack, mocks)."""
    stack = ExitStack()
    mocks = SimpleNamespace()

    async def block_tdd(block, config, state, progress):
        (p.dir / f"b{block.number}.py").write_text(f"n = {block.number}\n")
        for rel, text in (block_writes or {}).items():
            (p.dir / rel).write_text(text)
        return True

    def implementer(**kwargs):
        with (p.dir / "app.py").open("a") as f:
            f.write("# fixed\n")
        return AgentResult(success=True, output="fixed")

    def integration_fix(**kwargs):
        if end_fix:
            (p.dir / "broken.flag").unlink(missing_ok=True)
            (p.dir / "block.flag").unlink(missing_ok=True)
        return AgentResult(success=True, output="fixed")

    def refactor_all(**kwargs):
        for rel, text in (refactor_writes or {}).items():
            (p.dir / rel).write_text(text)
        return AgentResult(success=True, output="simplified")

    mocks.block = stack.enter_context(
        patch("build_pipeline.tdd_engine.run_block_tdd", side_effect=block_tdd)
    )
    mocks.review = stack.enter_context(
        patch(
            "build_pipeline.tdd_engine._run_quality_review",
            new_callable=AsyncMock,
            side_effect=list(reviews if reviews is not None else [_review(5)] * 9),
        )
    )
    mocks.implementer = stack.enter_context(
        patch(
            "build_pipeline.tdd_engine.run_implementer",
            new_callable=AsyncMock,
            side_effect=implementer,
        )
    )
    mocks.fixer = stack.enter_context(
        patch(
            "build_pipeline.tdd_engine.run_integration_fix",
            new_callable=AsyncMock,
            side_effect=integration_fix,
        )
    )
    mocks.refactor = stack.enter_context(
        patch(
            "build_pipeline.tdd_engine.run_refactor_all",
            new_callable=AsyncMock,
            side_effect=refactor_all,
        )
    )
    stack.enter_context(
        patch(
            "build_pipeline.tdd_engine.llm_call_structured",
            new_callable=AsyncMock,
            return_value=FindingAdjudication(),
        )
    )
    stack.enter_context(
        patch(
            "build_pipeline.tdd_engine._run_mutation",
            new_callable=AsyncMock,
            return_value=True,
        )
    )
    stack.enter_context(
        patch(
            "build_pipeline.tdd_engine._test_command_since",
            side_effect=lambda config, base: narrow,
        )
    )
    for each in (recorder or Recorder()).patches():
        stack.enter_context(each)
    return stack, mocks


async def _engine(p, standalone: bool = False) -> int:
    return await run_tdd_engine(
        p.config, SimpleNamespace(block=None), standalone=standalone
    )


def _runs(p) -> list[Path]:
    return sorted((p.config.change_dir / TEST_RUNS_DIR).glob("*"))


def _outcome(p) -> dict:
    """What a resumed run must share with one that never stopped: commit
    subjects, the files git sees (round records by name only: they hold
    timestamps), the saved TDD state's whole-suite fields, and how many
    failure files were written."""
    files = {}
    for name in _git(
        p.dir,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        ".",
        *git_ops.BOOKKEEPING_EXCLUDES,
    ).splitlines():
        path = p.dir / name
        files[name] = (
            "" if "/reviews/" in name or not path.exists() else path.read_text()
        )
    saved = BuildState.load(p.config.state_file_path()).tdd
    return {
        "subjects": _git(p.dir, "log", "--format=%s").splitlines(),
        "files": files,
        "dirty": _git(
            p.dir, "status", "--porcelain", "--", ".", *git_ops.BOOKKEEPING_EXCLUDES
        ),
        "tdd": {
            "whole_suite_deferred": saved.whole_suite_deferred,
            "end_suite_done": saved.end_suite_done,
            "end_suite": saved.end_suite,
            "refactor_all_done": saved.refactor_all_done,
            "blocks": [b.status for b in saved.blocks],
        },
        "runs": len(_runs(p)),
    }


# --- Done means 3 and 4: which command each gate runs --------------------------


class TestWhichCommandEachGateRuns:
    @pytest.mark.asyncio
    async def test_per_block_runs_the_whole_command_where_it_always_did(self, make):
        p = make(whole_suite="per_block")
        p.state.tdd.baseline_tests_pass = False
        p.state.checkpoint(p.config.state_file_path())
        rec = Recorder()
        stack, mocks = _agents(p, _round_with_fixes(), recorder=rec)
        with stack:
            assert await _engine(p) == EXIT_SUCCESS

        # Baseline; the block's integration gate, its review fix and its fix
        # on pass; the whole-change refactor's run before and after it.
        assert rec.calls == [
            ("baseline", WHOLE),
            ("integration", WHOLE),
            ("integration", WHOLE),
            ("integration", WHOLE),
            ("integration", WHOLE),
            ("integration", WHOLE),
        ]
        assert mocks.implementer.await_count == 2
        saved = BuildState.load(p.config.state_file_path()).tdd
        assert saved.whole_suite_deferred is False
        assert saved.end_suite_done is False
        assert "END_SUITE" not in p.config.progress_file_path().read_text()

    @pytest.mark.asyncio
    async def test_end_runs_the_whole_command_at_the_baseline_and_once_at_the_end(
        self, make
    ):
        p = make(blocks=3)
        p.state.tdd.baseline_tests_pass = False
        p.state.checkpoint(p.config.state_file_path())
        rec = Recorder()
        stack, mocks = _agents(p, _round_with_fixes() * 3, recorder=rec)
        with stack:
            assert await _engine(p) == EXIT_SUCCESS

        assert mocks.implementer.await_count == 6  # a review fix and a fix on pass each
        whole = [c for c in rec.calls if c[1] == WHOLE]
        assert whole == [("baseline", WHOLE), ("integration", WHOLE)]
        assert rec.calls[-1] == ("integration", WHOLE)  # the end run is the last run
        assert [c for c in rec.calls if c[1] != WHOLE] == [("integration", NARROW)] * 11
        saved = BuildState.load(p.config.state_file_path()).tdd
        assert saved.whole_suite_deferred is True
        assert saved.end_suite_done is True
        assert "phase=END_SUITE DONE" in p.config.progress_file_path().read_text()

    @pytest.mark.asyncio
    async def test_a_block_it_cannot_narrow_runs_the_whole_command_and_says_so(
        self, make, capsys, caplog
    ):
        p = make()
        rec = Recorder()
        stack, _ = _agents(p, [_review(5)], narrow=WHOLE, recorder=rec)
        with stack:
            assert await _engine(p) == EXIT_SUCCESS

        assert "whole-suite (could not narrow)" in capsys.readouterr().out
        assert caplog.text.count("could not be narrowed") == 1


# --- Done means 6: G31 --------------------------------------------------------


class TestNoAgentIsGivenTheWholeCommand:
    @pytest.mark.asyncio
    async def test_no_agent_in_a_block_or_after_it_gets_the_bare_whole_command(
        self, make
    ):
        p = make()
        stack, mocks = _agents(p, _round_with_fixes(), block_writes={"broken.flag": ""})
        with stack:
            assert await _engine(p) == EXIT_SUCCESS

        given = [
            call.kwargs["test_command"]
            for mock in (mocks.implementer, mocks.fixer, mocks.refactor)
            for call in mock.await_args_list
        ]
        assert mocks.fixer.await_count == 1  # the end fixer
        assert len(given) == 4
        assert WHOLE not in given
        assert all("Do not run this command" in text for text in given)
        assert mocks.fixer.await_args.kwargs["failing_tests"] == []

    def test_the_multi_command_text_before_any_change_never_says_before_you_finish(
        self, tmp_path
    ):
        from build_pipeline.narrow_tests import NarrowCommand

        (tmp_path / "web").mkdir()
        _git(tmp_path, "init", "-q")
        _git(
            tmp_path,
            "-c",
            "user.email=t@e",
            "-c",
            "user.name=T",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "i",
        )
        entries = [NarrowCommand(dir="web", command="npx jest", runner="jest")]
        text = agent_test_command_multi(entries, tmp_path, "HEAD", True, "make test")
        assert "before you finish" not in text


# --- Done means 7: G23 --------------------------------------------------------

PYTEST_OUT = (
    "....F\n=== short test summary info ===\n"
    "FAILED tests/test_a.py::test_x - AssertionError: no\n"
    "ERROR tests/test_b.py::test_y[a b] - RuntimeError\n"
    "1 failed, 1 error\n"
)
JEST_OUT = " PASS  src/ok.test.ts\n FAIL  src/bad.test.ts\n  ● bad › breaks\n"


class TestFailedRunsSaveTheirOutput:
    def test_failing_tests_reads_pytest_and_jest_ids(self):
        assert failing_tests(PYTEST_OUT) == [
            "tests/test_a.py::test_x",
            "tests/test_b.py::test_y[a b]",
        ]
        assert failing_tests(JEST_OUT) == ["src/bad.test.ts"]
        assert failing_tests("\x1b[31m FAIL \x1b[0m src/c.test.js\n") == [
            "src/c.test.js"
        ]

    @pytest.mark.parametrize("output", [PYTEST_OUT, JEST_OUT])
    def test_a_failed_gate_writes_the_file_and_names_it(self, tmp_path, output):
        log_path = tmp_path / "change" / TEST_RUNS_DIR / "x-block-1-integration.log"
        with patch("build_pipeline.gates.run_test_suite", return_value=(1, output)):
            gate = integration_gate("pytest -q", tmp_path, failure_log=log_path)
        assert not gate.passed
        assert gate.metadata["output_file"] == str(log_path)
        text = log_path.read_text()
        assert "command: pytest -q" in text and "exit code: 1" in text
        for test_id in failing_tests(output):
            assert f"  {test_id}\n" in text

    def test_a_run_that_did_not_run_saves_the_reason(self, tmp_path):
        log_path = tmp_path / "x.log"
        with patch(
            "build_pipeline.gates.run_test_suite",
            return_value=(None, "Test command failed to run: timed out"),
        ):
            gate = gates.baseline_test_gate("pytest", tmp_path, failure_log=log_path)
        assert gate.metadata["output_file"] == str(log_path)
        assert (
            "did not run: Test command failed to run: timed out" in log_path.read_text()
        )

    def test_a_passing_gate_writes_nothing(self, tmp_path):
        log_path = tmp_path / "x.log"
        with patch("build_pipeline.gates.run_test_suite", return_value=(0, "ok")):
            gate = integration_gate("pytest", tmp_path, failure_log=log_path)
        assert gate.passed and "output_file" not in gate.metadata
        assert not log_path.exists()

    def test_the_file_is_bookkeeping(self, make):
        p = make()
        log_path = gates.failure_log_path(p.config.change_dir, "baseline")
        gates.write_failure_log(log_path, "pytest", 1, PYTEST_OUT)
        assert log_path.exists()
        assert git_ops.tree_is_clean(p.dir)
        status = _git(
            p.dir, "status", "--porcelain", "--", ".", *git_ops.BOOKKEEPING_EXCLUDES
        )
        assert TEST_RUNS_DIR not in status
        assert git_ops.commit_tree(p.dir, "nothing", "") is None
        git_ops.discard_to(p.dir, _git(p.dir, "rev-parse", "HEAD"), "test")
        assert log_path.exists()

    def test_final_verification_never_reports_its_own_file_as_a_changed_input(
        self, make
    ):
        p = make()
        (p.dir / "broken.flag").write_text("")
        _git(p.dir, "add", "-A")
        _git(p.dir, "commit", "-qm", "red")
        # Outside the bookkeeping folder on purpose: only the write order
        # keeps it out of the inputs.
        log_path = p.dir / "final.log"
        gate = final_verification(WHOLE, p.dir, failure_log=log_path)
        assert gate.reason.startswith("Tests failing")
        assert "changed_files" not in gate.metadata
        assert gate.metadata["output_file"] == str(log_path)


# --- Done means 8 and 9: G32 --------------------------------------------------

JEST_FILE = """\
describe("add", () => {
  it("adds", () => { expect(add(1, 2)).toBe(3); });
  test("adds negatives", () => { expect(add(-1, -2)).toBe(-3); });
  it.each([[1, 1]])("adds %i", (a, b) => { expect(add(a, b)).toBe(2); });
  it.skip("later", () => {});
  xit("not yet", () => {});
  test.todo("someday");
});
"""


class TestWeakTestScanCountsJest:
    def test_a_jest_only_block_with_three_tests_passes(self, make):
        p = make()
        content = f"# --- src/add.test.ts ---\n{JEST_FILE}"
        gate = run_test_quality_check(content, "", p.config)
        assert gate.passed, gate.reason
        assert gate.metadata["total_tests"] == 3

    def test_a_mixed_block_counts_both(self, make):
        p = make()
        content = (
            "# --- tests/test_add.py ---\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            f"# --- web/__tests__/add.js ---\n{JEST_FILE}"
        )
        gate = run_test_quality_check(content, "", p.config)
        assert gate.metadata["total_tests"] == 4

    def test_the_block_reads_its_jest_files(self, make):
        p = make()
        block = p.state.tdd.blocks[0]
        (p.dir / "src").mkdir()
        (p.dir / "src" / "add.test.ts").write_text(JEST_FILE)
        (p.dir / "src" / "add.ts").write_text("export const add = 1;\n")
        content = tdd_engine._read_block_test_files(p.config, block)
        assert content.startswith("# --- src/add.test.ts ---")
        assert "# --- src/add.ts ---" not in content

    @pytest.mark.asyncio
    async def test_the_auditors_reason_reaches_the_progress_file(self, make):
        p = make()
        block = p.state.tdd.blocks[0]
        with patch(
            "build_pipeline.tdd_engine._audit_test_quality",
            new_callable=AsyncMock,
            return_value=TestQualityAudit(passed=True, reason="x"),
        ):
            assert await tdd_engine._enforce_test_quality(
                block, p.config, p.state, p.progress, "", "", 0, "no tests here"
            )
        assert "Test-quality audit (OVERTURNED): x" in (
            p.config.progress_file_path().read_text()
        )


# --- Stop and resume: the block rows (S1-S5) ----------------------------------


class TestBlockRows:
    @pytest.mark.asyncio
    async def test_S1_narrowed_gate_stopped_then_resumed(self, make):
        unstopped = make()
        stack, _ = _agents(unstopped)
        with stack:
            assert await _engine(unstopped) == EXIT_SUCCESS

        p = make()
        stopped = {"done": False}

        def gate_once(command, *args, **kwargs):
            if command == NARROW and not stopped["done"]:
                stopped["done"] = True
                raise Killed()
            return gates.integration_gate(command, *args, **kwargs)

        stack, _ = _agents(p)
        with (
            stack,
            patch("build_pipeline.tdd_engine.integration_gate", side_effect=gate_once),
            pytest.raises(Killed),
        ):
            await _engine(p)
        rec = Recorder()
        stack, _ = _agents(p, recorder=rec)
        with stack:
            assert await _engine(p) == EXIT_SUCCESS
        assert rec.commands()[0] == NARROW  # the gate runs again, still narrowed
        assert _outcome(p) == _outcome(unstopped)

    @pytest.mark.asyncio
    async def test_S2_block_finished_under_end_then_resumed(self, make):
        unstopped = make()
        stack, _ = _agents(unstopped)
        with stack:
            assert await _engine(unstopped) == EXIT_SUCCESS

        p = make()
        stack, _ = _agents(p)
        with (
            stack,
            patch(
                "build_pipeline.tdd_engine._run_refactor_all",
                new_callable=AsyncMock,
                side_effect=Killed(),
            ),
            pytest.raises(Killed),
        ):
            await _engine(p)
        saved = BuildState.load(p.config.state_file_path()).tdd
        assert saved.blocks[0].status == BlockStatus.DONE
        assert saved.whole_suite_deferred is True

        stack, mocks = _agents(p)
        with stack:
            assert await _engine(p) == EXIT_SUCCESS
        mocks.review.assert_not_awaited()  # the block is skipped
        assert _outcome(p) == _outcome(unstopped)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("first,then", [("end", "per_block"), ("per_block", "end")])
    async def test_S3_S4_setting_changed_between_stop_and_resume(
        self, make, first, then
    ):
        p = make(blocks=2, whole_suite=first)
        stack, _ = _agents(p, [_review(5), Killed()])
        with stack, pytest.raises(Killed):
            await _engine(p)
        saved = BuildState.load(p.config.state_file_path()).tdd
        assert saved.whole_suite_deferred is (first == "end")

        p.config.whole_suite = then
        rec = Recorder()
        stack, _ = _agents(p, recorder=rec)
        with stack:
            assert await _engine(p) == EXIT_SUCCESS
        block_two = WHOLE if then == "per_block" else NARROW
        # Block 2's gate; the whole-change refactor before and after, narrowed
        # because the end run is owed; the end run.
        assert rec.commands() == [block_two, NARROW, NARROW, WHOLE]
        saved = BuildState.load(p.config.state_file_path()).tdd
        assert saved.whole_suite_deferred is True
        assert saved.end_suite_done is True

    @pytest.mark.asyncio
    async def test_S5_failure_file_written_then_stopped_before_the_log_line(self, make):
        unstopped = make()
        stack, _ = _agents(unstopped, block_writes={"block.flag": ""})
        with stack:
            assert await _engine(unstopped) == EXIT_SUCCESS

        p = make()
        stack, _ = _agents(p, block_writes={"block.flag": ""})
        with (
            stack,
            patch("build_pipeline.tdd_engine._output_note", side_effect=Killed()),
            pytest.raises(Killed),
        ):
            await _engine(p)
        assert len(_runs(p)) == 1
        stack, _ = _agents(p, block_writes={"block.flag": ""})
        with stack:
            assert await _engine(p) == EXIT_SUCCESS
        expected = _outcome(unstopped)
        expected["runs"] += 1  # the stopped run's file stays; the resume writes its own
        assert _outcome(p) == expected


# --- Stop and resume: the end run (S6-S18) ------------------------------------


def _end_ready(p, red: bool = True):
    """The blocks are done under `end` and the whole-change refactor ran;
    with *red*, the last block committed what turns the whole suite red."""
    tdd = p.state.tdd
    for block in tdd.blocks:
        block.status = BlockStatus.DONE
    tdd.current_block = len(tdd.blocks)
    tdd.whole_suite_deferred = True
    tdd.refactor_all_done = True
    (p.dir / "b1.py").write_text("n = 1\n")
    if red:
        (p.dir / "broken.flag").write_text("")
    _git(p.dir, "add", "-A")
    _git(p.dir, "commit", "-qm", "fix(block-1): B1")
    p.state.checkpoint(p.config.state_file_path())
    return p


async def _end(p, *, standalone: bool = False, fixes: bool = True, **patches):
    """Run the end run in a fresh process: the state as saved on disk."""
    state = BuildState.load(p.config.state_file_path())
    stack, mocks = _agents(p, end_fix=fixes)
    with stack:
        for target, value in patches.items():
            stack.enter_context(patch(f"build_pipeline.tdd_engine.{target}", **value))
        result = await _run_end_suite(p.config, state, p.progress, standalone)
    return result, mocks


@pytest.fixture
def unstopped(make):
    async def run():
        p = _end_ready(make())
        result, mocks = await _end(p)
        assert result is True and mocks.fixer.await_count == 1
        return _outcome(p)

    return run


def _nth(n: int, real):
    """A side effect that stops on its *n*th call and runs *real* otherwise."""
    count = {"calls": 0}

    def run(*args, **kwargs):
        count["calls"] += 1
        if count["calls"] == n:
            raise Killed()
        return real(*args, **kwargs)

    return run


class TestEndRunRows:
    @pytest.mark.asyncio
    async def test_unstopped_end_run_fixes_and_commits(self, make, unstopped):
        outcome = await unstopped()
        assert outcome["subjects"][0] == "fix: end-of-build test suite"
        assert "broken.flag" not in outcome["files"]
        assert outcome["tdd"]["end_suite_done"] is True
        assert outcome["tdd"]["end_suite"] is None
        assert outcome["runs"] == 1  # the red first run

    @pytest.mark.asyncio
    async def test_S6_first_whole_run_stopped(self, make, unstopped):
        expected = await unstopped()
        p = _end_ready(make())
        with pytest.raises(Killed):
            await _end(p, integration_gate={"side_effect": Killed()})
        saved = BuildState.load(p.config.state_file_path()).tdd.end_suite
        assert saved.checkpoint == _git(p.dir, "rev-parse", "HEAD")
        assert saved.attempts == 0
        result, _ = await _end(p)
        assert result is True
        assert _outcome(p) == expected

    @pytest.mark.asyncio
    async def test_S7_fixer_stopped_mid_edit_refuses(self, make, capsys):
        p = _end_ready(make())

        def half_fix(**kwargs):
            (p.dir / "broken.flag").unlink()
            raise Killed()

        with pytest.raises(Killed):
            await _end(p, run_integration_fix={"side_effect": half_fix})
        before = BuildState.load(p.config.state_file_path()).tdd
        assert before.end_suite.attempts == 1
        assert before.end_suite.after_fix_content is None
        head = _git(p.dir, "rev-parse", "HEAD")

        result, mocks = await _end(p)
        assert result is False
        mocks.fixer.assert_not_awaited()
        out = capsys.readouterr().out
        assert "END_SUITE: did not resume — uncommitted changes in broken.flag" in out
        assert "Commit them to keep them or discard them" in out
        assert "did not resume" in p.config.progress_file_path().read_text()
        assert BuildState.load(p.config.state_file_path()).tdd == before
        assert _git(p.dir, "rev-parse", "HEAD") == head
        assert not (p.dir / "broken.flag").exists()

    @pytest.mark.asyncio
    async def test_S8_whole_run_after_the_fix_stopped(self, make, unstopped):
        expected = await unstopped()
        p = _end_ready(make())
        with pytest.raises(Killed):
            await _end(
                p, integration_gate={"side_effect": _nth(2, gates.integration_gate)}
            )
        saved = BuildState.load(p.config.state_file_path()).tdd.end_suite
        assert saved.attempts == 1 and saved.after_fix_content is not None
        result, mocks = await _end(p)
        assert result is True
        mocks.fixer.assert_not_awaited()  # the fixed tree is tested, not fixed again
        assert _outcome(p) == expected

    @pytest.mark.asyncio
    async def test_S9_green_before_the_fix_commit(self, make, unstopped):
        expected = await unstopped()
        p = _end_ready(make())
        with pytest.raises(Killed):
            await _end(p, unchanged_tests_gate={"side_effect": Killed()})
        rec = Recorder()
        state = BuildState.load(p.config.state_file_path())
        stack, mocks = _agents(p, recorder=rec)
        with stack:
            assert await _run_end_suite(p.config, state, p.progress, False)
        assert rec.commands() == [WHOLE]  # the one extra whole run
        mocks.fixer.assert_not_awaited()
        assert _outcome(p) == expected

    @pytest.mark.asyncio
    async def test_S10_fix_committed_before_done_is_saved(self, make, unstopped):
        expected = await unstopped()
        p = _end_ready(make())

        real_commit = git_ops.commit_tree

        def commit_then_stop(*args, **kwargs):
            real_commit(*args, **kwargs)
            raise Killed()

        with (
            patch(
                "build_pipeline.tdd_engine.git_ops.commit_tree",
                side_effect=commit_then_stop,
            ),
            pytest.raises(Killed),
        ):
            await _end(p)
        assert _git(p.dir, "log", "-1", "--format=%s") == "fix: end-of-build test suite"
        result, mocks = await _end(p)
        assert result is True
        mocks.fixer.assert_not_awaited()
        assert _outcome(p) == expected

    @pytest.mark.asyncio
    async def test_S11_exhausted_then_resumed(self, make, unstopped, capsys):
        expected = await unstopped()
        p = _end_ready(make())
        result, mocks = await _end(p, fixes=False)
        assert result is False
        assert mocks.fixer.await_count == 3
        assert mocks.fixer.await_args_list[-1].kwargs["escalate"] is True
        saved = BuildState.load(p.config.state_file_path()).tdd.end_suite
        assert saved.exhausted and saved.attempts == 3
        assert Path(saved.failure_file).exists()
        diffs = list(
            (p.config.change_dir / TEST_RUNS_DIR).glob("*-end-fix-attempts.diff")
        )
        assert len(diffs) == 1
        progress = p.config.progress_file_path().read_text()
        assert saved.failure_file in progress and str(diffs[0]) in progress
        assert "phase=END_SUITE FAIL" in capsys.readouterr().out
        assert git_ops.tree_is_clean(p.dir)
        exhausted_runs = len(_runs(p))

        result, mocks = await _end(p)
        assert result is True
        assert mocks.fixer.await_count == 1
        assert "previous run exhausted 3 fix attempts" in capsys.readouterr().out
        outcome = _outcome(p)
        assert outcome["runs"] == exhausted_runs + 1
        outcome["runs"] = expected["runs"]
        assert outcome == expected

    @pytest.mark.asyncio
    async def test_S12_user_edits_between_stop_and_resume_refuse(self, make, capsys):
        p = _end_ready(make())
        with pytest.raises(Killed):
            await _end(
                p, integration_gate={"side_effect": _nth(2, gates.integration_gate)}
            )
        (p.dir / "app.py").write_text("x = 2  # mine\n")
        before = BuildState.load(p.config.state_file_path()).tdd
        result, mocks = await _end(p)
        assert result is False
        mocks.fixer.assert_not_awaited()
        assert (
            "did not resume — uncommitted changes in app.py" in capsys.readouterr().out
        )
        assert BuildState.load(p.config.state_file_path()).tdd == before
        assert (p.dir / "app.py").read_text() == "x = 2  # mine\n"

    @pytest.mark.asyncio
    async def test_S13_history_moved_on_a_clean_tree(self, make):
        p = _end_ready(make())
        with pytest.raises(Killed):
            await _end(p, integration_gate={"side_effect": Killed()})
        old = BuildState.load(p.config.state_file_path()).tdd.end_suite.checkpoint
        (p.dir / "rebased.py").write_text("r = 1\n")
        _git(p.dir, "add", "-A")
        _git(p.dir, "commit", "-qm", "user: rebased")
        result, _ = await _end(p)
        assert result is True
        assert _git(p.dir, "log", "-2", "--format=%s").splitlines() == [
            "fix: end-of-build test suite",
            "user: rebased",
        ]
        assert old != _git(p.dir, "rev-parse", "HEAD~1")  # a new checkpoint
        assert BuildState.load(p.config.state_file_path()).tdd.end_suite_done

    @pytest.mark.asyncio
    async def test_S14_same_point_stopped_twice(self, make, unstopped):
        expected = await unstopped()
        p = _end_ready(make())
        with pytest.raises(Killed):
            await _end(
                p, integration_gate={"side_effect": _nth(2, gates.integration_gate)}
            )
        with pytest.raises(Killed):
            await _end(p, integration_gate={"side_effect": Killed()})
        assert BuildState.load(p.config.state_file_path()).tdd.end_suite.attempts == 1
        result, mocks = await _end(p)
        assert result is True
        mocks.fixer.assert_not_awaited()
        assert _outcome(p) == expected

    @pytest.mark.asyncio
    async def test_S15_no_lock_is_taken(self, make):
        p = _end_ready(make())
        before = set(p.config.change_dir.rglob("*"))
        result, _ = await _end(p)
        assert result is True
        added = {q.name for q in set(p.config.change_dir.rglob("*")) - before}
        assert not [name for name in added if LOCK.search(name)]
        saved = json.loads(p.config.state_file_path().read_text())["tdd"]
        assert not [key for key in saved if LOCK.search(key)]

    @pytest.mark.asyncio
    async def test_S16_refactor_committed_then_stopped_before_done(self, make):
        def run(p, **extra):
            return _agents(p, refactor_writes={"broken.flag": ""}, **extra)

        unstopped = make()
        stack, _ = run(unstopped)
        with stack:
            assert await _engine(unstopped) == EXIT_SUCCESS
        assert "refactor: simplify across blocks" not in _outcome(unstopped)["subjects"]

        p = make()
        real = ProgressWriter.log_phase

        def stop_after_commit(self, phase, details=""):
            if phase == "REFACTOR_ALL" and details == "COMMITTED":
                raise Killed()
            return real(self, phase, details)

        stack, _ = run(p)
        with (
            stack,
            patch.object(ProgressWriter, "log_phase", stop_after_commit),
            pytest.raises(Killed),
        ):
            await _engine(p)
        saved = BuildState.load(p.config.state_file_path()).tdd
        assert saved.refactor_all_commit == _git(p.dir, "rev-parse", "HEAD")
        assert saved.refactor_all_done is False

        stack, mocks = run(p)
        with stack:
            assert await _engine(p) == EXIT_SUCCESS
        mocks.fixer.assert_not_awaited()  # discarding the refactor made it green
        assert _outcome(p) == _outcome(unstopped)

    @pytest.mark.asyncio
    async def test_S17_standalone_final_check_reuses_the_end_run(self, make):
        p = _end_ready(make(), red=False)
        result, _ = await _end(p, standalone=True)
        assert result is True
        saved = BuildState.load(p.config.state_file_path())
        assert saved.verification["passed"] is True

        rec = Recorder()
        stack, _ = _agents(p, recorder=rec)
        with stack:
            assert await _engine(p, standalone=True) == EXIT_SUCCESS
        assert rec.calls == []  # end run done, inputs unchanged: no final run

    @pytest.mark.asyncio
    async def test_S17_standalone_final_check_runs_when_the_tree_moved(self, make):
        p = _end_ready(make(), red=False)
        assert (await _end(p, standalone=True))[0] is True
        (p.dir / "later.py").write_text("y = 1\n")
        _git(p.dir, "add", "-A")
        _git(p.dir, "commit", "-qm", "later")
        rec = Recorder()
        stack, _ = _agents(p, recorder=rec)
        with stack:
            assert await _engine(p, standalone=True) == EXIT_SUCCESS
        assert rec.calls == [("final", WHOLE)]

    @pytest.mark.asyncio
    async def test_S18_an_old_checkpoint_loads_and_per_block_runs_no_end_run(
        self, make
    ):
        p = make(whole_suite="per_block")
        _end_ready(p)
        raw = json.loads(p.config.state_file_path().read_text())
        for key in (
            "whole_suite_deferred",
            "end_suite_done",
            "end_suite",
            "refactor_all_commit",
        ):
            raw["tdd"].pop(key)
        p.config.state_file_path().write_text(json.dumps(raw))
        loaded = BuildState.load(p.config.state_file_path()).tdd
        assert loaded.whole_suite_deferred is False
        assert loaded.end_suite_done is False
        assert loaded.end_suite is None and loaded.refactor_all_commit is None

        rec = Recorder()
        state = BuildState.load(p.config.state_file_path())
        stack, _ = _agents(p, recorder=rec)
        with stack:
            assert await _run_end_suite(p.config, state, p.progress, False) is True
        assert rec.calls == []

        p.config.whole_suite = "end"
        result, _ = await _end(p)
        assert result is True  # under end, the end run runs on an old checkpoint


def test_EXIT_BUILD_FAILURE_when_the_end_run_fails(make):
    """A red end run that cannot be fixed fails the engine."""
    import asyncio

    p = make()
    stack, _ = _agents(p, block_writes={"broken.flag": ""}, end_fix=False)
    with stack:
        assert asyncio.run(_engine(p)) == EXIT_BUILD_FAILURE
