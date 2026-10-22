"""The change's feature branch, and the checkout pm-spec writes it in.

A change's specs live on a feature branch on `origin` named after its ticket:
`<TICKET>-<change>` (`name`), or the name `--branch` gives, which must still
start with `<TICKET>-`. The ticket is the change's own ref (`--ticket-ref`,
kept in `.change.yaml` as `ticket_refs.epic`), else its nearest parent's
(`tickets`): an epic's children, split before any ticket exists for them, go
on their parent's branch. A change with no ticket anywhere is refused before
anything is written, and so is a ticket id that cannot start a branch name
(`git check-ref-format`). pm-spec writes in a worktree of its own, so the
checkout it was started from (`--project-dir`) is never touched:

- `place` picks the branch and the checkout. With no `--branch`, the branch is
  the one the change's `.change.yaml` records (`branch`), in `--project-dir`
  or in a worktree of the repository on that branch or pm-spec's own for it
  (the parent's worktree holds a new child's change dir), else
  `<TICKET>-<change>`. For `resume` and `revise` in a build's worktree
  (buildme's `buildme-*` worktrees) whose copy of the change records the
  branch, the checkout is that worktree and nothing is pushed: the revision
  reaches the feature branch with that build's push. Otherwise the checkout is
  `--project-dir` itself when it is on that branch, else the worktree
  `<worktrees root>/pm-spec-<branch>`, the root restated from buildme's
  `git_ops.worktrees_root` (`$WORKSPACE/.worktrees`, else `../.worktrees`
  beside the repository's main checkout).
- `prepare` needs an `origin` remote and fetches it. It creates pm-spec's
  worktree when missing, with a detached HEAD (no local branch, so a build can
  check the branch out): at `origin/<branch>`, else at `origin/<default>`.
  Then it fast-forwards the checkout to `origin/<branch>`, and refuses one
  that has diverged from it, naming both shas.
- `adopt` takes a change dir in `--project-dir` that records no branch (one
  handed off before pm-spec used a branch, or specs written by hand): it
  copies the change dir, and the children it lists, into the worktree when
  the worktree lacks it, and commits them there. The originals are never
  deleted.
- `push` runs `git push origin HEAD:refs/heads/<branch>`, never with force.

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
WORKTREE_PREFIX = "pm-spec-"
# buildme's build worktrees (`git_ops.worktree_dir_name`), and the one it
# merges the default branch in (`feature_branch.WORKTREE_PREFIX`).
BUILD_WORKTREE_PREFIX = "buildme-"
BUILDME_MERGE_PREFIX = "buildme-feature-"
# The characters `git check-ref-format` refuses anywhere in a branch name.
_BAD_CHARS = " ~^:?*[\\"


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


def main_checkout(repo: Path) -> Path:
    """The repository's main checkout, which buildme's worktrees root is
    beside; *repo* itself when git cannot say."""
    common = _out(["rev-parse", "--path-format=absolute", "--git-common-dir"], repo)
    if common and Path(common).name == ".git":
        return Path(common).parent.resolve()
    return repo


def worktree_path(repo: Path, branch: str) -> Path:
    return worktrees_root(main_checkout(repo)) / (
        WORKTREE_PREFIX + branch.replace("/", "-")
    )


def name(ticket: str, change: str) -> str:
    """The feature branch of a change with *ticket*: `<TICKET>-<change>`."""
    return f"{ticket}-{change}"


def _own_ticket(change_dir: Path) -> str:
    try:
        raw = read_change_meta(change_dir).get("ticket_refs") or {}
    except (OSError, UnicodeError, ValueError):
        return ""
    value = raw.get("epic") if isinstance(raw, dict) else None
    return str(value).strip() if value else ""


def _parent(change_dir: Path) -> str:
    try:
        value = read_change_meta(change_dir).get("parent")
    except (OSError, UnicodeError, ValueError):
        return ""
    return value if isinstance(value, str) else ""


def tickets(change_dir: Path) -> list[str]:
    """The change's own ticket ref, then each parent's up the chain, nearest
    first, skipping those with none: `[]` when there is no ticket anywhere.
    A parent is read beside *change_dir* (`specs/changes/<parent>`)."""
    found, seen, here = [], set(), Path(change_dir)
    while here.name not in seen and len(seen) < 10:
        seen.add(here.name)
        if here.is_dir() and (own := _own_ticket(here)) and own not in found:
            found.append(own)
        parent = _parent(here) if here.is_dir() else ""
        if not parent:
            break
        here = here.parent / normalize_change_name(parent)
    return found


def _ticket_refusal(ticket: str, change: str, repo: Path) -> str | None:
    """Why *ticket* cannot start the branch name, or None."""
    full = name(ticket, change)
    if _out(["check-ref-format", "--branch", full], repo) is not None:
        return None
    bad = sorted({c for c in ticket if c in _BAD_CHARS or ord(c) < 32 or ord(c) == 127})
    if bad:
        why = (
            "it holds "
            + ", ".join(repr(c) for c in bad)
            + ", which git refuses in a branch name"
        )
    elif ".." in ticket or "@{" in ticket:
        why = "it holds '..' or '@{', which git refuses in a branch name"
    else:
        why = "git check-ref-format refuses it as the start of a branch name"
    return f"the ticket id {ticket!r} cannot start the branch name {full!r}: {why}"


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
    """`(path, branch)` of every worktree of *repo*; the branch is "" for a
    detached HEAD."""
    listed = _out(["worktree", "list", "--porcelain"], repo) or ""
    found: list[tuple[Path, str]] = []
    for line in listed.splitlines():
        if line.startswith("worktree "):
            found.append((Path(line[len("worktree ") :]), ""))
        elif line.startswith("branch refs/heads/") and found:
            found[-1] = (found[-1][0], line[len("branch refs/heads/") :])
    return found


def _in_build_worktree(repo: Path) -> bool:
    """Whether *repo* is one of buildme's build worktrees: a linked worktree
    named `buildme-<change>` (not its merge worktree)."""
    return (
        repo.name.startswith(BUILD_WORKTREE_PREFIX)
        and not repo.name.startswith(BUILDME_MERGE_PREFIX)
        and main_checkout(repo).resolve() != repo.resolve()
    )


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


def _copy(
    repo: Path, project_dir: Path, offset: Path, change: str, *, archived: bool
) -> tuple[Path | None, str]:
    """The change's dir this run starts from and the branch it records:
    `--project-dir`'s copy when it records one; else the copy in a worktree
    of the repository that is on the branch it records, or is pm-spec's own
    worktree for it; else `--project-dir`'s copy recording none, if it is
    there. `(None, "")` when there is no copy."""
    local = local_change_dir(project_dir, change, archived=archived)
    if _recorded(local):
        return local, _recorded(local)
    for path, on in _worktrees(repo):
        copy = local_change_dir(path / offset, change, archived=archived)
        mark = _recorded(copy)
        if mark and (
            on == mark or path.resolve() == worktree_path(repo, mark).resolve()
        ):
            return copy, mark
    return (local, "") if local.is_dir() else (None, "")


def place(
    project_dir: Path,
    change: str,
    flag: str = "",
    *,
    command: str = "new",
    ticket: str = "",
) -> Place:
    """The branch and the checkout this run writes in (module docstring).
    *ticket* is `--ticket-ref`."""
    project_dir = Path(project_dir).expanduser().resolve()
    change = normalize_change_name(change)
    repo_text = _out(["rev-parse", "--show-toplevel"], project_dir)
    if not repo_text:
        _refuse(f"{project_dir} is not in a git work tree")
    repo = Path(repo_text).resolve()
    offset = project_dir.relative_to(repo)
    copy, recorded = _copy(
        repo, project_dir, offset, change, archived=command == "revise"
    )
    ticket = ticket.strip()
    chain = tickets(copy) if copy is not None else []
    known = ([ticket] if ticket else []) + [t for t in chain if t != ticket]
    if not known and copy is None and command != "new":
        _refuse(
            f"there is no change {change} to {command}: start it with "
            f"`new {change} --ticket-ref <ticket id>`"
        )
    if not known:
        _refuse(
            f"{change} has no ticket ref, and pm-spec puts a change's specs on a branch named "
            f"after its ticket (<TICKET>-{change}); pass --ticket-ref <ticket id>, then run again"
        )
    why = _ticket_refusal(known[0], change, repo)
    if why:
        _refuse(f"{why}; pass another --ticket-ref, then run again")
    branch = flag or recorded or name(known[0], change)
    if flag and not any(flag.startswith(f"{t}-") for t in known):
        _refuse(
            f"--branch {flag!r} does not start with the ticket id ({known[0]}-): a change's "
            f"branch is <TICKET>-<name>, for example {name(known[0], change)}"
        )
    if _out(["check-ref-format", "--branch", branch], repo) is None:
        _refuse(f"{branch!r} is not a valid branch name")
    current = _out(["symbolic-ref", "--quiet", "--short", "HEAD"], project_dir) or ""
    if command != "new" and recorded == branch and _in_build_worktree(repo):
        log.info(
            "%s is a build's worktree (on %s) and %s records %s: working in place, "
            "pushing nothing",
            repo,
            current or "a detached HEAD",
            copy,
            branch,
        )
        return Place(branch, repo, project_dir, repo, own_worktree=False, push=False)
    if current == branch:
        return Place(branch, repo, project_dir, repo, own_worktree=False, push=True)
    dest = worktree_path(repo, branch)
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
        if _remote(repo, branch):
            start = f"origin/{branch}"
        else:
            default = default_branch(repo)
            if default is None:
                _refuse(
                    f"origin has neither {branch} nor a default branch to start it from; "
                    "push a first commit to origin, then run again"
                )
            start = f"origin/{default}"
        argv = ["worktree", "add", "--detach", str(checkout), start]
        added = git_ops._git(argv, repo)
        if added.returncode:
            _fail(
                f"git {' '.join(argv)} failed: {(added.stderr or added.stdout).strip()}"
            )
        log.info("WORKTREE %s at %s, for %s", checkout, start, branch)
    elif where.own_worktree and checkout.resolve() not in {
        path.resolve() for path, _ in _worktrees(repo)
    }:
        _refuse(
            f"{checkout} exists but is not a worktree of {repo}; move it aside, then run again"
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


def record_ticket(change_dir: Path, ticket: str) -> None:
    """Write *ticket* into *change_dir*'s `.change.yaml` as its own ticket ref
    (`ticket_refs.epic`, the children's refs kept) when it has none."""
    if not ticket or not change_dir.is_dir() or _own_ticket(change_dir):
        return
    try:
        raw = read_change_meta(change_dir).get("ticket_refs") or {}
    except (OSError, UnicodeError, ValueError):
        raw = {}
    children = raw.get("children") if isinstance(raw, dict) else None
    write_change_meta(
        change_dir,
        {"ticket_refs": {"epic": ticket, "children": list(children or [])}},
    )


def adopt(where: Place, source: Path, change: str, ticket: str = "") -> bool:
    """Copy a change dir that records no branch from *source*
    (`--project-dir`) into pm-spec's worktree, with the children its
    `.change.yaml` lists, record the branch in each (and *ticket*,
    `--ticket-ref`, in the change's when it has no ticket ref) and commit
    each there. False, with nothing written, when there is nothing to adopt:
    the worktree holds the change already, *source* does not, or *source*'s
    copy records a branch. The originals stay where they are."""
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
        if name == change:
            record_ticket(target / name, ticket)
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
    """`git push origin HEAD:refs/heads/<branch>`, never with force. Raises
    PushFailed with git's words."""
    pushed = git_ops._git(
        ["push", "origin", f"HEAD:refs/heads/{where.branch}"],
        where.checkout,
        timeout=NETWORK_TIMEOUT_S,
    )
    if pushed.returncode:
        raise PushFailed((pushed.stderr or pushed.stdout).strip()[-400:])
    log.info("PUSHED %s from %s", where.branch, where.checkout)
