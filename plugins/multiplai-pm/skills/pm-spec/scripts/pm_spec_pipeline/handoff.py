"""The handoff once the check passes (item 13, row S6).

In order: `tickets.json` is written; the ready record is written with
`multiplai_core.specs.mark_ready`, and the revision it recorded is saved in
the run state with `committed: false`; the change dir alone is committed
(`git_ops.commit_change_dir`, an isolated index); the run state records the
commit; `SPEC:READY:<change_dir>:<spec_revision>` is printed.

Row S6, the commit failed after the ready record was written: a resume
recomputes the revision (`is_ready`). When it still matches, only the commit
is retried. When it does not (the files changed since), the change is set back
to `draft` and the consistency check runs again before a new handoff.
"""

from __future__ import annotations

import logging

from multiplai_core.specs import is_ready, mark_draft, mark_ready

from . import tickets
from .git_ops import GitError, commit_change_dir
from .run import EXIT_FAILED, MARKER_READY, Run, Stop
from .state import Handoff

log = logging.getLogger(__name__)


def commit_message(run: Run) -> str:
    return f"specs({run.config.change_name}): ready for build"


def _commit(run: Run) -> None:
    handoff = run.state.handoff
    try:
        sha = commit_change_dir(run.config.project_dir, run.config.change_dir, commit_message(run))
    except GitError as e:
        run.progress("HANDOFF_ERROR", f"The commit failed: {e}")
        print(f"ERROR: handoff: the ready record is written but the commit failed: {e}\n"
              f"Fix that, then run `resume {run.config.change_name}`: it retries only the commit.", flush=True)
        raise Stop(EXIT_FAILED, str(e)) from e
    handoff.committed = True
    handoff.commit = sha
    run.save()
    run.progress("HANDOFF", f"Ready at {handoff.revision}; " + (f"committed {sha}" if sha else "nothing to commit"))
    print(f"{MARKER_READY}:{run.config.change_dir}:{handoff.revision}", flush=True)


def handoff(run: Run) -> None:
    """Write tickets.json and the ready record, commit the change dir, print SPEC:READY."""
    path = tickets.write(run.config.change_dir, run.config.project_dir)
    log.info("Wrote %s", path)
    revision = mark_ready(run.config.change_dir)
    run.state.handoff = Handoff(revision=revision)
    run.save()
    _commit(run)


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
    log.info("handoff: the ready record no longer matches the files (%s); back to draft", reason)
    mark_draft(run.config.change_dir)
    run.state.handoff = None
    run.save()
    run.progress("HANDOFF_STALE", f"The ready record is stale ({reason}); set to draft and checking again.")
    return False


def retry_commit(run: Run) -> None:
    log.info("handoff: the ready record matches the files; retrying only the commit")
    _commit(run)
