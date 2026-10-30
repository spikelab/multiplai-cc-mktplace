"""The epic lock: one process writes an epic directory at a time (item 6).

A process that writes the epic directory (the split, the approval, a re-run of
an approved build) takes an exclusive `flock` on `<epic dir>/.lock` before its
first write and holds it until it exits, as the review skill's `runlock.py`
does; the kernel drops it however the process dies. The handle is kept here,
once per process: a second `flock` on a new descriptor is refused even within
one process, so code that may run under a lock already taken (the coordinator,
from PR B1) calls `hold` again and gets the held one back. Taking the lock
creates the directory and `.lock`; only the holder writes its pid, host and
start time into it. Python opens the descriptor non-inheritable, so a child
process never holds it.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import socket
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

LOCK_FILE = ".lock"

# epic directory -> the descriptor this process holds the lock on.
_held: dict[Path, int] = {}


class EpicLocked(Exception):
    """Another process holds the lock on this epic directory."""

    def __init__(self, name: str, holder: str):
        self.name = name
        self.holder = holder
        super().__init__(f"epic {name} is running now ({holder})")


def _holder(fd: int) -> str:
    try:
        raw = os.pread(fd, 4096, 0)
        info = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (OSError, ValueError):
        info = {}
    if not isinstance(info, dict) or not info.get("pid"):
        return "holder unknown"
    return f"pid {info.get('pid')} on {info.get('host') or '?'}, since {info.get('since') or '?'}"


def held(epic_dir: Path) -> bool:
    return Path(epic_dir) in _held


def hold(epic_dir: Path, name: str) -> None:
    """Take the lock on *epic_dir* for the rest of this process, or return at
    once if this process already holds it. Raises EpicLocked when another
    process holds it. A filesystem without `flock` logs a warning and runs
    unlocked, as `runlock.py` does."""
    epic_dir = Path(epic_dir)
    if epic_dir in _held:
        return
    epic_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(epic_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            holder = _holder(fd)
            os.close(fd)
            raise EpicLocked(name, holder) from None
        log.warning("cannot lock %s, running unlocked: %s", epic_dir / LOCK_FILE, e)
    else:
        info = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "since": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, json.dumps(info).encode("utf-8"), 0)
        except OSError as e:  # the lock holds without the note
            log.warning("cannot write the holder into %s: %s", epic_dir / LOCK_FILE, e)
    _held[epic_dir] = fd


def holder_elsewhere(epic_dir: Path) -> str | None:
    """Who holds the lock on *epic_dir* when another process does, else None
    (free, held by this process, or no `.lock` yet). Tries the lock without
    waiting and drops it at once, so it never blocks and writes nothing."""
    epic_dir = Path(epic_dir)
    if epic_dir in _held:
        return None
    try:
        fd = os.open(epic_dir / LOCK_FILE, os.O_RDWR)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return _holder(fd)
        return None
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def release_all() -> None:
    """Drop every lock this process holds. Tests only: a real run holds its
    lock until it exits."""
    for fd in _held.values():
        try:
            os.close(fd)
        except OSError:
            pass
    _held.clear()
