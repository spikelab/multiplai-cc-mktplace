"""Repo hygiene — work in flight that never became a PR.

The session registry knows which agents are running and GitHub knows which pull
requests are open. Between those two lies the work that is neither: a dirty
tree nobody committed, a branch pushed and then forgotten, a worktree whose
branch merged three weeks ago. It is invisible to both views and it is where
half of "wait, what was I doing" lives.

Pure ``git``, no network. Every call is bounded and every failure degrades to
an ``error`` string on the record rather than an exception.
"""

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from lib.fleet_sources.common import MAX_WORKERS, Ran, run

logger = logging.getLogger(__name__)

# How deep under the workspace to look for checkouts. Three covers
# `PROJECTS/<project>` and one level of nesting (`PROJECTS/DolceBot/DolceEngine`),
# which is the deepest real layout; going deeper mostly finds vendored copies.
MAX_DEPTH = 3

# How long one repo may spend before the rest of its questions go unanswered.
# Six bounded `git` calls at the 5s default is 30s for a single stalled mount,
# which breaks the "a few seconds and one unreachable line" promise the timeout
# was there to keep. Ten seconds is well clear of a cold-cache repo on a healthy
# disk (measured: under 0.5s for the whole set) and well under the stall case.
REPO_BUDGET = 10.0

# Directories never worth descending into. `.worktrees` is excluded on purpose:
# worktrees are not independent repos and are reported *per repo* by
# `git worktree list`, so walking into them would double-count every branch.
_SKIP_DIRS = frozenset({
    ".git", ".worktrees", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages", ".next",
    "dist", "build", ".tox",
})

# git@github.com:owner/name.git | https://github.com/owner/name(.git)
_REMOTE_RE = re.compile(r"[:/](?P<slug>[^/:]+/[^/]+?)(?:\.git)?/?$")


@dataclass
class RepoState:
    """One checkout, as far as `git` alone can describe it."""

    path: str                                   # workspace-relative
    slug: str = ""                              # owner/name, "" without a remote
    branch: str = ""
    dirty: int = 0                              # changed + untracked paths
    untracked: int = 0
    unpushed: list[str] = field(default_factory=list)   # "branch (ahead 3)"
    no_upstream: list[str] = field(default_factory=list)
    worktrees: list[str] = field(default_factory=list)  # paths, minus the main one
    error: str = ""

    @property
    def clean(self) -> bool:
        """Nothing here is asking for attention."""
        return not (self.dirty or self.unpushed or self.no_upstream or self.error)


def find_repos(workspace: Path, max_depth: int = MAX_DEPTH) -> list[Path]:
    """Every git checkout under *workspace*, the workspace itself included.

    Descends *into* repos rather than stopping at the first ``.git``, because
    the real layout nests them — ``PROJECTS/DolceBot`` owns its loose files and
    ``PROJECTS/DolceBot/DolceEngine`` is a separate repo with its own remote.
    Stopping at the parent would hide nine of them.
    """
    found: list[Path] = []
    workspace = Path(workspace)
    if (workspace / ".git").exists():
        found.append(workspace)

    def walk(base: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            # `with`, because a bare `sorted(os.scandir(...))` leaks the
            # directory handle until GC and raises ResourceWarning under -W error.
            with os.scandir(base) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                continue
            child = Path(entry.path)
            if (child / ".git").exists():
                found.append(child)
            walk(child, depth + 1)

    walk(workspace, 1)
    return found


def _slug(repo: Path) -> str:
    got = run(["git", "remote", "get-url", "origin"], cwd=repo)
    if not got.ok:
        return ""
    m = _REMOTE_RE.search(got.out.strip())
    return m.group("slug") if m else ""


def _branch(repo: Path) -> str:
    got = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    return got.out.strip() if got.ok else ""


def _porcelain_counts(got: Ran) -> tuple[int, int]:
    dirty = untracked = 0
    for line in got.lines:
        dirty += 1
        if line.startswith("??"):
            untracked += 1
    return dirty, untracked


def _has_remote(repo: Path) -> bool:
    return bool(run(["git", "remote"], cwd=repo).lines)


def _tracking(repo: Path, has_remote: bool) -> tuple[list[str], list[str]]:
    """``(unpushed, no_upstream)`` branch labels.

    ``%(upstream:track)`` renders ``[ahead 3]`` / ``[gone]`` / empty. A branch
    with no upstream at all renders empty *and* has no ``%(upstream)``, which
    is how the two cases are told apart — and they mean different things:
    ahead-of-remote is work that exists elsewhere too, never-pushed is work
    that exists only on this disk.

    **A repo with no remote at all reports neither.** "Never pushed" is only a
    fact worth surfacing when there is somewhere to push *to*; against a local
    scratch repo it flags every branch forever, and a warning that is always on
    is one you stop seeing.
    """
    if not has_remote:
        return [], []
    got = run([
        "git", "for-each-ref",
        "--format=%(refname:short)\t%(upstream)\t%(upstream:track)",
        "refs/heads",
    ], cwd=repo)
    if not got.ok:
        return [], []
    unpushed: list[str] = []
    orphan: list[str] = []
    for line in got.lines:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, upstream, track = parts[0], parts[1], parts[2]
        if not name:
            continue
        if not upstream:
            orphan.append(name)
            continue
        m = re.search(r"ahead (\d+)", track)
        if m:
            unpushed.append(f"{name} (ahead {m.group(1)})")
        elif "gone" in track:
            unpushed.append(f"{name} (upstream gone)")
    return unpushed, orphan


def _worktrees(repo: Path) -> list[str]:
    """Linked worktree paths, excluding the main checkout itself."""
    got = run(["git", "worktree", "list", "--porcelain"], cwd=repo)
    if not got.ok:
        return []
    paths = [ln.split(" ", 1)[1].strip() for ln in got.lines if ln.startswith("worktree ")]
    main = str(repo.resolve())
    return [p for p in paths if str(Path(p).resolve()) != main]


def _load(repo: Path, workspace: Path) -> RepoState:
    try:
        rel = str(repo.relative_to(workspace)) or "."
    except ValueError:
        rel = str(repo)
    state = RepoState(path=rel)

    deadline = time.monotonic() + REPO_BUDGET
    status = run(["git", "status", "--porcelain"], cwd=repo)
    if not status.ok:
        # One clear failure beats five: if `git status` cannot run here, the
        # checkout is broken or unreachable and the other four calls will fail
        # the same way. Say so once and move on.
        state.error = status.err or "git status failed"
        return state

    state.dirty, state.untracked = _porcelain_counts(status)

    # The fast-fail above only catches a checkout that is broken outright. A
    # merely *slow* one answers `git status` after four seconds and then does it
    # again five more times, so one stalled mount could spend 30s of a digest
    # that promises to be bounded. Between calls, stop and report what we have.
    def over_budget() -> bool:
        if time.monotonic() < deadline:
            return False
        state.error = f"partial: exceeded the {REPO_BUDGET:g}s per-repo budget"
        return True

    if over_budget():
        return state
    state.slug = _slug(repo)
    state.branch = _branch(repo)
    if over_budget():
        return state
    state.unpushed, state.no_upstream = _tracking(repo, _has_remote(repo))
    if over_budget():
        return state
    state.worktrees = _worktrees(repo)
    return state


def collect_repos(workspace: Path, max_depth: int = MAX_DEPTH) -> list[RepoState]:
    """Describe every checkout under *workspace*, concurrently."""
    repos = find_repos(workspace, max_depth)
    if not repos:
        return []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        states = list(pool.map(lambda r: _load(r, workspace), repos))
    states.sort(key=lambda s: s.path)
    return states
