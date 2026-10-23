"""`python -m build_pipeline findings --pr <n>`: resolve the PR's change,
branch and worktree, then run the round loop.

- Change: `--change`, else from the PR's head branch: `<TICKET>-<name>` for
  a change the branch holds under `specs/changes/` or `specs/archive/` (the
  longest such name), or a build branch from before 0.105.0,
  `buildme/<name>`. Its directory is `specs/changes/<name>/`, else the
  newest `specs/archive/<date>-<name>/`. Neither → exit 2 naming `--change`.
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
import re
import sys
from pathlib import Path

from .. import git_ops
from ..change_manager import (
    archived_change_dirs,
    change_path,
    change_ref,
    normalize_change_name,
)
from ..config import BuildConfig
from . import fetch
from .loop import FindingsRun, run_findings

log = logging.getLogger(__name__)

EXIT_USAGE = 2
BRANCH_PREFIX = "buildme/"
_ARCHIVED = re.compile(r"^\d{4}-\d{2}-\d{2}-")


class ResolveError(RuntimeError):
    """A usage-level refusal (exit 2) with the reason to print."""


def pr_head(repo: str, pr: int, cwd: Path) -> tuple[str, bool]:
    """(head branch, head is in a fork)."""
    res = git_ops._run_gh(
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "headRefName,isCrossRepository",
        ],
        cwd=cwd,
    )
    if not res.ok:
        raise ResolveError(
            f"could not read PR {pr} on {repo}: {(res.stderr or res.stdout).strip()}"
        )
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise ResolveError(f"`gh pr view {pr}` did not return JSON: {e}") from e
    return str(data.get("headRefName") or ""), bool(data.get("isCrossRepository"))


def names_at(root: Path, head: str, specs_rel: str) -> list[str]:
    """The changes *head* holds under `<specs_rel>/changes/` and
    `<specs_rel>/archive/` (dates dropped), as origin has it, else the local
    branch; [] when neither has any."""
    for ref in (f"refs/remotes/origin/{head}", f"refs/heads/{head}"):
        names: list[str] = []
        for sub in ("changes", "archive"):
            path = f"{specs_rel}/{sub}" if specs_rel else sub
            res = git_ops._run(
                ["git", "ls-tree", "-d", "--name-only", f"{ref}:{path}"], cwd=root
            )
            if res.ok:
                names += [_ARCHIVED.sub("", n) for n in res.stdout.split()]
        if names:
            return names
    return []


def change_name_for(change: str, head: str, names: list[str] | tuple = ()) -> str:
    """`--change` (`<change>/<id>` for a component), else the change *head*
    is named for: `<TICKET>-<name>` for one of *names* (the longest), else
    `buildme/<name>`."""
    if change:
        return change_ref(change)
    if head.startswith(BRANCH_PREFIX) and head[len(BRANCH_PREFIX) :]:
        return normalize_change_name(head[len(BRANCH_PREFIX) :])
    named = [n for n in names if len(head) > len(n) + 1 and head.endswith(f"-{n}")]
    if named:
        return normalize_change_name(max(named, key=len))
    raise ResolveError(
        f"the PR's branch {head!r} is neither <TICKET>-<name> for a change it holds "
        "nor buildme/<name>; name the change with --change <name>"
    )


def change_dir_for(specs_dir: Path, name: str) -> Path:
    active = change_path(specs_dir, name)
    if active.is_dir():
        return active
    archived = archived_change_dirs(specs_dir, name)
    if archived:
        return archived[-1]
    raise ResolveError(
        f"no change '{name}' under {specs_dir / 'changes'} or "
        f"{specs_dir / 'archive'}; name it with --change <name>"
    )


def worktree_for(root: Path, branch: str, name: str) -> Path:
    existing = git_ops.worktree_for_branch(root, branch)
    if existing is not None:
        return existing
    dest = (
        git_ops.worktrees_root(root) / f"buildme-findings-{normalize_change_name(name)}"
    )
    if dest.exists():
        raise ResolveError(
            f"{dest} exists but does not have {branch} checked out; "
            "move it or check the branch out there"
        )
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
        raise ResolveError(
            f"PR {args.pr}'s branch lives in a fork; this command pushes to "
            "origin, so it only works on a PR from this repository"
        )
    if not head:
        raise ResolveError(f"PR {args.pr} has no head branch")
    names: list[str] = []
    if not args.change and not head.startswith(BRANCH_PREFIX):
        git_ops._run(["git", "fetch", "-q", "origin"], cwd=root)
        names = names_at(root, head, (project.relative_to(root) / "specs").as_posix())
    name = change_name_for(args.change or "", head, names)
    try:
        wt = worktree_for(root, head, name)
    except git_ops.GitLifecycleError as e:
        raise ResolveError(str(e)) from e
    if git_ops.tree_is_clean(wt):
        ff = git_ops.fast_forward_from_origin(wt, head)
        if not ff.ok:
            log.info(
                "No fast-forward of %s from origin: %s",
                head,
                (ff.stderr or ff.stdout).strip(),
            )
    wt_project = wt / project.relative_to(root)

    cli = argparse.Namespace(**vars(args))
    cli.project_dir = str(wt_project)
    cli.change = name
    config = BuildConfig.from_cli_args(cli)
    config.change_dir = change_dir_for(config.specs_dir, name)
    if getattr(args, "max_rounds", None) is not None:
        config.findings = dataclasses.replace(
            config.findings, max_rounds=args.max_rounds
        )
    return FindingsRun(
        config=config,
        pr=args.pr,
        repo=repo,
        branch=head,
        change_dir=config.change_dir,
        auto=bool(args.auto),
        trust_repo=bool(args.trust_repo),
        max_usd=getattr(args, "max_usd", None),
        severities=getattr(args, "severities", None),
        code_only=bool(getattr(args, "code_only", False)),
        review=not getattr(args, "no_review", False),
        escalate=not getattr(args, "no_escalation", False),
    )


def run_cli(args: argparse.Namespace) -> int:
    try:
        run = resolve(args)
    except (ResolveError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_USAGE
    print(f"FINDINGS:CHANGE:{run.change_dir}")
    print(f"FINDINGS:WORKTREE:{run.project_dir}")
    return asyncio.run(run_findings(run))
