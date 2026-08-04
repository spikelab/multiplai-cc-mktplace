"""The queues that fill up while you are looking somewhere else.

None of these is urgent on any given evening, which is exactly why they grow:
learnings waiting for a dream run, extraction markers that never drained, an
INBOX nobody swept. Each is cheap to count and impossible to notice, so the
digest carries counts and an oldest-age — enough to tell "healthy" from "this
has been broken for a week" without opening anything.

Counts and ages only. What is *in* the backlog is the dream skill's job and the
INBOX's own; repeating it here would be a second unread list.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Backlog:
    learnings_lines: int = 0
    learnings_files: int = 0
    oldest_learning: str = ""          # YYYY-MM-DD, from the filename
    dreams_pending: int = 0            # unapplied proposals in dreams/
    pending_extractions: int = 0
    failed_extractions: int = 0
    inbox_items: int = 0

    @property
    def empty(self) -> bool:
        return not (
            self.learnings_lines
            or self.dreams_pending
            or self.pending_extractions
            or self.failed_extractions
            or self.inbox_items
        )


def _count_files(directory: Path, pattern: str = "*") -> int:
    try:
        return sum(1 for p in directory.glob(pattern) if p.is_file())
    except OSError:
        return 0


def _count_learnings(learnings_dir: Path) -> tuple[int, int, str]:
    """``(non-blank lines, files, oldest date)`` across per-day learnings files.

    Lines rather than files, matching ``health_check._count_learnings``: the
    unit a dream run consumes is a learning, and one file can hold thirty. Two
    views of the same backlog disagreeing about its size is how you learn to
    ignore both.
    """
    total = files = 0
    oldest = ""
    try:
        entries = sorted(learnings_dir.glob("*.md"))
    except OSError:
        return 0, 0, ""
    for path in entries:
        try:
            content = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not content:
            continue
        total += len([ln for ln in content.splitlines() if ln.strip()])
        files += 1
        if not oldest:
            oldest = path.stem
    return total, files, oldest


def collect_backlog(
    data_dir: Path,
    learnings_dir: Path | None = None,
    dreams_dir: Path | None = None,
    inbox_dir: Path | None = None,
    now: datetime | None = None,
) -> Backlog:
    """Count every pending queue. Never raises; a missing directory is zero."""
    now = now or datetime.now(timezone.utc)
    workspace_root = data_dir.parent.parent

    learnings_dir = learnings_dir or (data_dir.parent / "learnings")
    dreams_dir = dreams_dir or (data_dir.parent / "dreams")
    inbox_dir = inbox_dir or (workspace_root / "INBOX")

    lines, files, oldest = _count_learnings(learnings_dir)
    return Backlog(
        learnings_lines=lines,
        learnings_files=files,
        oldest_learning=oldest,
        # The proposal filename, not every .md in the directory: `dreams/` also
        # holds `memory-lint-latest.md`, which is a report and not something to
        # apply. Counting it announced a pending dream on a workspace that had
        # none (2026-08-04), and "1 dream proposal(s)" that does not exist is
        # how a backlog line stops being believed. Same glob dream.py writes
        # and dream-remember reads.
        dreams_pending=_count_files(dreams_dir, "processed-learnings-*.md"),
        pending_extractions=_count_files(data_dir / "pending_extractions"),
        failed_extractions=_count_files(data_dir / "failed_extractions"),
        # Top level only: INBOX subdirectories are the user's own filing, and
        # recursing turns one swept folder into three hundred "items".
        inbox_items=_count_files(inbox_dir, "*.md"),
    )
