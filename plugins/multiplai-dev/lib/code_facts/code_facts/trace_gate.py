"""The trace gate: a pure, model-free check of one trace answer, hop by hop.

`gate_hops` keeps the first 20 hops (recording `hop_limit` when it cut any),
then checks each hop in this order and stops at the first that breaks:

1. `path_outside_root`: the path is absolute, has a `\\`, or has a `..` that leaves the root.
2. `missing_file`: no such file under the root.
3. `line_out_of_range`: start below 1, end before start, or end past the end of the file.
4. `text_mismatch`: the quote does not re-read at the cited lines at head, through `resolve`.
5. `base_side_citation`: the hop says it cites the base side.
6. `entry_mismatch` (hop 1 only): the range holds neither the entry's line nor its handler's line.
7. `unconnected_hop` (hop 2 on): see `_Gate._connected`.
8. `repeated_hop`: the same path and line range as an earlier hop.

Structure checks (1-3) read the snapshot at `root`; the text check re-reads
the commit, so a root that is not the commit's snapshot fails as `text_mismatch`.
"""

from __future__ import annotations

import importlib.util
import io
import logging
import re
import tokenize
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from pydantic import ConfigDict

from .entries import Entry, KnownEdge
from .explore import Resolve, check_citation
from .models import Citation, Side, _Model
from .paths import HOP_LIMIT, BreakCode, Location, PathHop, _Strict, normalise_rel_path
from .pyindex import PyIndex, count_lines

log = logging.getLogger(__name__)

BREAK_CODES: tuple[BreakCode, ...] = (
    "malformed_answer", "empty_trace", "path_outside_root", "missing_file", "line_out_of_range", "text_mismatch",
    "base_side_citation", "entry_mismatch", "unconnected_hop", "repeated_hop", "hop_limit")


class TraceHop(_Model):
    """A hop as a trace agent answers it. Lines are unchecked here so the gate can name a bad range."""
    # A branching answer (a nested `hops`, `alternatives`) must fail to parse, not be ignored.
    model_config = ConfigDict(extra="forbid")

    symbol_called: str
    path: str
    line_start: int
    line_end: int
    quote: str
    note: str = ""
    side: Side = "head"


class TraceAnswer(_Model):
    """One linear chain of hops, from the entry to its last effect."""
    model_config = ConfigDict(extra="forbid")

    hops: list[TraceHop]


class GateResult(_Strict):
    """The hops before the first break, and that break (`broken_at` is 1-based; 21 for `hop_limit`)."""
    accepted: list[PathHop]
    broken_at: int | None
    break_reason: BreakCode | None
    detail: str


def gate_hops(hops: Sequence[TraceHop], *, root: Path, resolve: Resolve, entry: Entry,
              known_edges: Sequence[KnownEdge], places: Sequence[Location],
              index: PyIndex | None = None) -> GateResult:
    """Check `hops` against the snapshot at `root` (see the module docstring). Makes no model call.

    `index` is a `PyIndex` already built for `root`; one is built when a
    connectivity check needs it.
    """
    if not hops:
        return GateResult(accepted=[], broken_at=1, break_reason="empty_trace", detail="the answer has no hops")
    gate = _Gate(root, resolve, entry, known_edges, places, index)
    accepted: list[PathHop] = []
    for n, hop in enumerate(hops[:HOP_LIMIT], start=1):
        checked = gate.check(hop, accepted)
        if isinstance(checked, Break):
            log.debug("trace of %s breaks at hop %d: %s (%s)", entry.name, n, checked.code, checked.detail)
            return GateResult(accepted=accepted, broken_at=n, break_reason=checked.code, detail=checked.detail)
        accepted.append(checked)
    if len(hops) > HOP_LIMIT:
        return GateResult(accepted=accepted, broken_at=HOP_LIMIT + 1, break_reason="hop_limit",
                          detail=f"{len(hops)} hops given; the first {HOP_LIMIT} are kept")
    return GateResult(accepted=accepted, broken_at=None, break_reason=None, detail="")


@dataclass(frozen=True)
class Break:
    """The first rule a hop or citation breaks, and why."""
    code: BreakCode
    detail: str


def check_head_citation(root: Path, resolve: Resolve, path: str, line_start: int, line_end: int,
                        quote: str) -> str | Break:
    """Rules 1-4 for one citation re-read at head: its normalised path when it passes, else its first break."""
    try:
        path = normalise_rel_path(root, path)
    except ValueError as e:
        return Break("path_outside_root", str(e))
    if not (root / path).is_file():
        return Break("missing_file", f"{path} is not a file under the snapshot root")
    lines = count_lines((root / path).read_bytes())
    if not 1 <= line_start <= line_end <= lines:
        return Break("line_out_of_range",
                     f"lines {line_start}-{line_end} are not within the {lines} lines of {path}")
    reason = check_citation(Citation(path=path, line_start=line_start, line_end=line_end, quote=quote, side="head"),
                            resolve)
    if reason is not None:
        return Break("text_mismatch", f"{path}:{line_start}-{line_end}: {reason}")
    return path


class _Gate:
    """One gate run: the snapshot, the entry, its known connections, and the names read so far."""

    def __init__(self, root: Path, resolve: Resolve, entry: Entry, known_edges: Sequence[KnownEdge],
                 places: Sequence[Location], index: PyIndex | None):
        self._root = root
        self._resolve = resolve
        self._entry = entry
        self._edges = known_edges
        self._places = places
        self._given_index = index
        self._names: dict[str, list[tuple[int, str]]] = {}

    @cached_property
    def _index(self) -> PyIndex:
        return self._given_index if self._given_index is not None else PyIndex.build(self._root)

    def check(self, hop: TraceHop, earlier: Sequence[PathHop]) -> PathHop | Break:
        """The hop as accepted, or its first break given the hops `earlier` accepted."""
        cited = self._cite(hop)
        if isinstance(cited, Break):
            return cited
        return self._link(cited, earlier) or cited

    def _cite(self, hop: TraceHop) -> PathHop | Break:
        """Rules 1-5: the hop as a head-side citation that re-reads, or the first rule it breaks."""
        # Always re-read at head: a base-side hop is reported by its own rule, after the text.
        path = check_head_citation(self._root, self._resolve, hop.path, hop.line_start, hop.line_end, hop.quote)
        if isinstance(path, Break):
            return path
        cited = PathHop(symbol_called=hop.symbol_called, path=path, line_start=hop.line_start,
                        line_end=hop.line_end, quote=hop.quote, note=hop.note)
        if hop.side == "base":
            return Break("base_side_citation", f"{_where(cited)} cites the base side")
        return cited

    def _link(self, cited: PathHop, earlier: Sequence[PathHop]) -> Break | None:
        """Rules 6-8: the first rule a re-read hop breaks in its place after `earlier`, or None."""
        if not earlier:
            if not self._anchored(cited):
                return Break("entry_mismatch", f"{_where(cited)} holds neither the entry line "
                                               f"{self._entry.location.path}:{self._entry.location.line} "
                                               "nor its handler's line")
        elif not self._connected(earlier[-1], cited):
            return Break("unconnected_hop", f"nothing connects {_where(earlier[-1])} to {cited.symbol_called!r} "
                                            f"at {_where(cited)}")
        if any(_span(h) == _span(cited) for h in earlier):
            return Break("repeated_hop", f"{_where(cited)} is already an earlier hop")
        return None

    def _anchored(self, hop: PathHop) -> bool:
        handler = self._entry.handler
        anchors = [self._entry.location] + ([Location(path=handler.path, line=handler.line)] if handler else [])
        return any(_holds(hop, anchor) for anchor in anchors)

    def _connected(self, prev: PathHop, hop: PathHop) -> bool:
        """Whether `hop` follows `prev`: by name, by a known edge, or at a known place.

        By name: the last dotted part of `hop`'s symbol is a NAME token in the
        `def`/`class` (or module) enclosing `prev`'s first line, so a comment or
        a string never counts. When `prev` is not parsed Python, that name must
        be a whole word of `prev`'s quote. A known edge must run from that same
        scope (`prev`'s cited range outside Python) into `hop`'s range; a place
        must lie in `hop`'s range.
        """
        start, end = self._scope(prev)
        named = self._is_named(prev, hop.symbol_called.strip().rsplit(".", 1)[-1], start, end)
        by_edge = any(edge.from_.path == prev.path and start <= edge.from_.line <= end and _holds(hop, edge.to)
                      for edge in self._edges)
        return named or by_edge or any(_holds(hop, place) for place in self._places)

    def _is_python(self, hop: PathHop) -> bool:
        return self._index.tree(hop.path) is not None

    def _scope(self, hop: PathHop) -> tuple[int, int]:
        """The lines a connection from `hop` may start in: its enclosing scope, or its cited range outside Python."""
        if self._is_python(hop):
            return self._index.enclosing_scope(hop.path, hop.line_start)
        return hop.line_start, hop.line_end

    def _is_named(self, hop: PathHop, name: str, start: int, end: int) -> bool:
        """Whether `name` is a NAME token in lines `start`-`end` of `hop`'s file, or a whole word of its quote."""
        if self._is_python(hop):
            return any(start <= line <= end and token == name for line, token in self._name_tokens(hop.path))
        return bool(name) and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", hop.quote) is not None

    def _name_tokens(self, path: str) -> list[tuple[int, str]]:
        """`(line, name)` of every NAME token of a parsed Python file."""
        if path not in self._names:
            text = importlib.util.decode_source((self._root / path).read_bytes())
            tokens = tokenize.generate_tokens(io.StringIO(text).readline)
            self._names[path] = [(tok.start[0], tok.string) for tok in tokens if tok.type == tokenize.NAME]
        return self._names[path]


def _holds(hop: PathHop, location: Location) -> bool:
    return location.path == hop.path and hop.line_start <= location.line <= hop.line_end


def _span(hop: PathHop) -> tuple[str, int, int]:
    return hop.path, hop.line_start, hop.line_end


def _where(hop: PathHop) -> str:
    return f"{hop.path}:{hop.line_start}-{hop.line_end}"
