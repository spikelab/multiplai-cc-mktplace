"""Find the repositories to search for references to the target.

A single search from a parent directory misses nested repositories whenever
that parent's `.gitignore` lists them (searching `PROJECTS/` finds 7 of the 10
DolceBot repos that mention Channex). So this module finds every `.git` and
the references stage searches each repository from its own root.

Default search root: the nearest ancestor of the target repository that holds
more than one repository. For `PROJECTS/DolceBot/DolceEngine` that is
`PROJECTS/DolceBot/`. `--search-root` and `--repos` override it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .models import RepoInfo
from .target import SKIP_DIRS, head_sha, remote_url

log = logging.getLogger(__name__)

MAX_DEPTH = 4


def find_repo_dirs(root: Path, max_depth: int = MAX_DEPTH) -> list[Path]:
    """Every directory under *root* (itself included) holding a `.git`, to *max_depth* levels."""
    root = root.resolve()
    found: list[Path] = []
    base_depth = len(root.parts)
    for dirpath, dirnames, _ in os.walk(root):
        here = Path(dirpath)
        if (here / ".git").exists():
            found.append(here)
        if len(here.parts) - base_depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and d != ".worktrees" and not d.startswith("."))
    return sorted(found)


def default_search_root(target_repo: Path) -> Path:
    """The nearest ancestor of *target_repo* holding more than one repository."""
    target_repo = target_repo.resolve()
    home = Path.home().resolve()
    for parent in target_repo.parents:
        if parent in (home, Path(parent.anchor)):
            break
        if len(find_repo_dirs(parent)) > 1:
            return parent
    return target_repo


def _unique_key(path: Path, root: Path, taken: set[str]) -> str:
    key = path.name
    if key in taken:
        rel = path.relative_to(root).as_posix() if path != root else path.name
        key = rel.replace("/", "-")
    n = 2
    base = key
    while key in taken:
        key = f"{base}-{n}"
        n += 1
    return key


def discover(target_repo: RepoInfo, *, search_root: str | None = None,
             repos: list[str] | None = None) -> tuple[dict[str, RepoInfo], Path, list[str]]:
    """(repos by key, the search root, warnings). The target repo keeps its key."""
    warnings: list[str] = []
    target_path = Path(target_repo.path).resolve()
    if repos:
        dirs = [Path(r).expanduser().resolve() for r in repos]
        root = Path(os.path.commonpath([str(d) for d in dirs + [target_path]]))
    else:
        root = Path(search_root).expanduser().resolve() if search_root else default_search_root(target_path)
        dirs = find_repo_dirs(root)
    if target_path not in dirs:
        dirs.append(target_path)
    found: dict[str, RepoInfo] = {target_repo.key: target_repo}
    taken = {target_repo.key}
    for d in sorted(dirs):
        if d == target_path:
            continue
        if not (d / ".git").exists():
            warnings.append(f"{d} is not a git repository; not searched")
            continue
        sha = head_sha(d)
        if sha is None:
            warnings.append(f"{d}: HEAD does not resolve; not searched")
            continue
        key = _unique_key(d, root, taken)
        taken.add(key)
        found[key] = RepoInfo(key=key, path=str(d), head_sha=sha, remote_url=remote_url(d))
    return found, root, warnings
