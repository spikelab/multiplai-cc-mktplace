"""The EPIC_SPLIT step and what a build does once it is an epic (items 1-9).

`run_epic_split_phase` decides one change or several. One change, or a skip
(`--no-epic-split`, `only` mode), completes the phase and the build goes on.
Several takes the epic lock, saves `epic.json` (`proposed`), renders
`split.md` and `plans/<id>.md`, then saves `state.epic_dir` and completes the
phase in one checkpoint.

`continue_epic` runs on every later build of an epic, and right after the
split: `proposed` prints `EPIC:SPLIT:<split.md>` and exits 3 until
`--approve-split`, which re-checks the table and plans as on disk and saves
`approved` with their hashes. An approved build checks the hashes (a changed
file exits 2) and prints `EPIC:APPROVED:<epic.json>` (exit 0); from PR B1 it
starts the coordinator instead.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..intake import WAITING_EXIT
from ..models import BuildPhase
from . import lock as epic_lock
from . import split, store

log = logging.getLogger(__name__)

MARKER_EPIC_SPLIT = "EPIC:SPLIT"
MARKER_EPIC_APPROVED = "EPIC:APPROVED"
EXIT_FAILED = 1
EXIT_REFUSED = 2


def _error(message: str) -> None:
    log.error("EPIC_SPLIT %s", message)
    print(f"ERROR:{message}", file=sys.stderr, flush=True)


def _repo(config, state) -> Path:
    """The repository the epic belongs to: the build's source repo when it made
    a worktree, else the project directory."""
    return Path(state.source_repo) if state.source_repo else Path(config.project_dir)


def skip_reason(config, args) -> str | None:
    if getattr(args, "no_epic_split", False):
        return "epic-child"
    if config.mode == "only":
        return "only-mode"
    return None


async def run_epic_split_phase(config, state, state_path: Path, args, progress) -> int | None:
    """The EPIC_SPLIT step. Returns an exit code to stop with, or None when the
    phase is complete (the caller then goes on, or hands an epic to
    `continue_epic`)."""
    reason = skip_reason(config, args)
    if reason:
        log.info("SKIP phase=EPIC_SPLIT reason=%s", reason)
        state.epic_single = True        # a retry without the flag skips too (A.S12)
        state.advance_to(BuildPhase.EPIC_SPLIT, state_path)
        return None

    name = config.change_name
    epic_dir = store.epic_dir_for(_repo(config, state), name)
    resumed = (epic_dir / store.EPIC_FILE).exists()
    if resumed:
        # Stopped after epic.json, before the phase was saved (row A.S3), or a
        # checkpoint that lost state.epic_dir: no call; render only the files
        # that are missing, since one on disk may hold the user's edit (A.S8).
        try:
            epic = store.load_epic(epic_dir)
        except store.EpicFileError as e:
            _error(str(e))
            return EXIT_FAILED
        log.info("EPIC_SPLIT epic.json already saved in %s: no call", epic_dir)
    else:
        inputs = split.split_inputs(config.change_dir, config.codebase_analysis_path,
                                    getattr(args, "context_files", []) or [])
        answer = await split.epic_split(config, inputs)
        errors = split.check_answer(answer)
        if errors:
            log.warning("EPIC_SPLIT the answer failed its checks, retrying once: %s", "; ".join(errors))
            answer = await split.epic_split(config, inputs, errors)
            errors = split.check_answer(answer)
            if errors:
                _error("the epic split failed its checks twice: " + "; ".join(errors))
                return EXIT_FAILED
        if answer.single:
            log.info("EPIC_SPLIT single change: %s", answer.reason)
            progress.log_phase("EPIC_SPLIT", f"One change: {answer.reason}")
            state.epic_single = True
            state.advance_to(BuildPhase.EPIC_SPLIT, state_path)
            return None
        epic = store.Epic(name=name, reason=answer.reason, children=answer.children)

    # Only a split that passed the checks gets here, so one change never
    # creates the directory. The lock comes before the first write.
    try:
        epic_lock.hold(epic_dir, name)
    except epic_lock.EpicLocked as e:
        _error(str(e))
        return EXIT_REFUSED
    if (epic_dir / store.EPIC_FILE).exists():
        # Saved before the lock, by this build's earlier run or by another build
        # of the same change while this one was deciding: that epic.json wins,
        # read as it is now, and no file on disk is written over (A.S8).
        try:
            epic = store.load_epic(epic_dir)
        except store.EpicFileError as e:
            _error(str(e))
            return EXIT_FAILED
        resumed = True
    else:
        store.save_epic(epic_dir, epic)
    if epic.state == "proposed":
        store.write_files(epic_dir, epic, keep_existing=resumed)
    state.epic_dir = str(epic_dir)
    state.advance_to(BuildPhase.EPIC_SPLIT, state_path)
    progress.log_phase("EPIC_SPLIT", f"Split into {len(epic.children)} children: {epic_dir / store.SPLIT_FILE}")
    log.info("EPIC_SPLIT wrote %d child plans to %s", len(epic.children), epic_dir)
    return None


def continue_epic(config, state, args, *, just_split: bool = False) -> int:
    """What a build that is an epic does (item 7). *just_split* is True in the
    run that wrote the split: it always waits, so an `--approve-split` given
    before anyone saw the split approves nothing."""
    epic_dir = Path(state.epic_dir)
    name = config.change_name
    split_md = epic_dir / store.SPLIT_FILE
    try:
        epic = store.load_epic(epic_dir)
    except store.EpicFileError as e:
        _error(str(e))
        return EXIT_FAILED

    if epic.state == "proposed" and (just_split or not getattr(args, "approve_split", False)):
        log.info("EPIC_SPLIT waiting for approval: %s", split_md)
        print(f"{MARKER_EPIC_SPLIT}:{split_md}", flush=True)
        return WAITING_EXIT

    try:
        epic_lock.hold(epic_dir, name)
    except epic_lock.EpicLocked as e:
        _error(str(e))
        return EXIT_REFUSED
    try:
        epic = store.load_epic(epic_dir)       # as it is now, under the lock
    except store.EpicFileError as e:
        _error(str(e))
        return EXIT_FAILED

    epic_json = epic_dir / store.EPIC_FILE
    if epic.state == "proposed":
        children, errors = store.read_children(epic_dir)
        if errors:
            _error("the split cannot be approved: " + "; ".join(errors))
            return EXIT_FAILED
        epic.children = children
        epic.hashes = store.file_hashes(epic_dir, children)
        epic.state = "approved"
        epic.approved_at = datetime.now(timezone.utc).isoformat()
        store.save_epic(epic_dir, epic)
        log.info("EPIC_SPLIT approved: %s", epic_json)
    else:
        changed = store.changed_file(epic_dir, epic)
        if changed == store.EPIC_FILE:
            _error(f"{changed} in {epic_dir} does not match the {store.SPLIT_FILE} and plans approved "
                   "with --approve-split; put it back as it was, or start a new epic")
            return EXIT_REFUSED
        if changed:
            _error(f"{changed} in {epic_dir} changed after the split was approved; "
                   "put it back as it was, or start a new epic")
            return EXIT_REFUSED
    print(f"{MARKER_EPIC_APPROVED}:{epic_json}", flush=True)
    return 0
