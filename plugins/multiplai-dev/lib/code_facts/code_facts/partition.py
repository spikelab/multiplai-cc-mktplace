"""Split a scope into explore units: by directory, then by size.

Each unit stays at or under *limit* lines. A single span already over the
limit goes alone and is flagged `oversize`.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from .models import Scope, Span, Unit


def partition(scope: Scope, limit: int) -> list[Unit]:
    by_dir: dict[str, list[Span]] = {}
    for s in scope.spans:
        by_dir.setdefault(str(PurePosixPath(s.path).parent), []).append(s)
    units: list[Unit] = []

    def add(spans: list[Span], oversize: bool = False) -> None:
        files = list(dict.fromkeys(s.path for s in spans))
        units.append(Unit(id=f"u{len(units) + 1}", files=files, lines=sum(s.lines for s in spans),
                          oversize=oversize, spans=spans))

    # Directories stay whole when they fit in the unit being filled; small
    # directories share a unit; a directory larger than the limit is split by size.
    current: list[Span] = []
    size = 0
    for d in sorted(by_dir):
        spans = sorted(by_dir[d], key=lambda s: (s.path, s.side, s.start))
        total = sum(s.lines for s in spans)
        if size + total > limit:
            if current:
                add(current)
            current, size = [], 0
        for s in spans:
            n = s.lines
            if n > limit:
                add([s], oversize=True)
                continue
            if size + n > limit:
                if current:
                    add(current)
                current, size = [], 0
            current.append(s)
            size += n
    if current:
        add(current)
    return units
