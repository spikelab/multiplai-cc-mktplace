"""`python -m build_pipeline findings --pr <n>`: resolve the PR's change,
branch and worktree, then run the round loop.

- Change: `--change`, else the PR's head branch `buildme/<name>`. Its
  directory is `specs/changes/<name>/`, else the newest
  `specs/archive/<date>-<name>/`. Neither → exit 2 naming `--change`.
- Worktree: the one that already has the PR's branch checked out, else a new
  one at `<worktrees root>/buildme-findings-<name>`. Never removed.
- A PR whose head lives in a fork is refused: the pipeline pushes to `origin`.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
from pathlib import Path

from .. import git_ops
from ..change_manager import archived_change_dirs, normalize_change_name
from ..config import BuildConfig
from . import fetch
from .loop import FindingsRun, run_findings

log = logging.getLogger(__name__)

EXIT_USAGE = 2
BRANCH_PREFIX = "buildme/"


class ResolveError(RuntimeError):
    """A usage-level refusal (exit 2) with the reason to print."""


def pr_head(repo: str, pr: int, cwd: Path) -> tuple[str, bool]:
    """(head branch, head is in a fork)."""
    res = git_ops._run_gh(["gh", "pr", "view", str(pr), "--repo", repo, "--json",
                           "headRefName,isCrossRepository"], cwd=cwd)
    if not res.ok:
        raise ResolveError(f"could not read PR {pr} on {repo}: {(res.stderr or res.stdout).strip()}")
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise ResolveError(f"`gh pr view {pr}` did not return JSON: {e}") from e
    return str(data.get("headRefName") or ""), bool(data.get("isCrossRepository"))


def change_name_for(change: str, head: str) -> str:
    if change:
        return normalize_change_name(change)
    if head.startswith(BRANCH_PREFIX) and head[len(BRANCH_PREFIX):]:
        return normalize_change_name(head[len(BRANCH_PREFIX):])
    raise ResolveError(f"the PR's branch {head!r} is not buildme/<name>; "
                       "name the change with --change <name>")


def change_dir_for(specs_dir: Path, name: str) -> Path:
    active = specs_dir / "changes" / name
    if active.is_dir():
        return active
    archived = archived_change_dirs(specs_dir, name)
    if archived:
        return archived[-1]
    raise ResolveError(f"no change '{name}' under {specs_dir / 'changes'} or "
                       f"{specs_dir / 'archive'}; name it with --change <name>")


def worktree_for(root: Path, branch: str, name: str) -> Path:
    existing = git_ops.worktree_for_branch(root, branch)
    if existing is not None:
        return existing
    dest = git_ops.worktrees_root(root) / f"buildme-findings-{name}"
    if dest.exists():
        raise ResolveError(f"{dest} exists but does not have {branch} checked out; "
                           "move it or check the branch out there")
    git_ops.add_worktree_for_branch(root, branch, dest)
    return dest


def resolve(args: argparse.Namespace) -> FindingsRun:
    project = Path(args.project_dir).expanduser().resolve()
    root = git_ops.repo_root(project)
    if root is None:
        raise ResolveError(f"{project} is not in a git repository")
    try:
        repo = fetch.repo_slug(root)
    except fetch.FetchError as e:
        raise ResolveError(str(e)) from e
    head, fork = pr_head(repo, args.pr, root)
    if fork:
        raise ResolveError(f"PR {args.pr}'s branch lives in a fork; this command pushes to "
                           "origin, so it only works on a PR from this repository")
    if not head:
        raise ResolveError(f"PR {args.pr} has no head branch")
    name = change_name_for(args.change or "", head)
    try:
        wt = worktree_for(root, head, name)
    except git_ops.GitLifecycleError as e:
        raise ResolveError(str(e)) from e
    if git_ops.tree_is_clean(wt):
        ff = git_ops.fast_forward_from_origin(wt, head)
        if not ff.ok:
            log.info("No fast-forward of %s from origin: %s", head, (ff.stderr or ff.stdout).strip())
    wt_project = wt / project.relative_to(root)

    cli = argparse.Namespace(**vars(args))
    cli.project_dir = str(wt_project)
    cli.change = name
    config = BuildConfig.from_cli_args(cli)
    config.change_dir = change_dir_for(config.specs_dir, name)
    if getattr(args, "max_rounds", None) is not None:
        config.findings = dataclasses.replace(config.findings, max_rounds=args.max_rounds)
    return FindingsRun(config=config, pr=args.pr, repo=repo, branch=head,
                       change_dir=config.change_dir, auto=bool(args.auto),
                       trust_repo=bool(args.trust_repo))


def run_cli(args: argparse.Namespace) -> int:
    try:
        run = resolve(args)
    except (ResolveError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_USAGE
    print(f"FINDINGS:CHANGE:{run.change_dir}")
    print(f"FINDINGS:WORKTREE:{run.project_dir}")
    return asyncio.run(run_findings(run))
