"""One invocation's context: its settings, its saved state, and its progress file."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import state as state_mod
from .config import SpecConfig
from .state import RunState

log = logging.getLogger(__name__)

# Markers and exit codes (SKILL.md, "Exit codes").
EXIT_DONE, EXIT_FAILED, EXIT_REFUSED, EXIT_WAITING, EXIT_BLOCKED, EXIT_LOCKED = (
    0,
    1,
    2,
    3,
    4,
    5,
)
MARKER_READY = "SPEC:READY"
MARKER_BRANCH = "SPEC:BRANCH"
MARKER_REVIEW = "SPEC:REVIEW"
MARKER_TICKETS = "SPEC:TICKETS"
MARKER_BLOCKED = "SPEC:BLOCKED"
MARKER_WARN = "SPEC:WARN"
MARKER_INTERVIEW_WAITING = "INTERVIEW:WAITING"


class Stop(Exception):
    """End the command with *code*; the message is already printed."""

    def __init__(self, code: int, message: str = ""):
        self.code = code
        super().__init__(message)


@dataclass
class Run:
    config: SpecConfig
    state: RunState
    # `resume --accept` (finding ids), `--accept-reason` and `--check-again`.
    accept: list[str] = field(default_factory=list)
    accept_reason: str = ""
    check_again: bool = False

    def save(self) -> None:
        state_mod.save(self.state, self.config.run_dir)

    def progress(self, phase: str, text: str) -> None:
        """Append one entry to `.pm-spec/progress.md`. Never raises."""
        try:
            state_mod.ensure_run_dir(self.config.run_dir)
            path = self.config.run_dir / state_mod.PROGRESS_FILE
            stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n## {phase} ({stamp})\n\n{text.rstrip()}\n")
        except OSError as e:
            log.warning("cannot write the progress file: %s", e)
