"""The model fallback: one read-only agent asked for the entry points static discovery missed.

`run_fallback` makes at most one call, through `ask`, under a content key
(`fallback_key`), so a rerun with the same answers asks nothing. The answer
may be a `FallbackAnswer`, a dict or a JSON string; one that does not parse,
or a call that raises one of `recoverable`, fails the fallback and adds no
entry. Any other exception propagates.

Each listed entry is then checked in this order, and the first check it fails
drops it:

1. `missing_citation`: it has no citation.
2. `citation_unverified`: its citation does not re-read at head (the gate's
   rules 1-4: inside the root, an existing file, lines in range, the quote).
3. `duplicate`: its cited line is a line a discovered entry already holds
   (its location, an invoker's location, a known edge's `from` line or its
   handler's line), or the line of a fallback entry kept before it; or it
   resolves to a discovered entry's handler.

Entries are checked in the order of what they cite, so which of two entries
citing one line is kept does not depend on the order the model listed them.
A kept entry has kind `model`, its location at the cited line, and as handler
the function `def` enclosing that line in a Python file. A line in a class body
outside any method has no handler.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, get_args

from pydantic import field_validator

from .cache import cache_key
from .entries import Entry, EntryKind, to_path_entry
from .explore import CALL_ERROR
from .models import Citation, _Model
from .paths import DropReason, DropStage, FallbackStatus, Handler, Location, PathDrop, normalise_rel_path
from .prompts import PATHS_FALLBACK_PROMPT_VERSION, paths_fallback
from .pyindex import PyIndex
from .trace_gate import Break, check_head_citation
from .tracing import MALFORMED, TraceContext, encode_answer

log = logging.getLogger(__name__)

FALLBACK_KINDS = frozenset(get_args(EntryKind))
OTHER_KIND = "other"
FALLBACK_LABEL = "paths:fallback"

# The location a dropped entry is recorded at when it cites no line under the snapshot root.
UNPLACED = Location(path="(unplaced)", line=1)

Line = tuple[str, int]


class FallbackEntry(_Model):
    """An entry point the fallback agent lists. A kind outside discovery's kinds becomes `other`."""
    kind: str
    name: str
    citation: Citation | None = None
    reason: str

    @field_validator("kind", mode="before")
    @classmethod
    def _known_or_other(cls, value: Any) -> str:
        return value if isinstance(value, str) and value in FALLBACK_KINDS else OTHER_KIND


class FallbackAnswer(_Model):
    entries: list[FallbackEntry]


def fallback_key(commit: str, entries: Sequence[Entry], scope: Sequence[str] | None) -> str:
    """The content key of the fallback request; independent of the order of `entries` and `scope`."""
    return cache_key("fallback", {
        "version": PATHS_FALLBACK_PROMPT_VERSION,
        "commit": commit,
        "entries": sorted([e.kind, e.location.path, e.location.line] for e in entries),
        "scope": None if scope is None else sorted(set(scope)),
    })


def encode_fallback_answer(out: Any) -> dict:
    """A fallback answer as stored: the validated FallbackAnswer's dump, or the reason it does not parse."""
    return encode_answer(FallbackAnswer, out)


async def run_fallback(*, ctx: TraceContext, entries: Sequence[Entry],
                       scope: Sequence[str] | None) -> tuple[FallbackStatus, list[Entry], list[PathDrop]]:
    """What the fallback agent adds to `entries` for `scope` (None: the whole snapshot).

    Returns the fallback's status, the kept entries in location order, and the
    drops of the others (see the module docstring).
    """
    stored = await ctx.answer(fallback_key(ctx.commit, entries, scope),
                              lambda: paths_fallback(commit=ctx.commit, scope=scope, entries=entries),
                              budget_label=lambda _: FALLBACK_LABEL, schema=FallbackAnswer,
                              encode=encode_fallback_answer)
    if (failed := _failed_status(stored)) is not None:
        return failed, [], []
    listed = FallbackAnswer.model_validate(stored).entries
    kept, dropped = _Filter(ctx, entries).apply(listed)
    log.info("fallback listed %d entries: %d kept, %d dropped", len(listed), len(kept), len(dropped))
    if ctx.progress:
        ctx.progress(f"  fallback: {len(kept)} entries kept, {len(dropped)} dropped")
    return FallbackStatus(status="ok", reason=None), kept, dropped


def _failed_status(stored: dict) -> FallbackStatus | None:
    """The `failed` status when `stored` records a recoverable call failure or a malformed answer, else None."""
    for marker, what in ((CALL_ERROR, "the fallback call failed"), (MALFORMED, "the fallback answer is malformed")):
        if marker in stored:
            log.warning("%s: %s", what, stored[marker])
            return FallbackStatus(status="failed", reason=f"{what}: {stored[marker]}")
    return None


class _Filter:
    """The citation and duplicate checks of one fallback answer against the discovered entries."""

    def __init__(self, ctx: TraceContext, entries: Sequence[Entry]):
        self._ctx = ctx
        self._taken: set[Line] = set()
        self._handlers: set[Line] = set()
        for e in entries:
            self._taken.add(_line(e.location))
            self._taken.update(_line(i.location) for i in e.also_invoked_by)
            self._taken.update(_line(k.from_) for k in e.known_edges)
            if e.handler is not None:
                self._taken.add((e.handler.path, e.handler.line))
                if e.handler.symbol is not None:
                    self._handlers.add((e.handler.path, e.handler.line))

    def apply(self, listed: Sequence[FallbackEntry]) -> tuple[list[Entry], list[PathDrop]]:
        kept: list[Entry] = []
        dropped: list[PathDrop] = []
        for item in sorted(listed, key=_listing_order):
            placed = self._place(item)
            if isinstance(placed, PathDrop):
                dropped.append(placed)
            elif (duplicate := self._duplicate(placed)) is not None:
                dropped.append(_drop("model_fallback_duplicate", "duplicate", duplicate, placed))
            else:
                self._taken.add(_line(placed.location))
                kept.append(placed)
        kept.sort(key=lambda e: (e.location.path, e.location.line, e.name))
        return kept, dropped

    def _place(self, item: FallbackEntry) -> Entry | PathDrop:
        """The entry at the line `item` cites, or its `model_fallback_citation` drop."""
        citation = item.citation
        if citation is None:
            return _drop("model_fallback_citation", "missing_citation", "the fallback agent gave no citation",
                         _model_entry(item, UNPLACED, None))
        path = check_head_citation(self._ctx.root, self._ctx.resolve, citation.path, citation.line_start,
                                   citation.line_end, citation.quote)
        if isinstance(path, Break):
            return _drop("model_fallback_citation", "citation_unverified", f"{path.code}: {path.detail}",
                         _model_entry(item, self._cited_location(citation), None))
        location = Location(path=path, line=citation.line_start)
        return _model_entry(item, location, _enclosing_handler(self._ctx.index, location))

    def _cited_location(self, citation: Citation) -> Location:
        try:
            return Location(path=normalise_rel_path(self._ctx.root, citation.path), line=citation.line_start)
        except ValueError:
            return UNPLACED

    def _duplicate(self, entry: Entry) -> str | None:
        """Why `entry` duplicates a discovered or earlier kept entry, or None."""
        where = f"{entry.location.path}:{entry.location.line}"
        if _line(entry.location) in self._taken:
            return f"{where} is already an entry point's line"
        handler = entry.handler
        if handler is not None and (handler.path, handler.line) in self._handlers:
            return f"{where} is in {handler.symbol}, already a discovered entry point's handler"
        return None


def _listing_order(item: FallbackEntry) -> tuple[Any, ...]:
    """A total order of listed entries by what they cite, so the model's order never matters."""
    c = item.citation
    cited = (c.path, c.line_start, c.line_end, c.quote) if c is not None else ("", 0, 0, "")
    return c is None, *cited, item.name, item.kind, item.reason


def _enclosing_handler(index: PyIndex, location: Location) -> Handler | None:
    """The innermost function `def` holding `location` in a parsed Python file, else None."""
    if index.tree(location.path) is None:
        return None
    found = index.enclosing_function(location.path, location.line)
    return Handler(path=location.path, line=found.start, symbol=found.name) if found else None


def _model_entry(item: FallbackEntry, location: Location, handler: Handler | None) -> Entry:
    return Entry(kind="model", name=item.name.strip() or f"{location.path}:{location.line}", location=location,
                 handler=handler, invocation=None, known_edges=[], also_invoked_by=[])


def _drop(stage: DropStage, reason: DropReason, detail: str, entry: Entry) -> PathDrop:
    return PathDrop(stage=stage, reason=reason, detail=detail, entry=to_path_entry(entry))


def _line(location: Location) -> Line:
    return location.path, location.line
