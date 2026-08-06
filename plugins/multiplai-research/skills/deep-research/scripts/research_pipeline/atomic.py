"""Crash-safe file writes — write to a temp file, fsync, then rename.

Extracted from `state.checkpoint()`, which had this logic inline. It was the
only writer that had it, and it is not the only writer that needs it: the quota
file is rewritten throughout a run, and the report is the artifact the whole run
exists to produce. A plain `write_text()` truncates the destination *before*
writing, so an interrupted write leaves a file that is neither the old content
nor the new one.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path


def replacement_mode(path: Path) -> int:
    """Mode a temp file should carry before it replaces *path*.

    ``mkstemp`` creates 0600, and ``os.replace`` carries the temp file's mode
    onto the destination — so without this, the first write silently turns a
    user-readable file owner-only. Keep the existing file's mode when there is
    one; otherwise use what a plain ``open()`` would have produced.
    """
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        umask = os.umask(0)
        os.umask(umask)
        return 0o666 & ~umask


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write *text* to *path* so a crash leaves either the old file or the new.

    The temp file is created in the same directory so ``os.replace`` is atomic
    (a cross-filesystem rename is not). Both fsyncs are best-effort: they are
    what makes the guarantee survive a *machine* crash rather than only a
    process crash, and a filesystem that refuses them (some network mounts)
    should not fail the write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            # Rename is only atomic with respect to *ordering*; without this the
            # data may still be in the page cache when the rename commits, so a
            # machine crash can leave the new name pointing at a zero-length
            # file — the exact loss this function exists to prevent.
            f.flush()
            with contextlib.suppress(OSError):
                os.fsync(f.fileno())
        with contextlib.suppress(OSError):
            os.chmod(tmp_name, replacement_mode(path))
        os.replace(tmp_name, path)
        # Durability of the rename itself lives in the directory entry.
        with contextlib.suppress(OSError):
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
