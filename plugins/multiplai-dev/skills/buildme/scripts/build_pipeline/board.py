"""Board seam — which kanban column this build's card sits in.

A state seam plus a JSON file. Nothing here renders a board, schedules cards,
or talks to any board service; it answers one question (`column_for`) and
records the answer to `specs/changes/<name>/.board.json` plus a
`BOARD:<slug>:<Column>` stdout line, alongside the existing `PHASE:`
protocol. The middle field is the normalized change slug (never contains
`:`); column values contain spaces, so consumers must split on the first two
`:` only and take the last field greedily.

## What the pipeline actually drives — read this before trusting a column

Driven, end to end, by a real build:

  * **Shaping** — bootstrap → interview → research → spec generation
    (proposal, requirements).
  * **Planning** — design audit → prototype → the review checkpoint
    (design.md / tasks.md / rubric.md, audited).
  * **In Development** — the TDD build, the documentation update, and the
    respec pass.
  * **In Review** — entered *only* when the PUBLISH phase has pushed the
    build's branch **and** opened its PR. That is what makes the column real:
    a reviewer can `git fetch` the branch and diff it. A run that finishes
    with no PR (`--no-push`, `--no-pr`, `--no-worktree`, or a failed push)
    leaves the card in In Development — reaching `BuildPhase.COMPLETE` is not
    by itself evidence anyone can review anything.

Set, but only at the edges:

  * **Accepted** — the mapping for `BuildPhase.INIT` (a card that exists and
    has not started). The pipeline does not write a card this early: the
    change directory does not exist yet at INIT, and this module never
    creates one (no card without a change dir — see `_record`). In practice
    the first recorded column is Shaping.
  * **Cancelled** — recorded by `record_failure` **only** when the run ended
    unrecoverably, defined as "no resumable checkpoint survives"
    (`.build-state.json` is gone). An ordinary failed phase leaves the
    checkpoint on disk, so the card stays in its last column and a resume
    continues from it.

**Never set by this pipeline: Backlog, Testing, Ready for Prod, Deploying,
Deployed.** They exist in `BoardColumn` (and therefore in `.board.json`'s
schema) so a later multi-agent scheduler has the full vocabulary. buildme has
no staging merge, no QA/E2E stage that can run outside docker, no prod-PR
automation and no deploy machinery, so it must not pretend to move a card into
any of them.

`BlockStatus` never changes the column today — every block state lives inside
In Development. In particular **`BlockStatus.REVIEWING` is not In Review**:
that review is in-process, against the working tree, with no pushed branch for
anyone to fetch. The parameter exists because the block state is the natural
refinement point for a future scheduler, and because it must be explicit that
it does *not* currently move the card.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from .change_manager import archived_change_dirs, normalize_change_name
from .models import BlockStatus, BoardColumn, BuildPhase

log = logging.getLogger(__name__)

BOARD_FILENAME = ".board.json"

# Owner seat per column, from the board definition. A dark factory puts an
# agent in each of these seats; the value names the seat, not a person.
_OWNERS: dict[BoardColumn, str | None] = {
    BoardColumn.BACKLOG: None,
    BoardColumn.ACCEPTED: None,
    BoardColumn.SHAPING: "product",
    BoardColumn.PLANNING: "eng",
    BoardColumn.IN_DEVELOPMENT: "author",
    BoardColumn.IN_REVIEW: "reviewer",
    BoardColumn.TESTING: "product-qa",
    BoardColumn.READY_FOR_PROD: None,
    BoardColumn.DEPLOYING: "ops",
    BoardColumn.DEPLOYED: None,
    BoardColumn.CANCELLED: None,
}

# The phase → column mapping, exhaustive over BuildPhase. See the module
# docstring for which of these the pipeline ever actually records.
_PHASE_COLUMNS: dict[BuildPhase, BoardColumn] = {
    BuildPhase.INIT: BoardColumn.ACCEPTED,
    BuildPhase.BOOTSTRAP: BoardColumn.SHAPING,
    BuildPhase.INTERVIEW_DONE: BoardColumn.SHAPING,
    BuildPhase.RESEARCH: BoardColumn.SHAPING,
    BuildPhase.SPEC_GENERATION: BoardColumn.SHAPING,
    BuildPhase.DESIGN_AUDIT: BoardColumn.PLANNING,
    BuildPhase.PROTOTYPE: BoardColumn.PLANNING,
    BuildPhase.REVIEW: BoardColumn.PLANNING,
    BuildPhase.TDD_BUILD: BoardColumn.IN_DEVELOPMENT,
    # Updating README/CHANGELOG/docs is part of producing the change, not a
    # separate review stage — same column as RESPEC.
    BuildPhase.DOCS_UPDATE: BoardColumn.IN_DEVELOPMENT,
    BuildPhase.RESPEC: BoardColumn.IN_DEVELOPMENT,
    # PUBLISH is still development until the push+PR actually land; the
    # In Review transition is recorded by the publish step from its result.
    BuildPhase.PUBLISH: BoardColumn.IN_DEVELOPMENT,
    BuildPhase.COMPLETE: BoardColumn.IN_REVIEW,
    BuildPhase.FAILED: BoardColumn.CANCELLED,
}


def column_for(phase: BuildPhase, block_status: BlockStatus | None = None) -> BoardColumn:
    """Pure mapping: which column a card is in for a (phase, block status).

    `block_status` is accepted for every phase and currently changes nothing —
    all block states are In Development (see the module docstring; REVIEWING is
    explicitly not In Review).
    """
    return _PHASE_COLUMNS[phase]


def owner_for(column: BoardColumn) -> str | None:
    """The owner seat for a column, or None for the unowned/terminal ones."""
    return _OWNERS[column]


def card_id_for(change_name: str) -> str:
    """Stable card id for a change — the same change is the same card across
    runs and resumes, which is what lets a scheduler correlate them."""
    return f"buildme-{normalize_change_name(change_name)}"


class BoardEvent(BaseModel):
    column: BoardColumn
    at: str
    note: str = ""


class BoardCard(BaseModel):
    """The `.board.json` document."""
    card_id: str
    change_name: str
    column: BoardColumn
    owner_agent: str | None = None
    entered_at: str
    branch: str | None = None
    worktree_path: str | None = None
    pr_url: str | None = None
    history: list[BoardEvent] = Field(default_factory=list)


def board_path(specs_dir: Path, change_name: str) -> Path:
    """Where this change's `.board.json` lives.

    Follows the change directory: in `--auto` runs the archive move happens
    before PUBLISH, so by the time In Review is recorded the card file already
    sits in `specs/archive/<date>-<name>/`. Writing to the active path then
    would resurrect an empty `changes/<name>/` directory.
    """
    norm = normalize_change_name(change_name)
    active = specs_dir / "changes" / norm / BOARD_FILENAME
    if active.parent.is_dir():
        return active
    # Anchored to exactly YYYY-MM-DD-<slug> (archived_change_dirs) — a loose
    # suffix match would let change `foo` write onto `…-bar-foo`'s card.
    archived = [
        d for d in archived_change_dirs(specs_dir, change_name)
        if (d / BOARD_FILENAME).exists()
    ]
    if archived:
        return archived[-1] / BOARD_FILENAME
    return active


def read_card(path: Path) -> BoardCard | None:
    """Load a card, or None when there is no readable one at `path`."""
    if not path.exists():
        return None
    try:
        return BoardCard.model_validate(json.loads(path.read_text()))
    except Exception as e:  # a corrupt card must not fail a build
        log.warning("Ignoring unreadable board card at %s: %s", path, e)
        return None


def record(
    config,
    state,
    phase: BuildPhase | None = None,
    *,
    block_status: BlockStatus | None = None,
    column: BoardColumn | None = None,
    note: str = "",
    progress=None,
) -> BoardColumn | None:
    """Write `.board.json` and announce the column. Returns the column when the
    card moved, None when it was already there.

    The git identity fields are refreshed from the **live** `BuildState` on
    every call, never re-read from `.build-state.json`: after an `--auto`
    archive the state file is already gone, and `pr_url` only ever exists in
    memory at that point.

    Never raises — a board write failure logs and lets the build continue.
    """
    col = column or column_for(phase or state.phase, block_status)
    try:
        return _record(config, state, col, note, progress)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("Could not record board column %s: %s", col.value, e)
        return None


def _record(config, state, col: BoardColumn, note: str, progress) -> BoardColumn | None:
    path = board_path(config.specs_dir, config.change_name)
    # No card without a change dir. Creating the directory here would pollute
    # whatever repo config currently points at (a pre-bootstrap failure runs
    # against the caller's SOURCE repo) with a changes/<name>/ that holds only
    # .board.json — junk this pipeline never cleans up.
    if not path.parent.is_dir():
        log.info(
            "No change dir for '%s' — skipping board record of %s",
            config.change_name, col.value,
        )
        return None

    now = datetime.now(timezone.utc).isoformat()
    card = read_card(path)
    moved = card is None or card.column != col

    if card is None:
        card = BoardCard(
            card_id=card_id_for(config.change_name),
            change_name=config.change_name,
            column=col,
            owner_agent=owner_for(col),
            entered_at=now,
        )
    if moved:
        card.column = col
        card.owner_agent = owner_for(col)
        card.entered_at = now
        card.history.append(BoardEvent(column=col, at=now, note=note))

    card.branch = state.branch or card.branch
    card.worktree_path = state.worktree_path or card.worktree_path
    card.pr_url = state.pr_url or card.pr_url

    # Atomic: a crash mid-write must never leave truncated JSON — read_card
    # treats a corrupt card as absent, which would silently wipe the history.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(card.model_dump_json(indent=2))
    os.replace(tmp, path)

    if not moved:
        return None

    slug = normalize_change_name(config.change_name)
    print(f"BOARD:{slug}:{col.value}", flush=True)
    log.info("BOARD change=%s column=%s owner=%s", slug, col.value, card.owner_agent)
    if progress is not None:
        try:
            progress.log_board(col.value, card.owner_agent, note)
        except OSError as e:
            log.warning("Could not write board column to progress file: %s", e)
    return col


def record_failure(config, state, reason: str = "", progress=None) -> BoardColumn | None:
    """Record `Cancelled` only when the run ended unrecoverably.

    Unrecoverable means no resumable checkpoint survives — the build's
    `.build-state.json` is gone, so nothing can pick the card back up. While a
    checkpoint exists the card deliberately stays in its last column, which is
    where a resume continues from.

    A *completed* build also has no checkpoint (post-success cleanup deletes
    it) — the orchestrator never calls this once its build-succeeded signal is
    set, so a late crash after cleanup cannot be misrecorded as Cancelled.
    """
    raw = state.state_file or ""
    state_file = Path(raw) if raw else config.state_file_path()
    if state_file.exists():
        log.info(
            "Build failed but %s survives — card stays in its current column for resume",
            state_file,
        )
        return None
    return record(config, state, column=BoardColumn.CANCELLED, note=reason, progress=progress)
