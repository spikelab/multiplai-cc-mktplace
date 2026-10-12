"""One running review per target directory.

The ledger cannot tell a review that is still running from one that was
killed: both leave a `started` line with nothing after it. So every run that
writes a target directory (`review`, `resume`, each target of a batch,
`--plan-only`) first takes an exclusive `flock` on
`<out>/.locks/<slug>.lock` and holds it until the run ends. The kernel drops
the lock when the process dies, however it dies, so a held lock means a
process is running that target now, and a free one means none is. The lock
file holds the holder's pid, host and start time, for the message only.

The lock lives beside the target directories rather than in one, so a run
refused before it writes anything leaves no target directory behind, and
`rounds.keep_round` never copies it. A filesystem that does not support
`flock` logs a warning and runs unlocked, as before this lock existed.
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

LOCKS_DIR = ".locks"


class Locked(Exception):
    """Another process holds the lock on this target directory."""

    def __init__(self, target_dir: Path, holder: str):
        self.target_dir = target_dir
        self.holder = holder
        super().__init__(f"a review of {target_dir} is running now ({holder}); "
                         "wait for it to finish, then run again")


def lock_path(target_dir: Path) -> Path:
    return target_dir.parent / LOCKS_DIR / f"{target_dir.name}.lock"


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
def hold(target_dir: Path) -> Iterator[None]:
    """Hold the lock on *target_dir* for the body; raise Locked if another process holds it."""
    path = lock_path(target_dir)
    try:
        path.parent.mkdir(exist_ok=True)  # not its parents: a missing `--out` is not created here
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
                raise Locked(target_dir, _holder(fd)) from None
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
