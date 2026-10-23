"""The CSS the HTML page is styled with: a design-system folder's `fonts.css` and `tokens.css` (`--design-system-dir`),
or the template's own tokens when no folder is given.

A folder is held to the design-system skill's contract (`design_system.check_folder`) before any model call, so a
bad folder costs nothing, and again before any file is written.
"""

from __future__ import annotations

from pathlib import Path

import design_system

FALLBACK = Path(__file__).resolve().parents[1] / "templates" / "tokens.css"


class DesignSystemError(Exception):
    """The design-system folder is missing or breaks the token contract. Exit code 1."""


def check(path: str | Path | None) -> None:
    """Raise DesignSystemError naming every problem of the folder at *path*; nothing to check when None."""
    if not path:
        return
    problems = design_system.check_folder(Path(path))
    if problems:
        raise DesignSystemError(f"design system {path}: {'; '.join(problems)}")


def css(path: str | Path | None) -> tuple[str, str]:
    """`(fonts, tokens)` for the page: the folder's `fonts.css` (empty when it has none) and `tokens.css`, else
    no fonts and the template's own tokens. Raises DesignSystemError for a folder that fails `check`."""
    if not path:
        return "", FALLBACK.read_text(encoding="utf-8")
    check(path)
    folder = Path(path)
    fonts = folder / "fonts.css"
    return (
        fonts.read_text(encoding="utf-8") if fonts.is_file() else "",
        (folder / "tokens.css").read_text(encoding="utf-8"),
    )
