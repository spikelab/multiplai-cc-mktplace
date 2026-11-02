"""The checks a build runs before its first paid call (item 33 of
plan-factory-merged-2026-10-29.md).

`run` calls no model. It checks that:

- `specs/config.yaml` parses to a mapping;
- there is a test command, unless `tdd.enabled: false` says to build
  without one (every test gate then runs nothing);
- the program of the test command, and of each `tdd.narrow_commands` entry,
  exists in the directory it runs from;
- the branch the build's PR targets exists on origin (when the repo has an
  `origin` remote and the caller names a target);
- `gh` can open a PR on the repo, only when the run publishes;
- the test database answers: `tdd.db_check`, a command run from the
  project, exits 0 (checked only when it is set);
- no other process holds the epic lock of the change's epic (a component
  build `<epic>/<id>` started by hand; a child the epic launched runs under
  its parent's lock).

Each failed check gives one `Failed`, and the caller prints one line per
failed check and exits before any model call.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import git_ops

log = logging.getLogger(__name__)

# Seconds each check that runs a command (git, gh, `tdd.db_check`) gets.
CHECK_TIMEOUT = 30
# The `viewerPermission` values that let gh push a branch and open a PR.
_CAN_OPEN_PR = ("ADMIN", "MAINTAIN", "WRITE")
# Shell builtins and keywords a `sh -c` / `bash -c` / `zsh -c` script may start
# with. They are not programs on PATH, so the runner check looked up `set` in
# DolceBot's `bash -c 'set -e; ...'` and failed a command that runs. A script
# that starts with one of these (or with an assignment) is checked as its shell.
_SHELL_BUILTINS = frozenset(
    {
        "set", "export", "unset", "cd", "source", ".", ":", "true", "false",
        "exec", "eval", "trap", "ulimit", "umask", "if", "for", "while",
        "until", "case", "{", "(", "[[", "!",
    }
)  # fmt: skip
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class Failed:
    """One failed check: its name and why it failed."""

    check: str
    reason: str

    @property
    def line(self) -> str:
        return f"PREFLIGHT:FAILED:{self.check}:{self.reason}"


def _config_parses(config) -> str | None:
    path = Path(config.specs_dir) / "config.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as e:
        return f"{path} does not parse: {' '.join(str(e).split())}"
    if data is not None and not isinstance(data, dict):
        return f"{path} is not a mapping"
    return None


def _program(command: str, project_dir: Path) -> tuple[str, Path]:
    """The program *command* runs and the directory it runs from, after its
    leading `cd DIR &&` clauses and any `sh -c` / `bash -c` / `zsh -c`
    wrapper. A wrapped script that starts with a shell builtin, keyword or
    assignment, or with a `cd` the shell expands, gives the shell and the
    directory the shell runs from. Raises ValueError for a command `gates`
    would not run."""
    from .gates import _test_invocation

    argv, directory = _test_invocation(command, project_dir)
    while (
        len(argv) == 3
        and Path(argv[0]).name in {"sh", "bash", "zsh"}
        and argv[1] == "-c"
    ):
        shell, outer = argv[0], directory
        try:
            argv, directory = _test_invocation(argv[2], directory)
        except ValueError:
            return shell, outer
        if argv and (
            argv[0].rstrip(";") in _SHELL_BUILTINS
            or argv[0].startswith("(")
            or _ASSIGNMENT_RE.match(argv[0])
        ):
            return shell, outer
    if not argv:
        raise ValueError("the command is empty")
    return argv[0], directory


def _runner_missing(command: str, project_dir: Path) -> str | None:
    try:
        program, directory = _program(command, project_dir)
    except ValueError as e:
        return f"{command!r} cannot be run: {e}"
    if not directory.is_dir():
        return f"{command!r} runs from {directory}, which does not exist"
    if "/" in program:
        path = directory / program
        if not (path.is_file() and os.access(path, os.X_OK)):
            return f"{command!r}: {program} is not an executable file in {directory}"
        return None
    if shutil.which(program) is None:
        return f"{command!r}: {program} is not on PATH"
    return None


#: Why `run` refuses an empty test command.
NO_TEST_COMMAND = (
    "no test command: specs/config.yaml sets no tdd.test_command and discovery "
    "found no root file it knows in {dir}, so every test gate would pass without "
    "running a test. Set tdd.test_command, or tdd.enabled: false to build with "
    "the test gates skipped."
)


def _test_command(config) -> str | None:
    if config.test_command or not getattr(config, "tdd_enabled", True):
        return None
    return NO_TEST_COMMAND.format(dir=config.project_dir)


def _test_runners(config) -> list[str]:
    from .narrow_tests import WHOLE, NarrowPart

    commands = [config.test_command] if config.test_command else []
    commands += [
        NarrowPart(n.dir, n.command, n.runner, WHOLE).shell
        for n in config.narrow_commands
    ]
    reasons = []
    for command in dict.fromkeys(commands):
        reason = _runner_missing(command, Path(config.project_dir))
        if reason:
            reasons.append(reason)
    return reasons


def _target_branch(repo: Path, target: str) -> str | None:
    res = git_ops._run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", target],
        cwd=repo,
        timeout=CHECK_TIMEOUT,
    )
    if res.ok:
        return None
    if res.returncode == 2:
        return f"origin has no branch {target}"
    return f"could not list origin's branches: {_last_line(res)}"


def _gh_permission(repo: Path) -> tuple[str | None, str]:
    """(gh's `viewerPermission` on *repo*'s GitHub repository, or None when
    gh could not answer; the error when it could not)."""
    res = git_ops._run_gh(
        [
            "gh",
            "repo",
            "view",
            "--json",
            "viewerPermission",
            "--jq",
            ".viewerPermission",
        ],
        cwd=repo,
        timeout=CHECK_TIMEOUT,
    )
    if not res.ok:
        return None, _last_line(res)
    return res.stdout.strip(), ""


def _gh_can_open_pr(repo: Path) -> str | None:
    permission, error = _gh_permission(repo)
    if permission is None:
        return f"gh cannot read the repository: {error}"
    if permission not in _CAN_OPEN_PR:
        return f"gh can read the repository but cannot push to it (permission {permission or 'none'})"
    return None


def _test_db(config) -> str | None:
    command = config.db_check
    from .gates import _repo_trusted, _test_invocation

    if not _repo_trusted():
        return "repo not trusted, so tdd.db_check was not run (set --trust-repo)"
    try:
        argv, directory = _test_invocation(command, Path(config.project_dir))
    except ValueError as e:
        return f"tdd.db_check {command!r} cannot be run: {e}"
    res = git_ops._run(argv, cwd=directory, timeout=CHECK_TIMEOUT, errors="replace")
    if res.ok:
        return None
    return f"tdd.db_check {command!r} exited {res.returncode}: {_last_line(res)}"


def _epic_lock(config) -> str | None:
    from .epic import CHILD_ENV, store
    from .epic import lock as epic_lock

    if os.environ.get(CHILD_ENV) == "1":
        return None
    # A child builds as `<epic>/<id>` when nested, `<epic>-<id>` when a
    # sibling change (`phase.child_change`); the epic's name may hold a `-`.
    name = config.change_name
    epic, sep, _ = name.partition("/")
    if sep:
        epics = [epic]
    else:
        epics = [name[:i] for i, c in enumerate(name) if c == "-" and i]
    if not epics:
        return None
    repo = git_ops.repo_root(config.project_dir)
    if repo is None:
        return None
    for epic in epics:
        holder = epic_lock.holder_elsewhere(store.epic_dir_for(repo, epic))
        if holder is not None:
            return f"epic {epic} is running now ({holder})"
    return None


def _last_line(res: git_ops.GitResult) -> str:
    lines = (res.stderr or res.stdout).strip().splitlines()
    return lines[-1].strip() if lines else f"exit {res.returncode}"


def run(config, *, publishes: bool, target: str | None) -> list[Failed]:
    """Every check above that fails, in order. *publishes* says whether the
    run pushes and opens a PR; *target* is the branch that PR targets (None
    skips the branch check)."""
    failed: list[Failed] = []
    reason = _config_parses(config)
    if reason:
        failed.append(Failed("config", reason))
    reason = _test_command(config)
    if reason:
        failed.append(Failed("test-command", reason))
    failed += [Failed("test-runner", r) for r in _test_runners(config)]
    repo = git_ops.repo_root(config.project_dir)
    if target and repo is not None and git_ops.has_remote(repo):
        reason = _target_branch(repo, target)
        if reason:
            failed.append(Failed("target-branch", reason))
    if publishes and repo is not None:
        reason = _gh_can_open_pr(repo)
        if reason:
            failed.append(Failed("gh", reason))
    if config.db_check:
        reason = _test_db(config)
        if reason:
            failed.append(Failed("test-db", reason))
    reason = _epic_lock(config)
    if reason:
        failed.append(Failed("epic-lock", reason))
    for f in failed:
        log.error("PREFLIGHT failed check=%s: %s", f.check, f.reason)
    return failed


def report(failed: list[Failed]) -> None:
    """Print one line per failed check to stdout."""
    for f in failed:
        print(f.line, flush=True)
