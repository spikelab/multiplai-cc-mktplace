"""Filesystem helpers shared across plugin scripts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def claude_config_dir() -> Path:
    """Claude Code's config directory: ``$CLAUDE_CONFIG_DIR`` or ``~/.claude``.

    Vanilla Claude Code exports ``CLAUDE_CONFIG_DIR`` only when the user has
    overridden the location (the kit does; a plain install does not), so the
    documented default ``~/.claude`` is the common case and every consumer
    must fall back to it. A set-but-empty value counts as unset. The result
    is expanded but not validated — callers that need an *existing* directory
    check that themselves.
    """
    raw = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".claude"


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


def atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write *data* as JSON, indent=2 (see :func:`atomic_write`)."""
    atomic_write(path, json.dumps(data, indent=2))
