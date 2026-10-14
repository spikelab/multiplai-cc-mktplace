"""The narrow test command a block's TDD loop runs instead of the whole suite.

Inside a block, buildme runs (and tells its agents to run) only:

1. the test files the block added or changed, from the block's git diff
   against its start (`BlockInfo.baseline_commit`), untracked files included;
2. for each source file the block changed, the test file named after it
   (`test_<module>.py` / `<module>_test.py` for pytest, `<name>.test.<ext>` /
   `<name>.spec.<ext>` for Node), the one nearest the source file when the
   project has several.

Tests are never selected by import. The paths are appended to the project's
test command only when its runner is known to accept them: pytest, or jest /
vitest run directly or through `npm test --`. Any other command (a task runner
such as tox, even behind `uv run`, or a chained command), an empty selection, a
changed file the engine counts as a test but this module cannot name (Django's
`tests.py`, a `__tests__/` file, a helper under `tests/`), a coverage setting (a narrow run misses a
coverage floor the whole suite meets), a Node path jest would read as a regex,
`tdd.narrow_tests: false`, no recorded block start, or a git error gives back
the whole command, so the RED gate never passes on a run that left out the
block's own tests. The gates that decide whether work is done keep the whole
command.

An agent's command is fixed before it changes anything, so `agent_test_command`
adds how to run the test files it writes and the test file named after each
module it changes.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

PYTEST = "pytest"
NODE = "node"

_PY_TEST_RE = re.compile(r"(^|/)(test_[^/]+|[^/]+_test)\.py$")
_NODE_EXT = r"[cm]?[jt]sx?"
_NODE_TEST_RE = re.compile(rf"(^|/)[^/]+\.(test|spec)\.{_NODE_EXT}$")
_NODE_SOURCE_RE = re.compile(rf"\.{_NODE_EXT}$")
_NPM_TEST = (("npm", "test"), ("npm", "t"), ("npm", "run", "test"))
# Task runners take their own arguments, not test paths, even when a target
# or environment is named after a test runner (`tox -e pytest`), and even
# behind a wrapper (`uv run tox -e pytest`, `python -m tox`).
_TASK_RUNNERS = ("tox", "nox", "make", "just", "invoke", "task")
# Appended paths go to the last command of a chain (and npm appends `-- paths`
# to the end of the whole script), so a chained command is never narrowed.
_CHAINED_RE = re.compile(r"[;&|]")
# jest reads each path as a regular expression. `.` is left out: it also
# matches itself, so a dotted path still selects its own file.
_REGEX_CHARS_RE = re.compile(r"[\[\](){}+*?^$|\\]")
_PYTEST_CONFIG_FILES = ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")
_NODE_CONFIG_PREFIXES = ("jest.config.", "vitest.config.", "vite.config.", "vitest.workspace.")


def _package_json(project_dir: Path) -> dict:
    try:
        data = json.loads((project_dir / "package.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _package_test_script(project_dir: Path) -> str:
    scripts = _package_json(project_dir).get("scripts")
    script = scripts.get("test") if isinstance(scripts, dict) else None
    return script if isinstance(script, str) else ""


def _runner_of_tokens(tokens: list[str]) -> str | None:
    # The first runner named decides: a task runner before the test runner
    # (`uv run tox -e pytest`) takes the arguments itself.
    for name in (Path(t).name for t in tokens):
        if name in _TASK_RUNNERS:
            return None
        if name in ("pytest", "py.test"):
            return PYTEST
        if name in ("jest", "vitest"):
            return NODE
    return None


def runner_of(test_command: str, project_dir: Path) -> str | None:
    """`"pytest"`, `"node"` (jest or vitest), or None when the command's runner
    is not one known to accept test paths."""
    try:
        tokens = shlex.split(test_command)
    except ValueError:
        return None
    if _CHAINED_RE.search(test_command):
        return None
    if any(tuple(tokens[:len(npm)]) == npm for npm in _NPM_TEST):
        script = _package_test_script(Path(project_dir))
        if _CHAINED_RE.search(script):
            return None
        try:
            script_tokens = shlex.split(script)
        except ValueError:
            return None
        return NODE if _runner_of_tokens(script_tokens) == NODE else None
    return _runner_of_tokens(tokens)


def _is_test(path: str, runner: str) -> bool:
    return bool((_PY_TEST_RE if runner == PYTEST else _NODE_TEST_RE).search(path))


def _may_hold_tests(path: str, runner: str) -> bool:
    """A file in the runner's language that the runner could collect tests
    from (pytest never does from `__init__.py` or `conftest.py`)."""
    if runner == PYTEST:
        return path.endswith(".py") and Path(path).name not in ("__init__.py", "conftest.py")
    return bool(_NODE_SOURCE_RE.search(path))


def _shared_dirs(a: str, b: str) -> int:
    n = 0
    for x, y in zip(Path(a).parent.parts, Path(b).parent.parts):
        if x != y:
            break
        n += 1
    return n


def _named_test_files(source: str, files: list[str], runner: str) -> list[str]:
    name = Path(source).name
    if runner == PYTEST:
        if not name.endswith(".py") or name == "conftest.py":
            return []
        stem = name[:-len(".py")]
        wanted = (f"test_{stem}.py", f"{stem}_test.py")
        found = [f for f in files if Path(f).name in wanted]
    else:
        if not _NODE_SOURCE_RE.search(name):
            return []
        stem = name.rsplit(".", 1)[0]
        pattern = re.compile(rf"^{re.escape(stem)}\.(test|spec)\.{_NODE_EXT}$")
        found = [f for f in files if pattern.match(Path(f).name)]
    if not found:
        return []
    # Several packages may each have a test file of that name (a monorepo);
    # the whole command may not collect the others, and pytest refuses two
    # same-named files it cannot tell apart. Take the nearest.
    nearest = max(_shared_dirs(source, f) for f in found)
    return [f for f in found if _shared_dirs(source, f) == nearest]


def select_test_paths(changed: list[str], files: list[str], runner: str | None) -> list[str]:
    """Sorted test paths for *runner*: the changed test files, plus the test
    file named after each changed source file found among *files*. Empty, so
    the whole command runs, when a changed file in the runner's language is
    one the engine counts as a test but this module cannot name: the block's
    own tests would otherwise be left out."""
    if runner not in (PYTEST, NODE):
        return []
    from .tdd_engine import _TEST_PATH_RE

    selected: set[str] = set()
    for path in changed:
        if _is_test(path, runner):
            selected.add(path)
        elif _may_hold_tests(path, runner) and _TEST_PATH_RE.search(path):
            return []
        else:
            selected.update(_named_test_files(path, files, runner))
    return sorted(selected)


def _needs_separator(test_command: str) -> bool:
    tokens = shlex.split(test_command)
    return bool(tokens) and tokens[0] == "npm" and "--" not in tokens


def narrow_command(test_command: str, paths: list[str], runner: str | None) -> str:
    """*test_command* with *paths* appended, or *test_command* unchanged when
    there are no paths, the runner is unknown, or a Node path holds a
    character jest would read as part of a regular expression."""
    if not paths or runner not in (PYTEST, NODE):
        return test_command
    if runner == NODE and any(_REGEX_CHARS_RE.search(p) for p in paths):
        return test_command
    quoted = " ".join(shlex.quote(p) for p in paths)
    if runner == NODE and _needs_separator(test_command):
        return f"{test_command} -- {quoted}"
    return f"{test_command} {quoted}"


def _config_dirs(project_dir: Path) -> list[Path]:
    """*project_dir* and its parents up to the repo root, where the runner's
    config may sit when the project is a subdirectory."""
    dirs = []
    for d in (project_dir, *project_dir.parents):
        dirs.append(d)
        if (d / ".git").exists():
            break
    return dirs


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _coverage_configured(test_command: str, runner: str, project_dir: Path) -> bool:
    """True when the run collects coverage, whose floor (`--cov-fail-under`,
    `coverageThreshold`) the whole suite meets and a narrow run need not."""
    if runner == PYTEST:
        if "--cov" in test_command or "--cov" in os.environ.get("PYTEST_ADDOPTS", ""):
            return True
        return any("--cov" in _read(d / name)
                   for d in _config_dirs(project_dir) for name in _PYTEST_CONFIG_FILES)
    if "coverage" in test_command.lower():
        return True
    if "coverage" in _package_test_script(project_dir).lower():
        return True
    if "coverage" in json.dumps(_package_json(project_dir).get("jest", "")).lower():
        return True
    try:
        names = [p.name for p in project_dir.iterdir()]
    except OSError:
        names = []
    return any(n.startswith(_NODE_CONFIG_PREFIXES) and "coverage" in _read(project_dir / n).lower()
               for n in names)


def _narrowable_runner(test_command: str, project_dir: Path, base: str | None,
                       enabled: bool) -> str | None:
    if not test_command or not enabled or not base:
        return None
    runner = runner_of(test_command, project_dir)
    if runner is None:
        return None
    if _coverage_configured(test_command, runner, project_dir):
        log.info("Narrow tests: the test run collects coverage — running the whole test command")
        return None
    return runner


def _git_lines(project_dir: Path, *args: str) -> list[str]:
    proc = subprocess.run(["git", *args], cwd=str(project_dir), capture_output=True,
                          text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {args[0]} exited {proc.returncode}")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _narrow(test_command: str, project_dir: Path, base: str | None,
            enabled: bool) -> tuple[str, str | None]:
    """The command, and the runner when narrowing applies (None otherwise)."""
    runner = _narrowable_runner(test_command, project_dir, base, enabled)
    if runner is None:
        return test_command, None
    try:
        # Paths relative to the project, which may be a repo subdirectory;
        # deleted files are left out because a runner rejects a missing path.
        changed = _git_lines(project_dir, "diff", "--name-only", "--relative",
                             "--diff-filter=d", base)
        untracked = _git_lines(project_dir, "ls-files", "--others", "--exclude-standard")
        tracked = _git_lines(project_dir, "ls-files")
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        log.warning("Narrow tests: could not list the block's changes (%s) — "
                    "running the whole test command", e)
        return test_command, None
    paths = select_test_paths(sorted(set(changed) | set(untracked)),
                              tracked + untracked, runner)
    # `ls-files` still lists a tracked file deleted but not staged.
    paths = [p for p in paths if (project_dir / p).is_file()]
    command = narrow_command(test_command, paths, runner)
    if command == test_command:
        log.info("Narrow tests: no test file selected — running the whole test command")
    return command, runner


def narrow_test_command(test_command: str, project_dir: Path | str, base: str | None,
                        *, enabled: bool = True) -> str:
    """The command a block's loop runs: *test_command* narrowed to the tests
    the block's changes since *base* select, or *test_command* itself."""
    return _narrow(test_command, Path(project_dir), base, enabled)[0]


def agent_test_command(test_command: str, project_dir: Path | str, base: str | None,
                       *, enabled: bool = True) -> str:
    """The test command a block's agent is given: the narrow command, plus,
    when narrowing applies, how to add the test files the agent writes and
    the test file named after each module it changes. Its command is fixed
    before it changes anything, so it cannot list those itself."""
    command, runner = _narrow(test_command, Path(project_dir), base, enabled)
    if runner is None:
        return command
    named = ("`test_<module>.py` or `<module>_test.py`" if runner == PYTEST
             else "`<name>.test.<ext>` or `<name>.spec.<ext>`")
    where = " after a `--`" if _needs_separator(command) else ""
    if command == test_command:
        scope = ("It runs the whole suite. To check one test file quickly while you "
                 f"work, add its path to the end of the command{where} (the test file "
                 f"for a module is named {named}); run the command as given before "
                 "you finish.")
    else:
        scope = ("It runs only this block's own test files and the tests named after "
                 "the modules the block has changed so far, not the whole suite. When "
                 "you write another test file, or change a module that has a test file "
                 f"named after it ({named}), add that file's path to the end of the "
                 f"command{where}.")
    return f"{command}\n\n{scope} buildme runs the whole suite itself after you finish."
