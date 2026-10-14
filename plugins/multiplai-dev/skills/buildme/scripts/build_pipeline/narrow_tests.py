"""The narrow test command a block's TDD loop runs instead of the whole suite.

Inside a block, buildme runs (and tells its agents to run) only:

1. the test files the block added or changed, from the block's git diff
   against its start (`BlockInfo.baseline_commit`), untracked files included;
2. for each source file the block changed, the test file named after it
   (`test_<module>.py` / `<module>_test.py` for pytest, `<name>.test.<ext>` /
   `<name>.spec.<ext>` for Node), wherever it sits in the project.

Tests are never selected by import. The paths are appended to the project's
test command only when its runner is known to accept them: pytest, or jest /
vitest run directly or through `npm test --`. Any other command, an empty
selection, `tdd.narrow_tests: false`, no recorded block start, or a git error
gives back the whole command, so the RED gate never passes on an empty run.
The gates that decide whether work is done keep the whole command.
"""
from __future__ import annotations

import json
import logging
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
_JEST_OR_VITEST_RE = re.compile(r"(^|[\s/])(jest|vitest)(\s|$)")
_NPM_TEST = (("npm", "test"), ("npm", "t"), ("npm", "run", "test"))


def _package_test_script(project_dir: Path) -> str:
    try:
        scripts = json.loads((project_dir / "package.json").read_text()).get("scripts")
    except (OSError, ValueError, AttributeError):
        return ""
    script = scripts.get("test") if isinstance(scripts, dict) else None
    return script if isinstance(script, str) else ""


def runner_of(test_command: str, project_dir: Path) -> str | None:
    """`"pytest"`, `"node"` (jest or vitest), or None when the command's runner
    is not one known to accept test paths."""
    try:
        tokens = shlex.split(test_command)
    except ValueError:
        return None
    names = [Path(t).name for t in tokens]
    if any(n in ("pytest", "py.test") for n in names):
        return PYTEST
    if any(n in ("jest", "vitest") for n in names):
        return NODE
    if any(tuple(tokens[:len(npm)]) == npm for npm in _NPM_TEST):
        if _JEST_OR_VITEST_RE.search(_package_test_script(Path(project_dir))):
            return NODE
    return None


def _is_test(path: str, runner: str) -> bool:
    return bool((_PY_TEST_RE if runner == PYTEST else _NODE_TEST_RE).search(path))


def _named_test_files(source: str, files: list[str], runner: str) -> list[str]:
    name = Path(source).name
    if runner == PYTEST:
        if not name.endswith(".py") or name == "conftest.py":
            return []
        stem = name[:-len(".py")]
        wanted = (f"test_{stem}.py", f"{stem}_test.py")
        return [f for f in files if Path(f).name in wanted]
    if not _NODE_SOURCE_RE.search(name):
        return []
    stem = name.rsplit(".", 1)[0]
    pattern = re.compile(rf"^{re.escape(stem)}\.(test|spec)\.{_NODE_EXT}$")
    return [f for f in files if pattern.match(Path(f).name)]


def select_test_paths(changed: list[str], files: list[str], runner: str | None) -> list[str]:
    """Sorted test paths for *runner*: the changed test files, plus the test
    file named after each changed source file found among *files*."""
    if runner not in (PYTEST, NODE):
        return []
    selected: set[str] = set()
    for path in changed:
        if _is_test(path, runner):
            selected.add(path)
        else:
            selected.update(_named_test_files(path, files, runner))
    return sorted(selected)


def narrow_command(test_command: str, paths: list[str], runner: str | None) -> str:
    """*test_command* with *paths* appended, or *test_command* unchanged when
    there are no paths or the runner is unknown."""
    if not paths or runner not in (PYTEST, NODE):
        return test_command
    quoted = " ".join(shlex.quote(p) for p in paths)
    tokens = shlex.split(test_command)
    if runner == NODE and tokens[0] == "npm" and "--" not in tokens:
        return f"{test_command} -- {quoted}"
    return f"{test_command} {quoted}"


def _git_lines(project_dir: Path, *args: str) -> list[str]:
    proc = subprocess.run(["git", *args], cwd=str(project_dir), capture_output=True,
                          text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {args[0]} exited {proc.returncode}")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def narrow_test_command(test_command: str, project_dir: Path | str, base: str | None,
                        *, enabled: bool = True) -> str:
    """The command a block's loop runs: *test_command* narrowed to the tests
    the block's changes since *base* select, or *test_command* itself."""
    if not test_command or not enabled or not base:
        return test_command
    project_dir = Path(project_dir)
    runner = runner_of(test_command, project_dir)
    if runner is None:
        return test_command
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
        return test_command
    paths = select_test_paths(sorted(set(changed) | set(untracked)),
                              tracked + untracked, runner)
    command = narrow_command(test_command, paths, runner)
    if command == test_command:
        log.info("Narrow tests: no test file selected — running the whole test command")
    return command
