"""Git and ``gh`` operations on a bank repo. Bounded, offline-tolerant, quiet.

Everything here shells out, and everything here can fail — a bank lives on a
network the user may not have, behind a credential that may have expired. Two
rules follow and are enforced by shape rather than by discipline:

* **Nothing raises.** Each call returns a :class:`GitResult` carrying an ok
  flag and the command's own message. A bank that cannot be pulled is
  stale-but-working; a session must never fail because a teammate's repo moved.
* **A failure never widens anything.** Contract C4. A failed pull leaves the
  previously synced content in place — it does not clear the bank, and it does
  not fall back to "treat this bank as writable because we could not check".
  A failed push leaves no PR, and the items stay in the review pile where a
  human can see them.

``git pull --ff-only`` is deliberate: a bank checkout is a *consumer* copy, and
the only correct outcome of a sync is "you now have what the remote has". A
merge commit created by a background hook, in a repo the user has not looked
at, is a mess nobody will be present to resolve.
"""

from __future__ import annotations

import dataclasses
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Sequence

__all__ = [
    "DEFAULT_TIMEOUT",
    "GitResult",
    "current_branch",
    "gh_available",
    "head_sha",
    "is_git_repo",
    "open_pull_request",
    "pull_ff_only",
    "push_branch",
    "run_git",
    "stage_commit",
]

logger = logging.getLogger(__name__)

#: Every subprocess here is on a hook-adjacent path. Bounded, always.
DEFAULT_TIMEOUT = 60


@dataclasses.dataclass(frozen=True)
class GitResult:
    """Outcome of one command. ``ok`` is the only thing callers should branch on."""

    ok: bool
    output: str = ""
    detail: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.ok


def _run(argv: Sequence[str], *, cwd: Optional[Path] = None, timeout: int = DEFAULT_TIMEOUT) -> GitResult:
    exe = shutil.which(argv[0])
    if exe is None:
        return GitResult(False, "", f"{argv[0]} is not installed")
    try:
        proc = subprocess.run(
            [exe, *argv[1:]],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return GitResult(False, "", f"{argv[0]} timed out after {timeout}s")
    except OSError as e:
        return GitResult(False, "", f"{argv[0]} could not run: {e}")
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return GitResult(False, out, err or f"exit {proc.returncode}")
    return GitResult(True, out, err)


def run_git(args: Sequence[str], *, cwd: Path, timeout: int = DEFAULT_TIMEOUT) -> GitResult:
    """``git -C <cwd> <args…>``, never raising."""
    return _run(["git", "-C", str(cwd), *args], timeout=timeout)


def is_git_repo(path: Path) -> bool:
    return _run(["git", "-C", str(path), "rev-parse", "--git-dir"], timeout=10).ok


def head_sha(path: Path) -> str:
    result = run_git(["rev-parse", "HEAD"], cwd=path, timeout=10)
    return result.output if result.ok else ""


def current_branch(path: Path) -> str:
    result = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path, timeout=10)
    return result.output if result.ok else ""


def pull_ff_only(path: Path, *, timeout: int = DEFAULT_TIMEOUT) -> GitResult:
    """Fast-forward the bank to its remote. Any failure is stale-but-working."""
    if not is_git_repo(path):
        return GitResult(False, "", f"{path} is not a git repository")
    return run_git(["pull", "--ff-only", "--quiet"], cwd=path, timeout=timeout)


def stage_commit(path: Path, *, pathspec: Sequence[str], message: str) -> GitResult:
    """Stage exactly *pathspec* and commit. No ``-A``, ever.

    A bank checkout may hold a user's own uncommitted experiment; ``git add -A``
    in a background path would sweep it into a pull request on somebody else's
    repo.
    """
    if not pathspec:
        return GitResult(False, "", "nothing to commit")
    staged = run_git(["add", "--", *pathspec], cwd=path)
    if not staged.ok:
        return staged
    return run_git(["commit", "-m", message], cwd=path)


def push_branch(path: Path, branch: str, *, remote: str = "origin") -> GitResult:
    return run_git(["push", "--set-upstream", remote, branch], cwd=path, timeout=120)


def gh_available() -> bool:
    return shutil.which("gh") is not None


def open_pull_request(
    path: Path, *, title: str, body: str, base: str, head: str
) -> GitResult:
    """Open a PR with ``gh``. The URL comes back in ``output`` on success."""
    if not gh_available():
        return GitResult(
            False,
            "",
            "the GitHub CLI (`gh`) is not installed — the branch has been "
            "pushed; open the pull request by hand",
        )
    return _run(
        [
            "gh", "pr", "create",
            "--title", title,
            "--body", body,
            "--base", base,
            "--head", head,
        ],
        cwd=path,
        timeout=120,
    )
