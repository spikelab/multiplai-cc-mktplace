"""Item 32 of plan-factory-merged-2026-10-29.md: every test-command run logs
its start and end, and only a run with no test results from a known runner is
run again. The resume rows S29 and S30 are in test_epic_unattended_resume.py."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import pytest
from build_pipeline import gates
from build_pipeline.gates import run_test_suite, runner_failure

# The line evals/epic_metrics.py (branch feat/epic-metrics) reads the test
# minutes from: `TEST_RUN_RE` there, on lines carrying the child's change tag.
METRICS_TEST_RUN_RE = re.compile(r"TEST_RUN end\b.*?\bseconds=([0-9]+(?:\.[0-9]+)?)")
METRICS_CHANGE_TAG_RE = re.compile(r"\[change:([^\]]+)\]")


@pytest.fixture
def trusted(monkeypatch):
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")


@pytest.fixture
def waits(monkeypatch):
    seen: list[float] = []
    monkeypatch.setattr(gates, "_sleep", seen.append)
    return seen


def _script(directory: Path, name: str, body: str) -> str:
    path = directory / name
    path.write_text(f"#!{sys.executable}\nimport sys\n{body}")
    path.chmod(0o755)
    return f"./{name}"


def test_each_run_logs_one_test_run_end_line_the_metrics_script_reads(
    tmp_path, trusted
):
    """`TEST_RUN end dir=<dir> exit=<code> seconds=<float>`, tagged
    `[change:<change>/<child>]` in the build-pipeline log."""
    from build_pipeline.__main__ import tag_log_records

    logger = logging.getLogger("build_pipeline")
    path = tmp_path / "build-pipeline.log"
    handler = logging.FileHandler(path)
    handler.setFormatter(logging.Formatter("[t] [x] %(levelname)s: %(message)s"))
    earlier = list(logger.handlers)
    for h in earlier:
        logger.removeHandler(h)
    logger.addHandler(handler)
    old = logger.level
    logger.setLevel(logging.INFO)
    try:
        tag_log_records("account-management/account-model")
        assert run_test_suite("true", tmp_path)[0] == 0
    finally:
        logger.removeHandler(handler)
        for h in earlier:
            logger.addHandler(h)
        logger.setLevel(old)
        handler.close()

    ends = [line for line in path.read_text().splitlines() if "TEST_RUN end" in line]
    assert len(ends) == 1
    (line,) = ends
    assert re.fullmatch(
        r"\[t\] \[x\] INFO: \[change:account-management/account-model\] "
        r"TEST_RUN end dir=\. exit=0 seconds=\d+\.\d{3}",
        line,
    ), line
    assert METRICS_CHANGE_TAG_RE.search(line).group(1) == (
        "account-management/account-model"
    )
    assert float(METRICS_TEST_RUN_RE.search(line).group(1)) >= 0


def test_an_assertion_failure_is_never_run_again(tmp_path, trusted, waits):
    command = _script(
        tmp_path,
        "pytest",
        'open("runs.txt", "a").write("run\\n")\n'
        'print("FAILED tests/test_a.py::test_a - assert 1 == 2")\n'
        'print("1 failed in 0.01s")\n'
        "sys.exit(1)\n",
    )
    code, output = run_test_suite(command, tmp_path)
    assert code == 1 and "1 failed" in output
    assert (tmp_path / "runs.txt").read_text() == "run\n"
    assert waits == []


def test_a_runner_failure_that_clears_on_retry_returns_the_retry(
    tmp_path, trusted, waits
):
    command = _script(
        tmp_path,
        "pytest",
        'import pathlib\nruns = pathlib.Path("runs.txt")\n'
        'runs.write_text(runs.read_text() + "run\\n" if runs.exists() else "run\\n")\n'
        "if len(runs.read_text().splitlines()) < 2:\n"
        '    print("INTERNALERROR> connection refused"); sys.exit(3)\n'
        'print("3 passed in 0.02s")\n',
    )
    assert run_test_suite(command, tmp_path) == (0, "3 passed in 0.02s\n")
    assert waits == [gates.RUNNER_RETRY_WAITS[0]]


def test_an_unknown_command_with_no_output_is_not_run_again(tmp_path, trusted, waits):
    """A command whose runner says nothing about tests ran (here a bare
    `python -c`) keeps today's meaning: a failed run, judged by the gates."""
    code, _ = run_test_suite(f'{sys.executable} -c "import sys; sys.exit(1)"', tmp_path)
    assert code == 1
    assert waits == []


@pytest.mark.parametrize(
    ("argv", "code", "output", "expected"),
    [
        # pytest: exit 3 (internal error) or a signal with no results and no
        # collection error; 1, 2, 4 and 5 say it got going, even with the
        # terminal reporter off.
        (["pytest"], 4, "ImportError while loading conftest", False),
        (["pytest"], 3, "ImportError while loading conftest", False),
        (["pytest"], 4, "", False),
        (["pytest"], 3, "INTERNALERROR> OperationalError", True),
        (["pytest"], -9, "", True),
        (["pytest", "-p", "no:terminal"], 1, "", False),
        (["pytest"], 2, "", False),
        (["pytest"], 4, "ERROR: usage: pytest [options]", False),
        (["pytest"], 1, "=== 1 failed, 2 passed in 0.12s ===", False),
        (["pytest"], 2, "1 error in 0.30s", False),
        (["python", "manage.py", "test"], 1, "OperationalError: no database", True),
        (["python", "manage.py", "test"], 1, "Ran 4 tests in 0.1s\n\nFAILED", False),
        (["python", "-m", "unittest"], 1, "ImportError: no module", False),
        (["python", "-m", "unittest"], 1, "Segmentation fault", True),
        (["npx", "jest"], 1, "Tests:       1 failed, 1 total", False),
        (["npx", "jest"], 1, "Cannot find module 'x'", False),
        (["npx", "jest"], 137, "", True),
        (["make", "test"], 1, "", False),
    ],
)
def test_runner_failure_needs_a_known_runner_and_no_results(
    tmp_path, argv, code, output, expected
):
    assert runner_failure(code, output, argv, " ".join(argv), tmp_path) is expected


def test_a_run_that_passed_or_could_not_run_is_never_a_runner_failure(tmp_path):
    assert not runner_failure(0, "", ["pytest"], "pytest", tmp_path)
    assert not runner_failure(None, "", ["pytest"], "pytest", tmp_path)


# A fake `pytest`: while `broken.import` exists it fails the way pytest does
# when a conftest cannot import the implementer's code (exit 4, a collection
# error); otherwise one test passes. Every run appends a line to `runs.txt`.
BROKEN_IMPORT_PYTEST = f"""#!{sys.executable}
import pathlib, sys
here = pathlib.Path.cwd()
with (here / "runs.txt").open("a") as f:
    f.write("run\\n")
if (here / "broken.import").exists():
    print("ImportError while loading conftest '/x/tests/conftest.py'.")
    print("tests/conftest.py:1: in <module>")
    print("    from app import thing")
    print("E   ImportError: cannot import name 'thing' from 'app' (/x/app.py)")
    sys.exit(4)
print("1 passed in 0.01s")
"""


@pytest.mark.asyncio
async def test_a_broken_import_goes_to_the_fix_agent_on_the_first_run(
    tmp_path, monkeypatch, waits
):
    """pytest exit 4 with a collection error is the implementer's code, not
    the runner: the integration gate sends it to the fix agent at once, with
    no retry and no stop."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from build_pipeline import tdd_engine
    from build_pipeline.models import AgentResult

    from tests.test_whole_suite_end import _make

    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    p = _make(tmp_path / "run", whole_suite="per_block")
    script = p.dir / "pytest"
    script.write_text(BROKEN_IMPORT_PYTEST)
    script.chmod(0o755)
    p.config.test_command = "./pytest"
    (p.dir / "broken.import").touch()

    async def block_tdd(block, config, state, progress):
        return True

    def fix(**kwargs):
        (p.dir / "broken.import").unlink()
        return AgentResult(success=True, output="fixed the import")

    fixer = AsyncMock(side_effect=fix)
    reached = AsyncMock(side_effect=RuntimeError("review reached"))
    with (
        patch("build_pipeline.tdd_engine.run_block_tdd", side_effect=block_tdd),
        patch("build_pipeline.tdd_engine.run_integration_fix", fixer),
        patch("build_pipeline.tdd_engine._run_quality_review", reached),
    ):
        await tdd_engine.run_tdd_engine(
            p.config, SimpleNamespace(block=None), standalone=False
        )

    fixer.assert_called_once()
    assert "ImportError" in fixer.call_args.kwargs["failure_output"]
    assert waits == []
    assert (p.dir / "runs.txt").read_text().splitlines() == ["run", "run"]
    # It went on to the review (whose stand-in raises, which is what ends
    # this run), not to the runner-failure stop.
    reached.assert_called()
    assert "test runner failed" not in p.config.progress_file_path().read_text()
    runs = p.config.change_dir / ".buildme-test-runs"
    assert not list(runs.glob("*.out"))
