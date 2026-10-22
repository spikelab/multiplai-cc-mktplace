"""The change's feature branch: where pm-spec pushed its specs, what every build
of the change starts from, and what its pull requests target.

pm-spec pushes a change's ready specs to `feat/<change>` on origin (or the
branch its `--branch` named, which each `.change.yaml` records as `branch`).
buildme builds from that branch:

- `resolve` names the branch: `build --branch`, else `feat/<change>` when
  origin has it (after `git fetch origin`), else the `branch` the change's
  `.change.yaml` in the checkout records. None when none applies: specs
  handed off before pm-spec pushed, or none at all.
- `merge_default` merges origin's default branch into the feature branch and
  pushes it, before a build starts from it and, in an epic, before each wave
  of children. It works in a detached worktree of its own,
  `<worktrees root>/buildme-feature-<branch>`, so no checkout a person or a
  build uses is touched. With nothing new on the default branch it does
  nothing. A conflict is aborted, leaving the branch as it was, and reported
  (`conflict_exit`: `FEATURE_BRANCH:CONFLICT:<branch>:<files>`, exit 3).
- `specs_at` reads a change dir as `origin/<branch>` holds it.

The pull request from the feature branch to the release branch is the
user's: nothing here opens one. Never forces a push. `shell=False`, fixed argv.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import git_ops, readiness

log = logging.getLogger(__name__)

PREFIX = "feat/"
WORKTREE_PREFIX = "buildme-feature-"
MARKER_CONFLICT = "FEATURE_BRANCH:CONFLICT"
EXIT_CONFLICT = 3


@dataclass
class Merged:
    """What `merge_default` did: `merged` (and pushed), `up to date`,
    `conflict` (*files* conflicted; aborted) or `failed` (*error* says why)."""

    outcome: str
    files: list[str] = field(default_factory=list)
    error: str = ""
    sha: str | None = None


def ref(branch: str) -> str:
    return f"origin/{branch}"


def _remote_sha(repo: Path, name: str) -> str | None:
    res = git_ops._run(
        [
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/remotes/origin/{name}^{{commit}}",
        ],
        cwd=repo,
    )
    return res.stdout.strip() if res.ok and res.stdout.strip() else None


def on_origin(repo: Path, branch: str) -> bool:
    return _remote_sha(repo, branch) is not None


def fetch(repo: Path) -> str | None:
    """`git fetch origin`; None when it worked, else git's words."""
    res = git_ops._run(
        ["git", "fetch", "origin"], cwd=repo, timeout=git_ops._PUSH_TIMEOUT
    )
    return (
        None
        if res.ok
        else ((res.stderr or res.stdout).strip()[-400:] or f"exit {res.returncode}")
    )


def recorded(change_dir: Path) -> str:
    """The `branch` the change dir's `.change.yaml` records, or ""."""
    from multiplai_core.specs import read_change_meta

    try:
        value = read_change_meta(Path(change_dir)).get("branch")
    except (OSError, UnicodeError, ValueError):
        return ""
    return value if isinstance(value, str) else ""


def resolve(repo: Path, change: str, flag: str, change_dir: Path) -> str | None:
    """The feature branch (module docstring); call after `fetch`."""
    if flag:
        return flag
    from .change_manager import normalize_change_name

    default = PREFIX + normalize_change_name(change)
    if on_origin(repo, default):
        return default
    return recorded(change_dir) or None


def default_name(repo: Path) -> str | None:
    """Origin's default branch: `origin/HEAD`, else `main` or `master`, else
    the repository's own default (`git_ops.default_branch`), the first origin
    has; None when it has none of them."""
    res = git_ops._run(
        ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd=repo
    )
    if res.ok and res.stdout.strip():
        return res.stdout.strip().removeprefix("refs/remotes/origin/")
    for name in ("main", "master", git_ops.default_branch(repo)):
        if name and on_origin(repo, name):
            return name
    return None


def worktree_path(repo: Path, branch: str) -> Path:
    return git_ops.worktrees_root(repo) / (WORKTREE_PREFIX + branch.replace("/", "-"))


def _git(w: Path, *args: str, timeout: int = git_ops._GIT_TIMEOUT) -> git_ops.GitResult:
    return git_ops._run(["git", *args], cwd=w, timeout=timeout)


def _checkout(repo: Path, w: Path, sha: str) -> str | None:
    """The detached worktree *w* at *sha*, made when missing; a merge an earlier
    run left in it is aborted first. None when it is there, else why not."""
    if not w.exists():
        w.parent.mkdir(parents=True, exist_ok=True)
        res = git_ops._run(
            ["git", "worktree", "add", "--detach", str(w), sha], cwd=repo
        )
        return (
            None
            if res.ok
            else f"git worktree add {w} failed: {(res.stderr or res.stdout).strip()}"
        )
    if _git(w, "rev-parse", "-q", "--verify", "MERGE_HEAD").ok:
        _git(w, "merge", "--abort")
    res = _git(w, "checkout", "-q", "--detach", "--force", sha)
    if not res.ok:
        return f"git checkout {sha[:12]} in {w} failed: {(res.stderr or res.stdout).strip()}"
    _git(w, "clean", "-fdq")
    return None


def merge_default(repo: Path, branch: str) -> Merged:
    """Merge `origin/<default>` into `origin/<branch>` with a merge commit and
    push it (module docstring). Fetches first."""
    repo = Path(repo)
    failed = fetch(repo)
    if failed is not None:
        return Merged("failed", error=f"git fetch origin failed: {failed}")
    default = default_name(repo)
    tip = _remote_sha(repo, branch)
    if tip is None:
        return Merged("failed", error=f"origin has no {branch}")
    if default is None or default == branch:
        # Nothing to merge: the branch is origin's default, or origin has no
        # other branch to call its default.
        return Merged("up to date", sha=tip)
    news = _git(
        repo,
        "rev-list",
        "--count",
        f"refs/remotes/origin/{branch}..refs/remotes/origin/{default}",
    )
    if not news.ok:
        return Merged(
            "failed", error=f"{news.command} failed: {news.stderr.strip()[-300:]}"
        )
    if news.stdout.strip() == "0":
        log.info("FEATURE_BRANCH %s holds origin/%s already", branch, default)
        return Merged("up to date", sha=tip)
    w = worktree_path(repo, branch)
    why = _checkout(repo, w, tip)
    if why:
        return Merged("failed", error=why)
    res = _git(
        w,
        "merge",
        "--no-ff",
        "--no-edit",
        "-m",
        f"Merge origin/{default} into {branch}",
        f"refs/remotes/origin/{default}",
    )
    if not res.ok:
        listed = _git(w, "diff", "--name-only", "--diff-filter=U")
        files = [f for f in listed.stdout.splitlines() if f.strip()]
        _git(w, "merge", "--abort")
        if files:
            log.warning(
                "FEATURE_BRANCH %s conflicts with origin/%s in %s",
                branch,
                default,
                files,
            )
            return Merged("conflict", files=files)
        return Merged(
            "failed",
            error=f"git merge origin/{default} failed in {w}: {(res.stderr or res.stdout).strip()[-400:]}",
        )
    sha = git_ops.rev_parse_head(w)
    pushed = _git(
        w, "push", "origin", f"HEAD:refs/heads/{branch}", timeout=git_ops._PUSH_TIMEOUT
    )
    if not pushed.ok:
        return Merged(
            "failed",
            error=f"git push origin {branch} failed: {(pushed.stderr or pushed.stdout).strip()[-400:]}",
        )
    _git(
        repo,
        "fetch",
        "origin",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        timeout=git_ops._PUSH_TIMEOUT,
    )
    log.info(
        "FEATURE_BRANCH merged origin/%s into %s at %s and pushed",
        default,
        branch,
        (sha or "")[:12],
    )
    return Merged("merged", sha=sha)


def conflict_exit(branch: str, files: list[str]) -> int:
    """Print `FEATURE_BRANCH:CONFLICT:<branch>:<files>` and what to do; exit 3."""
    print(f"{MARKER_CONFLICT}:{branch}:{','.join(files)}", flush=True)
    print(
        f"Merging origin's default branch into {branch} conflicts in {', '.join(files)}; "
        f"nothing was merged or built. Merge it into {branch} by hand, resolve the conflicts, "
        "push, then run the build again.",
        flush=True,
    )
    return EXIT_CONFLICT


def specs_at(repo: Path, branch: str, rel: str, dest: Path) -> Path | None:
    """The change dir *rel* as `origin/<branch>` holds it, copied under *dest*
    (`readiness.specs_at`); None when the branch does not hold it."""
    return readiness.specs_at(repo, f"refs/remotes/origin/{branch}", rel, dest)


def rel_to(repo: Path, path: Path) -> str | None:
    try:
        return Path(path).resolve().relative_to(Path(repo).resolve()).as_posix()
    except ValueError:
        return None


class Copy:
    """A temporary copy of a path as `origin/<branch>` holds it; `with` removes it."""

    def __init__(self, repo: Path, branch: str, rel: str):
        self.repo, self.branch, self.rel = Path(repo), branch, rel
        self._tmp = tempfile.TemporaryDirectory(prefix="buildme-feature-")

    def __enter__(self) -> Path | None:
        return specs_at(self.repo, self.branch, self.rel, Path(self._tmp.name))

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()
