"""Commit pm-spec's own files in one change dir and nothing else.

Restated from buildme's `git_ops.commit_paths`: the commit stages the change
dir in a copy of the index (`GIT_INDEX_FILE`), commits only the paths it
changed, and then brings the real index's entries for those paths up to the
new HEAD, so anything else the user has staged stays staged and out of the
commit. A path whose staged and working versions differ is refused rather
than guessed at. `.pm-spec/` is ignored by its own `.gitignore`, so the run's
state is never in the commit.

Only the files pm-spec owns are committed (`owned`): the change dir holds
buildme's files too when `revise` runs in a build's worktree, and a revise on
2026-10-23 committed buildme's `.build-state.json` and `.board.json` with the
specs. `shell=False`, fixed argv; never pushes (`branch.push` does).
"""

from __future__ import annotations

import fnmatch
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

GIT_TIMEOUT_S = 60

# The files pm-spec writes in a change dir or a component's dir. The issues
# files are buildme's, and `revise` moves them aside (`*.resolved-*.md`).
OWNED_FILES = (
    ".change.yaml",
    "prd.md",
    "ticket.md",
    "interview-questions.md",
    "interview.md",
    "proposal.md",
    "use-cases.md",
    "components.md",
    "plan.md",
    "tickets.json",
    "specs.md",
    "spec-issues.md",
    "spec-issues.resolved-*.md",
    "respec.md",
    "respec.resolved-*.md",
)
# Directories pm-spec owns whole: the prototype, and an old-layout change's
# capability files.
OWNED_DIRS = ("prototype", "requirements")


class GitError(RuntimeError):
    pass


def _git(
    args: list[str], cwd: Path, env: dict | None = None, timeout: int = GIT_TIMEOUT_S
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise GitError(f"git {args[0]} could not run: {e}") from e


def _index_entries(cwd: Path, rel: str, env: dict | None = None) -> str:
    return _git(["ls-files", "-s", "-z", "--", rel], cwd, env).stdout


def owned(rel: str) -> bool:
    """Whether *rel*, a path relative to a change dir, is a file pm-spec
    writes: one of `OWNED_FILES` in the change dir or in a component's
    (`components/<id>/`), or anything under one of their `OWNED_DIRS`."""
    parts = PurePosixPath(rel).parts
    if len(parts) > 2 and parts[0] == "components":
        parts = parts[2:]
    if len(parts) > 1:
        return parts[0] in OWNED_DIRS
    return (
        any(fnmatch.fnmatchcase(parts[0], p) for p in OWNED_FILES) if parts else False
    )


def committed_text(project_dir: Path, path: Path) -> str | None:
    """The text of *path* (inside `project_dir`) in HEAD's commit; None when HEAD
    does not hold it, or there is no HEAD."""
    project_dir = Path(project_dir)
    try:
        rel = Path(path).resolve().relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        raise GitError(f"{path} is not inside {project_dir}") from None
    shown = _git(["show", f"HEAD:./{rel}"], project_dir)
    return shown.stdout if shown.returncode == 0 else None


def commit_change_dir(project_dir: Path, change_dir: Path, message: str) -> str | None:
    """Commit pm-spec's files in `change_dir` (inside `project_dir`'s work
    tree, `owned`); the new sha, or None when none changed. `change_dir` may
    also be one file in a change dir, which is then all the commit holds."""
    project_dir = Path(project_dir)
    try:
        rel = Path(change_dir).resolve().relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        raise GitError(f"{change_dir} is not inside {project_dir}") from None
    top = PurePosixPath(rel) if Path(change_dir).is_dir() else PurePosixPath(rel).parent

    def mine(path: str) -> bool:
        return owned(PurePosixPath(path).relative_to(top).as_posix())

    if (
        _git(["rev-parse", "--is-inside-work-tree"], project_dir).stdout.strip()
        != "true"
    ):
        raise GitError(f"{project_dir} is not in a git work tree")
    if not _git(["rev-parse", "--verify", "-q", "HEAD"], project_dir).stdout.strip():
        raise GitError(
            f"{project_dir} has no commit yet; make a first commit, then run again"
        )
    staged = _git(
        ["diff", "--cached", "--name-only", "-z", "--relative", "--", rel], project_dir
    )
    working = _git(["diff", "--name-only", "-z", "--relative", "--", rel], project_dir)
    if staged.returncode or working.returncode:
        raise GitError(
            "cannot inspect the change dir's git status: "
            + (staged.stderr or working.stderr)
        )
    divergent = {
        p
        for p in set(staged.stdout.split("\0")) & set(working.stdout.split("\0"))
        if p and mine(p)
    }
    if divergent:
        raise GitError(
            f"cannot commit {rel}: staged and working versions differ: {', '.join(sorted(divergent))}. "
            "git add the paths whose working version should be committed, or git restore --staged "
            "the ones that should stay unstaged, then run again"
        )
    index_path = _git(["rev-parse", "--git-path", "index"], project_dir).stdout.strip()
    original = (
        Path(index_path) if Path(index_path).is_absolute() else project_dir / index_path
    )
    before = _index_entries(project_dir, ".")
    with tempfile.TemporaryDirectory(prefix="pm-spec-index-") as directory:
        temporary = Path(directory) / "index"
        if original.exists():
            shutil.copy2(original, temporary)
        env = dict(os.environ, GIT_INDEX_FILE=str(temporary))
        add = _git(["add", "-A", "--", rel], project_dir, env)
        if add.returncode:
            raise GitError("cannot stage the change dir: " + add.stderr.strip())
        diff = _git(
            [
                "diff",
                "--cached",
                "--no-renames",
                "--name-only",
                "-z",
                "--relative",
                "--",
                rel,
            ],
            project_dir,
            env,
        )
        if diff.returncode:
            raise GitError(
                "cannot compare the change dir with HEAD: " + diff.stderr.strip()
            )
        changed = [p for p in diff.stdout.split("\0") if p]
        paths = [p for p in changed if mine(p)]
        if skipped := sorted(set(changed) - set(paths)):
            log.info("Not pm-spec's, left out of the commit: %s", ", ".join(skipped))
        if not paths:
            return None
        pathspecs = [f":(literal){p}" for p in paths]
        commit = _git(["commit", "-m", message, "--", *pathspecs], project_dir, env)
        if commit.returncode:
            raise GitError("commit failed: " + (commit.stderr or commit.stdout).strip())
    if _index_entries(project_dir, ".") == before:
        _git(["reset", "-q", "HEAD", "--", *pathspecs], project_dir)
    else:
        log.warning("The index changed during the commit; left it as it is")
    sha = _git(["rev-parse", "HEAD"], project_dir).stdout.strip()
    log.info("COMMIT %s sha=%s", message, sha[:8])
    return sha
