"""Tracing: one gated, linear path per entry, with at most one re-ask.

Every model call goes through `ask`, with the read-only agent tools and the
snapshot root as `cwd` laid over the caller's `call_kwargs`. Answers are stored
under content keys (`trace_key`, `reask_key`), so a key that is already
answered is never asked again.

Outcomes. A trace that passes the gate is kept whole. One cut only by the hop
limit is kept with its first 20 hops. Any other break, and a malformed or empty
answer, gets one re-ask, whose answer is gated from hop 1 and replaces the
first: a break at hop k keeps hops 1..k-1 (`stops_at = k-1`), and a break at
hop 1 drops the entry. A call that raises one of `recoverable` drops the entry
as `call_failed`; any other exception propagates.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from .cache import cache_key
from .entries import Entry, to_path_entry
from .explore import CALL_ERROR, CallFn, Resolve, ask, bounded, canonical_digest, run_tasks
from .paths import AGENT_TOOLS, HOP_LIMIT, DropReason, E2EListing, Location, PathDrop, TracedPath
from .prompts import PATHS_REASK_PROMPT_VERSION, PATHS_TRACE_PROMPT_VERSION, paths_reask, paths_trace
from .pyindex import PyIndex
from .trace_gate import GateResult, TraceAnswer, gate_hops

log = logging.getLogger(__name__)

Stage = Literal["trace", "reask"]

# Stored-answer markers: `ask` stores a recoverable call failure under CALL_ERROR;
# an answer that does not parse is stored under "malformed". A TraceAnswer has neither key.
MALFORMED = "malformed"

# A valid placeholder: `assign_ids` numbers kept paths once they are ordered.
UNNUMBERED = "p1"


@dataclass(frozen=True)
class TraceContext:
    """What every trace of one `map_paths` run shares. `answers` maps content keys to stored answers."""
    root: Path
    commit: str
    resolve: Resolve
    call: CallFn
    index: PyIndex
    changed_lines: Mapping[str, frozenset[int]] | None
    places: Sequence[Location]
    answers: MutableMapping[str, dict]
    recoverable: tuple[type[BaseException], ...]
    call_kwargs: Mapping[str, Any]
    progress: Callable[[str], None] | None = None

    def ask_kwargs(self, stage: Stage) -> dict[str, Any]:
        """The `ask` arguments for one stage: read-only tools and `cwd` overwrite the caller's."""
        return {
            "call_kwargs": {**self.call_kwargs, "allowed_tools": list(AGENT_TOOLS), "cwd": str(self.root)},
            "errors": [], "recoverable": self.recoverable, "schema": TraceAnswer,
            "budget_label": lambda key: f"paths:{stage}:{key[:12]}", "encode": encode_trace_answer,
        }


def trace_key(entry: Entry, ctx: TraceContext) -> str:
    """The content key of an entry's first trace; independent of the order of any input."""
    changed = ctx.changed_lines
    return cache_key("trace", {
        "version": PATHS_TRACE_PROMPT_VERSION,
        "commit": ctx.commit,
        "entry": [entry.kind, entry.location.path, entry.location.line,
                  entry.handler.model_dump(mode="json") if entry.handler else None],
        "changed_lines": None if changed is None else {path: sorted(lines) for path, lines in changed.items()},
        "known_edges": sorted([e.from_.path, e.from_.line, e.to.path, e.to.line] for e in entry.known_edges),
        "places": sorted([p.path, p.line] for p in ctx.places),
        "hop_limit": HOP_LIMIT,
    })


def reask_key(first_key: str, first_answer: dict, gated: GateResult) -> str:
    """The content key of the re-ask after the first answer (stored under `first_key`) broke as `gated` says."""
    return cache_key("reask", {
        "version": PATHS_REASK_PROMPT_VERSION, "trace": first_key, "answer": canonical_digest(first_answer),
        "broken_at": gated.broken_at, "break_reason": gated.break_reason,
    })


def encode_trace_answer(out: Any) -> dict:
    """A call's answer as stored: the validated TraceAnswer's dump, or the reason it does not parse.

    The call may return a TraceAnswer, another model, a dict or a JSON string.
    Any answer that cannot be read as a TraceAnswer is malformed, so one odd
    answer costs only its own entry.
    """
    try:
        if isinstance(out, BaseModel):
            out = out.model_dump(mode="json")
        answer = (TraceAnswer.model_validate_json(out) if isinstance(out, (str, bytes))
                  else TraceAnswer.model_validate(out))
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(map(str, first["loc"]))
        return {MALFORMED: f"{where}: {first['msg']}" if where else first["msg"]}
    except Exception as e:  # a foreign model's own dump failing, e.g. PydanticSerializationError
        return {MALFORMED: (str(e).splitlines() or [type(e).__name__])[0][:200]}
    return answer.model_dump(mode="json")


async def trace_entries(entries: Sequence[Entry], *, ctx: TraceContext,
                        concurrency: int) -> list[TracedPath | PathDrop]:
    """Trace every entry, at most `concurrency` calls at a time; outcomes in entry order.

    The first requests go out together through `run_tasks`; then each entry is
    gated, and re-asked at most once.
    """
    first = {trace_key(e, ctx): _trace_prompt(e, ctx) for e in entries}
    await run_tasks(list(first.items()), ctx.call, ctx.answers, concurrency=concurrency, **ctx.ask_kwargs("trace"))
    return await bounded(entries, lambda e: trace_entry(e, ctx=ctx), concurrency)


async def trace_entry(entry: Entry, *, ctx: TraceContext) -> TracedPath | PathDrop:
    """The gated path of one entry (unnumbered: see `assign_ids`), or the drop that replaces it."""
    outcome = await _trace(entry, ctx)
    _report(entry, ctx, outcome)
    return outcome


async def _trace(entry: Entry, ctx: TraceContext) -> TracedPath | PathDrop:
    key = trace_key(entry, ctx)
    first = await _answer(ctx, "trace", key, lambda: _trace_prompt(entry, ctx))
    if (failed := _call_failed(entry, first)) is not None:
        return failed
    gated = _gate(first, entry, ctx)
    if gated.break_reason in (None, "hop_limit"):
        return _cut(entry, gated)
    second = await _answer(ctx, "reask", reask_key(key, first, gated), lambda: paths_reask(
        entry, commit=ctx.commit, broken_at=gated.broken_at, break_reason=gated.break_reason,
        detail=gated.detail, accepted=gated.accepted))
    if (failed := _call_failed(entry, second)) is not None:
        return failed
    return _cut(entry, _gate(second, entry, ctx))


def _trace_prompt(entry: Entry, ctx: TraceContext) -> str:
    return paths_trace(entry, commit=ctx.commit, changed_lines=ctx.changed_lines, known_edges=entry.known_edges,
                       places=ctx.places)


async def _answer(ctx: TraceContext, stage: Stage, key: str, prompt: Callable[[], str]) -> dict:
    """The stored answer under `key`, asking for it first when there is none."""
    if key not in ctx.answers:
        await ask(key, prompt(), ctx.call, ctx.answers, **ctx.ask_kwargs(stage))
    return ctx.answers[key]


def _gate(answer: dict, entry: Entry, ctx: TraceContext) -> GateResult:
    if MALFORMED in answer:
        return GateResult(accepted=[], broken_at=1, break_reason="malformed_answer", detail=answer[MALFORMED])
    return gate_hops(TraceAnswer.model_validate(answer).hops, root=ctx.root, resolve=ctx.resolve, entry=entry,
                     known_edges=entry.known_edges, places=ctx.places, index=ctx.index)


def _cut(entry: Entry, gated: GateResult) -> TracedPath | PathDrop:
    """The accepted hops as a path, cut at the break when there is one; a drop when none was accepted."""
    if not gated.accepted:
        return _drop(entry, gated.break_reason, gated.detail)
    cut = gated.break_reason is not None
    return TracedPath(id=UNNUMBERED, entry=to_path_entry(entry), hops=gated.accepted,
                      stops_at=len(gated.accepted) if cut else None, break_reason=gated.break_reason,
                      changed_lines_covered=[], tests=E2EListing(e2e=[], mocks_hop_2=[]))


def _call_failed(entry: Entry, answer: dict) -> PathDrop | None:
    """The `call_failed` drop when `answer` records a recoverable call failure, else None."""
    return _drop(entry, "call_failed", answer[CALL_ERROR]) if CALL_ERROR in answer else None


def _drop(entry: Entry, reason: DropReason, detail: str) -> PathDrop:
    return PathDrop(stage="trace", reason=reason, detail=detail, entry=to_path_entry(entry))


def _report(entry: Entry, ctx: TraceContext, outcome: TracedPath | PathDrop) -> None:
    if isinstance(outcome, PathDrop):
        log.info("trace of %s dropped: %s (%s)", entry.name, outcome.reason, outcome.detail)
        line = f"dropped ({outcome.reason})"
    else:
        cut = f", cut by {outcome.break_reason}" if outcome.break_reason else ""
        line = f"{len(outcome.hops)} hops{cut}"
    if ctx.progress:
        ctx.progress(f"  trace {entry.name}: {line}")
