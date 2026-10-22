"""The handoff once the check passes (item 13, row S6).

The ready record covers only the files the check read. The consistency step
records the revision it read (taken before its agent call); the handoff first
compares the files' revision now with it, and on any difference sets the
change back to `draft` and returns False, so the caller checks again and no
`SPEC:READY` is printed for files nobody checked. `mark_ready` then hashes the
files once more, and a revision other than the checked one is treated the
same way.

In order: `tickets.json` is written; the ready record is written with
`multiplai_core.specs.mark_ready`, and the revision it recorded is saved in
the run state with `committed: false`; the change dir alone is committed
(`git_ops.commit_change_dir`, an isolated index); the run state records the
commit; `SPEC:READY:<change_dir>:<spec_revision>` is printed.

Row S6, the commit failed after the ready record was written: a resume
recomputes the revision (`is_ready`). When it still matches, only the commit
is retried. When it does not (the files changed since), the change is set back
to `draft` and the consistency check runs again before a new handoff.

A change with an open `spec_issues` (a `spec-issues.md` buildme wrote) is
never handed off and never reported ready: `revise` fixes those first.
"""

from __future__ import annotations

import logging

from multiplai_core.specs import (
    is_ready,
    mark_draft,
    mark_ready,
    read_change_meta,
    spec_revision,
)

from . import consistency, tickets
from .git_ops import GitError, commit_change_dir
from .run import EXIT_BLOCKED, EXIT_FAILED, MARKER_BLOCKED, MARKER_READY, Run, Stop
from .state import Handoff

log = logging.getLogger(__name__)


def commit_message(run: Run) -> str:
    return f"specs({run.config.change_name}): ready for build"


def _commit(run: Run) -> None:
    handoff = run.state.handoff
    try:
        sha = commit_change_dir(
            run.config.project_dir, run.config.change_dir, commit_message(run)
        )
    except GitError as e:
        run.progress("HANDOFF_ERROR", f"The commit failed: {e}")
        print(
            f"ERROR: handoff: the ready record is written but the commit failed: {e}\n"
            f"Fix that, then run `resume {run.config.change_name}`: it retries only the commit.",
            flush=True,
        )
        raise Stop(EXIT_FAILED, str(e)) from e
    handoff.committed = True
    handoff.commit = sha
    run.save()
    run.progress(
        "HANDOFF",
        f"Ready at {handoff.revision}; "
        + (f"committed {sha}" if sha else "nothing to commit"),
    )
    print(f"{MARKER_READY}:{run.config.change_dir}:{handoff.revision}", flush=True)


MAX_CHECKS = 3


def open_issues(run: Run) -> str | None:
    """The `spec_issues` path in `.change.yaml`, or None when no issue is open."""
    value = read_change_meta(run.config.change_dir).get("spec_issues")
    return str(value) if value else None


def refuse_open_issues(run: Run) -> None:
    """Stop with exit 4 while requirements issues are open: never ready, never a handoff."""
    issues = open_issues(run)
    if issues is None:
        return
    name = run.config.change_name
    print(
        f"{name} has open requirements issues in {run.config.change_dir / issues}; "
        f"it cannot be ready until `revise {name}` resolves them.",
        flush=True,
    )
    print(f"{MARKER_BLOCKED}:{run.config.change_dir}", flush=True)
    raise Stop(EXIT_BLOCKED, "open requirements issues")


def _stale(run: Run, why: str) -> bool:
    log.info("handoff: %s; back to draft and checking again", why)
    mark_draft(run.config.change_dir)
    run.state.handoff = None
    run.save()
    run.progress("HANDOFF_STALE", f"{why}; set to draft and checking again.")
    return False


def handoff(run: Run) -> bool:
    """Write tickets.json and the ready record, commit the change dir, print SPEC:READY.
    False (and `draft`) when the files are not the ones the check passed."""
    refuse_open_issues(run)
    change_dir = run.config.change_dir
    step = run.state.steps.get(consistency.NAME)
    checked = step.revision if step and step.outcome == "passed" else ""
    if not checked:
        return _stale(run, "the consistency check has not passed")
    if spec_revision(change_dir) != checked:
        return _stale(run, "the files changed after the check read them")
    path = tickets.write(change_dir, run.config.project_dir)
    log.info("Wrote %s", path)
    revision = mark_ready(change_dir)
    if revision != checked:
        return _stale(run, "the files changed while the ready record was written")
    run.state.handoff = Handoff(revision=revision)
    run.save()
    _commit(run)
    return True


async def check_and_hand_off(run: Run) -> None:
    """The consistency check, then the handoff; the check again when the files
    changed under it, at most `MAX_CHECKS` times."""
    for _ in range(MAX_CHECKS):
        await consistency.run_consistency(run)
        if handoff(run):
            return
    print(
        f"ERROR: handoff: the files of {run.config.change_name} kept changing while they were checked; "
        "run `resume` once they are saved.",
        flush=True,
    )
    raise Stop(EXIT_FAILED, "files kept changing")


def pending_commit(run: Run) -> bool:
    """Row S6 on resume: True when only the commit is left to do (the commit is
    then retried by `retry_commit`). A ready record gone stale is set back to
    draft, the handoff forgotten, and False returned, so the check runs again."""
    handoff_state = run.state.handoff
    if handoff_state is None or handoff_state.committed:
        return False
    ready, reason = is_ready(run.config.change_dir)
    if ready:
        return True
    log.info(
        "handoff: the ready record no longer matches the files (%s); back to draft",
        reason,
    )
    mark_draft(run.config.change_dir)
    run.state.handoff = None
    run.save()
    run.progress(
        "HANDOFF_STALE",
        f"The ready record is stale ({reason}); set to draft and checking again.",
    )
    return False


def retry_commit(run: Run) -> None:
    refuse_open_issues(run)
    log.info("handoff: the ready record matches the files; retrying only the commit")
    _commit(run)
