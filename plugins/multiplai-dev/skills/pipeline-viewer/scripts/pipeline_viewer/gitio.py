"""Git, read side, plus the one argv runner `record.py` writes through.

Fixed argv, no shell, stdin closed. Every call runs with
`-c color.ui=never -c core.quotepath=off` so user config cannot change what
is parsed.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT_S = 60
# `review(<change>): ...` is the subject every decision commit carries,
# here and in buildme's `--approve-plan`.
SUBJECT_PREFIX = "review({change})"


class GitError(RuntimeError):
    pass


def run(
    cwd: Path | str, *args: str, check: bool = True, env: dict | None = None
) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.pop("GIT_EXTERNAL_DIFF", None)
    if env:
        full_env.update(env)
    try:
        res = subprocess.run(
            ["git", "-c", "color.ui=never", "-c", "core.quotepath=off", *args],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=GIT_TIMEOUT_S,
            env=full_env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"git {args[0]} failed: {exc}") from None
    if check and res.returncode != 0:
        raise GitError(
            f"git {' '.join(args[:2])}: {res.stderr.strip() or res.stdout.strip()}"
        )
    return res


def ok(cwd: Path | str, *args: str) -> str | None:
    """stdout (stripped) when git exits 0, else None."""
    try:
        res = run(cwd, *args, check=False)
    except GitError:
        return None
    return res.stdout.strip() if res.returncode == 0 else None


@dataclass
class Worktree:
    path: Path
    branch: str | None  # short name; None when detached
    head: str | None


def worktrees(repo: Path) -> list[Worktree]:
    """Every checkout of *repo* (the main one first), from `git worktree list`."""
    out = ok(repo, "worktree", "list", "--porcelain")
    if out is None:
        return []
    found: list[Worktree] = []
    cur: dict = {}
    for line in [*out.splitlines(), ""]:
        if not line:
            if cur.get("worktree"):
                br = cur.get("branch")
                found.append(
                    Worktree(
                        Path(cur["worktree"]),
                        br.removeprefix("refs/heads/") if br else None,
                        cur.get("HEAD"),
                    )
                )
            cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value or True
    return found


def current_branch(cwd: Path) -> str | None:
    return ok(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")


def upstream(cwd: Path, branch: str | None) -> str | None:
    """The ref the checkout pulls from and pushes to: the branch's upstream,
    or `origin/<branch>` for a detached checkout of a known branch (pm-spec's
    worktree) when that remote-tracking ref exists."""
    if current_branch(cwd):
        return ok(
            cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
        )
    if branch and ok(
        cwd, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"
    ):
        return f"origin/{branch}"
    return None


def ahead(cwd: Path, up: str | None) -> int:
    if not up:
        return 0
    out = ok(cwd, "rev-list", "--count", f"{up}..HEAD")
    return int(out) if out and out.isdigit() else 0


def parse_trailers(body: str) -> dict[str, str]:
    """`Key: value` lines, keys lower-cased; a repeated key keeps its last value."""
    out: dict[str, str] = {}
    for line in body.splitlines():
        key, colon, value = line.partition(":")
        if colon and key.strip() and " " not in key.strip():
            out[key.strip().lower()] = value.strip()
    return out


@dataclass
class Commit:
    sha: str
    author: str
    at: str
    subject: str
    trailers: dict[str, str]


def review_commits(cwd: Path, change: str) -> list[Commit]:
    """The change's decision commits reachable from HEAD, oldest first."""
    out = ok(
        cwd,
        "log",
        "HEAD",
        "--reverse",
        "--format=%H%x1f%an%x1f%cI%x1f%s%x1f%(trailers:only,unfold)%x1e",
    )
    if not out:
        return []
    prefix = SUBJECT_PREFIX.format(change=change)
    commits = []
    for record in out.split("\x1e"):
        parts = record.strip("\n").split("\x1f")
        if len(parts) != 5 or not parts[3].startswith(prefix):
            continue
        sha, author, at, subject, body = parts
        commits.append(Commit(sha.strip(), author, at, subject, parse_trailers(body)))
    return commits


def tree_revision(cwd: Path, paths: list[str]) -> str | None:
    """A sha1 over the committed blobs of *paths* (relative to *cwd*) at
    HEAD; None when none of them is committed."""
    if not paths:
        return None
    out = ok(cwd, "ls-tree", "-r", "--full-tree", "HEAD", "--", *paths)
    if not out:
        return None
    lines = sorted(out.splitlines())
    return hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()


def last_commit_time(cwd: Path, paths: list[str]) -> str | None:
    if not paths:
        return None
    return ok(cwd, "log", "-1", "--format=%cI", "HEAD", "--", *paths) or None


def dirty(cwd: Path, paths: list[str]) -> list[str]:
    """Paths (relative to the repo top) among *paths* with uncommitted edits,
    staged or not. Untracked files count."""
    if not paths:
        return []
    try:
        res = run(
            cwd,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            *paths,
            check=False,
        )
    except GitError:
        return []
    if res.returncode != 0:
        return []
    # Not stripped: each line's first column is a status letter or a space.
    return sorted(
        {line[3:].strip() for line in res.stdout.splitlines() if len(line) > 3}
    )


def toplevel(cwd: Path) -> Path | None:
    out = ok(cwd, "rev-parse", "--show-toplevel")
    return Path(out) if out else None


def common_dir(cwd: Path) -> Path | None:
    """The repository's shared git dir, the same for every worktree of it."""
    out = ok(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(out).resolve() if out else None
