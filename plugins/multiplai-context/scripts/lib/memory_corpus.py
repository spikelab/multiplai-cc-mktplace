"""The rest of the corpus a proposal can already be duplicating.

``dream._read_memory_files`` returns the memory bank the drafter writes *into*.
Two other places hold text a proposed item can already restate, and neither was
visible to anything in the pipeline:

* the always-loaded ``CLAUDE.md`` files — the global one under
  ``$CLAUDE_CONFIG_DIR``, the workspace one, and ``memory/CLAUDE.md``. The
  drafter is never shown them, so it re-proposes their rules on every run; 12
  of 17 rejections in one measured file were exactly this shape. The global
  file is the largest of the three (30,673 bytes on the measured machine, vs
  12,137 for the workspace one).
* the **shared memory banks**. A proposal can legitimately target a bank file
  (``## Updates for `teamname/dev.md```), and ``memory_dir`` is only the first
  of the ordered banks, so everything past it was unread.

Both are **dedup evidence only**. They never enter the H2 section registry — a
heading in a ``CLAUDE.md`` does not own a section name for routing purposes —
and nothing here ever writes to them.

One module rather than one per caller: the gate (`lib.routing_validation`, at
draft time) and the reviewer's lens (`dream_prescreen.py`, at review time) must
screen against the same files, or a run is clean under one and dirty under the
other.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from lib.fsio import claude_config_dir

logger = logging.getLogger(__name__)

# Read cap per file. The corpus is prose; nothing legitimate here is larger,
# and an accidental binary or a generated dump must not become the corpus.
MAX_FILE_BYTES = 512 * 1024


def workspace_root(paths) -> Path | None:
    """The workspace root, or ``None`` when it cannot be established.

    ``memory_dir`` is *not* a safe basis: it is overridable by the
    ``memory_dir`` plugin option and by ``MEMORY_DIR``, and in the pure
    standalone layout it is ``~/.multiplai/memory`` — so ``parent.parent``
    would be ``$HOME`` and the "workspace" ``CLAUDE.md`` would be
    ``~/CLAUDE.md``. Derive from ``$WORKSPACE`` when the launcher set it, and
    otherwise only from a base that still *looks* like ``.multiplai/``.
    Returning ``None`` drops one file from the corpus; guessing adds a wrong
    one.
    """
    env = os.environ.get("WORKSPACE", "").strip()
    if env:
        candidate = Path(env).expanduser()
        if candidate.is_dir():
            return candidate
    base = Path(paths.diary_dir).parent
    if base.name == ".multiplai":
        return base.parent
    return None


def claude_md_paths(paths) -> list[tuple[str, Path]]:
    """``(label, path)`` for each always-loaded ``CLAUDE.md`` that exists.

    ``memory/CLAUDE.md`` is deliberately **not** here. It is already a
    ``memory_dir/*.md`` file, so every consumer reads it under its own name;
    returning it again would put one file in the corpus twice under two labels
    and emit two warnings for one hit.
    """
    config_dir = claude_config_dir()
    memory_dir = Path(paths.memory_dir)
    root = workspace_root(paths)

    candidates: list[tuple[str, Path]] = [
        ("CLAUDE.md (global)", config_dir / "CLAUDE.md"),
    ]
    if root is not None:
        candidates.append(("CLAUDE.md (workspace)", root / "CLAUDE.md"))

    out: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    try:
        seen.add(memory_dir.resolve() / "CLAUDE.md")
    except OSError:
        pass
    for label, path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        out.append((label, path))
    return out


def bank_paths(paths) -> list[tuple[str, Path]]:
    """``(label, path)`` for every memory file in a **shared** bank.

    The personal bank is skipped — its directory is ``memory_dir``, which
    callers already read. Labels are ``bank/file.md``, the same form a
    proposal uses to target one.
    """
    try:
        banks = paths.memory_banks()
    except Exception:  # a malformed memory-banks.yaml must not break dedup
        logger.exception("memory_banks() failed — shared banks excluded from the corpus")
        return []

    memory_dir = Path(paths.memory_dir)
    out: list[tuple[str, Path]] = []
    for bank in banks:
        path = Path(bank.path)
        if path == memory_dir or not path.is_dir():
            continue
        for f in sorted(path.glob("*.md")):
            if f.name != "learnings.md" and f.is_file():
                out.append((f"{bank.name}/{f.name}", f))
    return out


def read_files(labelled: list[tuple[str, Path]]) -> dict[str, str]:
    """``{label: content}``, skipping anything unreadable or oversized."""
    out: dict[str, str] = {}
    for label, path in labelled:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                logger.info("Corpus: skipping %s (%s) — over %d bytes", label, path, MAX_FILE_BYTES)
                continue
            out[label] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            logger.info("Corpus: skipping unreadable %s (%s)", label, path)
    return out


def extra_contents(paths) -> dict[str, str]:
    """``{label: content}`` for the always-loaded files and the shared banks.

    This is what gets handed to the dedup half of the routing gate as
    ``dedup_extra`` — never as ``memory_contents``, which also builds the
    section registry.
    """
    return read_files(claude_md_paths(paths) + bank_paths(paths))
