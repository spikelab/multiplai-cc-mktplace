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
    """Write via tempfile + rename so readers never see a partial file.

    The explicit ``chmod`` is not cosmetic. ``mkstemp`` creates 0600, while the
    ``write_text`` calls that now route through here left 0644 — and some of
    these files are read by a *different* process than the one that wrote them
    (``claude.sh`` on the Mac drains the extraction markers a container wrote).
    A 0600 marker an unmatched uid cannot read is indistinguishable, to the
    drain's ``except OSError``, from no marker at all: a day's diary and
    learnings would go missing with nothing logged.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, 0o644)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: dict) -> None:
    r"""Atomically write *data* as JSON, indent=2, trailing newline.

    The newline keeps these POSIX text files, which is what the hand-rolled
    ``json.dumps(...) + "\n"`` calls that now route through here produced. Without
    it `git diff` reports "\ No newline at end of file" on any of them that is
    committed, and `cat` runs the shell prompt onto the closing brace.
    """
    atomic_write(path, json.dumps(data, indent=2) + "\n")
