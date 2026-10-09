"""The user's standing video-edit preferences, kept outside the plugin.

`${CLAUDE_CONFIG_DIR:-$HOME/.claude}/multiplai-media/video-edit-preferences.md`
survives plugin updates. The session reads it at the start of every job and
appends lines to it only after the user approves them (SKILL.md, "Learning
loop"); this module never decides what to write.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

HEADER = ("# video-edit preferences\n\n"
          "Standing corrections the user approved. The video-edit skill reads this file at the\n"
          "start of every job. One preference per line; newest at the bottom.\n\n")


def path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / "multiplai-media" / "video-edit-preferences.md"


def read() -> str:
    p = path()
    return p.read_text(encoding="utf-8") if p.exists() else ""


def append(lines: list[str], today: date | None = None) -> Path:
    """Append approved preference lines as `- (YYYY-MM-DD) text`; returns the file."""
    clean = [" ".join(line.split()) for line in lines]
    clean = [c.removeprefix("- ") for c in clean if c]
    if not clean:
        raise ValueError("no preference text given")
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = (today or date.today()).isoformat()
    existing = p.read_text(encoding="utf-8") if p.exists() else HEADER
    if existing and not existing.endswith("\n"):
        existing += "\n"
    p.write_text(existing + "".join(f"- ({stamp}) {c}\n" for c in clean), encoding="utf-8")
    return p
