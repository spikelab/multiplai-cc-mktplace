"""Commit one change dir and nothing else.

Restated from buildme's `git_ops.commit_paths`: the commit stages the change
dir in a copy of the index (`GIT_INDEX_FILE`), commits only that pathspec, and
then brings the real index's entries for those paths up to the new HEAD, so
anything else the user has staged stays staged and out of the commit. A path
whose staged and working versions differ is refused rather than guessed at.
`.pm-spec/` is ignored by its own `.gitignore`, so the run's state is never in
the commit. `shell=False`, fixed argv; never pushes.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

GIT_TIMEOUT_S = 60


class GitError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_S, env=env, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        raise GitError(f"git {args[0]} could not run: {e}") from e


def _index_entries(cwd: Path, rel: str, env: dict | None = None) -> str:
    return _git(["ls-files", "-s", "-z", "--", rel], cwd, env).stdout


def commit_change_dir(project_dir: Path, change_dir: Path, message: str) -> str | None:
    """Commit `change_dir` (inside `project_dir`'s work tree); the new sha, or None when nothing changed."""
    project_dir = Path(project_dir)
    try:
        rel = Path(change_dir).resolve().relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        raise GitError(f"{change_dir} is not inside {project_dir}") from None
    if _git(["rev-parse", "--is-inside-work-tree"], project_dir).stdout.strip() != "true":
        raise GitError(f"{project_dir} is not in a git work tree")
    if not _git(["rev-parse", "--verify", "-q", "HEAD"], project_dir).stdout.strip():
        raise GitError(f"{project_dir} has no commit yet; make a first commit, then run again")
    staged = _git(["diff", "--cached", "--name-only", "-z", "--relative", "--", rel], project_dir)
    working = _git(["diff", "--name-only", "-z", "--relative", "--", rel], project_dir)
    if staged.returncode or working.returncode:
        raise GitError("cannot inspect the change dir's git status: " + (staged.stderr or working.stderr))
    divergent = (set(staged.stdout.split("\0")) & set(working.stdout.split("\0"))) - {""}
    if divergent:
        raise GitError(f"cannot commit {rel}: staged and working versions differ: {', '.join(sorted(divergent))}. "
                       "git add the paths whose working version should be committed, or git restore --staged "
                       "the ones that should stay unstaged, then run again")
    index_path = _git(["rev-parse", "--git-path", "index"], project_dir).stdout.strip()
    original = Path(index_path) if Path(index_path).is_absolute() else project_dir / index_path
    before = _index_entries(project_dir, ".")
    with tempfile.TemporaryDirectory(prefix="pm-spec-index-") as directory:
        temporary = Path(directory) / "index"
        if original.exists():
            shutil.copy2(original, temporary)
        env = dict(os.environ, GIT_INDEX_FILE=str(temporary))
        add = _git(["add", "-A", "--", rel], project_dir, env)
        if add.returncode:
            raise GitError("cannot stage the change dir: " + add.stderr.strip())
        diff = _git(["diff", "--cached", "--quiet", "--", rel], project_dir, env)
        if diff.returncode == 0:
            return None
        if diff.returncode != 1:
            raise GitError("cannot compare the change dir with HEAD: " + diff.stderr.strip())
        commit = _git(["commit", "-m", message, "--", rel], project_dir, env)
        if commit.returncode:
            raise GitError("commit failed: " + (commit.stderr or commit.stdout).strip())
    if _index_entries(project_dir, ".") == before:
        _git(["reset", "-q", "HEAD", "--", rel], project_dir)
    else:
        log.warning("The index changed during the commit; left it as it is")
    sha = _git(["rev-parse", "HEAD"], project_dir).stdout.strip()
    log.info("COMMIT %s sha=%s", message, sha[:8])
    return sha
