"""The shared run row of a single build (`multiplai_core.runs`).

`__main__` calls `append` once a `build` command ends, however it ends, with
its exit code (None after Ctrl-C). An epic's parent writes its own row from
the coordinator (`epic.coordinator._run_row`), so a `build` that ran an epic
writes none here once that row is written (`epic_row_written`); an epic
refused or failed before then gets its one row here, of kind `epic`
(`mark_epic`). A child build started by an epic names it in `requested_by`:
the epic's own row already holds that child's spend.

The row copies what the build's checkpoint holds that the scorecard reads
(phase, failed phase, blocks, spend), so deleting the build's worktree loses
none of it. The outcome comes from the exit code:

- 0: `succeeded`;
- 3: `waiting` (`PLAN:WAITING`, `RESEARCH:NEEDED`, `SPLIT:REQUIRED`,
  `BUILD:COMPLETE`: the last marker is the cause);
- 128 + a signal, or None after Ctrl-C: `stopped`;
- anything else: `failed`, with the last marker line as the cause when the
  run printed one, else `exit-<code>`.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from multiplai_core import runs

log = logging.getLogger(__name__)

PRODUCER = runs.producer(Path(__file__).resolve().parents[4], "buildme")
_is_epic = False
_epic_row_written = False

# The marker lines a build prints when it stops short; the last one printed
# is the row's cause. Other marker lines (`BOARD:`, `WORKTREE:`, `PR:`) are not.
STOP_MARKERS = (
    "PLAN:WAITING",
    "RESEARCH:NEEDED",
    "SPLIT:REQUIRED",
    "BUILD:COMPLETE",
    "SPEC:NOT_READY",
    "SPEC:ISSUES",
    "PREFLIGHT:FAILED",
    "FEATURE_BRANCH:CONFLICT",
    "SPEC_EXTRACT:RUNNING",
)
MARKER = re.compile(r"^(" + "|".join(map(re.escape, STOP_MARKERS)) + r")\b")


def mark_epic() -> None:
    """This process runs an epic: a row written here has kind `epic`."""
    global _is_epic
    _is_epic = True


def epic_row_written() -> None:
    """The coordinator wrote this run's row: none is written here."""
    global _epic_row_written
    _epic_row_written = True


def outcome_of(code: int | None, marker: str | None) -> tuple[str, str | None]:
    """(outcome, cause) of a build that exited *code* after printing *marker* last."""
    if code == 0:
        return "succeeded", None
    if code is None:
        return "stopped", "interrupted"
    if code > 128:
        return "stopped", f"signal-{code - 128}"
    if code == 3:
        return "waiting", marker
    return "failed", marker or f"exit-{code}"


def _state(config) -> dict:
    """What the scorecard reads from the build's checkpoint, or {} with none."""
    from .state import BuildState, completed_state_path

    try:
        path = Path(config.state_file_path())
    except Exception:  # noqa: BLE001 - no change dir means no checkpoint
        return {}
    for p in (path, completed_state_path(path)):
        if not p.is_file():
            continue
        try:
            state = BuildState.load(p)
        except Exception as e:  # noqa: BLE001 - an unreadable checkpoint is no data
            log.warning("run row: cannot read %s: %s", p, e)
            return {}
        blocks = state.tdd.blocks if state.tdd else []
        statuses: dict[str, int] = {}
        for b in blocks:
            key = str(getattr(b.status, "value", b.status))
            statuses[key] = statuses.get(key, 0) + 1
        return {
            "phase": str(getattr(state.phase, "value", state.phase)),
            "stopped_on_failure": state.failed_stop is not None,
            "blocks": statuses,
            "checkpoint_cost_usd": (state.budget or {}).get("cost_usd"),
            "branch": state.branch,
            "pr_url": state.pr_url,
            "started_at": state.started_at,
        }
    return {}


def append(config, code: int | None, started_at: str, marker: str | None) -> None:
    """Append this build's row. Never raises."""
    if _epic_row_written:
        return
    try:
        from .budget import process_spend
        from .epic import CHILD_ENV, store
        from .git_ops import repo_root

        project = Path(config.project_dir)
        repo = repo_root(project) or project
        outcome, cause = outcome_of(code, marker)
        spend = process_spend()
        state = _state(config)
        branch = config.feature_branch or state.get("branch")
        child = getattr(config, "epic_child", False) or os.environ.get(CHILD_ENV) == "1"
        row = runs.make_row(
            producer=PRODUCER,
            kind="epic" if _is_epic else "build",
            target={
                "repo": store.repo_folder(repo),
                "remote": store._remote_url(repo),
                "branch": branch,
                "change": config.change_name,
            },
            requested_by=(
                {"branch": branch, "child": config.change_name.rpartition("/")[2]}
                if child
                else None
            ),
            started_at=started_at,
            outcome=outcome,
            cause=cause,
            spend_usd=round(spend.cost_usd, 6),
            run={**state, "exit_code": code, "spend_complete": spend.complete},
        )
    except Exception as e:  # noqa: BLE001 - the row must never fail the build
        log.warning("could not build this build's run row: %s", e)
        return
    runs.append_row(row)
