"""The one check a resume runs before it keeps or discards a stopped step's work.

A step that stops (an exception, a timeout, Ctrl-C) leaves edits in the tree.
Its resume keeps them under a wip ref and discards back to where the step
started. It must never do that to work the step did not leave: an edit the
user made after the stop, or a commit added since. So a stopping step records
what the tree held (`record`), and the resume compares the tree with that
record (`others_work`) before it touches anything.

The rules, in order (`others_work`):

1. **Scope.** Only paths under the project directory are checked, leaving out
   buildme's bookkeeping (`git_ops.BOOKKEEPING_EXCLUDES`) and *ignore*, a
   directory the caller owns. Paths outside the project are not checked:
   buildme's commits, keeps and discards leave them alone.
2. **Git fails.** When git cannot list the changes, or cannot read HEAD
   while *head* is given, the resume must not go on.
3. **No record.** With no record, any uncommitted path is refused: the
   step's edits cannot be told from anyone else's.
4. **Record.** A path whose content differs from the record (a path missing
   from it, or one changed after the stop) is refused. A recorded path that
   is now clean is not.
5. **HEAD.** When *head* is given, HEAD must equal it, or be wholly on
   *origin_ref*: a commit added since would be discarded.

Callers choose *head*, *ignore* and *origin_ref*, and keep their own output
format. A later caller may add a keyword parameter only if its default leaves
every existing caller's result unchanged. This module calls git only through
`git_ops`, and it is the only module that compares a tree with a stop record
(`tests/test_stop_record.py` checks that).
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from . import git_ops

log = logging.getLogger(__name__)


class StepStop(BaseModel):
    """What the tree held when a step last stopped: HEAD, and every
    uncommitted path under the project, outside the caller's own directory,
    with a fingerprint of its content (`git_ops.dirty_files`). A resume keeps
    and discards only this."""

    head: str | None = None
    edits: dict[str, str] = Field(default_factory=dict)
    index: dict[str, str] | None = None


def record(project_dir: Path | str, ignore: Path | str | None = None) -> StepStop | None:
    """The tree as a step stops: HEAD and its uncommitted paths with their
    content. None when either cannot be read, so a record always has a head;
    the caller then stores no record, and the resume refuses any uncommitted
    change. Never raises."""
    try:
        edits = git_ops.dirty_files(project_dir, ignore=ignore)
        if edits is None:
            return None
        head = git_ops.rev_parse_head(project_dir)
        if not head:
            return None
        index = git_ops.index_entries(project_dir, ignore=ignore)
        if index is None:
            return None
        return StepStop(head=head, edits=edits, index=index)
    except Exception:
        log.exception("could not record the stop in %s", project_dir)
        return None


def _names(paths) -> str:
    paths = sorted(paths)
    more = f" and {len(paths) - 5} more" if len(paths) > 5 else ""
    return ", ".join(paths[:5]) + more


def others_work(project_dir: Path | str, stop: StepStop | None, *,
                ignore: Path | str | None = None,
                head: str | None = None,
                origin_ref: str | None = None) -> str | None:
    """Why a resume must not keep and discard what the tree holds, or None
    when it may. The reason is one line. Only reads; never raises."""
    try:
        return _others_work(project_dir, stop, ignore=ignore, head=head, origin_ref=origin_ref)
    except Exception as e:
        log.exception("could not compare %s with its stop record", project_dir)
        return f"git could not check {project_dir} ({type(e).__name__}); inspect it before re-running"


def _others_work(project_dir, stop, *, ignore, head, origin_ref) -> str | None:
    on_origin = False
    if head is not None:
        now = git_ops.rev_parse_head(project_dir)
        if not now:
            return f"git could not read HEAD in {project_dir}; inspect it before re-running"
        if now != head:
            on_origin = bool(origin_ref and git_ops.merge_base(project_dir, now, origin_ref) == now)
            if not on_origin:
                return (f"commits were added after the step stopped at {head[:12]}; resuming would "
                        "discard them, so move those commits to another branch first")
    edits = git_ops.dirty_files(project_dir, ignore=ignore)
    if edits is None:
        return f"git could not list the changes in {project_dir}; inspect it before re-running"
    if stop is None:
        if edits:
            return (f"{project_dir} has uncommitted changes ({_names(edits)}), and the stopped "
                    "step recorded none of its own edits, so they cannot be told apart from "
                    "anyone else's; stash them first")
    else:
        if stop.index is None:
            return ("legacy stop record has no index evidence; inspect and save the index and "
                    "working tree separately before restarting in a clean checkout")
        index = git_ops.index_entries(project_dir, ignore=ignore)
        if index is None:
            return f"git could not read the index in {project_dir}; inspect it before re-running"
        # A wholly published HEAD may advance the index with it, but never
        # excuse staged work that differs from that current HEAD.
        # Callers omitting a HEAD constraint keep committed work rather than
        # rewinding it. They may accept an index that moved with a new commit.
        kept_commit = head is None and stop.head and git_ops.rev_parse_head(project_dir) != stop.head
        index_at_head = (on_origin or kept_commit) and git_ops._run(
            ["git", "diff", "--cached", "--quiet", "HEAD", "--", ".", *git_ops.BOOKKEEPING_EXCLUDES],
            cwd=project_dir).ok
        if index != stop.index and not index_at_head:
            paths = {p for p in index.keys() | stop.index.keys() if index.get(p) != stop.index.get(p)}
            return f"index changed after the stopped step ({_names(paths)}); save staged work before re-running"
        theirs = [p for p, fp in edits.items() if stop.edits.get(p) != fp]
        if theirs:
            return (f"{project_dir} has uncommitted changes the stopped step did not leave "
                    f"({_names(theirs)}); stash them first")
    return None
