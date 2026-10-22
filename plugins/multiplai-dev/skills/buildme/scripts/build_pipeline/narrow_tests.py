"""The narrow test command a block's TDD loop runs instead of the whole suite.

Inside a block, buildme runs (and tells its agents to run) only:

1. the test files the block added or changed, from the block's git diff
   against its start (`BlockInfo.baseline_commit`), untracked files included;
2. for each source file the block changed, the test file named after it
   (`test_<module>.py` / `<module>_test.py` for pytest, `<name>.test.<ext>` /
   `<name>.spec.<ext>` for Node), the one nearest the source file when the
   project has several.

Only files the whole command collects are taken: never one under a tool or
environment directory (`.venv`, `node_modules`), and for pytest only one under
its `testpaths` and outside its `norecursedirs` and `--ignore`, because pytest
runs any file named on its command line. Tests are never selected by import.

The paths are appended to the project's test command only when its runner is
known to accept them: pytest, or jest / vitest run directly or through
`npm test --`. The whole command is given back for: any other command (a task
runner such as tox, even behind `uv run`, or a chained command); a command
that already names what to run (paths, a jest / vitest filter, pytest
`addopts` paths, another pytest config or rootdir, `python_files`); a pytest
config it cannot read; a selected pytest file below another pytest config or
`setup.py` than the whole run's (pytest finds its config from the paths it is
given), or under a `conftest.py` pytest loads whose `collect_ignore` or
`collect_ignore_glob` could cover it, or that has a `pytest_ignore_collect`
hook (pytest does not apply them to a path named on its command line); a
selection too long for one command; an empty
selection; a changed file the engine counts as a test but this module cannot
name (Django's `tests.py`, a `__tests__/` file, a helper under `tests/`); a
coverage setting (a narrow run misses a coverage floor the whole suite meets);
a Node path jest would read as a regex; `tdd.narrow_tests: false`; no recorded
block start; or a git error. So the RED gate never passes on a run that left
out the block's own tests. The gates that decide whether work is done keep the
whole command.

An agent's command is fixed before it changes anything, so `agent_test_command`
adds how to run the test files it writes and the test file named after each
module it changes.
"""

from __future__ import annotations

import ast
import configparser
import fnmatch
import json
import logging
import os
import re
import shlex
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path, PurePosixPath

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
# The order pytest (9) looks for its config in each directory, and the INI
# section each file needs (None for a TOML file). pytest.toml and pytest.ini
# count even without one.
_PYTEST_INI_ORDER = (
    ("pytest.toml", None),
    (".pytest.toml", None),
    ("pytest.ini", "pytest"),
    (".pytest.ini", "pytest"),
    ("pyproject.toml", None),
    ("tox.ini", "pytest"),
    ("setup.cfg", "tool:pytest"),
)
_PYTEST_CONFIG_FILES = tuple(name for name, _ in _PYTEST_INI_ORDER)
# What in a conftest.py makes pytest skip files the whole run would collect.
# pytest never applies it to a path named on its command line.
_CONFTEST_IGNORE_HOOK = "pytest_ignore_collect"
_CONFTEST_IGNORE_LISTS = ("collect_ignore", "collect_ignore_glob")
_PYTEST_DEFAULT_NORECURSE = (
    "*.egg",
    ".*",
    "_darcs",
    "build",
    "CVS",
    "dist",
    "node_modules",
    "venv",
    "{arch}",
)
# Options whose value is a path that is not a test to run.
_PYTEST_PATH_VALUE_OPTS = ("--ignore", "--ignore-glob", "--deselect")
# Options that point pytest at another config or root than the one read here,
# or read the arguments as package names.
_PYTEST_OTHER_ROOT_OPTS = ("--rootdir", "--config-file", "--inifile", "--pyargs", "-c")
# vitest subcommands that take no test filter themselves.
_VITEST_SUBCOMMANDS = ("run", "watch", "dev", "bench")
# jest / vitest options whose value is not a test filter.
_NODE_VALUE_OPTS = frozenset(
    {
        "--config",
        "-c",
        "--maxWorkers",
        "-w",
        "--reporters",
        "--reporter",
        "--shard",
        "--environment",
        "--env",
        "--testTimeout",
        "--pool",
        "--root",
        "-r",
        "--dir",
        "--mode",
        "--project",
        "--selectProjects",
        "--outputFile",
        "--testNamePattern",
        "-t",
    }
)
# jest / vitest options that already choose which test files run.
_NODE_FILTER_OPTS = (
    "--testPathPattern",
    "--testPathPatterns",
    "--findRelatedTests",
    "--related",
)
# A longer command is not narrowed: it goes into agents' prompts, and one argv
# string past the OS limit would make run_test_suite raise.
_MAX_COMMAND_CHARS = 20_000
_NODE_CONFIG_PREFIXES = (
    "jest.config.",
    "vitest.config.",
    "vite.config.",
    "vitest.workspace.",
)


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
    if any(tuple(tokens[: len(npm)]) == npm for npm in _NPM_TEST):
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
        return path.endswith(".py") and Path(path).name not in (
            "__init__.py",
            "conftest.py",
        )
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
        stem = name[: -len(".py")]
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


def select_test_paths(
    changed: list[str],
    files: list[str],
    runner: str | None,
    collected: Callable[[str], bool] | None = None,
) -> list[str]:
    """Sorted test paths for *runner*: the changed test files, plus the test
    file named after each changed source file found among *files*. Only paths
    the whole command collects count: never one under a tool or environment
    directory, and none that *collected* rejects. Empty, so the whole command
    runs, when a collected changed file in the runner's language is one the
    engine counts as a test but this module cannot name: the block's own
    tests would otherwise be left out."""
    if runner not in (PYTEST, NODE):
        return []
    from .tdd_engine import _TEST_PATH_RE, _is_tool_cache

    def runs(path: str) -> bool:
        return not _is_tool_cache(path) and (collected is None or collected(path))

    pool = [f for f in files if runs(f)]
    selected: set[str] = set()
    for path in changed:
        if _is_tool_cache(path):
            continue
        if _is_test(path, runner):
            if runs(path):
                selected.add(path)
        elif _may_hold_tests(path, runner) and _TEST_PATH_RE.search(path):
            if runs(path):
                return []
        else:
            selected.update(_named_test_files(path, pool, runner))
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
        command = f"{test_command} -- {quoted}"
    else:
        command = f"{test_command} {quoted}"
    return command if len(command) <= _MAX_COMMAND_CHARS else test_command


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
        return any(
            "--cov" in _read(d / name)
            for d in _config_dirs(project_dir)
            for name in _PYTEST_CONFIG_FILES
        )
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
    return any(
        n.startswith(_NODE_CONFIG_PREFIXES)
        and "coverage" in _read(project_dir / n).lower()
        for n in names
    )


class _UnreadableConfig(Exception):
    pass


def _pytest_section(path: Path, section: str | None) -> dict | None:
    """The pytest options in *path*, or None when it holds no pytest section."""
    text = _read(path)
    if section is None:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise _UnreadableConfig(f"{path}: {e}") from e
        if path.name in ("pytest.toml", ".pytest.toml"):
            table = data.get("pytest")
            return table if isinstance(table, dict) else {}
        tool = data.get("tool", {})
        table = tool.get("pytest") if isinstance(tool, dict) else None
        if not isinstance(table, dict):
            return None
        ini = table.get("ini_options")
        if isinstance(ini, dict):
            return ini
        return {k: v for k, v in table.items() if k != "ini_options"} or None
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error as e:
        raise _UnreadableConfig(f"{path}: {e}") from e
    if parser.has_section(section):
        return dict(parser[section])
    return {} if path.name in ("pytest.ini", ".pytest.ini") else None


def _pytest_config(project_dir: Path) -> tuple[Path, dict] | None:
    """The directory and options of the config pytest picks when run in
    *project_dir*: the first one found from there up to the repo root."""
    for d in _config_dirs(project_dir):
        for name, section in _PYTEST_INI_ORDER:
            if (d / name).is_file():
                options = _pytest_section(d / name, section)
                if options is not None:
                    return d, options
    return None


def _pytest_setup(start: Path) -> Path | None:
    """What sets pytest's config and rootdir when its search starts in
    *start*: the first config file with pytest settings from there up, else
    the first `pyproject.toml`, else the nearest `setup.py` (whose directory
    becomes the rootdir, and no conftest.py above it is loaded)."""
    dirs = _config_dirs(start)
    pyproject = None
    for d in dirs:
        for name, section in _PYTEST_INI_ORDER:
            path = d / name
            if path.is_file():
                if name == "pyproject.toml" and pyproject is None:
                    pyproject = path
                if _pytest_section(path, section) is not None:
                    return path
    if pyproject is not None:
        return pyproject
    return next((d / "setup.py" for d in dirs if (d / "setup.py").is_file()), None)


def _ignore_lists(tree: ast.Module) -> dict[str, list[str]] | None:
    """The `collect_ignore` / `collect_ignore_glob` entries a conftest's
    *tree* writes out, or None when it has a `pytest_ignore_collect` hook or
    sets or changes a list in any other way than one top-level assignment
    of a list or tuple of strings."""
    lists: dict[str, list[str]] = {}
    written: set[int] = set()
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in _CONFTEST_IGNORE_LISTS
        ):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError, RecursionError):
                return None
            if not isinstance(value, (list, tuple)) or not all(
                isinstance(v, str) for v in value
            ):
                return None
            lists.setdefault(node.targets[0].id, []).extend(value)
            written.add(id(node.targets[0]))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and id(node) not in written
            and (node.id in _CONFTEST_IGNORE_LISTS or node.id == _CONFTEST_IGNORE_HOOK)
        ):
            return None
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == _CONFTEST_IGNORE_HOOK
        ):
            return None
        if isinstance(node, ast.alias) and _CONFTEST_IGNORE_HOOK in (
            node.name,
            node.asname,
        ):
            return None
    return lists


def _conftest_ignores(conftest: Path, selected: Path) -> bool:
    """True when *conftest* could make the whole run skip *selected*, a
    file below its directory. A mention in a comment or a string ignores
    nothing; a hook, a list that is not written out, or a conftest that does
    not parse counts as ignoring it."""
    text = _read(conftest)
    if not any(
        name in text for name in (_CONFTEST_IGNORE_HOOK, *_CONFTEST_IGNORE_LISTS)
    ):
        return False
    try:
        lists = _ignore_lists(ast.parse(text))
    except (SyntaxError, ValueError, RecursionError):
        return True
    if lists is None:
        return True
    here = Path(os.path.abspath(conftest.parent))
    # pytest checks each directory on the way down, then the file.
    walked = [Path(os.path.abspath(selected))]
    walked += [p for p in walked[0].parents if here in p.parents]
    ignored = {
        Path(os.path.normpath(here / entry))
        for entry in lists.get("collect_ignore", [])
    }
    globs = [str(here / entry) for entry in lists.get("collect_ignore_glob", [])]
    return any(
        p in ignored or any(fnmatch.fnmatch(str(p), g) for g in globs) for p in walked
    )


def _pytest_runs_alike(paths: list[str], project_dir: Path) -> str | None:
    """Why the narrow pytest run of *paths* could behave unlike the whole
    run, or None. pytest finds its config from the directories of the paths
    it is given (the whole run starts from *project_dir*), and does not
    apply a conftest's `collect_ignore`, `collect_ignore_glob` or
    `pytest_ignore_collect` to those paths. It loads no conftest above the
    directory of its config file (else its rootdir), the whole run's
    `confcutdir`."""
    try:
        whole = _pytest_setup(project_dir)
        cut = whole.parent if whole is not None else project_dir
        for path in paths:
            start = (project_dir / path).parent
            if _pytest_setup(start) != whole:
                return f"{path} is below another pytest config or setup.py"
            for d in _config_dirs(start):
                if _conftest_ignores(d / "conftest.py", project_dir / path):
                    return f"{d / 'conftest.py'} may make the whole run skip {path}"
                if d == cut:
                    break
    except _UnreadableConfig as e:
        return f"cannot read the pytest config ({e})"
    return None


def _words(value: object, *, shell: bool = False) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if not isinstance(value, str):
        return []
    return shlex.split(value) if shell else value.split()


def _option_values(tokens: list[str], option: str) -> list[str]:
    values = []
    for i, token in enumerate(tokens):
        if token == option and i + 1 < len(tokens):
            values.append(tokens[i + 1])
        elif token.startswith(option + "="):
            values.append(token[len(option) + 1 :])
    return values


def _relative(value: str, project_dir: Path) -> str | None:
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(project_dir)
        except ValueError:
            return None
    parts = PurePosixPath(path.as_posix()).parts
    return "/".join(p for p in parts if p != ".")


def _under(parts: tuple[str, ...], root: str) -> int | None:
    """How many leading parts of *parts* *root* covers, or None if it does not."""
    root_parts = PurePosixPath(root).parts if root else ()
    return len(root_parts) if parts[: len(root_parts)] == root_parts else None


def _norecurse(parts: tuple[str, ...], depth: int, patterns: list[str]) -> bool:
    """True when a directory of *parts* below the first *depth* matches one of
    pytest's `norecursedirs` patterns: by name, or by path when the pattern
    holds a `/`."""
    for j in range(depth, len(parts) - 1):
        for pattern in patterns:
            if "/" in pattern:
                if fnmatch.fnmatch(
                    "/" + "/".join(parts[: j + 1]), "*/" + pattern.strip("/")
                ):
                    return True
            elif fnmatch.fnmatch(parts[j], pattern):
                return True
    return False


def _pytest_collects(
    test_command: str, project_dir: Path
) -> Callable[[str], bool] | None:
    """Which project-relative paths the whole pytest command collects, or None
    when it cannot tell: the command (or `addopts`) already names paths, or
    points pytest at another config or root, the project sets its own
    `python_files`, or its config cannot be read."""
    tokens = shlex.split(test_command)
    at = next(i for i, t in enumerate(tokens) if Path(t).name in ("pytest", "py.test"))
    try:
        found = _pytest_config(project_dir)
        config_dir, options = found if found else (None, {})
        args = (
            _words(options.get("addopts"), shell=True)
            + shlex.split(os.environ.get("PYTEST_ADDOPTS", ""))
            + tokens[at + 1 :]
        )
    except (_UnreadableConfig, ValueError) as e:
        log.info(
            "Narrow tests: cannot read the pytest config (%s) — running the whole "
            "test command",
            e,
        )
        return None
    if options.get("python_files"):
        return None
    if any(t.startswith(_PYTEST_OTHER_ROOT_OPTS) for t in args):
        return None
    skip = False
    for token in args:
        if skip:
            skip = False
        elif token in _PYTEST_PATH_VALUE_OPTS:
            skip = True
        elif (
            token
            and not token.startswith("-")
            and (project_dir / token.split("::")[0]).exists()
        ):
            return None
    # pytest uses testpaths only when it runs in the config's own directory,
    # and falls back to that directory when none of them exists.
    roots = [""]
    if config_dir is not None and config_dir.resolve() == project_dir.resolve():
        try:
            matched = sorted(
                {
                    p.relative_to(project_dir).as_posix()
                    for pattern in _words(options.get("testpaths"))
                    for p in project_dir.glob(pattern)
                }
            )
        except (ValueError, NotImplementedError):
            return None
        roots = matched or roots
    norecurse = _words(options.get("norecursedirs")) or list(_PYTEST_DEFAULT_NORECURSE)
    ignored = [
        r
        for r in (_relative(v, project_dir) for v in _option_values(args, "--ignore"))
        if r is not None
    ]
    ignore_globs = _option_values(args, "--ignore-glob")

    def collects(path: str) -> bool:
        parts = PurePosixPath(path).parts
        if any(_under(parts, i) is not None for i in ignored if i):
            return False
        if any(fnmatch.fnmatch(path, g) for g in ignore_globs):
            return False
        for root in roots:
            depth = _under(parts, root)
            if depth is not None and not _norecurse(parts, depth, norecurse):
                return True
        return False

    return collects


def _node_names_tests(test_command: str, project_dir: Path) -> bool:
    """True when the jest / vitest command already filters which test files
    run: appended paths would widen that filter, not narrow the run."""
    tokens = shlex.split(test_command)
    if any(tuple(tokens[: len(npm)]) == npm for npm in _NPM_TEST):
        extra = tokens[tokens.index("--") + 1 :] if "--" in tokens else []
        tokens = shlex.split(_package_test_script(project_dir)) + extra
    at = next(i for i, t in enumerate(tokens) if Path(t).name in ("jest", "vitest"))
    args = tokens[at + 1 :]
    if (
        Path(tokens[at]).name == "vitest"
        and args[:1]
        and args[0] in _VITEST_SUBCOMMANDS
    ):
        args = args[1:]
    skip = False
    for token in args:
        if skip:
            skip = False
        elif token in _NODE_VALUE_OPTS:
            skip = True
        elif token.startswith(_NODE_FILTER_OPTS) or not token.startswith("-"):
            return True
    return False


def _narrowable_runner(
    test_command: str,
    project_dir: Path,
    base: str | None,
    enabled: bool,
) -> tuple[str, Callable[[str], bool] | None] | None:
    """The runner, and which paths its whole command collects (None when
    any path it runs counts), or None when the command is not narrowed."""
    if not test_command or not enabled or not base:
        return None
    runner = runner_of(test_command, project_dir)
    if runner is None:
        return None
    if _coverage_configured(test_command, runner, project_dir):
        log.info(
            "Narrow tests: the test run collects coverage — running the whole test command"
        )
        return None
    if runner == NODE:
        if _node_names_tests(test_command, project_dir):
            log.info(
                "Narrow tests: the command already filters its tests — running it whole"
            )
            return None
        return runner, None
    collected = _pytest_collects(test_command, project_dir)
    if collected is None:
        log.info(
            "Narrow tests: cannot tell which files the whole pytest command collects "
            "— running it whole"
        )
        return None
    return runner, collected


def _git_lines(project_dir: Path, *args: str) -> list[str]:
    proc = subprocess.run(
        ["git", *args], cwd=str(project_dir), capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.strip() or f"git {args[0]} exited {proc.returncode}"
        )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _narrow(
    test_command: str, project_dir: Path, base: str | None, enabled: bool
) -> tuple[str, str | None]:
    """The command, and the runner when narrowing applies (None otherwise)."""
    narrowable = _narrowable_runner(test_command, project_dir, base, enabled)
    if narrowable is None:
        return test_command, None
    runner, collected = narrowable
    try:
        # Paths relative to the project, which may be a repo subdirectory;
        # deleted files are left out because a runner rejects a missing path.
        changed = _git_lines(
            project_dir, "diff", "--name-only", "--relative", "--diff-filter=d", base
        )
        untracked = _git_lines(
            project_dir, "ls-files", "--others", "--exclude-standard"
        )
        tracked = _git_lines(project_dir, "ls-files")
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        log.warning(
            "Narrow tests: could not list the block's changes (%s) — "
            "running the whole test command",
            e,
        )
        return test_command, None
    paths = select_test_paths(
        sorted(set(changed) | set(untracked)), tracked + untracked, runner, collected
    )
    # `ls-files` still lists a tracked file deleted but not staged.
    paths = [p for p in paths if (project_dir / p).is_file()]
    if paths and runner == PYTEST:
        reason = _pytest_runs_alike(paths, project_dir)
        if reason is not None:
            log.info("Narrow tests: %s — running the whole test command", reason)
            return test_command, None
    command = narrow_command(test_command, paths, runner)
    if command == test_command:
        log.info("Narrow tests: no test file selected — running the whole test command")
    return command, runner


def narrow_test_command(
    test_command: str,
    project_dir: Path | str,
    base: str | None,
    *,
    enabled: bool = True,
) -> str:
    """The command a block's loop runs: *test_command* narrowed to the tests
    the block's changes since *base* select, or *test_command* itself."""
    return _narrow(test_command, Path(project_dir), base, enabled)[0]


def agent_test_command(
    test_command: str,
    project_dir: Path | str,
    base: str | None,
    *,
    enabled: bool = True,
) -> str:
    """The test command a block's agent is given: the narrow command, plus,
    when narrowing applies, how to add the test files the agent writes and
    the test file named after each module it changes. Its command is fixed
    before it changes anything, so it cannot list those itself."""
    command, runner = _narrow(test_command, Path(project_dir), base, enabled)
    if runner is None:
        return command
    named = (
        "`test_<module>.py` or `<module>_test.py`"
        if runner == PYTEST
        else "`<name>.test.<ext>` or `<name>.spec.<ext>`"
    )
    where = " after a `--`" if _needs_separator(command) else ""
    if command == test_command:
        scope = (
            "It runs the whole suite. To check one test file quickly while you "
            f"work, add its path to the end of the command{where} (the test file "
            f"for a module is named {named}); run the command as given before "
            "you finish."
        )
    else:
        scope = (
            "It runs only this block's own test files and the tests named after "
            "the modules the block has changed so far, not the whole suite. When "
            "you write another test file, or change a module that has a test file "
            f"named after it ({named}), add that file's path to the end of the "
            f"command{where}."
        )
    return f"{command}\n\n{scope} buildme runs the whole suite itself after you finish."
