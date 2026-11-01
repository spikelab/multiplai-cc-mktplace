"""The change's feature branch: where pm-spec pushed its specs, what every build
of the change starts from, and where it lands.

pm-spec pushes a change's ready specs to `<TICKET>-<change>` on origin (or
the branch its `--branch` named, which each `.change.yaml` records as
`branch`). A single change builds on that branch itself and opens one pull
request from it to origin's default branch; an epic's children build on
branches of their own (`name`: `<TICKET>-<child>`) cut from it, and their
pull requests target it:

- `resolve` names the branch: `build --branch`, else the `branch` the
  change's `.change.yaml` in the checkout records, else the one origin branch
  (after `git fetch origin`) named `<something>-<change>` that holds the
  change's specs as their feature branch (`candidates`). Several such
  branches are listed for the caller to refuse; none applies to specs handed
  off before pm-spec pushed, or none at all.
- `tickets_at` reads the change's ticket id on the branch: its own ref, else
  its nearest parent's (pm-spec's split makes children before their tickets
  exist).
- `merge_default` merges origin's default branch into the feature branch and
  pushes it, before a build starts from it and, in an epic, before each wave
  of children. It works in a detached worktree of its own,
  `<worktrees root>/buildme-feature-<branch>`, so no checkout a person or a
  build uses is touched. With nothing new on the default branch it does
  nothing. A conflict is aborted, leaving the branch as it was, and reported
  (`conflict_exit`: `FEATURE_BRANCH:CONFLICT:<branch>:<files>`, exit 3).
  `merge_default_into` does the same in a single change's own build worktree
  when a run resumes it, and pushes nothing: the build's push takes it.
- `specs_at` reads a change dir as `origin/<branch>` holds it.
- `archive_on` archives a change with components on the branch, once every
  component merged into it, in the same detached worktree, and pushes it.

An epic's pull request from the feature branch to the default branch is the
user's: nothing here opens one. Never forces a push. `shell=False`, fixed argv.
"""

from __future__ import annotations

import logging
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import git_ops, readiness

log = logging.getLogger(__name__)

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


# git's words when another git updated a ref this fetch was updating: the
# coordinator and an epic's children (in worktrees of the same repository)
# share one ref store. The other git finishes in moments, so the fetch is run
# again, at most FETCH_ATTEMPTS times in all.
REF_LOCK_LOST = "cannot lock ref"
FETCH_ATTEMPTS = 3


def fetch(repo: Path) -> str | None:
    """`git fetch origin`; None when it worked, else git's words. Run again
    when it lost a ref lock to another git (`REF_LOCK_LOST`)."""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        res = git_ops._run(
            ["git", "fetch", "origin"], cwd=repo, timeout=git_ops._PUSH_TIMEOUT
        )
        if res.ok:
            return None
        why = (res.stderr or res.stdout).strip()[-400:] or f"exit {res.returncode}"
        if REF_LOCK_LOST not in why or attempt == FETCH_ATTEMPTS:
            return why
        log.warning("git fetch origin lost a ref lock (attempt %d): %s", attempt, why)
        time.sleep(0.5 * attempt)
    return why


def recorded(change_dir: Path) -> str:
    """The `branch` the change dir's `.change.yaml` records, or ""."""
    from multiplai_core.specs import read_change_meta

    try:
        value = read_change_meta(Path(change_dir)).get("branch")
    except (OSError, UnicodeError, ValueError):
        return ""
    return value if isinstance(value, str) else ""


def name(ticket: str, change: str) -> str:
    """A branch buildme or pm-spec makes for *change*: `<TICKET>-<change>`."""
    from .change_manager import normalize_change_name

    return f"{ticket}-{normalize_change_name(change)}"


def ticket_refusal(repo: Path, ticket: str, change: str) -> str | None:
    """Why *ticket* cannot start the branch `name(ticket, change)`, or None
    when `git check-ref-format --branch` takes it."""
    full = name(ticket, change)
    res = git_ops._run(["git", "check-ref-format", "--branch", full], cwd=repo)
    if res.ok:
        return None
    bad = sorted(
        {c for c in ticket if c in " ~^:?*[\\" or ord(c) < 32 or ord(c) == 127}
    )
    why = (
        f"it holds {', '.join(repr(c) for c in bad)}, which git refuses in a branch name"
        if bad
        else "git check-ref-format refuses it as the start of a branch name"
    )
    return f"the ticket id {ticket!r} cannot start the branch name {full!r}: {why}"


def candidates(repo: Path, change: str, changes_rel: str | None = None) -> list[str]:
    """Origin's branches named `<something>-<change>`, origin's default and
    `HEAD` left out, sorted. With *changes_rel* (the specs' `changes` dir,
    relative to the repo), only those that are the change's feature branch
    (`_feature_branch_of`): another change's branch whose name ends the same
    way (`ENG-40-auth-<change>`), or an epic child's build branch, is not."""
    from .change_manager import normalize_change_name

    suffix = "-" + normalize_change_name(change)
    res = git_ops._run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/remotes/origin/"],
        cwd=repo,
    )
    names = [
        r.removeprefix("refs/remotes/origin/") for r in res.stdout.split() if res.ok
    ]
    default = default_name(repo)
    return sorted(
        n
        for n in names
        if n.endswith(suffix)
        and len(n) > len(suffix)
        and n not in ("HEAD", default)
        and (changes_rel is None or _feature_branch_of(repo, n, changes_rel, change))
    )


def _feature_branch_of(repo: Path, branch: str, changes_rel: str, change: str) -> bool:
    """Whether `origin/<branch>` is *change*'s feature branch. It holds the
    change dir, or, once an `--auto` build archived it, its newest
    `archive/YYYY-MM-DD-<change>` beside the changes dir. That copy's
    `.change.yaml` records *branch* as its `branch`; or records none, and
    *branch* is `<TICKET>-<change>` for one of the change's tickets, or the
    change has no ticket (the build then refuses it for that). An epic
    child's build branch holds the child's change dir, but that records the
    epic's branch; another change's branch does not hold it at all."""
    import re

    from .change_manager import normalize_change_name

    ref = f"refs/remotes/origin/{branch}"
    here = normalize_change_name(change)
    meta = _meta_at(repo, ref, f"{changes_rel}/{here}" if changes_rel else here)
    tickets: list[str] = []
    if meta:
        tickets = tickets_at(repo, ref, changes_rel, change)
    else:
        archive = (Path(changes_rel).parent / "archive").as_posix()
        archive = "" if archive == "." else archive
        res = git_ops._run(
            ["git", "ls-tree", "--name-only", f"{ref}:{archive}"]
            if archive
            else ["git", "ls-tree", "--name-only", ref],
            cwd=repo,
        )
        pattern = re.compile(rf"^\d{{4}}-\d{{2}}-\d{{2}}-{re.escape(here)}$")
        dated = sorted(n for n in res.stdout.split() if res.ok and pattern.match(n))
        if not dated:
            return False
        meta = _meta_at(repo, ref, f"{archive}/{dated[-1]}" if archive else dated[-1])
        if not meta:
            return False
        if own := own_ticket(meta):
            tickets.append(own)
        parent = meta.get("parent")
        if isinstance(parent, str) and parent:
            tickets += [
                t
                for t in tickets_at(repo, ref, changes_rel, parent)
                if t not in tickets
            ]
    mark = meta.get("branch")
    if isinstance(mark, str) and mark:
        return mark == branch
    return not tickets or any(branch == name(t, change) for t in tickets)


@dataclass
class Resolved:
    """`resolve`'s answer: the branch, or the several origin has (*several*)."""

    branch: str | None
    several: list[str] = field(default_factory=list)


def resolve(repo: Path, change: str, flag: str, change_dir: Path) -> Resolved:
    """The feature branch (module docstring); call after `fetch`."""
    if flag:
        return Resolved(flag)
    mark = recorded(change_dir)
    if mark:
        return Resolved(mark)
    found = candidates(repo, change, rel_to(repo, Path(change_dir).parent))
    if len(found) == 1:
        return Resolved(found[0])
    return Resolved(None, found)


def _meta_at(repo: Path, ref: str, rel: str) -> dict:
    """`<rel>/.change.yaml` as *ref* holds it, parsed; {} when it is not there."""
    import yaml

    res = git_ops._run(["git", "show", f"{ref}:{rel}/.change.yaml"], cwd=repo)
    if not res.ok:
        return {}
    try:
        data = yaml.safe_load(res.stdout)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def own_ticket(meta: dict) -> str:
    """The change's own ticket ref in its `.change.yaml` (`ticket_refs.epic`)."""
    raw = meta.get("ticket_refs") or {}
    value = raw.get("epic") if isinstance(raw, dict) else None
    return str(value).strip() if value else ""


def tickets_at(repo: Path, ref: str, changes_rel: str, change: str) -> list[str]:
    """The change's own ticket ref, then each parent's, nearest first, as
    *ref* holds `<changes_rel>/<name>/.change.yaml`: `[]` for none. A
    component `<change>/<id>` reads `<change>/components/<id>` first, then
    its change."""
    from .change_manager import normalize_change_name, split_component

    found: list[str] = []
    here, seen = normalize_change_name(change), set()
    if component := split_component(change):
        rel = f"{component[0]}/components/{component[1]}"
        meta = _meta_at(repo, ref, f"{changes_rel}/{rel}" if changes_rel else rel)
        if own := own_ticket(meta):
            found.append(own)
        here = component[0]
    while here and here not in seen and len(seen) < 10:
        seen.add(here)
        meta = _meta_at(repo, ref, f"{changes_rel}/{here}" if changes_rel else here)
        if (own := own_ticket(meta)) and own not in found:
            found.append(own)
        parent = meta.get("parent")
        here = normalize_change_name(parent) if isinstance(parent, str) else ""
    return found


def tickets_in(change_dir: Path) -> list[str]:
    """`tickets_at` for a change dir on disk (its parents beside it; a
    component's change above it)."""
    from multiplai_core.specs import change_root, read_change_meta

    found: list[str] = []
    here, seen = Path(change_dir), set()
    while here not in seen and len(seen) < 10:
        seen.add(here)
        try:
            meta = read_change_meta(here) if here.is_dir() else {}
        except (OSError, UnicodeError, ValueError):
            meta = {}
        if (own := own_ticket(meta)) and own not in found:
            found.append(own)
        if change_root(here) != here:
            here = change_root(here)
            continue
        parent = meta.get("parent")
        if not isinstance(parent, str) or not parent:
            break
        here = here.parent / parent
    return found


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


def _ours(repo: Path, w: Path) -> bool:
    """Whether *w* is a linked worktree of *repo*."""
    res = git_ops._run(["git", "worktree", "list", "--porcelain"], cwd=repo)
    return res.ok and any(
        line == f"worktree {w}"
        or Path(line[len("worktree ") :]).resolve() == w.resolve()
        for line in res.stdout.splitlines()
        if line.startswith("worktree ")
    )


def _checkout(repo: Path, w: Path, sha: str) -> str | None:
    """The detached worktree *w* at *sha*, made when missing; a merge an earlier
    run left in it is aborted first. None when it is there, else why not."""
    if w.exists() and not _ours(repo, w):
        return (
            f"{w} exists but is not a worktree of {repo}; move it aside, then run again"
        )
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


def _dirty(w: Path) -> list[str]:
    res = _git(
        w,
        "status",
        "--porcelain",
        "--untracked-files=no",
        "--",
        ".",
        *git_ops.BOOKKEEPING_EXCLUDES,
    )
    return (
        [line[3:] for line in res.stdout.splitlines() if line.strip()]
        if res.ok
        else ["?"]
    )


def merge_default_into(w: Path, branch: str) -> Merged:
    """Merge `origin/<default>` into the build worktree *w* (a single change's,
    on *branch* or a branch cut from it) with a merge commit, pushing
    nothing. Fetches first. Skipped, as `up to date` with *error* saying why,
    when a tracked file in *w* has uncommitted changes, so that an abort
    never touches them."""
    w = Path(w)
    failed = fetch(w)
    if failed is not None:
        return Merged("failed", error=f"git fetch origin failed: {failed}")
    default = default_name(w)
    head = git_ops.rev_parse_head(w)
    if default is None or default == branch or head is None:
        return Merged("up to date", sha=head)
    news = _git(w, "rev-list", "--count", f"HEAD..refs/remotes/origin/{default}")
    if not news.ok:
        return Merged(
            "failed", error=f"{news.command} failed: {news.stderr.strip()[-300:]}"
        )
    if news.stdout.strip() == "0":
        return Merged("up to date", sha=head)
    dirty = _dirty(w)
    if dirty:
        log.warning(
            "FEATURE_BRANCH not merging origin/%s into %s: uncommitted %s",
            default,
            w,
            dirty[:5],
        )
        return Merged(
            "up to date",
            sha=head,
            error=f"{w} has uncommitted changes ({', '.join(dirty[:5])}); the next run merges",
        )
    return _merge_in(w, default, branch, push=False)


def _merge_in(w: Path, default: str, branch: str, *, push: bool) -> Merged:
    """`git merge --no-ff origin/<default>` in *w*; a conflict is aborted and
    listed; with *push*, `HEAD` is pushed to *branch* (never forced)."""
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
    if not push:
        log.info(
            "FEATURE_BRANCH merged origin/%s into %s in %s at %s",
            default,
            branch,
            w,
            (sha or "")[:12],
        )
        return Merged("merged", sha=sha)
    pushed = _git(
        w, "push", "origin", f"HEAD:refs/heads/{branch}", timeout=git_ops._PUSH_TIMEOUT
    )
    if not pushed.ok:
        return Merged(
            "failed",
            error=f"git push origin {branch} failed: {(pushed.stderr or pushed.stdout).strip()[-400:]}",
        )
    return Merged("merged", sha=sha)


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
    merged = _merge_in(w, default, branch, push=True)
    if merged.outcome != "merged":
        return merged
    sha = merged.sha
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


def archive_on(repo: Path, branch: str, specs_rel: str, change: str) -> Merged:
    """Archive the change *change* on `origin/<branch>` and push it: in the
    detached worktree `merge_default` uses, `multiplai_core.specs.archive_change`
    merges the capabilities of the change and of each of its components into
    `<specs_rel>/registry/` and moves the change dir to
    `<specs_rel>/archive/<date>-<change>/`; that is committed as
    `chore(specs): archive <change>` and pushed to *branch* (never forced).
    Fetches first. `up to date` when the branch no longer holds the change
    dir (an earlier run archived it), `merged` once pushed, else `failed`."""
    from multiplai_core.specs import archive_change

    repo = Path(repo)
    failed = fetch(repo)
    if failed is not None:
        return Merged("failed", error=f"git fetch origin failed: {failed}")
    tip = _remote_sha(repo, branch)
    if tip is None:
        return Merged("failed", error=f"origin has no {branch}")
    rel = f"{specs_rel}/changes/{change}"
    held = _git(repo, "ls-tree", "--name-only", tip, "--", f"{rel}/.change.yaml")
    if not held.ok:
        return Merged(
            "failed", error=f"{held.command} failed: {held.stderr.strip()[-300:]}"
        )
    if not held.stdout.strip():
        return Merged("up to date", sha=tip)
    w = worktree_path(repo, branch)
    why = _checkout(repo, w, tip)
    if why:
        return Merged("failed", error=why)
    specs = w / specs_rel
    try:
        dest = archive_change(specs / "changes" / change, specs)
    except (OSError, ValueError) as e:
        _git(w, "checkout", "-q", "--detach", "--force", tip)
        _git(w, "clean", "-fdq")
        return Merged("failed", error=f"archiving {rel} failed: {e}")
    paths = [rel, dest.relative_to(w).as_posix(), f"{specs_rel}/registry"]
    added = _git(w, "add", "-A", "--", *paths)
    message = f"chore(specs): archive {change}"
    committed = (
        _git(w, "commit", "-q", "-m", message, "--", *paths) if added.ok else added
    )
    if not committed.ok:
        return Merged(
            "failed",
            error=f"{committed.command} failed: {(committed.stderr or committed.stdout).strip()[-400:]}",
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
        "FEATURE_BRANCH archived %s on %s at %s and pushed",
        change,
        branch,
        (sha or "")[:12],
    )
    return Merged("merged", sha=sha)


def checkout_tip(repo: Path, branch: str) -> tuple[Path, str] | str:
    """The detached worktree `merge_default` uses, at `origin/<branch>` after
    a fetch, and that sha; or why not."""
    repo = Path(repo)
    failed = fetch(repo)
    if failed is not None:
        return f"git fetch origin failed: {failed}"
    tip = _remote_sha(repo, branch)
    if tip is None:
        return f"origin has no {branch}"
    w = worktree_path(repo, branch)
    why = _checkout(repo, w, tip)
    return why if why else (w, tip)


def commit_and_push(
    repo: Path, w: Path, branch: str, message: str, paths: list[str] | None
) -> str | None:
    """Commit *paths* in the detached worktree *w* (`checkout_tip`), or, with
    *paths* None, an empty commit holding only *message*, and push it to
    *branch*, never forced; then update `origin/<branch>` in *repo*. None once
    pushed, else why not."""
    if paths is None:
        committed = _git(w, "commit", "-q", "--allow-empty", "-m", message)
    else:
        added = _git(w, "add", "-A", "--", *paths)
        committed = (
            _git(w, "commit", "-q", "-m", message, "--", *paths) if added.ok else added
        )
    if not committed.ok:
        return f"{committed.command} failed: {(committed.stderr or committed.stdout).strip()[-400:]}"
    pushed = _git(
        w, "push", "origin", f"HEAD:refs/heads/{branch}", timeout=git_ops._PUSH_TIMEOUT
    )
    if not pushed.ok:
        return f"git push origin {branch} failed: {(pushed.stderr or pushed.stdout).strip()[-400:]}"
    _git(
        Path(repo),
        "fetch",
        "origin",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        timeout=git_ops._PUSH_TIMEOUT,
    )
    return None


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
