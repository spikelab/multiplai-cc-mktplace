"""One running pm-spec per change.

A run takes an exclusive `flock` on `<change_dir>/.pm-spec/run.lock` and holds
it until the command ends. The kernel drops the lock when the process dies,
however it dies, so a held lock means a process is working on that change now.
The lock file holds the holder's pid, host and start time, for the message
only (row S8).

Restated from spec-extract's `runlock.py`: this package does not import
another plugin. A filesystem without `flock` logs a warning and runs unlocked,
as spec-extract does.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
import socket
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

LOCK_FILE = "run.lock"


class Locked(Exception):
    """Another process holds the lock on this change."""

    def __init__(self, run_dir: Path, holder: str):
        self.run_dir = run_dir
        self.holder = holder
        super().__init__(f"a pm-spec run of {run_dir.parent} is running now ({holder}); "
                         "wait for it to finish, then run again")


def lock_path(run_dir: Path) -> Path:
    return Path(run_dir) / LOCK_FILE


def _holder(fd: int) -> str:
    try:
        raw = os.pread(fd, 4096, 0)
        info = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (OSError, ValueError):
        info = {}
    if not isinstance(info, dict) or not info.get("pid"):
        return "holder unknown"
    return f"pid {info.get('pid')} on {info.get('host') or '?'}, since {info.get('since') or '?'}"


@contextlib.contextmanager
def hold(run_dir: Path) -> Iterator[None]:
    """Hold the lock on *run_dir* for the body; raise Locked if another process holds it."""
    path = lock_path(run_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        log.warning("cannot open the run lock %s, running unlocked: %s", path, e)
        fd = None
    if fd is None:
        yield
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise Locked(Path(run_dir), _holder(fd)) from None
            log.warning("cannot lock %s, running unlocked: %s", path, e)
        else:
            info = {"pid": os.getpid(), "host": socket.gethostname(),
                    "since": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            try:
                os.ftruncate(fd, 0)
                os.pwrite(fd, json.dumps(info).encode("utf-8"), 0)
            except OSError as e:  # the lock holds without the note
                log.warning("cannot write the holder into %s: %s", path, e)
        yield
    finally:
        os.close(fd)
