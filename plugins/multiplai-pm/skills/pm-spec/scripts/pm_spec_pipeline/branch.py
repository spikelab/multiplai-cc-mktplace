"""The change's feature branch, and the checkout pm-spec writes it in.

A change's specs live on a feature branch on `origin`: `feat/<change>`, or
the name `--branch` gives. A child of an epic uses its parent's branch. pm-spec
writes in a worktree of its own, so the checkout it was started from
(`--project-dir`) is never touched:

- `place` picks the branch and the checkout. With no `--branch`, the branch is
  the one the change's `.change.yaml` in `--project-dir` records (`branch`),
  else the branch of a worktree of the repository whose copy of the change
  records that branch (the parent's worktree holds a new child's change dir),
  else `feat/<change>`. The checkout is `--project-dir` itself when it is on
  that branch; for `resume` and `revise`, also when its copy of the change
  records the branch while another branch is checked out (a build's worktree,
  whose revision reaches the feature branch through that build's pull
  request: nothing is pushed). Otherwise it is the worktree
  `<worktrees root>/pm-spec-<branch>`, the root restated from buildme's
  `git_ops.worktrees_root` (`$WORKSPACE/.worktrees`, else
  `<repo>/../.worktrees`).
- `prepare` needs an `origin` remote and fetches it. It creates the worktree
  when missing: on the local branch when there is one, else from
  `origin/<branch>`, else a new branch from `origin/<default>`. Then it
  fast-forwards the checkout to `origin/<branch>`, and refuses one that has
  diverged from it, naming both shas.
- `adopt` takes a change dir in `--project-dir` that records no branch (one
  handed off before pm-spec used a branch, or specs written by hand): it
  copies the change dir, and the children it lists, into the worktree when
  the worktree lacks it, and commits them there. The originals are never
  deleted.
- `push` runs `git push -u origin <branch>`, never with force.

`shell=False`, fixed argv, through `git_ops._git`.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from multiplai_core.specs import (
    archived_change_dirs,
    normalize_change_name,
    read_change_meta,
    write_change_meta,
)

from . import git_ops
from .run import EXIT_FAILED, EXIT_REFUSED, Stop

log = logging.getLogger(__name__)

NETWORK_TIMEOUT_S = 300
PREFIX = "feat/"
WORKTREE_PREFIX = "pm-spec-"


@dataclass
class Place:
    branch: str
    # The repository `--project-dir` is in (its top level).
    repo: Path
    # Where the run reads and writes: the checkout plus the project's offset
    # in its repository.
    project_dir: Path
    checkout: Path
    # True when the checkout is pm-spec's own worktree, which `prepare` creates.
    own_worktree: bool
    # False in a build's worktree: its branch is not the feature branch, so
    # nothing is fast-forwarded or pushed.
    push: bool


def _out(args: list[str], cwd: Path) -> str | None:
    res = git_ops._git(args, cwd)
    return res.stdout.strip() if res.returncode == 0 else None


def _refuse(message: str) -> None:
    print(f"ERROR: {message}", flush=True)
    raise Stop(EXIT_REFUSED, message)


def _fail(message: str) -> None:
    print(f"ERROR: {message}", flush=True)
    raise Stop(EXIT_FAILED, message)


def worktrees_root(repo: Path) -> Path:
    """`$WORKSPACE/.worktrees` when WORKSPACE is set, else `<repo>/../.worktrees`
    (buildme's `git_ops.worktrees_root`, restated: no plugin imports another)."""
    workspace = os.environ.get("WORKSPACE", "").strip()
    if workspace:
        return Path(workspace).expanduser() / ".worktrees"
    return repo.parent / ".worktrees"


def worktree_path(repo: Path, branch: str) -> Path:
    return worktrees_root(repo) / (WORKTREE_PREFIX + branch.replace("/", "-"))


def default_branch(repo: Path) -> str | None:
    """Origin's default branch: `origin/HEAD`, else `main` or `master` when
    origin has it; None when origin has neither."""
    head = _out(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], repo)
    if head:
        return head.rsplit("/", 1)[-1]
    for name in ("main", "master"):
        if _out(["rev-parse", "--verify", "-q", f"refs/remotes/origin/{name}"], repo):
            return name
    return None


def _recorded(change_dir: Path) -> str:
    try:
        value = read_change_meta(change_dir).get("branch")
    except (OSError, UnicodeError, ValueError):
        return ""
    return value if isinstance(value, str) else ""


def _worktrees(repo: Path) -> list[tuple[Path, str]]:
    """`(path, branch)` of every worktree of *repo* with a branch checked out."""
    listed = _out(["worktree", "list", "--porcelain"], repo) or ""
    found, path = [], None
    for line in listed.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :])
        elif line.startswith("branch refs/heads/") and path is not None:
            found.append((path, line[len("branch refs/heads/") :]))
    return found


def local_change_dir(project_dir: Path, change: str, *, archived: bool) -> Path:
    """The change's dir in *project_dir*; with *archived*, its newest archived
    copy when `specs/changes/<change>/` is gone (as `revise` reads it)."""
    specs = project_dir / "specs"
    active = specs / "changes" / change
    if archived and not active.is_dir():
        dirs = archived_change_dirs(specs, change)
        if dirs:
            return dirs[-1]
    return active


def place(
    project_dir: Path, change: str, flag: str = "", *, command: str = "new"
) -> Place:
    """The branch and the checkout this run writes in (module docstring)."""
    project_dir = Path(project_dir).expanduser().resolve()
    change = normalize_change_name(change)
    repo_text = _out(["rev-parse", "--show-toplevel"], project_dir)
    if not repo_text:
        _refuse(f"{project_dir} is not in a git work tree")
    repo = Path(repo_text).resolve()
    offset = project_dir.relative_to(repo)
    local = local_change_dir(project_dir, change, archived=command == "revise")
    recorded = _recorded(local)
    branch = flag or recorded
    if not branch:
        for path, name in _worktrees(repo):
            copy = local_change_dir(path / offset, change, archived=command == "revise")
            if _recorded(copy) == name:
                branch = name
                break
    branch = branch or PREFIX + change
    if _out(["check-ref-format", "--branch", branch], repo) is None:
        _refuse(f"{branch!r} is not a valid branch name")
    current = _out(["symbolic-ref", "--quiet", "--short", "HEAD"], project_dir) or ""
    if current == branch:
        return Place(branch, repo, project_dir, repo, own_worktree=False, push=True)
    if command != "new" and recorded == branch:
        log.info(
            "%s records %s and %s is on %s: working in place, pushing nothing",
            local,
            branch,
            project_dir,
            current or "a detached HEAD",
        )
        return Place(branch, repo, project_dir, repo, own_worktree=False, push=False)
    dest = worktree_path(repo, branch)
    for path, name in _worktrees(repo):
        if name == branch and path.resolve() != dest.resolve():
            _refuse(
                f"{branch} is checked out in {path}; pm-spec writes in its own worktree, {dest}. "
                f"Run it with --project-dir {path / offset} to write there, or check another "
                "branch out there, then run again"
            )
    return Place(branch, repo, dest / offset, dest, own_worktree=True, push=True)


def _remote(checkout: Path, branch: str) -> str | None:
    return _out(
        ["rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}"], checkout
    )


def _ancestor(checkout: Path, a: str, b: str) -> bool:
    return git_ops._git(["merge-base", "--is-ancestor", a, b], checkout).returncode == 0


def prepare(where: Place) -> None:
    """`origin` fetched, the worktree there and fast-forwarded (module
    docstring). Stops with exit 2 on a refusal and exit 1 when git fails."""
    repo = where.repo
    if _out(["remote", "get-url", "origin"], repo) is None:
        _refuse(
            f"pm-spec pushes the change's branch to origin, and {repo} has no origin remote; "
            "add one (`git remote add origin <url>`), then run again"
        )
    if not where.push:
        return
    fetched = git_ops._git(["fetch", "origin"], repo, timeout=NETWORK_TIMEOUT_S)
    if fetched.returncode:
        _fail(
            f"git fetch origin failed: {(fetched.stderr or fetched.stdout).strip()[-400:]}"
        )
    branch, checkout = where.branch, where.checkout
    if where.own_worktree and not checkout.exists():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        if _out(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], repo):
            argv = ["worktree", "add", str(checkout), branch]
        elif _remote(repo, branch):
            argv = [
                "worktree",
                "add",
                "--track",
                "-b",
                branch,
                str(checkout),
                f"origin/{branch}",
            ]
        else:
            default = default_branch(repo)
            if default is None:
                _refuse(
                    f"origin has neither {branch} nor a default branch to start it from; "
                    "push a first commit to origin, then run again"
                )
            argv = [
                "worktree",
                "add",
                "--no-track",
                "-b",
                branch,
                str(checkout),
                f"origin/{default}",
            ]
        added = git_ops._git(argv, repo)
        if added.returncode:
            _fail(
                f"git {' '.join(argv)} failed: {(added.stderr or added.stdout).strip()}"
            )
        log.info("WORKTREE %s on %s", checkout, branch)
    elif where.own_worktree and (checkout.resolve(), branch) not in {
        (path.resolve(), name) for path, name in _worktrees(repo)
    }:
        _refuse(
            f"{checkout} exists but is not a worktree of {repo} with {branch} checked out; "
            "move it aside, then run again"
        )
    remote = _remote(checkout, branch)
    head = _out(["rev-parse", "HEAD"], checkout)
    if (
        remote is None
        or head is None
        or head == remote
        or _ancestor(checkout, remote, head)
    ):
        return  # origin lacks it, or holds nothing the checkout lacks: a push sends the rest
    if not _ancestor(checkout, head, remote):
        _refuse(
            f"{checkout} has diverged from origin/{branch}: its HEAD {head} and origin/{branch} "
            f"{remote} each hold commits the other lacks. Merge or rebase them there, then run again"
        )
    merged = git_ops._git(
        ["merge", "--ff-only", "-q", f"refs/remotes/origin/{branch}"], checkout
    )
    if merged.returncode:
        _fail(
            f"cannot fast-forward {checkout} to origin/{branch}: "
            f"{(merged.stderr or merged.stdout).strip()[-400:]}"
        )
    log.info("FAST-FORWARD %s %s..%s", checkout, head[:8], remote[:8])


def record(change_dir: Path, branch: str) -> None:
    """Write `branch: <branch>` into *change_dir*'s `.change.yaml` when it holds another."""
    if _recorded(change_dir) != branch:
        write_change_meta(change_dir, {"branch": branch})


def adopt(where: Place, source: Path, change: str) -> bool:
    """Copy a change dir that records no branch from *source*
    (`--project-dir`) into pm-spec's worktree, with the children its
    `.change.yaml` lists, record the branch in each and commit each there.
    False, with nothing written, when there is nothing to adopt: the worktree
    holds the change already, *source* does not, or *source*'s copy records
    a branch. The originals stay where they are."""
    if not where.own_worktree:
        return False
    change = normalize_change_name(change)
    origin = Path(source).expanduser().resolve() / "specs" / "changes"
    target = where.project_dir / "specs" / "changes"
    if (
        (target / change).exists()
        or not (origin / change).is_dir()
        or _recorded(origin / change)
    ):
        return False
    children = read_change_meta(origin / change).get("children") or []
    names = [change, *(c for c in children if isinstance(c, str))]
    moved = []
    for name in names:
        if not (origin / name).is_dir() or (target / name).exists():
            continue
        # The run lock is the holder's in the source, never the copy's.
        shutil.copytree(
            origin / name,
            target / name,
            symlinks=True,
            ignore=shutil.ignore_patterns("run.lock"),
        )
        record(target / name, where.branch)
        moved.append(name)
    for name in moved:
        try:
            git_ops.commit_change_dir(
                where.checkout, target / name, f"specs({name}): moved to {where.branch}"
            )
        except git_ops.GitError as e:
            _fail(
                f"the specs of {name} are copied into {target / name}, but the commit failed: {e}"
            )
    print(
        f"Copied {', '.join(moved)} from {origin} into {target} on {where.branch}; "
        "the originals are left as they are.",
        flush=True,
    )
    return True


def ahead(where: Place) -> bool:
    """Whether the checkout holds commits `origin/<branch>` lacks (or origin lacks the branch)."""
    remote = _remote(where.checkout, where.branch)
    head = _out(["rev-parse", "HEAD"], where.checkout)
    return head is not None and (
        remote is None or (head != remote and _ancestor(where.checkout, remote, head))
    )


class PushFailed(RuntimeError):
    pass


def push(where: Place) -> None:
    """`git push -u origin <branch>`, never with force. Raises PushFailed with git's words."""
    pushed = git_ops._git(
        ["push", "-u", "origin", where.branch],
        where.checkout,
        timeout=NETWORK_TIMEOUT_S,
    )
    if pushed.returncode:
        raise PushFailed((pushed.stderr or pushed.stdout).strip()[-400:])
    log.info("PUSHED %s from %s", where.branch, where.checkout)
