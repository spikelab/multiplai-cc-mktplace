"""One running spec-extract per run folder.

A run takes an exclusive `flock` on `<root>/<repo>/.locks/<object>.lock`,
beside the run folder `<root>/<repo>/<object>/`, and holds it until the run
ends. The kernel drops the lock when the process dies, however it dies, so a
held lock means a process is running that target now. The lock file holds the
holder's pid, host and start time, for the message only.

The lock lives outside the run folder, so `--restart`'s rename never moves it
and a run refused before it writes anything leaves no run folder behind.
Restated from review's `runlock.py` (plan, Decided 14): this package does not
import review. A filesystem without `flock` logs a warning and runs unlocked,
as review does.
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
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

LOCKS_DIR = ".locks"


class Locked(Exception):
    """Another process holds the lock on this run folder."""

    def __init__(self, run_dir: Path, holder: str):
        self.run_dir = run_dir
        self.holder = holder
        super().__init__(
            f"a spec-extract run of {run_dir} is running now ({holder}); "
            "wait for it to finish, then run again"
        )


def lock_path(run_dir: Path) -> Path:
    return run_dir.parent / LOCKS_DIR / f"{run_dir.name}.lock"


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
    """Hold the lock on *run_dir* for the body; raise Locked if another process holds it.

    Creates `.locks/` in *run_dir*'s parent, which must exist, and nothing else.
    """
    path = lock_path(run_dir)
    try:
        path.parent.mkdir(exist_ok=True)
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
                raise Locked(run_dir, _holder(fd)) from None
            log.warning("cannot lock %s, running unlocked: %s", path, e)
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
                log.warning("cannot write the holder into %s: %s", path, e)
        yield
    finally:
        os.close(fd)
