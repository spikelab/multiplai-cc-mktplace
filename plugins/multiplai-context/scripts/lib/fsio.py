"""Filesystem helpers shared across plugin scripts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, content: str) -> None:
    """Write via tempfile + rename so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: dict, *, indent: int | None = 2) -> None:
    """Atomically write *data* as JSON (see :func:`atomic_write`)."""
    atomic_write(path, json.dumps(data, indent=indent))
