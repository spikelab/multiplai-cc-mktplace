"""Scoping a run to its changed lines: reachability and the trace limit before tracing, coverage and the cap after.

Only a run given changed lines is scoped. Before any call, an entry that cannot
reach a changed file through the static import graph is dropped as
`unreachable`; the rest are ranked by import distance, then kind and location,
and those past `trace_limit` are dropped as `over_trace_limit`. After tracing,
a path whose retained hops cover no changed line is dropped as
`no_changed_line_covered`; the rest are put in output order and those past
`cap` are dropped as `over_cap`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from .entries import Entry, output_kind, to_path_entry
from .paths import KIND_RANK, Location, PathDrop, PathHop, TracedPath, order_paths
from .pyindex import PyIndex

log = logging.getLogger(__name__)


def entry_distance(
    entry: Entry, index: PyIndex, changed: Mapping[str, frozenset[int]]
) -> int | None:
    """Import edges from `entry` to a changed file; None when it cannot reach one.

    0 when the entry's location, an invoker's location or its handler file is a
    changed file. Otherwise measured from the handler's module, or from the
    location's module when there is no handler.
    """
    changed_files = frozenset(changed)
    source = entry.handler.path if entry.handler is not None else entry.location.path
    own = {
        source,
        entry.location.path,
        *(i.location.path for i in entry.also_invoked_by),
    }
    if not changed_files.isdisjoint(own):
        return 0
    return index.import_distance(source, changed_files)


def _pre_trace_key(ranked: tuple[int, Entry]) -> tuple[int, int, str, int]:
    """The pre-trace order of a reachable entry: import distance, then kind rank, then location."""
    distance, entry = ranked
    return (
        distance,
        KIND_RANK[output_kind(entry)],
        entry.location.path,
        entry.location.line,
    )


def covered_lines(
    hops: Sequence[PathHop], changed: Mapping[str, frozenset[int]]
) -> list[Location]:
    """The distinct changed lines inside some hop's inclusive line range, sorted by path, then line."""
    covered = {
        (hop.path, line)
        for hop in hops
        for line in changed.get(hop.path, ())
        if hop.line_start <= line <= hop.line_end
    }
    return [Location(path=path, line=line) for path, line in sorted(covered)]


def select_for_trace(
    entries: Sequence[Entry],
    *,
    index: PyIndex,
    changed: Mapping[str, frozenset[int]],
    trace_limit: int,
) -> tuple[list[Entry], list[PathDrop]]:
    """The entries to trace, closest to the change first, and the drops of the unreachable and the excess."""
    reachable: list[tuple[int, Entry]] = []
    drops: list[PathDrop] = []
    for entry in entries:
        distance = entry_distance(entry, index, changed)
        if distance is None:
            drops.append(
                PathDrop(
                    stage="import_reachability",
                    reason="unreachable",
                    detail="no import path to a changed file",
                    entry=to_path_entry(entry),
                )
            )
        else:
            reachable.append((distance, entry))
    reachable.sort(key=_pre_trace_key)
    for rank, (distance, entry) in enumerate(
        reachable[trace_limit:], start=trace_limit + 1
    ):
        detail = f"rank {rank} of {len(reachable)} (import distance {distance}), trace_limit {trace_limit}"
        drops.append(
            PathDrop(
                stage="trace_limit",
                reason="over_trace_limit",
                detail=detail,
                entry=to_path_entry(entry),
            )
        )
    log.info(
        "scope: %d of %d entries reachable, tracing %d",
        len(reachable),
        len(entries),
        min(len(reachable), trace_limit),
    )
    return [entry for _, entry in reachable[:trace_limit]], drops


def keep_covering(
    paths: Sequence[TracedPath], *, changed: Mapping[str, frozenset[int]], cap: int
) -> tuple[list[TracedPath], list[PathDrop]]:
    """The paths that cover a changed line, with their coverage, in output order up to `cap`, and the drops of
    the rest.
    """
    covering: list[TracedPath] = []
    drops: list[PathDrop] = []
    for path in paths:
        covered = covered_lines(path.hops, changed)
        if covered:
            covering.append(path.model_copy(update={"changed_lines_covered": covered}))
        else:
            drops.append(
                PathDrop(
                    stage="no_changed_line",
                    reason="no_changed_line_covered",
                    detail=f"none of {len(path.hops)} retained hops covers a changed line",
                    entry=path.entry,
                )
            )
    ordered = order_paths(covering)
    for rank, path in enumerate(ordered[cap:], start=cap + 1):
        drops.append(
            PathDrop(
                stage="cap",
                reason="over_cap",
                detail=f"rank {rank} of {len(ordered)}, cap {cap}",
                entry=path.entry,
            )
        )
    return ordered[:cap], drops
