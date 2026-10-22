"""The one module that reads `paths.v1`: the paths stage's result as the rest of the review sees it.

`code_facts.map_paths` writes nothing the review reads directly; the paths
stage writes its result to `paths.json` with `code_facts.write_paths_file`,
and every later reader (the path finders, path ids on findings, the report's
Paths section) goes through `load_paths`, and the path finders read each path
as a `PathView` (`views`), so a schema change is one edit here. After merge,
`assign_paths` gives each finding the ids of the paths it lies on.
This module also turns what the mapping could not do (dropped entries, cut
traces) into coverage records.
"""

from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from code_facts.paths import PathDrop, PathsResult, TracedPath

from .models import DEPS_DIR, CoverageRecord, Finding, ReviewState

log = logging.getLogger(__name__)

PATHS_FILE = "paths.json"

# Coverage level and reason of each drop stage. Scoping is normal and only `info`; a trace that
# failed is an `error`. The model fallback's own drops (a citation that did not check, a
# duplicate of a discovered entry) are scoping too.
DROP_CLASSES: dict[str, tuple[str, str]] = {
    "import_reachability": ("info", "out-of-scope"),
    "no_changed_line": ("info", "out-of-scope"),
    "trace_limit": ("info", "over-cap"),
    "cap": ("info", "over-cap"),
    "trace": ("error", "trace-failed"),
    "model_fallback_citation": ("info", "out-of-scope"),
    "model_fallback_duplicate": ("info", "out-of-scope"),
}


def load_paths(target_dir: Path, state: ReviewState) -> PathsResult | None:
    """The run's `paths.json`, or None unless the paths stage produced one (`ok` or `empty`).

    The status decides, not the file: a `paths.json` beside a state that says
    the stage failed or was skipped is not this run's. A file that does not
    parse is logged and read as no paths.
    """
    if state.paths_status not in {"ok", "empty"}:
        return None
    path = Path(target_dir) / PATHS_FILE
    try:
        return PathsResult.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("cannot read %s: %s", path, e)
        return None


def entry_subject(drop: PathDrop) -> str:
    """How a dropped entry is named in coverage and `state.errors`: its name and declaration line."""
    loc = drop.entry.location
    return f"{drop.entry.name} at {loc.path}:{loc.line}"


def classify_drop(drop: PathDrop) -> CoverageRecord:
    level, reason = DROP_CLASSES.get(drop.stage, ("info", "out-of-scope"))
    return CoverageRecord(
        stage="paths",
        subject=entry_subject(drop),
        level=level,
        reason=reason,
        detail=drop.detail or drop.reason,
    )


def classify_broken(path: TracedPath) -> CoverageRecord:
    """A kept path whose trace was cut: still mapped, but only up to hop `stops_at`."""
    return CoverageRecord(
        stage="paths",
        subject=path.id,
        level="error",
        reason="broken-trace",
        detail=f"stops at hop {path.stops_at}",
    )


class Anchor(NamedTuple):
    """The code an entry dispatches to."""

    path: str
    line: int | None
    symbol: str | None


class Hop(NamedTuple):
    file: str
    line: int | None
    description: str


class E2ETest(NamedTuple):
    """A test listed for a path; `mocked` when it patches hop 2, so it does not count as end to end."""

    id: str
    mocked: bool


@dataclass(frozen=True)
class PathView:
    """One mapped path as a path finder's prompt shows it."""

    id: str
    name: str
    file: str
    line: int | None
    handler: Anchor | None
    invokers: tuple[str, ...]
    hops: tuple[Hop, ...]
    changed_lines: tuple[tuple[str, int], ...]
    e2e: tuple[E2ETest, ...]
    broken_at: int | None  # the last hop the trace kept, when it was cut


def _hop_description(hop) -> str:
    text = f"calls `{hop.symbol_called}`: {hop.quote}"
    return f"{text} ({hop.note})" if hop.note else text


def views(result: PathsResult) -> list[PathView]:
    """Every path of *result* in its order, a repeated id kept once (the first)."""
    out: list[PathView] = []
    seen: set[str] = set()
    for p in result.paths:
        if p.id in seen:
            continue
        seen.add(p.id)
        e = p.entry
        handler = (
            Anchor(e.handler.path, e.handler.line, e.handler.symbol)
            if e.handler
            else None
        )
        invokers = (
            e.invoked_by,
            *(
                f"{i.name} at {i.location.path}:{i.location.line}"
                for i in e.also_invoked_by
            ),
        )
        out.append(
            PathView(
                id=p.id,
                name=e.name,
                file=e.location.path,
                line=e.location.line,
                handler=handler,
                invokers=tuple(invokers),
                hops=tuple(
                    Hop(h.path, h.line_start, _hop_description(h)) for h in p.hops
                ),
                changed_lines=tuple((c.path, c.line) for c in p.changed_lines_covered),
                e2e=tuple(
                    [E2ETest(t.node_id, False) for t in p.tests.e2e]
                    + [E2ETest(t.node_id, True) for t in p.tests.mocks_hop_2]
                ),
                broken_at=p.stops_at,
            )
        )
    return out


def _norm(path: str | None) -> str:
    """A repo-relative POSIX path: `posixpath.normpath`, no leading `./`, no trailing `/`."""
    if not path:
        return ""
    text = posixpath.normpath(path.strip())
    while text.startswith("./"):
        text = text[2:]
    return "" if text == "." else text.rstrip("/")


def _anchor(path: str | None, line) -> tuple[str, int] | None:
    file = _norm(path)
    if not file or not isinstance(line, int) or isinstance(line, bool) or line < 1:
        return None
    return file, line


def anchors(view: PathView) -> list[tuple[str, int]]:
    """(file, line) of the path's entry, its handler and every hop, in that order; a malformed one is skipped."""
    found = [_anchor(view.file, view.line)]
    if view.handler is not None:
        found.append(_anchor(view.handler.path, view.handler.line))
    found += [_anchor(h.file, h.line) for h in view.hops]
    return [a for a in found if a is not None]


def assign_paths(findings: list[Finding], result: PathsResult | None) -> list[Finding]:
    """*findings*, in order, each with `paths` set to the ids of the paths it lies on (None for none).

    A finding lies on a path when one of its citations names the same file as
    an anchor (`anchors`) and its line range contains the anchor's line. A
    citation into `.review-deps/` never counts. Ids are listed once, in
    `paths.json` order. Only `paths` changes; with no *result* the findings
    are returned as they are.
    """
    if result is None:
        return findings
    by_path = [(v.id, anchors(v)) for v in views(result)]
    out = []
    for f in findings:
        ranges = [
            (_norm(c.path), c.line_start, c.line_end)
            for c in f.citations
            if not c.is_web and not _norm(c.path).startswith(DEPS_DIR + "/")
        ]
        ids = [
            pid
            for pid, points in by_path
            if any(
                file == cfile and start <= line <= end
                for file, line in points
                for cfile, start, end in ranges
            )
        ]
        out.append(f.model_copy(update={"paths": ids or None}))
    return out
