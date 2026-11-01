"""Quality gates — pure code assertions between pipeline stages.

Each gate returns a GateResult. Failed gates include an action hint
that the pipeline uses to decide recovery strategy.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import NamedTuple

from .models import GateResult, ImplementationNote, ReviewGatePolicy, ReviewResult
from .narrow_tests import (
    NODE,
    PYTEST,
    _pytest_config,
    _runner_of_tokens,
    _UnreadableConfig,
    _words,
    runner_of,
)

log = logging.getLogger(__name__)


def _repo_trusted() -> bool:
    """Mirror of sdk._repo_is_trusted (kept dependency-free here). Gates that run
    the repo's own test_command / conftest.py must not execute against a repo the
    user has not vouched for — the test command is arbitrary argv and pytest runs
    any conftest.py in the tree at collection time (CWE-94)."""
    return os.environ.get("BUILDME_TRUST_REPO", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def wiring_task_gate(tasks_path: Path, project_dir: Path) -> GateResult:
    """Check that tasks.md has a wiring task if the project is an app."""
    app_markers = [
        project_dir / ".xcodeproj",
        project_dir / "src" / "__main__.py",
        *project_dir.glob("**/__main__.py"),
    ]
    is_app = False
    for marker in app_markers:
        if isinstance(marker, Path) and marker.exists():
            is_app = True
            break
    # Check package.json for entry points
    pkg_json = project_dir / "package.json"
    if pkg_json.exists():
        import json

        data = json.loads(pkg_json.read_text())
        if "main" in data or "bin" in data or "scripts" in data.get("scripts", {}):
            is_app = True

    if not is_app:
        return GateResult(passed=True, reason="Not detected as app project")

    tasks_text = tasks_path.read_text() if tasks_path.exists() else ""
    wiring_patterns = re.compile(
        r"(wir(?:e|ing)|entry.?point|connect.*into|startup.?sequence|runnable)",
        re.IGNORECASE,
    )
    if wiring_patterns.search(tasks_text):
        return GateResult(passed=True, reason="Wiring task found in tasks.md")

    return GateResult(
        passed=False,
        reason="No entry-point wiring task found. TDD agents will build "
        "mocked units but nothing assembles them into a working app.",
        action="add_wiring_task",
    )


def _test_invocation(test_command: str, project_dir: Path) -> tuple[list[str], Path]:
    """Resolve literal leading `cd DIR &&` clauses without running shell code."""
    directory = project_dir
    # Tokenize only the leading cd clause as shell syntax. The remaining argv
    # retains literal punctuation, matching the historical shlex.split contract.
    clause = re.compile(
        r"""^\s*cd\s+((?:\\.|"(?:\\.|[^"\\])*"|'[^']*'|[^\s&"'\\])+)\s*&&\s*"""
    )
    while match := clause.match(test_command):
        (target,) = shlex.split(match.group(1), comments=False)
        if any(char in target for char in "$`~"):
            raise ValueError("Test command cd must name a literal directory")
        directory = (directory / target).resolve()
        test_command = test_command[match.end() :]
    return shlex.split(test_command, comments=False), directory


def _test_directory(test_command: str, project_dir: Path) -> Path:
    argv, directory = _test_invocation(test_command, project_dir)
    if (
        len(argv) == 3
        and Path(argv[0]).name in {"sh", "bash", "zsh"}
        and argv[1] == "-c"
    ):
        return _test_directory(argv[2], directory)
    return directory


# Seconds a full test run gets when `tdd.test_timeout` sets nothing.
DEFAULT_TEST_TIMEOUT = 300

# Runs of the test command that may end with no test results (the runner
# crashed, the test database is down, the project failed to start) before the
# block stops: the first run and two retries.
RUNNER_ATTEMPTS = 3
# Seconds waited before the second and the third of those runs.
RUNNER_RETRY_WAITS = (10, 30)
# The wait between runner failures; a seam for tests.
_sleep = time.sleep

# Lines a runner prints only when tests ran: pytest's summary (`=== 1 failed,
# 2 passed in 0.12s ===`, or `-q`'s bare `1 failed, 2 passed in 0.12s`, or
# `no tests ran`), Jest's `Tests:` line, Vitest's indented `Tests  1 failed
# (1)` line, unittest's and Django's `Ran N tests`, and pytest's `FAILED <id>` /
# `ERROR <id>` lines.
_TEST_RESULTS_RE = re.compile(
    r"^=+ .*\b(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|"
    r"no tests ran)\b.* in [\d.]+s\b|"
    r"^\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b.*"
    r" in [\d.]+s\b|"
    r"^no tests ran\b|"
    r"^Tests:\s+\d|"
    r"^[ \t]*Tests[ \t]+\d+ (?:passed|failed|skipped|todo)\b|"
    r"^Ran \d+ tests? in |"
    r"^(?:FAILED|ERROR)\s+\S+::",
    re.MULTILINE,
)


class RunnerFailed(Exception):
    """The test command ended `RUNNER_ATTEMPTS` times in a row with no test
    results from a known runner (`runner_failure`). *outputs* are the saved
    output files of those runs (none when the caller gave no output path)."""

    def __init__(self, command: str, outputs: list[str], tail: str):
        self.command = command
        self.outputs = outputs
        self.tail = tail
        where = (
            "; outputs: " + ", ".join(outputs) if outputs else f"; last output:\n{tail}"
        )
        super().__init__(
            f"the test runner failed {RUNNER_ATTEMPTS} times with no test "
            f"results ({command}){where}"
        )


def _runner_kind(argv: list[str], test_command: str, project_dir: Path) -> str | None:
    """`"pytest"`, `"other"` (jest or vitest, Django's `manage.py test`,
    `python -m unittest`: runners whose output says when tests ran), or None
    for a command that is none of these."""
    while (
        len(argv) == 3
        and Path(argv[0]).name in {"sh", "bash", "zsh"}
        and argv[1] == "-c"
    ):
        try:
            argv, _ = _test_invocation(argv[2], project_dir)
        except ValueError:
            return None
    if (
        _runner_of_tokens(argv) == PYTEST
        or runner_of(test_command, project_dir) == PYTEST
    ):
        return PYTEST
    if _runner_of_tokens(argv) is not None or runner_of(test_command, project_dir):
        return "other"
    names = [Path(t).name for t in argv]
    if "manage.py" in names and "test" in names:
        return "other"
    if any(a == "-m" and b == "unittest" for a, b in pairwise(argv)):
        return "other"
    return None


# pytest's exit codes that say it got going: 1 tests failed, 2 interrupted
# (a collection error), 4 usage error (a conftest or test module that fails
# to import or collect), 5 none collected. 3 (internal error) and anything
# else, such as a signal, say it did not.
_PYTEST_GOT_GOING = (1, 2, 4, 5)
# A collection or import error in the output: the code or the tests are
# broken, usually by the implementer, which is a fix agent's job, never a
# retry's.
_COLLECTION_ERROR_RE = re.compile(
    r"ERROR collecting|errors? during collection|"
    r"ImportError while (?:loading conftest|importing test module)|"
    r"^E\s+(?:ModuleNotFoundError|ImportError|SyntaxError|IndentationError)\b|"
    r"^(?:ModuleNotFoundError|ImportError|SyntaxError|IndentationError):|"
    r"Test suite failed to run|Cannot find module|Failed to load url|"
    r"^ERROR: usage: |: error: unrecognized arguments",
    re.MULTILINE,
)


def runner_failure(
    exit_code: int | None,
    output: str,
    argv: list[str],
    test_command: str,
    project_dir: Path,
) -> bool:
    """True when a known runner never got going: the run failed with no test
    results it printed and no collection or import error in its output (it
    was killed, crashed, or printed nothing useful; a test database that is
    down). An assertion failure, a collection error, pytest's usage error and
    a pytest exit code that says it got going are test failures for the
    gates and the fix agent; a command buildme does not know says nothing
    either way, so it is never one."""
    if exit_code in (0, None):
        return False
    text = _ANSI_RE.sub("", output)
    if _TEST_RESULTS_RE.search(text) or _COLLECTION_ERROR_RE.search(text):
        return False
    kind = _runner_kind(argv, test_command, project_dir)
    if kind == PYTEST:
        return exit_code not in _PYTEST_GOT_GOING
    return kind is not None


def _relative_dir(directory: Path, project_dir: Path) -> str:
    try:
        return directory.resolve().relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        return str(directory)


def _run_once(
    argv: list[str],
    directory: Path,
    project_dir: Path,
    timeout: int,
    out_path: Path,
) -> tuple[int | None, str, bool]:
    """Run *argv* once in a process group of its own, its stdout and stderr
    streamed to *out_path*. Returns (exit code, output, timed out). On a
    timeout, and on anything that ends the wait (Ctrl-C, SIGTERM or SIGHUP
    through `git_ops._exit_on_termination`), the whole group is killed and
    awaited; after a normal exit, any process the run left in its group is
    killed. Logs `TEST_RUN start` and `TEST_RUN end ... seconds=<s>`.
    Raises FileNotFoundError when the program does not exist."""
    from . import git_ops

    where = _relative_dir(directory, project_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    with git_ops._exit_on_termination(), out_path.open("wb") as sink:
        started = time.monotonic()
        proc = subprocess.Popen(
            argv,
            cwd=directory,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log.info("TEST_RUN start dir=%s output=%s", where, out_path)
        try:
            exit_code: int | None = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out, exit_code = True, None
        except BaseException:
            git_ops._kill_group(proc)
            log.info(
                "TEST_RUN end dir=%s exit=stopped seconds=%.3f",
                where,
                time.monotonic() - started,
            )
            raise
        git_ops._kill_group(proc)
        log.info(
            "TEST_RUN end dir=%s exit=%s seconds=%.3f",
            where,
            "timeout" if timed_out else exit_code,
            time.monotonic() - started,
        )
    output = out_path.read_text(encoding="utf-8", errors="replace")
    return exit_code, output, timed_out


def _free_path(output: Path, attempt: int) -> Path:
    """`<output stem>-run<attempt>.out`, or with `-<n>` added when a file of
    that name exists: a run never overwrites an earlier run's output."""
    path = output.with_name(f"{output.stem}-run{attempt}.out")
    n = 1
    while path.exists():
        n += 1
        path = output.with_name(f"{output.stem}-run{attempt}-{n}.out")
    return path


def run_test_suite(
    test_command: str,
    project_dir: Path,
    timeout: int = DEFAULT_TEST_TIMEOUT,
    output: Path | None = None,
) -> tuple[int | None, str]:
    """Run the repo's test suite under the trust guard (shared mechanics for
    the integration and RED gates).

    Returns (exit_code, combined output). exit_code is None when the command
    could not run at all (missing binary, timeout, untrusted repo) — callers
    must treat None as "could not verify", never as pass or fail evidence.

    Each run streams its output to a file (`_run_once`): with *output* set,
    `<output stem>-run<k>.out` beside it, else a temporary file. The file is
    removed once the run printed test results, and kept when it timed out,
    failed with no results, or was stopped part way, so a later run never
    overwrites it. A run that fails with no test results from a known runner
    (`runner_failure`) runs again after `RUNNER_RETRY_WAITS`; the third such
    run raises `RunnerFailed` with the kept files. An assertion failure is
    never run again.
    """
    if not _repo_trusted():
        return None, (
            "Repo not trusted — refusing to run its test_command/conftest.py "
            "(set --trust-repo or BUILDME_TRUST_REPO=1)."
        )
    try:
        argv, directory = _test_invocation(test_command, project_dir)
    except ValueError as e:
        return None, f"Test command failed to run: {e}"
    kept: list[str] = []
    for attempt in range(1, RUNNER_ATTEMPTS + 1):
        if output is not None:
            out_path = _free_path(output, attempt)
            temporary = False
        else:
            fd, name = tempfile.mkstemp(prefix="buildme-test-run-", suffix=".out")
            os.close(fd)
            out_path, temporary = Path(name), True
        try:
            exit_code, text, timed_out = _run_once(
                argv, directory, project_dir, timeout, out_path
            )
        except FileNotFoundError as e:
            out_path.unlink(missing_ok=True)
            return None, f"Test command failed to run: {e}"
        except BaseException:
            if temporary:
                out_path.unlink(missing_ok=True)
            raise
        if timed_out:
            if temporary:
                out_path.unlink(missing_ok=True)
                saved = ""
            else:
                saved = f" (partial output: {out_path})"
            return None, (
                f"Test command failed to run: timed out after {timeout} seconds"
                f"{saved}\n" + "\n".join(text.splitlines()[-40:])
            )
        if not runner_failure(exit_code, text, argv, test_command, project_dir):
            out_path.unlink(missing_ok=True)
            return exit_code, text
        if temporary:
            out_path.unlink(missing_ok=True)
        else:
            kept.append(str(out_path))
        log.warning(
            "TEST_RUN runner failure %d/%d (exit %s, no test results)%s",
            attempt,
            RUNNER_ATTEMPTS,
            exit_code,
            f": {out_path}" if not temporary else "",
        )
        if attempt < RUNNER_ATTEMPTS:
            _sleep(RUNNER_RETRY_WAITS[attempt - 1])
    raise RunnerFailed(test_command, kept, "\n".join(text.splitlines()[-40:]))


# Wrappers that run their command in the project's own environment, where
# `<wrapper> python` is the interpreter that runs `<wrapper> pytest`.
_ENV_RUNNERS = (
    ("uv", "run"),
    ("poetry", "run"),
    ("pdm", "run"),
    ("hatch", "run"),
    ("pipenv", "run"),
    ("rye", "run"),
)
XDIST_PROBE = ("-c", "import xdist")
XDIST_PROBE_TIMEOUT = 120


def _pytest_at(tokens: list[str]) -> int:
    return next(
        i for i, t in enumerate(tokens) if Path(t).name in ("pytest", "py.test")
    )


def _xdist_probe(tokens: list[str], project_dir: Path) -> list[str] | None:
    """The argv that imports xdist in the environment the pytest command
    *tokens* runs in, or None when this cannot tell which one that is."""
    at = _pytest_at(tokens)
    if at >= 2 and tokens[at - 1] == "-m":  # `<python> -m pytest`
        return [*tokens[: at - 1], *XDIST_PROBE]
    prefix = tokens[:at]
    if any(tuple(prefix[: len(r)]) == r for r in _ENV_RUNNERS):
        return [*prefix, "python", *XDIST_PROBE]
    if prefix:  # `env X=1 pytest`, an unknown wrapper
        return None
    script = tokens[at]
    found = str(project_dir / script) if "/" in script else shutil.which(script)
    python = Path(found).parent / "python" if found else None
    return (
        [str(python), *XDIST_PROBE] if python is not None and python.is_file() else None
    )


def _chose_workers(args: list[str]) -> bool:
    """True when the pytest arguments already set xdist's workers, turn the
    plugin off, end the options (`--`), or ask for a debugger xdist refuses."""
    for i, token in enumerate(args):
        if token == "--" or token in ("--pdb", "--trace", "-d", "--dist", "--tx"):
            return True
        if token.startswith(("-n", "--numprocesses", "--dist=", "--tx=")):
            return True
        if token in ("-p", "-pno:xdist") and (
            token == "-pno:xdist" or args[i + 1 : i + 2] == ["no:xdist"]
        ):
            return True
    return False


def _xdist_loads(args: list[str]) -> bool:
    """False when pytest will not load xdist, and so rejects `-n`: plugin
    autoloading is off (`PYTEST_DISABLE_PLUGIN_AUTOLOAD` set to any value, or
    `--disable-plugin-autoload`) and neither `-p xdist` / `-p xdist.plugin`
    nor `PYTEST_PLUGINS=xdist.plugin` names it."""
    if (
        not os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD")
        and "--disable-plugin-autoload" not in args
    ):
        return True
    if "xdist.plugin" in (
        p.strip() for p in os.environ.get("PYTEST_PLUGINS", "").split(",")
    ):
        return True
    named = [
        args[i + 1] if t == "-p" and i + 1 < len(args) else t[2:]
        for i, t in enumerate(args)
        if t.startswith("-p")
    ]
    return any(n in ("xdist", "xdist.plugin") for n in named)


def parallel_command(
    test_command: str, project_dir: Path, parallel: str = "off"
) -> str:
    """*test_command* with `-n auto` appended when *parallel* is "auto", the
    command runs pytest (`narrow_tests.runner_of`), nothing in it, its
    `addopts` or `PYTEST_ADDOPTS` chose the workers already, pytest loads
    xdist (`_xdist_loads`), and `python -c "import xdist"` succeeds in the
    environment pytest runs in. Otherwise *test_command* unchanged. Runs the
    project's interpreter, so callers check the repo is trusted first."""
    if parallel != "auto" or not test_command:
        return test_command
    project_dir = Path(project_dir)
    if runner_of(test_command, project_dir) != PYTEST:
        return test_command
    try:
        tokens = shlex.split(test_command)
        found = _pytest_config(project_dir)
        addopts = _words(found[1].get("addopts"), shell=True) if found else []
        args = (
            addopts
            + shlex.split(os.environ.get("PYTEST_ADDOPTS", ""))
            + tokens[_pytest_at(tokens) + 1 :]
        )
    except (_UnreadableConfig, ValueError):
        return test_command
    if _chose_workers(args) or not _xdist_loads(args):
        return test_command
    probe = _xdist_probe(tokens, project_dir)
    if probe is None:
        log.info(
            "Parallel tests: cannot tell which Python runs %r — running it as written",
            test_command,
        )
        return test_command
    try:
        proc = subprocess.run(
            probe, cwd=project_dir, capture_output=True, timeout=XDIST_PROBE_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired):
        return test_command
    if proc.returncode != 0:
        return test_command
    log.info(
        "Parallel tests: pytest-xdist is installed — running the full suite with -n auto"
    )
    return f"{test_command} -n auto"


def _full_suite(test_command: str, project_dir: Path, parallel: str) -> str:
    return (
        parallel_command(test_command, project_dir, parallel)
        if _repo_trusted()
        else test_command
    )


# Lines of a failed run's output its failure file keeps (`write_failure_log`).
FAILURE_LOG_LINES = 400
# The change dir's folder of failure files: bookkeeping, never committed,
# cleaned or verified (`git_ops.BOOKKEEPING_EXCLUDES`).
TEST_RUNS_DIR = ".buildme-test-runs"


def failure_log_path(change_dir: Path, label: str) -> Path:
    """`<change_dir>/.buildme-test-runs/<UTC stamp>-<label>.log`."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return Path(change_dir) / TEST_RUNS_DIR / f"{stamp}-{label}.log"


_FAILED_TEST_RE = re.compile(
    r"^(?:FAILED|ERROR)\s+(.+?)(?:\s+-\s.*)?\s*$|^\s*FAIL\s+(\S+)", re.MULTILINE
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def failing_tests(output: str) -> list[str]:
    """The failing test ids a run's output names, in order and once each:
    pytest's `FAILED <nodeid>` / `ERROR <nodeid>` summary lines and Jest's
    `FAIL <path>` lines."""
    found: dict[str, None] = {}
    for match in _FAILED_TEST_RE.finditer(_ANSI_RE.sub("", output)):
        found[match.group(1) or match.group(2)] = None
    return list(found)


def write_failure_log(
    path: Path, command: str, exit_code: int | None, output: str
) -> str | None:
    """Write a failed run's command, its exit code (or, when it did not run,
    `run_test_suite`'s reason), the failing test ids and the output's last
    `FAILURE_LOG_LINES` lines to *path*. Returns the path, or None when the
    file could not be written (logged; the gate's verdict does not change)."""
    status = (
        f"exit code: {exit_code}"
        if exit_code is not None
        else f"did not run: {output.strip()}"
    )
    ids = failing_tests(output)
    lines = output.splitlines()[-FAILURE_LOG_LINES:]
    text = (
        f"command: {command}\n{status}\n\n"
        f"failing tests ({len(ids)}):\n"
        + "".join(f"  {test_id}\n" for test_id in ids)
        + f"\nlast {len(lines)} lines of output:\n"
        + "\n".join(lines)
        + "\n"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as error:
        log.warning(
            "Could not write the failed test run's output to %s: %s", path, error
        )
        return None
    return str(path)


def _attach_failure_log(
    gate: GateResult,
    failure_log: Path | None,
    command: str,
    exit_code: int | None,
    output: str,
) -> GateResult:
    if failure_log is not None and not gate.passed:
        written = write_failure_log(failure_log, command, exit_code, output)
        if written:
            gate.metadata["output_file"] = written
    return gate


def _suite_run(
    test_command: str,
    project_dir: Path,
    parallel: str,
    timeout: int,
    failure_log: Path | None = None,
) -> tuple[str, int | None, str]:
    """(the command run, its exit code, its output) for one whole-suite run.
    With *failure_log*, the run's output streams beside it (`run_test_suite`)."""
    command = _full_suite(test_command, project_dir, parallel)
    exit_code, output = run_test_suite(command, project_dir, timeout, failure_log)
    return command, exit_code, output


def baseline_test_gate(
    test_command: str,
    project_dir: Path,
    *,
    parallel: str = "off",
    timeout: int = DEFAULT_TEST_TIMEOUT,
    failure_log: Path | None = None,
) -> GateResult:
    """Run the test suite and check it passes before TDD starts. *parallel*
    is `tdd.parallel` (`parallel_command`), *timeout* `tdd.test_timeout`.
    A failed run with *failure_log* set writes its output there
    (`write_failure_log`) and names the file in `metadata["output_file"]`."""
    if not test_command:
        return GateResult(
            passed=True, reason="No test command configured — skipping baseline"
        )
    command, exit_code, output = _suite_run(
        test_command, project_dir, parallel, timeout, failure_log
    )
    return _attach_failure_log(
        _baseline_verdict(exit_code, output), failure_log, command, exit_code, output
    )


def _baseline_verdict(exit_code: int | None, output: str) -> GateResult:
    if exit_code is None:
        # Could not verify — untrusted repo, missing binary or timeout. The
        # message run_test_suite returns already names which.
        return GateResult(passed=False, reason=output, action="fix_tests")
    if exit_code == 0:
        return GateResult(
            passed=True,
            reason="Baseline tests pass",
            metadata={"output": output[-500:]},
        )
    return GateResult(
        passed=False,
        reason=f"Baseline tests failing (exit {exit_code})",
        action="fix_tests",
        metadata={"output": output[-1500:]},
    )


def integration_gate(
    test_command: str,
    project_dir: Path,
    *,
    parallel: str = "off",
    timeout: int = DEFAULT_TEST_TIMEOUT,
    failure_log: Path | None = None,
) -> GateResult:
    """Run the full test suite after a block completes. *parallel* is
    `tdd.parallel` (`parallel_command`), *timeout* `tdd.test_timeout`.
    A failed run with *failure_log* set writes its output there
    (`write_failure_log`) and names the file in `metadata["output_file"]`."""
    if not test_command:
        return GateResult(
            passed=True, reason="No test command — skipping integration gate"
        )
    command, exit_code, output = _suite_run(
        test_command, project_dir, parallel, timeout, failure_log
    )
    return _attach_failure_log(
        _integration_verdict(exit_code, output), failure_log, command, exit_code, output
    )


def _integration_verdict(exit_code: int | None, output: str) -> GateResult:
    if exit_code is None:
        # No action: an unverifiable suite is not something a fix agent can
        # repair (the pre-collapse gate returned no action here either).
        return GateResult(passed=False, reason=output)
    if exit_code == 0:
        return GateResult(
            passed=True,
            reason="All tests pass",
            metadata={"output": output[-2000:]},
        )
    return GateResult(
        passed=False,
        reason=f"Tests failing (exit {exit_code})",
        action="spawn_fix_agent",
        metadata={"output": output[-1500:]},
    )


# Failure signatures that prove tests fail "for the right reason": the code
# under test is missing or incomplete, not that the test files themselves are
# broken. Besides an assertion, pytest's own `Failed` counts: `pytest.fail()`,
# and `pytest.raises` / `pytest.warns` that saw nothing ("DID NOT RAISE"),
# as an `E   Failed:` line, the `path:line: Failed` line under a traceback,
# `--tb=line`'s `path:line: <message>`, or `-rf`'s `FAILED <id> - <message>`.
# A `pytest.fail()` in a fixture or at module level is a setup or collection
# error, which `_RED_BROKEN_TESTS` rejects first. NameError, TypeError and
# other errors raised by the test's own code are not here.
_RED_RIGHT_REASON = re.compile(
    r"AssertionError|NotImplementedError|AttributeError:.*has no attribute|"
    r"^E\s+(?:assert\b|Failed:)|"
    r"^\S+:\d+: (?:assert\b|Failed(?::|$))|"
    r"^FAILED\s+\S+\s+-\s+(?:assert\b|Failed:)",
    re.MULTILINE,
)

# Signatures of broken test files: pytest could not even collect/parse them.
_RED_BROKEN_TESTS = re.compile(
    r"^[ \t]*(?:E\s+|FAILED\s+.+?\s+-\s+)?"
    r"(?:SyntaxError|IndentationError|FileNotFoundError|ModuleNotFoundError|ImportError|PermissionError)(?::|$)|"
    r"^[_= ]*(?:ERROR collecting|ERROR at (?:setup|teardown)|INTERNALERROR)\b|"
    r"^(?:E\s+)?fixture .+ not found|"
    r"^.*\b\d+ errors? during collection\b|"
    r"^[ \t]*(?:Error: )?(?:Cannot find module|Cannot find package|command not found)\b|"
    r"^[ \t]*(?:●[ \t]*)?Test suite failed to run\b|"
    r"^[ \t]*Error: Failed to load url\b",
    re.IGNORECASE | re.MULTILINE,
)


class _RedFacts(NamedTuple):
    """What the RED gate observed about one run of the block's tests."""

    ran: bool  # the command ran (exit code not None)
    exit_code: int | None
    runner: str | None  # as given: `narrow_tests.runner_of`
    # "direct_pytest": the runner, or the executed argv after unwrapping
    # `cd DIR &&` and `sh -c` / `bash -c` / `zsh -c`, is pytest;
    # "pytest_output": the output is pytest's (a `FAILED <path>::` line or a
    # collection or setup error) from some other command; "jest": the runner
    # is jest or vitest (`narrow_tests.NODE`) or the output has Jest's or
    # Vitest's summary; "other": none of these.
    runner_kind: str
    broken: bool  # the output matches `_RED_BROKEN_TESTS`
    failed_paths: frozenset[
        str
    ]  # `FAILED <path>::` paths, project-relative when resolved
    failed_ids: tuple[str, ...]  # the node ids on those `FAILED` lines, in order
    # None when not checked (no expected tests given, or no `FAILED` path);
    # "unresolvable" when a path cannot be placed inside the project;
    # "inside" when every path is one of the expected tests; else "outside".
    attribution: str | None
    right_reason: bool  # the output matches `_RED_RIGHT_REASON`
    jest_failed: (
        bool  # Jest's or Vitest's `FAIL <file>` and `Tests N failed` lines, N > 0
    )
    tail: str  # the output's last 1000 characters


_FAILED_LINE_RE = re.compile(r"^FAILED\s+(.+?)::", re.MULTILINE)
_FAILED_ID_RE = re.compile(r"^FAILED\s+(.+?::.+?)(?:\s+-\s|\s*$)", re.MULTILINE)


def _red_facts(
    test_output: str,
    exit_code: int | None,
    runner: str | None,
    expected_tests: list[str] | None,
    test_command: str,
    project_dir: Path | None,
) -> _RedFacts:
    """The facts `red_gate` decides on, computed the way the gate always has.
    It decides nothing."""
    direct_pytest = runner == "pytest"
    if project_dir is not None and test_command:
        try:
            argv, directory = _test_invocation(test_command, project_dir)
            while (
                len(argv) == 3
                and Path(argv[0]).name in {"sh", "bash", "zsh"}
                and argv[1] == "-c"
            ):
                argv, directory = _test_invocation(argv[2], directory)
            # The executed argv keeps punctuation literal; runner_of rejects
            # shell-looking text even inside a quoted pytest argument.
            direct_pytest = direct_pytest or _runner_of_tokens(argv) == PYTEST
        except ValueError:
            pass
    pytest_output = direct_pytest or bool(
        re.search(
            r"^FAILED\s+.+?::|ERROR collecting|ERROR at (?:setup|teardown)|errors? during collection",
            test_output,
            re.MULTILINE,
        )
    )
    jest_output = runner in ("jest", "vitest", NODE) or bool(
        re.search(
            r"^Test Suites:|Test suite failed to run|^[ \t]*Test Files[ \t]+\d",
            test_output,
            re.MULTILINE,
        )
    )
    kind = (
        "direct_pytest"
        if direct_pytest
        else "pytest_output"
        if pytest_output
        else "jest"
        if jest_output
        else "other"
    )
    failed_paths = set(_FAILED_LINE_RE.findall(test_output))
    attribution = None
    if expected_tests is not None and failed_paths:
        attribution = "inside"
        if project_dir is not None:
            try:
                directory = _test_directory(test_command, project_dir)
                failed_paths = {
                    (directory / p)
                    .resolve()
                    .relative_to(project_dir.resolve())
                    .as_posix()
                    for p in failed_paths
                }
            except ValueError:
                attribution = "unresolvable"
        if attribution == "inside" and (
            not expected_tests or not failed_paths <= set(expected_tests)
        ):
            attribution = "outside"
    return _RedFacts(
        ran=exit_code is not None,
        exit_code=exit_code,
        runner=runner,
        runner_kind=kind,
        broken=bool(_RED_BROKEN_TESTS.search(test_output)),
        failed_paths=frozenset(failed_paths),
        failed_ids=tuple(_FAILED_ID_RE.findall(test_output)),
        attribution=attribution,
        right_reason=bool(_RED_RIGHT_REASON.search(test_output)),
        jest_failed=bool(
            re.search(r"^[ \t]*FAIL\s+\S", test_output, re.MULTILINE)
            and re.search(
                r"^(?:Tests:|[ \t]*Tests[ \t])\s*[^\n]*\b[1-9]\d* failed\b",
                test_output,
                re.MULTILINE,
            )
        ),
        tail=test_output[-1000:],
    )


# The outcomes the RED table matches on, each a predicate over the facts.
def _not_run(f: _RedFacts) -> bool:
    return not f.ran


def _exit_zero(f: _RedFacts) -> bool:
    return f.exit_code == 0


def _pytest_shaped(f: _RedFacts) -> bool:
    return f.runner_kind in ("direct_pytest", "pytest_output")


def _tests_broken(f: _RedFacts) -> bool:
    return f.runner_kind != "other" and f.broken


def _direct_exit_1_without_paths(f: _RedFacts) -> bool:
    return f.runner_kind == "direct_pytest" and f.exit_code == 1 and not f.failed_paths


def _no_usable_failures(f: _RedFacts) -> bool:
    return _pytest_shaped(f) and (
        not f.failed_paths or (f.runner_kind == "direct_pytest" and f.exit_code != 1)
    )


def _paths_unresolvable(f: _RedFacts) -> bool:
    return _pytest_shaped(f) and f.attribution == "unresolvable"


def _direct_outside_changed_tests(f: _RedFacts) -> bool:
    return f.runner_kind == "direct_pytest" and f.attribution == "outside"


def _wrapper_outside_changed_tests(f: _RedFacts) -> bool:
    return f.runner_kind == "pytest_output" and f.attribution == "outside"


def _jest_reported_failures(f: _RedFacts) -> bool:
    return f.runner_kind == "jest" and f.jest_failed


def _other_runner(f: _RedFacts) -> bool:
    return f.runner_kind == "other"


def _right_reason(f: _RedFacts) -> bool:
    return (
        _pytest_shaped(f)
        and (f.exit_code == 1 or f.runner != "pytest")
        and f.right_reason
    )


def _anything(f: _RedFacts) -> bool:
    return True


class RedRow(NamedTuple):
    """One row of `RED_TABLE`. *verdict* is "reject", "unverified" or
    "behavioral"; *reason* is formatted with `exit_code` and `tail`."""

    runner_kind: str
    outcome: Callable[[_RedFacts], bool]
    verdict: str
    action: str | None
    reason: str
    # Two unverified rows have never put the exit code in the metadata.
    reports_exit_code: bool = True


# Ordered: the first row whose outcome holds decides. The runner-kind column
# says which kinds an outcome can match; the predicate itself checks it.
RED_TABLE: tuple[RedRow, ...] = (
    RedRow(
        "any",
        _not_run,
        "reject",
        "fix_tests",
        "RED gate could not run the suite: {tail}",
    ),
    RedRow(
        "any",
        _exit_zero,
        "reject",
        "rewrite_tests",
        "RED gate failed: suite passed BEFORE implementation — the new "
        "tests exercise nothing unimplemented and prove nothing",
    ),
    RedRow(
        "pytest-shaped or jest",
        _tests_broken,
        "reject",
        "fix_tests",
        "RED gate failed: tests error instead of failing "
        "(collection/syntax problem, exit {exit_code}): {tail}",
    ),
    RedRow(
        "direct_pytest",
        _direct_exit_1_without_paths,
        "unverified",
        None,
        "Pytest exited 1 without failure paths; RED is unverified",
    ),
    RedRow(
        "pytest-shaped",
        _no_usable_failures,
        "reject",
        "fix_tests",
        "RED gate failed: pytest supplied no usable test failures (exit {exit_code}): {tail}",
    ),
    RedRow(
        "pytest-shaped",
        _paths_unresolvable,
        "unverified",
        None,
        "Pytest failure paths cannot be resolved; RED is unverified",
        reports_exit_code=False,
    ),
    RedRow(
        "direct_pytest",
        _direct_outside_changed_tests,
        "reject",
        "fix_tests",
        "RED gate failed: pytest failures are outside the changed tests",
    ),
    RedRow(
        "pytest_output",
        _wrapper_outside_changed_tests,
        "unverified",
        None,
        "RED cannot attribute the wrapper's failure to the changed tests; accepted as unverified",
        reports_exit_code=False,
    ),
    RedRow(
        "jest",
        _jest_reported_failures,
        "unverified",
        None,
        "Nonzero test-command exit accepted as unverified RED for this runner",
    ),
    RedRow(
        "other",
        _other_runner,
        "unverified",
        None,
        "Nonzero test-command exit accepted as unverified RED for this runner",
    ),
    RedRow(
        "pytest-shaped",
        _right_reason,
        "behavioral",
        None,
        "RED confirmed: tests fail for the right reason (exit {exit_code})",
    ),
    RedRow(
        "any",
        _anything,
        "reject",
        "fix_tests",
        "RED gate failed: suite exits {exit_code} but with no recognizable "
        "test-failure signature: {tail}",
    ),
)


def _red_row(facts: _RedFacts) -> RedRow:
    return next(row for row in RED_TABLE if row.outcome(facts))


def red_gate(
    test_output: str,
    exit_code: int | None,
    *,
    runner: str | None = "pytest",
    expected_tests: list[str] | None = None,
    test_command: str = "",
    project_dir: Path | None = None,
) -> GateResult:
    """RED-phase gate: prove the freshly written tests fail for the right reason.

    The verdict is the first row of `RED_TABLE` whose outcome holds for the
    run's facts (`_red_facts`). In order:

    1. the command did not run → reject, `fix_tests`;
    2. exit 0 → reject, `rewrite_tests`: the new tests exercise nothing that
       is still unimplemented, so they prove nothing;
    3. pytest-shaped or Jest output that matches `_RED_BROKEN_TESTS`
       (collection, setup or syntax errors) → reject, `fix_tests`;
    4. direct pytest, exit 1, no `FAILED` paths (a suppressed summary) →
       unverified;
    5. pytest-shaped with no `FAILED` paths, or direct pytest with an exit
       other than 1 → reject, `fix_tests`;
    6. `FAILED` paths that cannot be placed inside the project → unverified;
    7. direct pytest whose `FAILED` paths are outside the changed tests →
       reject, `fix_tests`;
    8. a wrapper's pytest paths outside the changed tests → unverified;
    9. Jest whose summary shows failed tests → unverified;
    10. any other runner's non-zero exit (a `make` missing target included) →
        unverified;
    11. pytest-shaped, attributable or unchecked paths, a right-reason
        signature, and exit 1 or a runner other than pytest → behavioral;
    12. anything else → reject, `fix_tests`.

    `metadata` holds `exit_code` (except for rows 6 and 8) and, on a pass,
    `classification` (the row's verdict).
    """
    facts = _red_facts(
        test_output, exit_code, runner, expected_tests, test_command, project_dir
    )
    row = _red_row(facts)
    metadata: dict = {"exit_code": exit_code} if row.reports_exit_code else {}
    passed = row.verdict != "reject"
    if passed:
        metadata["classification"] = row.verdict
    return GateResult(
        passed=passed,
        reason=row.reason.format(exit_code=exit_code, tail=facts.tail),
        action=row.action,
        metadata=metadata,
    )


_AGENT_STATUS_RE = re.compile(
    r"^\s*(?:[-*]\s*|\*\*)?STATUS:?\*{0,2}\s*[:\-]?\s*"
    r"(DONE_WITH_CONCERNS|DONE|NEEDS_CONTEXT|BLOCKED)\b",
    re.MULTILINE | re.IGNORECASE,
)
_AGENT_STATUS_PROCEED = {"DONE", "DONE_WITH_CONCERNS"}


def parse_agent_status(output: str) -> str | None:
    """The last `STATUS:` slot an agent reported, uppercased ("" → None).

    Agents are instructed to close their report with a REQUIRED STATUS slot.
    The last occurrence wins so a status quoted mid-report (e.g. restating the
    instructions) never outranks the agent's own final verdict.
    """
    matches = _AGENT_STATUS_RE.findall(output or "")
    return matches[-1].upper() if matches else None


def agent_status_gate(output: str, agent_name: str) -> GateResult:
    """Gate an agent's self-reported STATUS slot.

    NEEDS_CONTEXT and BLOCKED are the agent saying it could not do the work —
    the pipeline surfaces the stated reason and fails the block rather than
    proceeding on an admitted non-result. A missing slot is not fatal (the
    deterministic gates are the real verification), but it is reported.
    """
    status = parse_agent_status(output)
    if status is None:
        return GateResult(
            passed=True,
            reason=f"{agent_name} reported no STATUS slot",
            metadata={"status": None},
        )
    if status in _AGENT_STATUS_PROCEED:
        return GateResult(
            passed=True,
            reason=f"{agent_name} reported STATUS: {status}",
            metadata={"status": status},
        )
    return GateResult(
        passed=False,
        reason=f"{agent_name} reported STATUS: {status} — it could not complete "
        f"the work. Agent report:\n{(output or '')[-1500:]}",
        action="escalate_to_human",
        metadata={"status": status},
    )


# Slot values that say "nothing here". A slot filled with "none" is a real
# answer (the agent looked and found nothing) but carries no content to act on.
_EMPTY_SLOT_VALUES = {
    "",
    "none",
    "none.",
    "n/a",
    "na",
    "-",
    "—",
    "(none)",
    "nothing",
    "tbd",
}


# --- Implementation notes (SURPRISES: / SPEC_IMPACT: slots) ---

# The implementer's declaration that a test genuinely had to change. It is an
# escape hatch, not an authorization: it downgrades the gate from fail to a
# flagged claim that the reviewer (and the adjudicator) must verify — the same
# treatment RED/GREEN evidence already gets.
TEST_CHANGE_MARKER = "TEST CHANGE REQUIRED:"

# Every REQUIRED slot label an agent report can carry — used as the stop
# boundary when reading the free-text SURPRISES and SEARCHED slots, together
# with `_TEST_CHANGE_LINE` below.
_REPORT_SLOT_LABELS = "STATUS|TESTS_RUN|GREEN|FILES|TEST_COUNT|RED_EXPECTED|SURPRISES|SPEC_IMPACT|SEARCHED|RECURRING|NOTES_FOR_LATER_BLOCKS"

# The colon after a slot label is MANDATORY — every legitimate slot form
# carries one (`SURPRISES: x`, `- **SURPRISES:** x`, `**SURPRISES**: x`).
# Requiring it keeps prose that merely mentions the word ("Surprises were
# minimal this block.", or an echo of the prompt's own coaching sentence)
# from being recorded as a note, and keeps a continuation line that happens
# to open with a slot word ("STATUS quo in the repo differs...") from
# truncating a multiline value. The colon may sit inside or outside the
# closing ** of a bold label.
_SLOT_COLON = r"(?::\*{0,2}|\*{1,2}:)"

# A bullet (`-`, `*`, `+`), number (`1.`, `2)`) or quote (`>`) before a line's
# first word, nested in any order (`> - `).
_LIST_PREFIX = r"[ \t]*(?:(?:[-*+]|\d+[.)]|>)[ \t]*)*"

# A markdown heading's `#`s, or an inline-code backtick that a second backtick
# closes at the end of the line, before a declaration's marker. Inline code
# that ends before the line does ("`TEST CHANGE REQUIRED:` lines are not
# needed") is a mention, not a declaration.
_HEADING_PREFIX = r"(?:#{1,6}[ \t]+)?"
_LINE_IN_CODE = r"(?:`(?=[^`\n]*`[ \t]*$))?"

# The start of a test-change declaration: the marker opens the line, after a
# list or quote prefix, a heading or an inline code span that holds the whole
# line, and optional bold, with the colon inside or outside the bold. The
# prompts put these lines at the end of the report, after the other slots, so
# the free-text slots stop here too and never read one in.
_TEST_CHANGE_LINE = (
    r"^"
    + _LIST_PREFIX
    + _HEADING_PREFIX
    + _LINE_IN_CODE
    + r"(?:\*\*)?"
    + re.escape(TEST_CHANGE_MARKER.removesuffix(":"))
    + _SLOT_COLON
)

_SURPRISES_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?SURPRISES" + _SLOT_COLON + r"[ \t]*"
    r"(.*?)"
    r"(?=^\s*(?:[-*]\s*)?(?:\*\*)?(?:"
    + _REPORT_SLOT_LABELS
    + r")"
    + _SLOT_COLON
    + r"|"
    + _TEST_CHANGE_LINE
    + r"|\Z)",
    re.MULTILINE | re.IGNORECASE | re.DOTALL,
)

_SPEC_IMPACT_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?SPEC_IMPACT" + _SLOT_COLON + r"\s*"
    r"(none|clarify|contradicts)\b",
    re.MULTILINE | re.IGNORECASE,
)

# Placeholder answers that mean "nothing to report" — treated as an empty
# SURPRISES slot rather than as content. The empty-slot vocabulary
# (_EMPTY_SLOT_VALUES) minus "tbd": an implementer writing "tbd" under
# SURPRISES is deferring something real, which the respec loop must keep.
_EMPTY_SURPRISES = _EMPTY_SLOT_VALUES - {"tbd"}


def _clean_surprises(raw: str) -> str:
    """Strip fences and the prompt's own angle-bracket placeholder from the
    free-text SURPRISES slot; return "" when it says nothing."""
    lines = [
        ln
        for ln in (raw or "").strip().splitlines()
        if not ln.strip().startswith("```")
    ]
    text = "\n".join(lines).strip()
    # The agent echoing the template (`SURPRISES: <what did not match ...>`)
    # is not a finding.
    if text.startswith("<") and text.endswith(">"):
        return ""
    return "" if text.lower() in _EMPTY_SURPRISES else text


def parse_implementation_note(
    output: str,
    *,
    block_number: int,
    block_name: str,
    role: str,
) -> ImplementationNote | None:
    """Build an ImplementationNote from an agent's SURPRISES/SPEC_IMPACT slots.

    Returns None when the agent reported nothing worth recording (no slots, or
    "none" on both) — the notes file stays signal-only. The last occurrence of
    each slot wins, so a report that quotes the instructions before answering
    them never outranks the agent's own answer.
    """
    surprises_matches = _SURPRISES_RE.findall(output or "")
    surprises = _clean_surprises(surprises_matches[-1]) if surprises_matches else ""

    impact_matches = _SPEC_IMPACT_RE.findall(output or "")
    spec_impact = impact_matches[-1].lower() if impact_matches else "none"

    if not surprises and spec_impact == "none":
        return None

    return ImplementationNote(
        block_number=block_number,
        block_name=block_name,
        role=role,
        surprises=surprises,
        spec_impact=spec_impact,
    )


# --- Notes for later blocks (NOTES_FOR_LATER_BLOCKS: slot) ---

# Asked for only under `carry_block_notes`. Same label grammar as SURPRISES:
# the colon is mandatory, and the section runs to the next slot label or the end.
_HANDOFF_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?NOTES_FOR_LATER_BLOCKS" + _SLOT_COLON + r"[ \t]*"
    r"(.*?)"
    r"(?=^\s*(?:[-*]\s*)?(?:\*\*)?(?:"
    + _REPORT_SLOT_LABELS
    + r")"
    + _SLOT_COLON
    + r"|"
    + _TEST_CHANGE_LINE
    + r"|\Z)",
    re.MULTILINE | re.IGNORECASE | re.DOTALL,
)


def parse_handoff_note(output: str) -> str | None:
    """The agent's `NOTES_FOR_LATER_BLOCKS:` section, or None when it has none
    or says nothing ("none", an echoed placeholder). The last section wins, as
    in `parse_implementation_note`."""
    matches = _HANDOFF_RE.findall(output or "")
    if not matches:
        return None
    return _clean_surprises(matches[-1]) or None


# --- Review fix: where the agent looked (SEARCHED: slot) ---

# A review-fix agent ends its report with `SEARCHED:` — per finding, where it
# looked for the same mistake. Same label grammar as SURPRISES: the colon is
# mandatory, and the section runs to the next slot label or the end.
_SEARCHED_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?SEARCHED" + _SLOT_COLON + r"[ \t]*"
    r"(.*?)"
    r"(?=^\s*(?:[-*]\s*)?(?:\*\*)?(?:"
    + _REPORT_SLOT_LABELS
    + r")"
    + _SLOT_COLON
    + r"|"
    + _TEST_CHANGE_LINE
    + r"|\Z)",
    re.MULTILINE | re.IGNORECASE | re.DOTALL,
)


def parse_searched(output: str) -> list[str] | None:
    """The entries of a review-fix agent's `SEARCHED:` section, one per line,
    bullets stripped. ``None`` when the report has no such section, so a
    record can tell "did not say" from "said nothing". The last section wins,
    as in `parse_implementation_note`.

    Code cannot check that a search happened; this records the claim.
    """
    matches = _SEARCHED_RE.findall(output or "")
    if not matches:
        return None
    entries = []
    for line in matches[-1].splitlines():
        text = line.strip()
        if text.startswith("```"):
            continue
        if text[:2] in ("- ", "* "):
            text = text[2:].strip()
        if text:
            entries.append(text)
    return entries


# --- Escalated review fix: findings that share a cause (RECURRING: slot) ---

# The review loop's last fix (`prompts.implementation.REVIEW_FIX_ESCALATION`)
# ends with `RECURRING: <ids that share a cause, and the cause> | none`. Same
# label grammar as SEARCHED; the slot is the rest of its line.
_RECURRING_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?RECURRING" + _SLOT_COLON + r"[ \t]*(.*?)[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)


def parse_recurring(output: str) -> str | None:
    """The escalated fix's `RECURRING:` answer (``none`` included), or None
    when its report has no such slot. The last slot wins. Recorded, not
    gated, like `SEARCHED:`."""
    matches = [m.strip("`").strip() for m in _RECURRING_RE.findall(output or "")]
    return matches[-1] if matches and matches[-1] else None


# --- Test writer: which tests must fail (RED_EXPECTED: slot) ---

# The test writer names the tests it expects the RED run to fail and why:
# `RED_EXPECTED: <ids, comma-separated> — <what is missing>`. Same label
# grammar as SEARCHED: the colon is mandatory, and the slot runs to the next
# slot label, a test-change declaration or the end.
_RED_EXPECTED_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?RED_EXPECTED" + _SLOT_COLON + r"[ \t]*"
    r"(.*?)"
    r"(?=^\s*(?:[-*]\s*)?(?:\*\*)?(?:"
    + _REPORT_SLOT_LABELS
    + r")"
    + _SLOT_COLON
    + r"|"
    + _TEST_CHANGE_LINE
    + r"|\Z)",
    re.MULTILINE | re.IGNORECASE | re.DOTALL,
)

# The dash between the ids and the reason: an em or en dash, `--`, or a
# hyphen with a space on each side.
_RED_EXPECTED_DASH_RE = re.compile(r"\s*[—–]\s*|\s+--?\s+")


def _split_ids(text: str) -> list[str]:
    """Comma-separated ids, keeping a comma inside a parametrize `[...]`."""
    ids, depth, current = [], 0, ""
    for char in text:
        depth += (char == "[") - (char == "]")
        if char == "," and depth <= 0:
            ids.append(current)
            current = ""
        else:
            current += char
    ids.append(current)
    return [i.strip().strip("`").strip() for i in ids if i.strip().strip("`").strip()]


def parse_red_expected(report: str) -> tuple[list[str], str] | None:
    """The test ids and the reason in the test writer's `RED_EXPECTED:` slot.
    ``None`` when the report has no such slot, or the slot is empty or echoes
    the prompt's `<...>` template. The last slot wins, as in
    `parse_searched`."""
    matches = _RED_EXPECTED_RE.findall(report or "")
    if not matches:
        return None
    value = " ".join(
        line.strip()
        for line in matches[-1].splitlines()
        if line.strip() and not line.strip().startswith("```")
    )
    if not value or (value.startswith("<") and value.endswith(">")):
        return None
    dash = _RED_EXPECTED_DASH_RE.search(value)
    if not dash:
        return _split_ids(value), ""
    return _split_ids(value[: dash.start()]), value[dash.end() :].strip()


def _names_node(named: str, node: str) -> bool:
    """Whether a test id the test writer named is the pytest node id *node*:
    the same id, or the same one written relative to another directory, or
    a bare test name, or the id without its parametrize `[...]`."""
    for candidate in {node, node.split("[", 1)[0]}:
        if (
            candidate == named
            or candidate.endswith("/" + named)
            or named.endswith("/" + candidate)
            or candidate.endswith("::" + named)
        ):
            return True
    return False


def red_expected_note(
    report: str | None,
    test_output: str,
    exit_code: int | None,
    *,
    runner: str | None,
    expected_tests: list[str] | None = None,
    test_command: str = "",
    project_dir: Path | None = None,
) -> str:
    """`BlockInfo.red_expected` for a passed RED gate: the test writer's
    `RED_EXPECTED:` slot and one result line. The result is "named tests
    failed" when the run is direct pytest and every named id is a `FAILED`
    node id; "named tests not among the failures: <ids>" when some are not;
    "not stated" when the report (None on resume) has no slot or names no
    test; and "cannot check for this runner" when the run is not direct
    pytest or prints no `FAILED` line. It decides no verdict."""
    stated = parse_red_expected(report or "")
    if stated is None:
        return "RED_EXPECTED result: not stated"
    ids, reason = stated
    slot = "RED_EXPECTED: " + ", ".join(ids) + (f" — {reason}" if reason else "")
    facts = _red_facts(
        test_output, exit_code, runner, expected_tests, test_command, project_dir
    )
    if not ids:
        result = "not stated"
    elif facts.runner_kind != "direct_pytest" or not facts.failed_ids:
        result = "cannot check for this runner"
    else:
        missing = [
            i for i in ids if not any(_names_node(i, node) for node in facts.failed_ids)
        ]
        result = (
            "named tests failed"
            if not missing
            else "named tests not among the failures: " + ", ".join(missing)
        )
    return f"{slot}\nRED_EXPECTED result: {result}"


# --- Documentation update (DOCS_IMPACT: slot) ---

# The docs agent closes its report with a REQUIRED `DOCS_IMPACT:` slot, in the
# same shape as the implementation agents' STATUS/SURPRISES slots. The value is
# either `none` or a comma-separated list of the documentation files it wrote.
_DOCS_IMPACT_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?DOCS_IMPACT" + _SLOT_COLON + r"[ \t]*(.*)$",
    re.MULTILINE | re.IGNORECASE,
)

# NOTE: this name deliberately does not start with `test` — pytest collects any
# `test*` callable imported into a test module as a test case (see
# `unchanged_tests_gate` for the same reason).


def parse_docs_impact(output: str) -> list[str] | None:
    """The documentation files the docs agent reported writing.

    Returns ``None`` when the report carries no `DOCS_IMPACT:` slot at all
    (the agent never answered), and ``[]`` when it explicitly answered `none`.
    Both mean "no document was updated", but only one of them is an answer, so
    the distinction is preserved rather than flattened here.

    The last occurrence wins, so a report that quotes the instructions before
    answering them never outranks the agent's own answer.
    """
    matches = _DOCS_IMPACT_RE.findall(output or "")
    if not matches:
        return None
    raw = matches[-1].strip().strip("*_` ")
    # The agent echoing the template (`DOCS_IMPACT: <none, or a ...>`) is not
    # an answer; treat it as an unfilled slot. The opening `<` alone decides:
    # a real path list never starts with an angle bracket, and requiring the
    # closing `>` too would miss an echo of a placeholder that wraps onto the
    # next line (the regex captures a single line, so the `>` is out of reach).
    if raw.startswith("<"):
        return None
    if raw.lower() in _EMPTY_SLOT_VALUES:
        return []
    parts = [p.strip().strip("`\"'") for p in re.split(r"[,\n]", raw)]
    return [p for p in parts if p and p.lower() not in _EMPTY_SLOT_VALUES]


_DIFF_PATH_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)\s*$", re.MULTILINE)

# README*, CHANGELOG*, and prose files under a docs/ directory — the same
# inventory the docs prompt tells the agent to take. Two deliberate
# tightenings, both guarding the same thing: every path this matches is a path
# the freshness warning stops counting as source, so a loose match quietly
# shrinks the evidence the warning is computed from.
#
#   * the name must be readme / changelog plus at most a `.ext` or `-suffix` —
#     a bare `readme*` prefix would classify `src/readme_parser.py` as a doc;
#   * a path under `docs/` must also *look* like a document — `docs/` holds
#     generators, conf.py, and theme assets in plenty of projects, and
#     `src/docs/generator.py` is source code by any reading.
_DOC_SUFFIXES = (".md", ".markdown", ".rst", ".txt", ".adoc", ".mdx")
_DOC_NAME_RE = re.compile(r"(?:^|/)(?:readme|changelog)(?:[.-][^/]*)?$", re.IGNORECASE)
_DOC_DIR_RE = re.compile(r"(?:^|/)docs?/", re.IGNORECASE)


def parse_diff_paths(diff: str) -> list[str]:
    """Every file path a unified diff touches, in first-seen order."""
    seen: dict[str, None] = {}
    for old, new in _DIFF_PATH_RE.findall(diff or ""):
        for path in (old, new):
            seen.setdefault(path, None)
    return list(seen)


def is_doc_path(path: str) -> bool:
    """Whether a repo-relative path is one of the documents this phase owns."""
    path = path or ""
    if _DOC_NAME_RE.search(path):
        return True
    return bool(_DOC_DIR_RE.search(path)) and path.lower().endswith(_DOC_SUFFIXES)


def source_paths_in_diff(diff: str) -> list[str]:
    """Changed paths that are neither documentation nor the build's own specs.

    `specs/` is excluded because those artifacts are the build's paperwork, not
    the code a reader of the README would notice changing.
    """
    return [
        p
        for p in parse_diff_paths(diff)
        if not is_doc_path(p) and not p.startswith("specs/")
    ]


def _changelog_in(project_dir: Path) -> str | None:
    """The project's changelog file name, or None when it keeps none."""
    try:
        entries = sorted(p.name for p in project_dir.iterdir() if p.is_file())
    except OSError:
        return None
    for name in entries:
        if name.lower().startswith("changelog"):
            return name
    return None


def docs_freshness_gate(
    diff: str, docs_impact: list[str] | None, project_dir: Path
) -> GateResult:
    """Warn when a build changed source, keeps a changelog, and updated no docs.

    **This gate never fails.** Plenty of real changes have no user-visible
    delta — an internal refactor, a test-only fix — and failing a finished build
    over a judgment call about documentation would be worse than the staleness
    it is trying to catch. It returns a passing GateResult carrying
    ``action="docs_may_be_stale"``, which the caller prints as a warning for the
    human reviewing the PR.

    A missing `DOCS_IMPACT:` slot is treated the same as `none`: either way no
    document was written. `parse_docs_impact` keeps the two distinguishable for
    callers that care.
    """
    if docs_impact:
        return GateResult(
            passed=True,
            reason=f"Documentation updated: {', '.join(docs_impact)}",
            metadata={"docs_impact": docs_impact},
        )

    source = source_paths_in_diff(diff)
    if not source:
        return GateResult(
            passed=True,
            reason="No source files changed in the build diff — nothing to document",
            metadata={"docs_impact": [], "source_files": []},
        )

    changelog = _changelog_in(project_dir)
    if changelog is None:
        return GateResult(
            passed=True,
            reason="No documentation updated and this project keeps no changelog",
            metadata={"docs_impact": [], "source_files": source},
        )

    return GateResult(
        passed=True,
        action="docs_may_be_stale",
        reason=(
            f"{len(source)} source file(s) changed and {changelog} exists, but the "
            f"documentation phase reported DOCS_IMPACT: none. The docs may now be "
            f"stale — check {changelog} and the README before merging. "
            f"Changed: {', '.join(source[:10])}" + (" …" if len(source) > 10 else "")
        ),
        metadata={
            "warning": True,
            "changelog": changelog,
            "docs_impact": [],
            "source_files": source,
        },
    )


def review_score_gate(
    review: ReviewResult, policy: ReviewGatePolicy | None = None
) -> GateResult:
    """Graded two-verdict gate.

    Spec compliance stays a **hard floor** — no confidence weighting applies to
    "the spec behavior is missing", because that is a factual claim about the
    diff, not a judgment call.

    The rubric dimensions are graded: each score is discounted toward neutral
    by the reviewer's confidence (see `ReviewScore.effective_score`), so an
    unsure reviewer moves the needle less than a certain one and a panel that
    disagreed with itself moves it barely at all. Thresholds come from
    *policy* (`specs/config.yaml: code_review.gate`); its defaults, together
    with the default confidence of 1.0, reproduce the previous binary
    behavior exactly.
    """
    pol = policy or ReviewGatePolicy()
    avg = review.weighted_average_with(pol)
    failing = review.failing_dimensions_with(pol)
    graded = any(s.confidence < 1.0 for s in review.scores)

    if not review.spec_compliant:
        parts = []
        if review.missing:
            parts.append(f"missing: {review.missing}")
        if review.misunderstood:
            parts.append(f"misunderstood: {review.misunderstood}")
        return GateResult(
            passed=False,
            reason=f"Spec-compliance verdict failed — {'; '.join(parts)}",
            action="fix_spec_compliance",
            metadata={
                "missing": review.missing,
                "misunderstood": review.misunderstood,
                "extra": review.extra,
                "weighted_average": avg,
            },
        )
    if failing:
        return GateResult(
            passed=False,
            reason=(
                f"Dimension(s) at or below the critical score "
                f"{pol.critical_score:g}: {failing}"
            ),
            action="fix_critical_dimension",
            metadata={
                "failing_dimensions": failing,
                "weighted_average": avg,
                "graded": graded,
            },
        )
    if avg < pol.min_weighted_average:
        return GateResult(
            passed=False,
            reason=f"Weighted average {avg:.1f} < {pol.min_weighted_average:g} threshold",
            action="fix_low_scores",
            metadata={"weighted_average": avg, "graded": graded},
        )
    return GateResult(
        passed=True,
        reason=f"Review passed: weighted average {avg:.1f}",
        metadata={"weighted_average": avg, "graded": graded},
    )


def block_severity_gate(
    review: ReviewResult,
    findings: list,
    severities: tuple[str, ...] | list[str],
) -> GateResult:
    """The per-block gate under `code_review.component_review`: the block
    fails only on an accepted finding rated one of *severities*
    (`code_review.block_gate_severities`, lower-case), or on the
    spec-compliance verdict, which stays a hard floor. The scores do not
    gate; findings rated lower go to the component review.

    *findings* are the block's open accepted findings (the earlier rounds'
    carried ones and this round's new ones)."""
    if not review.spec_compliant:
        return review_score_gate(review)
    wanted = {s.lower() for s in severities}
    blocking = [f for f in findings if f.severity.strip().lower() in wanted]
    named = ", ".join(s.capitalize() for s in severities) or "(none)"
    if blocking:
        return GateResult(
            passed=False,
            reason=f"{len(blocking)} accepted finding(s) rated {named}",
            action="fix_blocking_findings",
            metadata={"blocking": len(blocking)},
        )
    return GateResult(
        passed=True,
        reason=f"Review passed: no accepted finding rated {named}",
        metadata={"blocking": 0},
    )


# A declaration counts only on a line that starts with the marker
# (`_TEST_CHANGE_LINE`), as the prompts ask. Every implementer and
# integration-fix prompt shows the marker, so prose that mentions it ("no
# TEST CHANGE REQUIRED: line is needed") must not count as one.
_TEST_CHANGE_RE = re.compile(_TEST_CHANGE_LINE + r"[ \t]*(.*)$", re.IGNORECASE)

# A declaration line that opens an inline code span; its closing backtick is
# not part of the reason.
_IN_CODE_RE = re.compile(r"^" + _LIST_PREFIX + _HEADING_PREFIX + r"`")

# A line that cannot be the reason of a marker left alone on its line: a slot
# label, another declaration, or a code fence.
_NOT_A_REASON_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?(?:"
    + _REPORT_SLOT_LABELS
    + r")"
    + _SLOT_COLON
    + r"|"
    + _TEST_CHANGE_LINE
    + r"|^\s*```",
    re.IGNORECASE,
)


def parse_test_change_claims(report: str) -> list[str]:
    """The reasons an implementer declared for changing a test file.

    A marker left alone on its line takes the next non-blank line of *report*
    as its reason, unless that line is a slot label, another declaration or a
    fence. So pass one agent's report at a time: in two reports joined, a bare
    marker at the end of the first would take the second's first line.
    An echo of the prompt's template (`<reason>`) and an empty answer
    (an `_EMPTY_SLOT_VALUES` entry such as `none` or `tbd`, or `no`, also with
    a trailing parenthesis after it or a full stop, as in
    `None (no test files modified)`) declare nothing. A reason written wholly
    in parentheses is not an empty answer.
    """
    lines = (report or "").splitlines()
    claims = []
    for i, line in enumerate(lines):
        m = _TEST_CHANGE_RE.match(line)
        if not m:
            continue
        reason = m.group(1).strip()
        if _IN_CODE_RE.match(line):
            reason = reason.removesuffix("`").strip()
        if not reason:
            following = next((ln for ln in lines[i + 1 :] if ln.strip()), "")
            if not _NOT_A_REASON_RE.match(following):
                reason = re.sub(r"^" + _LIST_PREFIX, "", following).strip()
        if reason.startswith("<") and reason.endswith(">"):
            continue
        bare = re.sub(r"(?<=\S)\s*\(.*\)$", "", reason).rstrip(" .").lower()
        if bare in _EMPTY_SLOT_VALUES or bare == "no":
            continue
        claims.append(reason)
    return claims


def unchanged_tests_gate(
    before: dict[str, str],
    after: dict[str, str],
    implementer_report: str | Sequence[str] = "",
) -> GateResult:
    """Fail when the tests that gated this block changed after they gated it.

    The threat is not hypothetical. `IMPLEMENTER_TOOLS` grants Write/Edit/Bash
    with **no path restriction**, so test files are writable for the entire
    implement phase; the review-fix agent is the same implementer, so every fix
    iteration re-opens that window; and both the weak-test scan and its LLM
    auditor run at test-writing time only, so nothing re-checks the tests
    afterwards. An implementer measured by "the tests pass" can therefore make
    them pass by editing them, and today nothing would notice.

    Restricting the tool allow-list is NOT the fix: the SDK allow-list is
    per-tool rather than per-path, and Bash routes around it anyway. Hashing is
    the enforcement; the prompt instruction is only a hint.

    Args:
        before: {test file path: sha256} snapshotted when the RED gate passed.
        after: the same map recomputed now.
        implementer_report: checked for the TEST CHANGE REQUIRED escape hatch.
            One report, or a list of the reports of every agent that wrote in
            this window; each is parsed on its own (`parse_test_change_claims`).

    Returns a passing gate when nothing was snapshotted (no test command, or a
    checkpoint predating this gate) — an unverifiable gate reports that it did
    not run rather than failing a block on absent evidence.
    """
    if not before:
        return GateResult(
            passed=True,
            reason="Test integrity not checked: no snapshot taken for this block",
            metadata={"checked": False},
        )

    mutated = sorted(p for p, h in before.items() if p in after and after[p] != h)
    deleted = sorted(p for p in before if p not in after)
    changed = mutated + deleted

    if not changed:
        return GateResult(
            passed=True,
            reason=f"Test integrity OK: {len(before)} test file(s) unchanged since RED",
            metadata={"checked": True, "files": len(before)},
        )

    reports = (
        [implementer_report]
        if isinstance(implementer_report, str)
        else implementer_report
    )
    claims = [c for r in reports for c in parse_test_change_claims(r)]
    detail = ", ".join(mutated + [f"{p} (deleted)" for p in deleted])
    if claims:
        # Declared, so not a silent mutation — but a declaration is a claim,
        # not proof. Pass the gate and hand the claim to the reviewer.
        return GateResult(
            passed=True,
            reason=(
                f"Test files changed after RED with a declared reason "
                f"(unverified claim): {detail}"
            ),
            action="flag_test_change_claim",
            metadata={
                "checked": True,
                "changed_files": changed,
                "claims": claims,
                "flagged": True,
            },
        )

    return GateResult(
        passed=False,
        reason=(
            f"Test files changed after the RED gate with no declared reason: {detail}. "
            f"The tests that were supposed to gate this block are not the tests that "
            f"gated it. Restore them, or state '{TEST_CHANGE_MARKER} <reason>' in the "
            f"report so a reviewer can verify the change was legitimate."
        ),
        action="restore_tests",
        metadata={"checked": True, "changed_files": changed, "flagged": False},
    )


# --- Unknowns / explainer gate (B1) ---

# A dependency's section is a level-2 heading; its required lists are level-3
# subsections. Matching is by heading text, so the gate reads the same document
# a human reads.
_UNKNOWNS_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_UNKNOWNS_SUBSECTION_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_EDGE_CASES_RE = re.compile(r"edge\s*case", re.IGNORECASE)
_ASSUMPTIONS_RE = re.compile(r"assumption", re.IGNORECASE)
# A filled list item: a bullet or numbered item with real text after it —
# the (?!<) lookahead refuses template placeholders (`- <case>: <what happens>`).
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?!<)(\S.*)$", re.MULTILINE)


def _split_sections(text: str, pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    """(heading, body) pairs for every heading matched by ``pattern``."""
    matches = list(pattern.finditer(text or ""))
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((m.group(1).strip(), text[m.end() : end]))
    return out


def _has_filled_list(body: str) -> bool:
    """True when the body carries at least one list item with real content
    (the item pattern itself already refuses `<placeholder>` items)."""
    return _LIST_ITEM_RE.search(body or "") is not None


def _heading_names_dep(heading: str, needle: str) -> bool:
    """Whether a ``## heading`` is THIS dependency's section.

    Whole-token match over package-name characters — `react` must not ride on
    a `## react-query` heading — while still tolerating decoration around the
    name (backticks, a trailing ``— what it is`` clause).
    """
    return (
        re.search(
            r"(?<![A-Za-z0-9._+@/-])" + re.escape(needle) + r"(?![A-Za-z0-9._+@/-])",
            heading.lower(),
        )
        is not None
    )


def unknowns_gate(unknowns_text: str, deps) -> GateResult:
    """Structural gate on `unknowns.md` — the explainer must actually explain.

    Fails when a detected dependency has no section at all, or when its
    "Edge cases & failure modes" or "Assumptions we are making" list is empty.
    Those two lists are the whole point of the explainer (the Whisper-silence
    class of surprise), so an empty one is an unwritten explainer, not a
    stylistic nit.

    ``deps`` accepts NewDependency objects or plain name strings. With no
    dependencies the gate passes — "nothing new to this project" is a recorded
    finding, not a failure.
    """
    names = [getattr(d, "name", d) for d in (deps or [])]
    if not names:
        return GateResult(
            passed=True,
            reason="No dependencies new to this project — nothing to explain",
            metadata={"dependencies": []},
        )

    sections = _split_sections(unknowns_text or "", _UNKNOWNS_SECTION_RE)
    findings: list[str] = []
    for name in names:
        needle = str(name).lower()
        match = next(
            (body for heading, body in sections if _heading_names_dep(heading, needle)),
            None,
        )
        if match is None:
            findings.append(f"`{name}`: no `## {name}` section in unknowns.md")
            continue

        subsections = _split_sections(match, _UNKNOWNS_SUBSECTION_RE)
        edge_body = next((b for h, b in subsections if _EDGE_CASES_RE.search(h)), None)
        assum_body = next(
            (b for h, b in subsections if _ASSUMPTIONS_RE.search(h)), None
        )
        if edge_body is None:
            findings.append(
                f"`{name}`: missing an 'Edge cases & failure modes' subsection"
            )
        elif not _has_filled_list(edge_body):
            findings.append(
                f"`{name}`: 'Edge cases & failure modes' list is empty — needs at "
                f"least one bullet naming observed behavior on empty, malformed, "
                f"oversized, concurrent, and offline input"
            )
        if assum_body is None:
            findings.append(
                f"`{name}`: missing an 'Assumptions we are making' subsection"
            )
        elif not _has_filled_list(assum_body):
            findings.append(
                f"`{name}`: 'Assumptions we are making' list is empty — needs at "
                f"least one falsifiable claim"
            )

    if findings:
        return GateResult(
            passed=False,
            reason="Explainer sections incomplete:\n"
            + "\n".join(f"- {f}" for f in findings),
            action="regenerate_unknowns",
            metadata={"findings": findings, "dependencies": names},
        )
    return GateResult(
        passed=True,
        reason=f"All {len(names)} dependency explainer(s) complete",
        metadata={"dependencies": names},
    )


def review_iteration_gate(iteration: int, max_iterations: int = 3) -> GateResult:
    """Check if review fix cycles are exhausted."""
    if iteration < max_iterations:
        return GateResult(
            passed=True,
            reason=f"Review iteration {iteration + 1}/{max_iterations}",
        )
    return GateResult(
        passed=False,
        reason=f"Review loop exhausted after {max_iterations} iterations",
        action="halt_build",
    )


def verification_inputs(
    test_command: str,
    project_dir: Path,
    *,
    parallel: str = "off",
    plan_revision: str = "",
    include_files: bool = False,
) -> dict:
    """Exact inputs to the final check; unavailable Git/file evidence raises.

    Paths are project-relative. Bookkeeping and untracked interpreter/test caches
    are not build inputs; tracked generated files always are. A submodule, or a
    repository nested untracked, is one input: its HEAD, its index and its
    uncommitted paths (`_checkout_input`).
    """
    import hashlib
    import json

    from . import git_ops

    head = git_ops.rev_parse_head(project_dir)
    index = git_ops.index_entries(project_dir)
    if not head or index is None:
        raise RuntimeError(
            "Cannot identify the Git revision/index for final verification"
        )
    result = git_ops._run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            ".",
            *git_ops.BOOKKEEPING_EXCLUDES,
        ],
        cwd=project_dir,
    )
    if not result.ok:
        raise RuntimeError("Cannot read final verification inputs: " + result.stderr)
    files = _path_inputs(project_dir, result.stdout.split("\0"), index)
    content = json.dumps({"files": files, "index": index}, sort_keys=True).encode()
    result = {
        "head": head,
        "content": hashlib.sha256(content).hexdigest(),
        "command": test_command,
        "parallel": parallel,
        "plan_revision": plan_revision,
    }
    if include_files:
        result.update(files=files, index=index)
    return result


def _path_inputs(
    root: Path, names: Sequence[str], index: dict[str, str]
) -> dict[str, str]:
    """Each listed path under *root* mapped to its content: a symlink's target, a
    file's executable bits and hash, a checkout's `_checkout_input`, or "deleted".
    Untracked interpreter/test caches are left out."""
    import hashlib

    files = {}
    for name in sorted(set(filter(None, names))):
        if name not in index and any(
            part in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
            for part in Path(name).parts
        ):
            continue
        path = root / name
        if path.is_symlink():
            files[name] = "link:" + os.readlink(path)
        elif path.is_dir():
            files[name] = _checkout_input(path, name, index.get(name, ""))
        elif path.exists():
            files[name] = (
                f"{path.stat().st_mode & 0o111}:"
                + hashlib.sha256(path.read_bytes()).hexdigest()
            )
        else:
            files[name] = "deleted"
    return files


def _checkout_input(path: Path, name: str, index_entry: str) -> str:
    """A directory Git lists as one path: a submodule, or a repository nested
    untracked. A checkout gives its HEAD and a hash of its index and of the
    content of every path it lists as the project's own check does, each nested
    submodule included; nothing in it is written. An empty directory at a
    gitlink (mode 160000) is "gitlink:uninitialized". Any other directory
    raises: its content cannot be told apart from a checkout's."""
    import hashlib
    import json

    from . import git_ops

    def read(*args: str) -> str:
        result = git_ops._run(["git", "--no-optional-locks", *args], cwd=path)
        if not result.ok:
            raise RuntimeError(
                f"{name} is a checkout Git cannot read: {result.stderr.strip()}"
            )
        return result.stdout

    top = git_ops._run(["git", "rev-parse", "--show-toplevel"], cwd=path)
    if not (top.ok and Path(top.stdout.strip()).resolve() == path.resolve()):
        if index_entry.startswith("160000 ") and not any(path.iterdir()):
            return "gitlink:uninitialized"
        raise RuntimeError(
            f"{name} is a directory holding files but no checkout Git can read"
        )
    head = read("rev-parse", "--verify", "HEAD").strip()
    index = git_ops.index_entries(path)
    if index is None:
        raise RuntimeError(f"{name} is a checkout whose index Git cannot read")
    listed = read(
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        ".",
        *git_ops.BOOKKEEPING_EXCLUDES,
    )
    files = _path_inputs(path, listed.split("\0"), index)
    content = json.dumps({"files": files, "index": index}, sort_keys=True).encode()
    return f"checkout:{head}:" + hashlib.sha256(content).hexdigest()


def final_verification(
    test_command: str,
    project_dir: Path,
    *,
    parallel: str = "off",
    timeout: int = DEFAULT_TEST_TIMEOUT,
    plan_revision: str = "",
    failure_log: Path | None = None,
) -> GateResult:
    """The shared completion gate, after the last writer and before publication.
    A failed run with *failure_log* set writes its output there after the
    second input read, so the file is never one of the inputs it compares."""
    if not test_command.strip():
        return GateResult(
            passed=False, reason="Final verification requires a configured test command"
        )
    try:
        before = verification_inputs(
            test_command,
            project_dir,
            parallel=parallel,
            plan_revision=plan_revision,
            include_files=True,
        )
        command, exit_code, output = _suite_run(
            test_command, project_dir, parallel, timeout
        )
        gate = _integration_verdict(exit_code, output)
        after = verification_inputs(
            test_command,
            project_dir,
            parallel=parallel,
            plan_revision=plan_revision,
            include_files=True,
        )
    except (OSError, RuntimeError, ValueError) as error:
        return GateResult(
            passed=False, reason=f"Final verification unavailable: {error}"
        )
    _attach_failure_log(gate, failure_log, command, exit_code, output)
    gate.metadata["inputs"] = {
        k: v for k, v in after.items() if k not in {"files", "index"}
    }
    if any(before[k] != after[k] for k in before if k != "content"):
        changed = sorted(
            {
                name
                for field in ("files", "index")
                for name in before[field].keys() | after[field].keys()
                if before[field].get(name) != after[field].get(name)
            }
        )
        details = ", ".join(changed) or "Git HEAD or verification configuration"
        checkouts = "".join(
            f" {name} is a submodule or nested repository: run git -C {name.rstrip('/')} status "
            "to see what the command changed in it."
            for name in changed
            if any(
                side["files"].get(name, "").startswith(("checkout:", "gitlink:"))
                for side in (before, after)
            )
        )
        return GateResult(
            passed=False,
            reason=f"Inputs changed during final verification: {details}. "
            "If a listed file is a generated report, run git rm --cached <path>, "
            "add it to .gitignore, and commit. "
            "Inspect changes to tracked files and source inputs." + checkouts,
            metadata={**gate.metadata, "changed_files": changed},
        )
    return gate
