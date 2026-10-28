"""Where a change lives: its copies in the repo and in live worktrees, and
the one checkout review writes go to.

A change dir is `specs/changes/<name>/`. Copies of it can sit in:

- the repo's own checkout;
- pm-spec's worktree for the change's branch, `<worktrees root>/pm-spec-<branch>`,
  detached at `origin/<branch>` (pm-spec's `branch.py`);
- a build's worktree, which holds `.build-state.json` (or
  `.build-state.complete.json`) in its copy and has the branch checked out.

Worktrees come from `git worktree list` plus every directory directly under
*worktrees_root* that holds the change.

**The write checkout** (B3): the build's worktree when there is one, else
pm-spec's worktree, else the repo when it has the change's branch checked
out, else none (writes refuse). The build's copy wins for buildme files, and
the write checkout's copy is the one documents are read from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from multiplai_core.specs import read_change_meta

from . import gitio

CHANGES = Path("specs") / "changes"
BUILD_STATE = ".build-state.json"
BUILD_STATE_DONE = ".build-state.complete.json"
PM_SPEC_DIR = ".pm-spec"
SKIP = {"archive", "registry"}


@dataclass
class Copy:
    checkout: Path
    kind: str  # "build" | "pm-spec" | "repo" | "other"
    branch: str | None  # what the checkout has checked out; None when detached

    def change_dir(self, name: str) -> Path:
        return self.checkout / CHANGES / name


@dataclass
class Place:
    name: str
    repo: Path
    branch: str | None = None
    copies: list[Copy] = field(default_factory=list)
    write: Copy | None = None

    def build(self) -> Copy | None:
        return next((c for c in self.copies if c.kind == "build"), None)

    def pm_spec(self) -> Copy | None:
        return next((c for c in self.copies if c.kind == "pm-spec"), None)

    def read_dir(self) -> Path:
        """The change dir documents are read from."""
        if self.write is not None:
            return self.write.change_dir(self.name)
        for c in self.copies:
            return c.change_dir(self.name)
        return self.repo / CHANGES / self.name

    def pm_spec_dir(self) -> Path | None:
        """The change dir holding `.pm-spec/` (its run state)."""
        for kind in ("pm-spec", "repo", "build", "other"):
            for c in self.copies:
                if c.kind == kind and (c.change_dir(self.name) / PM_SPEC_DIR).is_dir():
                    return c.change_dir(self.name)
        return None


def _names(checkout: Path) -> set[str]:
    root = checkout / CHANGES
    try:
        return {
            p.name
            for p in root.iterdir()
            if p.is_dir() and not p.name.startswith(".") and p.name not in SKIP
        }
    except OSError:
        return set()


def _checkouts(repo: Path, worktrees_root: Path | None) -> list[gitio.Worktree]:
    repo = repo.resolve()
    seen: dict[Path, gitio.Worktree] = {}
    for wt in gitio.worktrees(repo):
        seen.setdefault(wt.path.resolve(), wt)
    if repo not in seen:
        seen[repo] = gitio.Worktree(repo, gitio.current_branch(repo), None)
    if worktrees_root is not None and worktrees_root.is_dir():
        for d in sorted(worktrees_root.iterdir()):
            r = d.resolve()
            if r in seen or not (d / ".git").exists() or not (d / CHANGES).is_dir():
                continue
            seen[r] = gitio.Worktree(r, gitio.current_branch(r), None)
    return list(seen.values())


def _kind(repo: Path, wt: gitio.Worktree, name: str) -> str:
    d = wt.path / CHANGES / name
    if wt.path.resolve() == repo.resolve():
        return "repo"
    if (d / BUILD_STATE).is_file() or (d / BUILD_STATE_DONE).is_file():
        return "build"
    if wt.path.name.startswith("pm-spec-"):
        return "pm-spec"
    return "other"


def _branch_of(copies: list[Copy], name: str) -> str | None:
    for kind in ("pm-spec", "repo", "build", "other"):
        for c in copies:
            if c.kind != kind:
                continue
            try:
                meta = read_change_meta(c.change_dir(name)) or {}
            except (OSError, ValueError):  # a broken .change.yaml names no branch
                meta = {}
            if isinstance(meta, dict) and isinstance(meta.get("branch"), str):
                return meta["branch"]
    for c in copies:
        if c.kind == "build" and c.branch:
            return c.branch
    return None


def list_changes(repo: Path, worktrees_root: Path | None = None) -> dict[str, Place]:
    """Every change in the repo or a live worktree, by name."""
    repo = Path(repo).resolve()
    checkouts = _checkouts(repo, worktrees_root)
    places: dict[str, Place] = {}
    for wt in checkouts:
        for name in _names(wt.path):
            place = places.setdefault(name, Place(name=name, repo=repo))
            place.copies.append(Copy(wt.path, _kind(repo, wt, name), wt.branch))
    order = {"build": 0, "pm-spec": 1, "repo": 2, "other": 3}
    for place in places.values():
        place.copies.sort(key=lambda c: order[c.kind])
        place.branch = _branch_of(place.copies, place.name)
        place.write = _write_copy(place)
    return dict(sorted(places.items()))


def _write_copy(place: Place) -> Copy | None:
    build = place.build()
    if build is not None:
        return build
    pm = place.pm_spec()
    if pm is not None:
        return pm
    for c in place.copies:
        if c.kind == "repo" and place.branch and c.branch == place.branch:
            return c
    return None
