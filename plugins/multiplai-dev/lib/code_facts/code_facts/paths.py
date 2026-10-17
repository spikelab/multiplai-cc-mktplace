"""The paths.v1 contract: the output models, their JSON Schema, `map_paths` and `write_paths_file`.

A path runs from one entry point (a CLI subcommand, a hook, a skill command, …)
through the code it calls. `map_paths` returns a `PathsResult`; only
`write_paths_file` writes `paths.json`, and only after the document validates
against `schema/paths.v1.schema.json`.

Locations. Every path in the output is relative to the snapshot root passed to
`map_paths`, uses `/`, and has no `.` or `..` segment. Lines are 1-based.

Field order. Serialisation order is declaration order, which is also the
schema's `properties` order, so the field order of every model below is part of
the contract.

The committed schema is generated: `paths_schema_text()` is its source.
"""

from __future__ import annotations

import functools
import importlib.resources
import json
import logging
import os
import posixpath
import re
import secrets
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, get_args

import jsonschema
from pydantic import ConfigDict, Field, ValidationError, model_validator

from .explore import CallFn, Resolve
from .models import _Model

log = logging.getLogger(__name__)

SCHEMA_VERSION = "paths.v1"
SCHEMA_RESOURCE = ("schema", "paths.v1.schema.json")
# The same draft as review's facts.v1 schema.
JSON_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
HOP_LIMIT = 20
# The directory under `cache_dir` that holds the stored model answers.
ANSWERS_DIR = "paths-answers"

# Relative, no leading `/`, no `.` or `..` segment, no backslash. Compiled, so
# pydantic matches it with Python's `re`, which supports the lookaheads.
REL_PATH = re.compile(r"^(?!/)(?!.*(^|/)\.\.?(/|$))[^\\]+$")
PATH_ID = r"^p[1-9][0-9]*$"

# Output kinds of discovered entries; `model` is added only for fallback entries, which never merge.
DiscoveredKind = Literal["argparse", "click", "typer", "main", "script", "skill_command", "hook", "seed"]
EntryKind = Literal[DiscoveredKind, "model"]
MatchKind = Literal["direct_call", "cli_argv", "cli_runner", "subprocess", "runpy"]
DropStage = Literal["model_fallback_citation", "model_fallback_duplicate", "import_reachability", "trace_limit",
                    "trace", "no_changed_line", "cap"]
DiagnosticReason = Literal["parse_error", "unresolved_target", "unresolved_handler", "dynamic_name",
                           "seed_not_found"]
SkipReason = Literal["decode_error", "syntax_error"]
FallbackState = Literal["ok", "failed", "skipped"]

# A cut kept path breaks at hop 2 or later; a break at hop 1 leaves no hop, so
# these three only ever reach a drop.
CutReason = Literal["path_outside_root", "missing_file", "line_out_of_range", "text_mismatch",
                    "base_side_citation", "unconnected_hop", "repeated_hop", "hop_limit"]
BreakCode = Literal["malformed_answer", "empty_trace", "entry_mismatch", CutReason]
DropReason = Literal["missing_citation", "citation_unverified", "duplicate", "unreachable", "over_trace_limit",
                     "call_failed", BreakCode, "no_changed_line_covered", "over_cap"]

# The closed reason codes of each drop stage, in stage sort order.
DROP_REASONS: Mapping[DropStage, tuple[DropReason, ...]] = {
    "model_fallback_citation": ("missing_citation", "citation_unverified"),
    "model_fallback_duplicate": ("duplicate",),
    "import_reachability": ("unreachable",),
    "trace_limit": ("over_trace_limit",),
    "trace": ("call_failed", *get_args(BreakCode)),
    "no_changed_line": ("no_changed_line_covered",),
    "cap": ("over_cap",),
}
# The sort rank of each drop stage: its position in DROP_REASONS.
_STAGE_RANK = {stage: rank for rank, stage in enumerate(DROP_REASONS)}
# Drop stages that only a scoped run can produce.
SCOPE_STAGES = frozenset({"import_reachability", "trace_limit", "no_changed_line", "cap"})

# The fixed entry ranking: merge precedence, pre-trace order and output order all use it.
KIND_RANK: dict[EntryKind, int] = {"argparse": 1, "click": 2, "typer": 3, "main": 4, "script": 5,
                                   "skill_command": 6, "hook": 7, "seed": 8, "model": 9}

# The only tools an agent of `map_paths` gets, whatever the caller's `call_kwargs` say.
AGENT_TOOLS = ("Read", "Grep", "Glob")


def _pinned(fields: Iterable[str], **constraints: Any) -> dict[str, Any]:
    """A schema for an object with `fields` where only `constraints` restrict their values."""
    return {"properties": {name: constraints.get(name, True) for name in fields}, "additionalProperties": False}


def _one_of(*arms: Mapping[str, Any]) -> Callable[[dict[str, Any]], None]:
    """A `json_schema_extra` putting a model's cross-field rule into the schema: exactly one arm holds."""
    def extra(schema: dict[str, Any]) -> None:
        schema["oneOf"] = [_pinned(schema["properties"], **arm) for arm in arms]
    return extra


class _Strict(_Model):
    # Output models: code builds them, so an unknown key is a bug, not model chatter.
    # Defaults are always serialised, so the schema requires them.
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_serialization_defaults_required=True)


# --- the paths.v1 models -----------------------------------------------------------------


class Location(_Strict):
    """A line in a file under the snapshot root."""
    path: str = Field(pattern=REL_PATH)
    line: int = Field(ge=1)


class Handler(_Strict):
    """The code an entry runs: a function's `def` line, or line 1 of a non-Python target (no symbol)."""
    path: str = Field(pattern=REL_PATH)
    line: int = Field(ge=1)
    symbol: str | None


class Invoker(_Strict):
    """Another entry that resolved to the same handler and was merged into this one."""
    kind: DiscoveredKind
    name: str
    location: Location


class PathEntry(_Strict):
    """Where a path starts: the declaration line, and the handler it dispatches to."""
    kind: EntryKind
    name: str
    location: Location
    handler: Handler | None
    invocation: str | None
    also_invoked_by: list[Invoker]


class PathHop(_Strict):
    """One step of a path. The fields of the codebase-walkthrough `Hop`, then the commit side."""
    symbol_called: str
    path: str = Field(pattern=REL_PATH)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    quote: str
    note: str = ""
    side: Literal["head"] = "head"


class _TestRecord(_Strict):
    # Shared fields only: the two records are siblings so neither list accepts the other's records.
    node_id: str
    file: str = Field(pattern=REL_PATH)
    line: int = Field(ge=1)
    match: MatchKind
    invocation_line: int = Field(ge=1)


class E2ETest(_TestRecord):
    """A test that invokes the path's entry; `line` is its `def`, `invocation_line` the matching call."""


class MockedE2ETest(_TestRecord):
    """An e2e test that patches the symbol at hop 2; the patch may sit in a fixture file."""
    patch_file: str = Field(pattern=REL_PATH)
    patch_line: int = Field(ge=1)


class E2EListing(_Strict):
    """Tests of a path, sorted by (file, line, node_id). A test is in at most one list."""
    e2e: list[E2ETest]
    mocks_hop_2: list[MockedE2ETest]

    @model_validator(mode="after")
    def _disjoint(self) -> E2EListing:
        both = {t.node_id for t in self.e2e} & {t.node_id for t in self.mocks_hop_2}
        if both:
            raise ValueError(f"listed in both e2e and mocks_hop_2: {sorted(both)}")
        return self


class TracedPath(_Strict):
    """A kept path. A cut path has `stops_at` (the 1-based index of the last hop kept) and `break_reason`."""
    model_config = ConfigDict(json_schema_extra=_one_of(
        {"stops_at": {"type": "null"}, "break_reason": {"type": "null"}},
        {"stops_at": {"type": "integer"}, "break_reason": {"type": "string"}},
    ))

    id: str = Field(pattern=PATH_ID)
    entry: PathEntry
    hops: list[PathHop] = Field(min_length=1, max_length=HOP_LIMIT)
    stops_at: int | None = Field(ge=1)
    break_reason: CutReason | None
    changed_lines_covered: list[Location]
    tests: E2EListing

    @model_validator(mode="after")
    def _cut(self) -> TracedPath:
        if (self.stops_at is None) != (self.break_reason is None):
            raise ValueError("stops_at and break_reason are set together or not at all")
        if self.stops_at is not None and not 1 <= self.stops_at <= len(self.hops):
            raise ValueError(f"stops_at {self.stops_at} is not a hop index in 1..{len(self.hops)}")
        return self


class PathDrop(_Strict):
    """An entry or path left out, with the stage that dropped it and a reason code; `detail` is for humans."""
    model_config = ConfigDict(json_schema_extra=_one_of(
        *({"stage": {"const": stage}, "reason": {"enum": list(reasons)}} for stage, reasons in DROP_REASONS.items())
    ))

    stage: DropStage
    reason: DropReason
    detail: str
    entry: PathEntry

    @model_validator(mode="after")
    def _reason_of_stage(self) -> PathDrop:
        if self.reason not in DROP_REASONS[self.stage]:
            raise ValueError(f"{self.reason!r} is not a reason of drop stage {self.stage!r}")
        return self


class PathDiagnostic(_Strict):
    """A discovery candidate that was examined but not emitted, or an entry whose handler did not resolve."""
    path: str = Field(pattern=REL_PATH)
    line: int | None = Field(ge=1)
    reason: DiagnosticReason
    detail: str


class ListingSkip(_Strict):
    """A test file the e2e listing could not read."""
    file: str = Field(pattern=REL_PATH)
    reason: SkipReason


class FallbackStatus(_Strict):
    """What the model fallback did; `reason` says why it failed and is null otherwise."""
    model_config = ConfigDict(json_schema_extra=_one_of(
        {"status": {"const": "failed"}, "reason": {"type": "string"}},
        {"status": {"enum": ["ok", "skipped"]}, "reason": {"type": "null"}},
    ))

    status: FallbackState
    reason: str | None = Field(min_length=1)

    @model_validator(mode="after")
    def _reason_iff_failed(self) -> FallbackStatus:
        if (self.status == "failed") != (self.reason is not None):
            raise ValueError("fallback reason is given exactly when the status is failed")
        return self


class PathsResult(_Strict):
    """The paths.v1 document. `scoped` is true exactly when changed lines were given; `cap` is null otherwise."""
    model_config = ConfigDict(json_schema_extra=_one_of(
        {"scoped": {"const": True}, "cap": {"type": "integer"}},
        {"scoped": {"const": False}, "cap": {"type": "null"},
         "paths": {"items": _pinned(TracedPath.model_fields, changed_lines_covered={"maxItems": 0})},
         "dropped": {"items": _pinned(PathDrop.model_fields, stage={
             "enum": [stage for stage in DROP_REASONS if stage not in SCOPE_STAGES]})}},
    ))

    schema_version: Literal["paths.v1"]
    commit: str
    scoped: bool
    cap: int | None = Field(ge=1)
    fallback: FallbackStatus
    paths: list[TracedPath]
    dropped: list[PathDrop]
    diagnostics: list[PathDiagnostic]
    test_listing_skips: list[ListingSkip]

    @model_validator(mode="after")
    def _consistent(self) -> PathsResult:
        if self.scoped != (self.cap is not None):
            raise ValueError("cap is set exactly when the run is scoped")
        ids = [p.id for p in self.paths]
        if ids != [f"p{n}" for n in range(1, len(ids) + 1)]:
            raise ValueError(f"path ids are not p1..p{len(ids)} in order: {ids}")
        if not self.scoped:
            if any(d.stage in SCOPE_STAGES for d in self.dropped):
                raise ValueError("an unscoped run has no scope-stage drops")
            if any(p.changed_lines_covered for p in self.paths):
                raise ValueError("an unscoped run covers no changed lines")
        return self


class Seed(_Strict):
    """A caller-chosen entry: a path plus a line or a symbol, with an optional display name."""
    path: str
    line: int | None = None
    symbol: str | None = None
    name: str | None = None


# --- schema ----------------------------------------------------------------------------------


def paths_schema_text() -> str:
    """The JSON Schema for PathsResult, in its committed form (`schema/paths.v1.schema.json`)."""
    # Serialisation mode: the schema describes the documents `write_paths_file` writes.
    schema = {"$schema": JSON_SCHEMA_DRAFT, "$id": f"{SCHEMA_VERSION}.schema.json",
              **PathsResult.model_json_schema(mode="serialization")}
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


@functools.cache
def _validator() -> jsonschema.protocols.Validator:
    """A validator for the committed schema, loaded once from the package resources."""
    text = importlib.resources.files("code_facts").joinpath(*SCHEMA_RESOURCE).read_text(encoding="utf-8")
    schema = json.loads(text)
    return jsonschema.validators.validator_for(schema)(schema)


# --- arguments -------------------------------------------------------------------------------


def normalise_rel_path(root: Path, path: str) -> str:
    """`path` as a POSIX path relative to `root`, with `.` segments and a leading `./` removed.

    Raises ValueError for an empty, absolute or backslashed path, or one that
    resolves outside `root`. The file does not have to exist.
    """
    if not path or "\\" in path:
        raise ValueError(f"not a relative POSIX path: {path!r}")
    if posixpath.isabs(path):
        raise ValueError(f"absolute path: {path!r}")
    norm = posixpath.normpath(path)
    if norm in (".", "..") or norm.startswith("../"):
        raise ValueError(f"path escapes the snapshot root: {path!r}")
    if not (root / norm).resolve().is_relative_to(root.resolve()):
        raise ValueError(f"path resolves outside the snapshot root: {path!r}")
    return norm


def normalise_changed_lines(root: Path, changed_lines: Mapping[str, Iterable[int]]) -> dict[str, frozenset[int]]:
    """Changed lines keyed by normalised path; two spellings of one file are merged.

    Raises ValueError for a bad path (see `normalise_rel_path`) or a line below 1.
    """
    out: dict[str, set[int]] = {}
    for path, lines in changed_lines.items():
        norm = normalise_rel_path(root, path)
        bucket = out.setdefault(norm, set())
        for line in lines:
            if isinstance(line, bool) or not isinstance(line, int):
                raise TypeError(f"changed line in {path!r} is not an int: {line!r}")
            if line < 1:
                raise ValueError(f"changed line in {path!r} is below 1: {line}")
            bucket.add(line)
    return {path: frozenset(out[path]) for path in sorted(out)}


def _require_positive(name: str, value: int) -> None:
    """Raise ValueError when a scoped-run limit (`cap`, `trace_limit`) is below 1."""
    if value < 1:
        raise ValueError(f"{name} must be at least 1 when changed lines are given, got {value}")


# --- mapping ---------------------------------------------------------------------------------


async def map_paths(
    *,
    root: Path,
    commit: str,
    resolve: Resolve,
    call: CallFn,
    changed_lines: Mapping[str, Iterable[int]] | None = None,
    seeds: Sequence[Seed] = (),
    places: Sequence[Location] = (),
    cap: int = 10,
    trace_limit: int = 30,
    concurrency: int = 4,
    cache_dir: Path | None = None,
    fallback: bool = True,
    recoverable: tuple[type[BaseException], ...] = (),
    call_kwargs: Mapping[str, Any] | None = None,
    progress: Callable[[str], None] | None = None,
) -> PathsResult:
    """The paths from every entry point in the snapshot at `root`.

    `commit` is the label written to the result; citations are re-read through
    `resolve`, and every model call goes through `call`. Scoping (reachability,
    `trace_limit`, coverage, `cap`) applies only when `changed_lines` is not
    None. Arguments are checked before any model call. Nothing is written
    outside `cache_dir`: given one, every answer is kept under
    `<cache_dir>/paths-answers/`, so a rerun asks only what it does not have.
    """
    # Deferred: these modules build on the models above.
    from .cache import _DiskAnswers, purge_failed
    from .entries import discover_entries
    from .pyindex import PyIndex
    from .tracing import TraceContext, trace_entries

    for name, fn in (("call", call), ("resolve", resolve)):
        if not callable(fn):
            raise TypeError(f"{name} must be callable, got {type(fn).__name__}")
    scoped = changed_lines is not None
    changed = None
    if scoped:
        _require_positive("cap", cap)
        _require_positive("trace_limit", trace_limit)
        changed = normalise_changed_lines(root, changed_lines)
    log.info("mapping paths at %s (%s)", commit, "scoped" if scoped else "unscoped")
    answers = _DiskAnswers(cache_dir / ANSWERS_DIR if cache_dir is not None else None)
    purge_failed(answers)
    index = PyIndex.build(root)
    discovery = discover_entries(root, seeds, index=index)
    ctx = TraceContext(root=root, commit=commit, resolve=resolve, call=call, index=index, changed_lines=changed,
                       places=tuple(places), answers=answers, recoverable=recoverable,
                       call_kwargs=dict(call_kwargs or {}), progress=progress)
    outcomes = await trace_entries(discovery.entries, ctx=ctx, concurrency=concurrency)
    traced = sorted((o for o in outcomes if isinstance(o, TracedPath)), key=path_order)
    dropped = sorted((o for o in outcomes if isinstance(o, PathDrop)), key=drop_order)
    # Until the model fallback exists, every run records it as skipped.
    return PathsResult(
        schema_version=SCHEMA_VERSION, commit=commit, scoped=scoped, cap=cap if scoped else None,
        fallback=FallbackStatus(status="skipped", reason=None),
        paths=assign_ids(traced), dropped=dropped, diagnostics=discovery.diagnostics, test_listing_skips=[],
    )


def path_order(path: TracedPath) -> tuple[int, int, str, int]:
    """The sort key of a kept path: fewest hops first, then entry kind rank, then entry location."""
    entry = path.entry
    return len(path.hops), KIND_RANK[entry.kind], entry.location.path, entry.location.line


def drop_order(drop: PathDrop) -> tuple[int, str, int, str]:
    """The sort key of a drop: stage order, then entry location, then entry kind."""
    entry = drop.entry
    return _STAGE_RANK[drop.stage], entry.location.path, entry.location.line, entry.kind


def assign_ids(paths: Sequence[TracedPath]) -> list[TracedPath]:
    """`paths` numbered `p1`…`pN` in the order given."""
    return [path.model_copy(update={"id": f"p{n}"}) for n, path in enumerate(paths, start=1)]


# --- writing ---------------------------------------------------------------------------------


def serialise_paths(result: PathsResult) -> str:
    """`result` as paths.v1 JSON text: declaration-order keys, 2-space indent, non-ASCII kept, one trailing newline."""
    return json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"


def write_paths_file(result: PathsResult, path: Path) -> Path:
    """Write `result` to `path` as paths.v1 JSON, atomically, after validating it.

    The text is checked against the schema, then parsed back into the model,
    which also checks the rules a schema cannot state (contiguous ids, disjoint
    test lists) on lists mutated after construction. Raises ValueError naming
    the JSON pointer of the first error; then nothing is written and an
    existing file at `path` is left as it was.
    """
    text = serialise_paths(result)
    document = json.loads(text)
    errors = list(_validator().iter_errors(document))
    # `oneOf` only carries cross-field rules; a value invalid on its own is the better pointer.
    own = [e for e in errors if "oneOf" not in e.schema_path]
    error = jsonschema.exceptions.best_match(own or errors)
    if error is not None:
        raise _invalid(error.absolute_path, error.message)
    try:
        PathsResult.model_validate(document)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise _invalid(first["loc"], first["msg"]) from exc
    write_atomic(path, text.encode("utf-8"))
    return path


def _invalid(location: Iterable[str | int], message: str) -> ValueError:
    """The error for an invalid document, naming the JSON pointer of the failing value."""
    pointer = "/" + "/".join(map(str, location))
    return ValueError(f"{SCHEMA_VERSION} document is invalid at {pointer}: {message}")


def write_atomic(path: Path, data: bytes) -> None:
    """Write via a temp file in the same directory, fsync'd, then renamed over `path`."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    # os.open with 0o666 so the umask applies, as for a plain write.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
