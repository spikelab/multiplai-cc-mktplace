"""Resolve a session ``cwd`` onto a stable project name.

The diary tags each session block with the raw working directory. The "now"
writer groups diary entries by project, and the SessionStart hook injects the
matching project's status. Both must agree on *which project a cwd belongs to*,
so that mapping lives here, in one place.

Resolution order (config-driven, generic-first so it works with no config):

  1. ``project_roots`` — a cwd strictly under a configured parent dir resolves
     to the first path segment beneath that root (longest matching root wins).
     E.g. root ``…/PROJECTS`` + cwd ``…/PROJECTS/foo/sub`` → ``foo``. This
     collapses subdirectories and in-project worktrees onto the project.
  2. ``umbrella_roots`` — a cwd that *is* an umbrella root, or sits under one
     without matching a project root, resolves to ``"workspace"``. This keeps
     cross-project / workspace-root sessions out of the per-project buckets.
  3. ``detection`` default strategy when nothing above matched:
       - ``git`` (default): the enclosing git repo, with linked worktrees
         collapsed onto the main repo; falls back to the cwd basename if cwd
         is not in a git repo.
       - ``basename``: the final path component of cwd.
       - ``roots``: no default — return ``None`` (config-only mode).
  4. Empty / unresolvable cwd → ``None`` (caller skips it).

Config lives at ``paths.project_map_file()`` (``.multiplai/project-map.yaml``);
absent or unreadable means "use defaults". ``resolve_project`` accepts an
explicit ``config`` dict so callers can resolve a batch without re-reading.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

WORKSPACE_PROJECT = "workspace"
_GIT_TIMEOUT_SECONDS = 3

# Placeholder cwd values written by the capture pipeline when the real
# working directory was unavailable. They name no project, so resolution
# returns None and the caller skips the entry rather than inventing a
# bogus "unknown" bucket.
_NULL_CWDS = {"", "unknown", "none", "null"}


def _norm(p: str) -> Path:
    """Absolute, ``~``-expanded, ``..``-collapsed path — no filesystem access.

    We deliberately avoid ``Path.resolve()``: a cwd recorded in an old diary
    may no longer exist on disk, and we still want to match it against config.
    """
    return Path(os.path.normpath(str(Path(p).expanduser())))


def _segment_under(cwd: Path, root: Path) -> Optional[str]:
    """First path segment of *cwd* beneath *root*, or ``None`` if not under it."""
    rparts = root.parts
    cparts = cwd.parts
    if len(cparts) > len(rparts) and cparts[: len(rparts)] == rparts:
        return cparts[len(rparts)]
    return None


def _is_at_or_under(cwd: Path, root: Path) -> bool:
    """True when *cwd* equals *root* or is nested anywhere beneath it."""
    rparts = root.parts
    cparts = cwd.parts
    return len(cparts) >= len(rparts) and cparts[: len(rparts)] == rparts


def _run_git(cwd: str, *args: str) -> Optional[str]:
    """Run ``git -C cwd <args>`` and return stripped stdout, or ``None``."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    val = out.stdout.strip()
    return val or None


def _git_project_name(cwd: str) -> Optional[str]:
    """Name of the enclosing git repo, collapsing worktrees onto the main repo.

    ``--git-common-dir`` points at the *main* repo's ``.git`` even from a linked
    worktree, so its parent is the canonical project root. Falls back to
    ``--show-toplevel`` on older git that lacks ``--path-format``.
    """
    common = _run_git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        p = Path(common)
        name = p.parent.name if p.name == ".git" else p.name
        return name or None
    top = _run_git(cwd, "rev-parse", "--show-toplevel")
    if top:
        return Path(top).name or None
    return None


def resolve_project(cwd: str, config: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Map a session working directory onto a stable project name.

    Returns ``None`` when *cwd* is empty or cannot be attributed to a project.
    Pass *config* (the parsed project-map) to avoid re-reading it per call;
    omit it to load from disk via :func:`load_project_map`.
    """
    if not cwd or cwd.strip().lower() in _NULL_CWDS:
        return None
    if config is None:
        config = load_project_map()

    cwd_path = _norm(cwd)

    # 1. project_roots — longest matching root wins.
    best_name: Optional[str] = None
    best_depth = -1
    for raw_root in config.get("project_roots") or []:
        root = _norm(str(raw_root))
        seg = _segment_under(cwd_path, root)
        if seg is not None and len(root.parts) > best_depth:
            best_depth = len(root.parts)
            best_name = seg
    if best_name:
        return best_name

    # 2. umbrella_roots — workspace-level / cross-project sessions.
    for raw_umbrella in config.get("umbrella_roots") or []:
        if _is_at_or_under(cwd_path, _norm(str(raw_umbrella))):
            return WORKSPACE_PROJECT

    # 3. default strategy.
    detection = str(config.get("detection") or "git").lower()
    if detection == "basename":
        return cwd_path.name or None
    if detection == "roots":
        return None
    # git (default)
    git_name = _git_project_name(cwd)
    if git_name:
        return git_name
    return cwd_path.name or None


def load_project_map() -> dict[str, Any]:
    """Load ``project-map.yaml`` from the workspace root; ``{}`` if absent."""
    try:
        from multiplai_core.paths import get_paths
        from multiplai_core.config import load_yaml

        return load_yaml(get_paths().project_map_file())
    except Exception:
        logger.warning("Could not load project-map.yaml; using defaults")
        return {}
