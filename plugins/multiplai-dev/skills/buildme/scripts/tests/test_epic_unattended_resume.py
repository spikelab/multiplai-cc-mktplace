"""Stop and resume rows of plan-factory-merged-2026-10-29.md that the
unattended epic run depends on. Each `test_s<n>_...` is row S<n>: it stops the
step at the row's point, resumes, and compares the result with a run that
never stopped.

S29 and S30 (item 32): the test command runs in a process group of its own,
streams its output to a file, and tells a runner that failed with no test
results from a test that failed.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from build_pipeline import gates, tdd_engine
from build_pipeline.gates import (
    RUNNER_ATTEMPTS,
    RUNNER_RETRY_WAITS,
    RunnerFailed,
    integration_gate,
    run_test_suite,
)
from build_pipeline.models import BlockStatus

from tests.test_whole_suite_end import _make, _runs

# A fake `pytest`: while `db.down` exists it fails the way a runner does when
# it cannot start (no test results, exit 4); otherwise it reports one passed
# test. Every run appends a line to `runs.txt`.
FAKE_PYTEST = f"""#!{sys.executable}
import pathlib, sys
here = pathlib.Path.cwd()
with (here / "runs.txt").open("a") as f:
    f.write("run\\n")
if (here / "db.down").exists():
    print("ImportError while loading conftest '/x/conftest.py'.")
    print("django.db.utils.OperationalError: could not connect to server")
    sys.exit(4)
print("1 passed in 0.01s")
"""

# Writes a grandchild's pid, prints a line, then sleeps past any timeout.
SLOW = """import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
pathlib.Path("child.pid").write_text(str(child.pid))
print("partial line", flush=True)
time.sleep(120)
"""


@pytest.fixture
def trusted(monkeypatch):
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")


@pytest.fixture
def waits(monkeypatch):
    seen: list[float] = []
    monkeypatch.setattr(gates, "_sleep", seen.append)
    return seen


def _fake_pytest(directory: Path) -> str:
    script = directory / "pytest"
    script.write_text(FAKE_PYTEST)
    script.chmod(0o755)
    return "./pytest"


def _runs_made(directory: Path) -> int:
    path = directory / "runs.txt"
    return len(path.read_text().splitlines()) if path.exists() else 0


def _gone(pid: int) -> bool:
    """True when *pid* no longer runs (gone, or a zombie no one reaped yet)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
    except OSError:
        return False
    return state == "Z"


def _wait_gone(pid: int) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        time.sleep(0.05)
    return False


def test_s29_a_timed_out_run_is_killed_with_its_group_and_its_output_kept(
    tmp_path, trusted
):
    (tmp_path / "slow.py").write_text(SLOW)
    output = tmp_path / "runs" / "stamp-block-1-integration.log"

    code, text = run_test_suite(f"{sys.executable} slow.py", tmp_path, 2, output)

    assert code is None
    assert "timed out after 2 seconds" in text
    partial = output.with_name("stamp-block-1-integration-run1.out")
    assert "partial line" in partial.read_text()
    assert str(partial) in text
    # The old group is gone: the grandchild was killed with it.
    assert _wait_gone(int((tmp_path / "child.pid").read_text()))

    # Resume: the test command runs again, and the old partial output stays
    # beside the new run's.
    (tmp_path / "slow.py").write_text("print('1 passed in 0.01s')\n")
    again = tmp_path / "runs" / "stamp2-block-1-integration.log"
    assert run_test_suite(f"{sys.executable} slow.py", tmp_path, 30, again) == (
        0,
        "1 passed in 0.01s\n",
    )
    assert "partial line" in partial.read_text()
    assert not again.with_name("stamp2-block-1-integration-run1.out").exists()


def test_s29_a_build_stopped_mid_run_kills_the_group_and_keeps_the_output(
    tmp_path, trusted
):
    """A build that gets SIGTERM while its tests run (an epic's coordinator
    stopping its children): the build runs in its own process here, as it
    does in an epic, so its signal handlers are its main thread's."""
    (tmp_path / "slow.py").write_text(SLOW)
    output = tmp_path / "runs" / "stamp-block-1-red.log"
    build = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from pathlib import Path\n"
                "from build_pipeline.gates import run_test_suite\n"
                "run_test_suite(sys.argv[1], Path(sys.argv[2]), 60, Path(sys.argv[3]))\n"
            ),
            f"{sys.executable} slow.py",
            str(tmp_path),
            str(output),
        ],
        cwd=Path(__file__).resolve().parents[1],
    )
    deadline = time.monotonic() + 30
    while not (tmp_path / "child.pid").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    build.send_signal(signal.SIGTERM)
    assert build.wait(timeout=30) == 128 + signal.SIGTERM

    partial = output.with_name("stamp-block-1-red-run1.out")
    assert "partial line" in partial.read_text()
    assert _wait_gone(int((tmp_path / "child.pid").read_text()))

    (tmp_path / "slow.py").write_text("print('1 passed in 0.01s')\n")
    again = tmp_path / "runs" / "stamp2-block-1-red.log"
    assert run_test_suite(f"{sys.executable} slow.py", tmp_path, 30, again)[0] == 0
    assert "partial line" in partial.read_text()


def test_s30_the_same_runner_failure_three_times_stops_with_three_outputs(
    tmp_path, trusted, waits
):
    command = _fake_pytest(tmp_path)
    (tmp_path / "db.down").touch()
    failure_log = tmp_path / "runs" / "stamp-block-1-integration.log"

    with pytest.raises(RunnerFailed) as failed:
        integration_gate(command, tmp_path, timeout=30, failure_log=failure_log)

    assert _runs_made(tmp_path) == RUNNER_ATTEMPTS == 3
    assert waits == list(RUNNER_RETRY_WAITS)
    outputs = [Path(p) for p in failed.value.outputs]
    assert [p.name for p in outputs] == [
        f"stamp-block-1-integration-run{k}.out" for k in (1, 2, 3)
    ]
    assert all("OperationalError" in p.read_text() for p in outputs)
    assert all(str(p) in str(failed.value) for p in outputs)

    # Resume: the test command runs once more, and passes now the DB is up.
    (tmp_path / "db.down").unlink()
    gate = integration_gate(command, tmp_path, timeout=30, failure_log=failure_log)
    assert gate.passed
    assert _runs_made(tmp_path) == 4
    assert all(p.exists() for p in outputs)


@pytest.mark.asyncio
async def test_s30_the_block_stops_resumable_and_no_fix_agent_runs(
    tmp_path, monkeypatch, waits
):
    """The engine's side of S30: the runner failure stops the block (FAILED,
    resumable, its outputs named in the diagnosis), spends no fix agent, and
    the resumed block runs the test command again and finishes."""
    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    p = _make(tmp_path / "run", whole_suite="per_block")
    p.config.test_command = _fake_pytest(p.dir)
    (p.dir / "db.down").touch()

    async def block_tdd(block, config, state, progress):
        return True

    fixer = AsyncMock()
    with (
        patch("build_pipeline.tdd_engine.run_block_tdd", side_effect=block_tdd),
        patch("build_pipeline.tdd_engine.run_integration_fix", fixer),
        patch(
            "build_pipeline.tdd_engine._run_quality_review",
            new_callable=AsyncMock,
            side_effect=Exception("the review is not reached"),
        ),
    ):
        code = await tdd_engine.run_tdd_engine(
            p.config, SimpleNamespace(block=None), standalone=False
        )

    assert code == tdd_engine.EXIT_BUILD_FAILURE
    fixer.assert_not_called()
    saved = tdd_engine.BuildState.load(p.config.state_file_path())
    block = saved.tdd.blocks[0]
    assert block.status == BlockStatus.FAILED and block.resumable
    outs = [r for r in _runs(p) if r.suffix == ".out"]
    assert len(outs) == 3
    progress = p.config.progress_file_path().read_text()
    assert all(str(r) in progress for r in outs)

    # Resume with the DB up: the integration gate runs the test command once
    # more, passes, and the block goes on to its review.
    (p.dir / "db.down").unlink()
    before = _runs_made(p.dir)
    reached = AsyncMock(side_effect=RuntimeError("review reached"))
    with (
        patch("build_pipeline.tdd_engine.run_block_tdd", side_effect=block_tdd),
        patch("build_pipeline.tdd_engine.run_integration_fix", fixer),
        patch("build_pipeline.tdd_engine._run_quality_review", reached),
    ):
        await tdd_engine.run_tdd_engine(
            p.config, SimpleNamespace(block=None), standalone=False
        )
    assert _runs_made(p.dir) == before + 1
    reached.assert_called()
    fixer.assert_not_called()
