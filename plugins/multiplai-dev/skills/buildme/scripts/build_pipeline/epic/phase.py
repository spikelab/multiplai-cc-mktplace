"""The EPIC_SPLIT step and what a build does once it is an epic (items 1-5, 7, 9).

`run_epic_split_phase` decides one change or several. One change, or a skip
(`--no-epic-split`, `only` mode), completes the phase and the build goes on.
Several saves `epic.json` (`proposed`), renders `split.md` and
`plans/<id>.md`, then saves `state.epic_dir` and completes the phase in one
checkpoint.

`continue_epic` runs on every later build of an epic, and right after the
split: it prints `EPIC:SPLIT:<split.md>` and exits 3.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from ..intake import WAITING_EXIT
from ..models import BuildPhase
from . import split, store

log = logging.getLogger(__name__)

MARKER_EPIC_SPLIT = "EPIC:SPLIT"
EXIT_FAILED = 1


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
        state.advance_to(BuildPhase.EPIC_SPLIT, state_path)
        return None

    name = config.change_name
    epic_dir = store.epic_dir_for(_repo(config, state), name)
    if (epic_dir / store.EPIC_FILE).exists():
        # Stopped after epic.json, before the phase was saved (row A.S3): no
        # call; render every file again, since nobody has seen them yet.
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
            state.advance_to(BuildPhase.EPIC_SPLIT, state_path)
            return None
        epic = store.Epic(name=name, reason=answer.reason, children=answer.children)

    # Only a split that passed the checks gets here, so one change never
    # creates the directory.
    if not (epic_dir / store.EPIC_FILE).exists():
        store.save_epic(epic_dir, epic)
    if epic.state == "proposed":
        store.write_files(epic_dir, epic)
    state.epic_dir = str(epic_dir)
    state.advance_to(BuildPhase.EPIC_SPLIT, state_path)
    progress.log_phase("EPIC_SPLIT", f"Split into {len(epic.children)} children: {epic_dir / store.SPLIT_FILE}")
    log.info("EPIC_SPLIT wrote %d child plans to %s", len(epic.children), epic_dir)
    return None


def continue_epic(config, state, args) -> int:
    """What a build that is an epic does: wait for the split to be read
    (`EPIC:SPLIT`, exit 3). An unreadable epic.json exits 1 and is left as it is."""
    epic_dir = Path(state.epic_dir)
    split_md = epic_dir / store.SPLIT_FILE
    try:
        store.load_epic(epic_dir)
    except store.EpicFileError as e:
        _error(str(e))
        return EXIT_FAILED
    log.info("EPIC_SPLIT waiting: %s", split_md)
    print(f"{MARKER_EPIC_SPLIT}:{split_md}", flush=True)
    return WAITING_EXIT
