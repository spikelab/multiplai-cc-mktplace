"""Split the target's files into explore units: by directory, then by size.

Each unit stays at or under `--unit-lines`. A single file already over the
limit goes alone and is flagged `oversize`. `gates.partition_gate` checks that
every file is in exactly one unit. The split itself is `code_facts.partition`.
"""

from __future__ import annotations

from code_facts import Scope
from code_facts import partition as partition_scope

from .models import TargetInfo, Unit


def partition(target: TargetInfo, limit: int) -> list[Unit]:
    # Split on repo-relative paths, so directories sort as before, then make
    # every path workspace-relative.
    scope = Scope.whole_files((f, target.file_lines.get(f, 0)) for f in target.files)
    units = partition_scope(scope, limit)
    for u in units:
        u.files = [target.ws(f) for f in u.files]
        for s in u.spans:
            s.path = target.ws(s.path)
    return units


def ws_lines(target: TargetInfo) -> dict[str, int]:
    return {target.ws(f): n for f, n in target.file_lines.items()}
