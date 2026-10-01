"""Split the target's files into explore units: by directory, then by size.

Each unit stays at or under `--unit-lines`. A single file already over the
limit goes alone and is flagged `oversize`. `gates.partition_gate` checks that
every file is in exactly one unit.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from .models import TargetInfo, Unit


def partition(target: TargetInfo, limit: int) -> list[Unit]:
    by_dir: dict[str, list[str]] = {}
    for f in target.files:
        by_dir.setdefault(str(PurePosixPath(f).parent), []).append(f)
    units: list[Unit] = []

    def flush(files: list[str]) -> None:
        if files:
            lines = sum(target.file_lines.get(f, 0) for f in files)
            units.append(Unit(id=f"u{len(units) + 1}", files=[target.ws(f) for f in files], lines=lines))

    # Directories stay whole when they fit in the unit being filled; small
    # directories share a unit; a directory larger than the limit is split by size.
    current: list[str] = []
    size = 0
    for d in sorted(by_dir):
        files = sorted(by_dir[d])
        total = sum(target.file_lines.get(f, 0) for f in files)
        if size + total > limit:
            flush(current)
            current, size = [], 0
        for f in files:
            n = target.file_lines.get(f, 0)
            if n > limit:
                units.append(Unit(id=f"u{len(units) + 1}", files=[target.ws(f)], lines=n, oversize=True))
                continue
            if size + n > limit:
                flush(current)
                current, size = [], 0
            current.append(f)
            size += n
    flush(current)
    return units


def ws_lines(target: TargetInfo) -> dict[str, int]:
    return {target.ws(f): n for f, n in target.file_lines.items()}
