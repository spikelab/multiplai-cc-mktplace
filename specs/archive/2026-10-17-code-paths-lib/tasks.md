## 1. Write an empty but valid `paths.v1` document through `map_paths` and `write_paths_file`

This block delivers the whole `paths.v1` contract for a snapshot that has no entries. It adds strict output models on `class _Strict(_Model)` (`model_config = ConfigDict(extra="forbid", frozen=True)`) using the normative field orders from Global Constraints, and `PathHop` declares the fields of `walkthrough_pipeline/models.py:122-128` `Hop` in order, then `side: Literal["head"] = "head"`. It also adds `plugins/multiplai-dev/lib/code_facts/code_facts/schema/paths.v1.schema.json` with these properties:
- `$schema` copied byte-for-byte from `skills/review/schema/facts.v1.schema.json`;
- `$id` taken from facts.v1 with `facts.v1` replaced by `paths.v1`;
- `$defs`, and `additionalProperties: false` on every object;
- location pattern `^(?!/)(?!.*(^|/)\.\.?(/|$))[^\\]+$` and line `minimum: 1`;
- id pattern `^p[1-9][0-9]*$` and `hops` with 1–20 items;
- closed enums for kinds, stages and reasons.

`write_paths_file` works in four steps:
1. Serialise with `json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"`.
2. Validate with `jsonschema.validators.validator_for(schema)`, loading the schema through `importlib.resources.files("code_facts")`.
3. On failure, raise `ValueError` whose message contains `"/" + "/".join(map(str, err.absolute_path))`.
4. Write a same-directory temp file, `fsync` it and `os.replace` it over the target. Any exception unlinks the temp file, so an existing target is left unchanged.

`map_paths` validates its arguments and returns an empty `PathsResult`. It raises errors before any `CallFn` call:
- `TypeError` when `call` or `resolve` is missing;
- `ValueError` for an absolute `changed_lines` path, a path that escapes `root`, or a line below 1;
- `ValueError` for `cap` or `trace_limit` below 1 when `changed_lines is not None`.

It normalises `./pkg/core.py` to `pkg/core.py`. It sets `scoped` and `cap`, with `cap` written as `null` when unscoped. It records `fallback` as `{"status": "skipped", "reason": null}` for `fallback=False`, and also for `fallback=True` until block 7 replaces this. It writes nothing outside `cache_dir`. The models reject three things:
- `stops_at` and `break_reason` that are not both null or both set;
- `stops_at` outside `1..len(hops)`;
- a test id present in both `tests.e2e` and `tests.mocks_hop_2`.

Supporting changes in this block:
- Add `"jsonschema>=4"` to `[project] dependencies` and run `uv lock` at the repository root.
- Export `map_paths`, `write_paths_file`, `PathsResult`, `TracedPath`, `PathEntry`, `PathHop`, `PathDrop`, `E2ETest`, `MockedE2ETest`, `E2EListing`, `Location` and `Seed` from `code_facts/__init__.py`, leaving every existing export unchanged.
- Amend the paths-output-contract scenarios per D1 rows 6, 7, 9 and 10.

Run the tests with `uv run --directory plugins/multiplai-dev/lib/code_facts python -m pytest tests/`. They cover the following:
- An empty `tmp_path` repository maps to a schema-valid file with `"paths": []` that ends in exactly one `\n` and round-trips byte-identically through `model_validate_json`.
- `$schema` equals facts.v1's value.
- Every new model is a subclass of `_Model`.
- An `ast` comparison of `PathHop` with the walkthrough `Hop` passes.
- No module under `code_facts/` imports `walkthrough_pipeline`.
- A hand-built result that uses every enum value and nullable field validates.

Interfaces:
- Produces: `class _Strict(_Model)` with `model_config = ConfigDict(extra="forbid", frozen=True)`
- Produces: `class Location(_Strict): path: str; line: int`
- Produces: `class Handler(_Strict): path: str; line: int; symbol: str | None`
- Produces: `class Invoker(_Strict): kind: str; name: str; location: Location`
- Produces: `class PathEntry(_Strict): kind: Literal["argparse","click","typer","main","script","skill_command","hook","seed","model"]; name: str; location: Location; handler: Handler | None; invocation: str | None; also_invoked_by: list[Invoker]`
- Produces: `class PathHop(_Strict)`: the fields of walkthrough `Hop` in declaration order, then `side: Literal["head"] = "head"`
- Produces: `class E2ETest(_Strict): node_id: str; file: str; line: int; match: Literal["direct_call","cli_argv","cli_runner","subprocess","runpy"]; invocation_line: int`
- Produces: `class MockedE2ETest(_Strict): node_id: str; file: str; line: int; match: Literal["direct_call","cli_argv","cli_runner","subprocess","runpy"]; invocation_line: int; patch_file: str; patch_line: int`
- Produces: `class E2EListing(_Strict): e2e: list[E2ETest]; mocks_hop_2: list[MockedE2ETest]`
- Produces: `class TracedPath(_Strict): id: str; entry: PathEntry; hops: list[PathHop]; stops_at: int | None; break_reason: str | None; changed_lines_covered: list[Location]; tests: E2EListing`
- Produces: `class PathDrop(_Strict): stage: Literal["model_fallback_citation","model_fallback_duplicate","import_reachability","trace_limit","trace","no_changed_line","cap"]; reason: str; detail: str; entry: PathEntry`
- Produces: `class PathDiagnostic(_Strict): path: str; line: int | None; reason: Literal["parse_error","unresolved_target","unresolved_handler","dynamic_name","seed_not_found"]; detail: str`
- Produces: `class ListingSkip(_Strict): file: str; reason: Literal["decode_error","syntax_error"]`
- Produces: `class FallbackStatus(_Strict): status: Literal["ok","failed","skipped"]; reason: str | None`
- Produces: `class PathsResult(_Strict): schema_version: Literal["paths.v1"]; commit: str; scoped: bool; cap: int | None; fallback: FallbackStatus; paths: list[TracedPath]; dropped: list[PathDrop]; diagnostics: list[PathDiagnostic]; test_listing_skips: list[ListingSkip]`
- Produces: `class Seed(_Strict): path: str; line: int | None = None; symbol: str | None = None; name: str | None = None`
- Produces: `async def map_paths(*, root: Path, commit: str, resolve: Resolve, call: CallFn, changed_lines: Mapping[str, Iterable[int]] | None = None, seeds: Sequence[Seed] = (), places: Sequence[Location] = (), cap: int = 10, trace_limit: int = 30, concurrency: int = 4, cache_dir: Path | None = None, fallback: bool = True, recoverable: tuple[type[BaseException], ...] = (), call_kwargs: Mapping[str, Any] | None = None, progress: Callable[[str], None] | None = None) -> PathsResult`
- Produces: `def write_paths_file(result: PathsResult, path: Path) -> Path`
- Produces: `def normalise_changed_lines(root: Path, changed_lines: Mapping[str, Iterable[int]]) -> dict[str, frozenset[int]]`

Satisfies:
- paths-output-contract:
  - "Public entry points exported from the library"
  - "`map_paths` returns a model and writes only cache files"
  - "Schema file exists and follows the review facts schema style"
  - "Top-level document shape"
  - "Path object shape"
  - "Entry object shape" (Unknown kind rejected)
  - "Hop object shape mirrors the walkthrough Hop", as amended by D1#9
  - "Locations are relative to the snapshot root" (Absolute or escaping path rejected; Line zero rejected)
  - "Cut paths carry stop and reason together" (Mismatched pair rejected)
  - "Test-listing shape" (Overlap rejected), as amended by D1#7
  - "Every drop is recorded with a reason" (Empty reason rejected)
  - "Empty results are valid"
  - "`write_paths_file` validates before writing"
  - "`write_paths_file` writes atomically"
  - "Byte-stable serialisation" (Trailing newline)
  - "Round-trip between file and model"
  - "Schema and models agree"
  - "Schema validation dependency is declared"
- path-scope-and-cap: "Changed-line paths are validated and normalised" (Absolute path rejected; Path escaping the snapshot rejected; Non-positive line rejected; Equivalent spellings normalise to one file); "Trace limit bounds the number of entries traced" (Invalid trace limit rejected); "Cap limits kept paths" (Invalid cap rejected)
- trace-gate: "Gate does not import the walkthrough pipeline"

## 2. Discover argparse subcommands and caller seeds from a static `PyIndex`

This block delivers `discover_entries()` on top of a new `pyindex.py`. The index parses each `.py` file once with `ast`. It records the following:
- dotted module names;
- each file's repository root: the nearest ancestor at or below `root` with `.git` or `pyproject.toml`, falling back to `root`;
- symbol tables;
- enclosing `def`/`class` spans;
- same-repository import resolution under `<repo>/`, `<repo>/src/` and the importing file's directory.

Imports are collected at every nesting level, including function bodies, `try` blocks and methods, and excluding `if TYPE_CHECKING:` blocks. A function-local import binds only inside its function. Traversal works as follows:
- It uses `os.walk(followlinks=False)`.
- It skips directories named `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `site-packages`, `build` and `dist`.
- It skips any symlink whose `resolve()` falls outside `root`.
- An undecodable or unparseable file yields a `parse_error` diagnostic with `line: null`.

**argparse entries.** Discovery emits one `argparse_subcommand` entry per `add_parser(<str literal>)` on a value bound from `add_subparsers(...)`:
- Nested names are space-joined, as in `db migrate`.
- `location` is the `add_parser` line.
- A non-literal name yields a `dynamic_name` diagnostic and no entry.

**Handlers and known edges.** Handlers resolve only in two ways: through `set_defaults(func=<name>)`, or through a dict literal mapping the literal subcommand name to a function. That dict may live in the parser module or in any same-repository module it imports, including through function-local imports (D13). Each resolved handler gets a known edge from the dispatch call (`args.func(...)` or `<DICT>[...](...)`) to the handler's `def`. With no dispatch call, the edge starts at the `set_defaults` line or the dict-entry line. Two cases leave `handler` as `null`:
- `args.command == "x"` comparisons are never resolved, and they produce no diagnostic.
- An out-of-repository target produces an `unresolved_handler` diagnostic.

**Seeds and ordering.** A seed of `{path, line|symbol, name?}` becomes a `seed` entry. Its handler is the enclosing `def` at the line or the named symbol, and its name defaults to `<path>:<symbol>`. A missing file, an out-of-range line or an unknown symbol yields `seed_not_found`. Entries are sorted by `(location.path, location.line, kind, name)` and diagnostics by `(path, line, reason)`, independent of filesystem order and seed order.

**Tests.** They call `discover_entries` directly with no `CallFn` on `make_tree(tmp_path, files)` fixtures. Fixture modules write a sentinel file and raise at import time, and the tests check that no sentinel file appears. The tests also cover:
- a comparison-dispatch fixture where `open`/`edit`/`render` have `handler == null` at their `add_parser` lines;
- a multi-repository `repo_a`/`repo_b` fixture with colliding `tool` packages;
- a live-copy test of `plugins/multiplai-dev/skills/pr/scripts/`, skipped if that directory is absent. It asserts that `open`, `edit` and `render` resolve to `cmd_*` through `COMMANDS`, with the edge from `commands.py:409`.

Interfaces:
- Consumes: Block 1: `class Location(_Strict): path: str; line: int`
- Consumes: Block 1: `class Handler(_Strict): path: str; line: int; symbol: str | None`
- Consumes: Block 1: `class Invoker(_Strict): kind: str; name: str; location: Location`
- Consumes: Block 1: `class Seed(_Strict): path: str; line: int | None = None; symbol: str | None = None; name: str | None = None`
- Produces: `class PyIndex` with `@classmethod def build(cls, root: Path) -> PyIndex`
- Produces: `PyIndex.module_name(self, path: str) -> str | None`
- Produces: `PyIndex.repo_root(self, path: str) -> str`
- Produces: `PyIndex.resolve_module(self, importer: str, dotted: str, level: int = 0) -> str | None` (same-repository only)
- Produces: `PyIndex.resolve_name(self, path: str, line: int, name: str) -> Handler | None` (function scope first, then module scope)
- Produces: `PyIndex.enclosing_scope(self, path: str, line: int) -> tuple[int, int]` (innermost `def`/`class` span, or the whole module)
- Produces: `PyIndex.parse_errors(self) -> list[tuple[str, str]]`
- Produces: `class KnownEdge(_Strict): from_: Location = Field(alias="from"); to: Location`
- Produces: `class Diagnostic(_Strict): path: str; line: int | None; reason: Literal["parse_error","unresolved_target","unresolved_handler","dynamic_name","seed_not_found"]; detail: str`
- Produces: `class Entry(_Strict): kind: Literal["argparse_subcommand","click_command","typer_command","project_script","main_module","skill_command","hook_command","seed"]; name: str; location: Location; handler: Handler | None; invocation: str | None; known_edges: list[KnownEdge]; also_invoked_by: list[Invoker]`
- Produces: `class Discovery(_Strict): entries: list[Entry]; diagnostics: list[Diagnostic]`
- Produces: `def discover_entries(root: Path, seeds: Sequence[Seed] = (), *, index: PyIndex | None = None) -> Discovery`

Satisfies:
- entry-point-discovery:
  - "Discovery is deterministic and makes no model call" (Discovery runs without a CallFn)
  - "Discovery never imports or runs target code"
  - "Entry record shape"
  - "Paths are relative to the snapshot root, including multi-repository snapshots"
  - "Excluded and unsafe locations are not scanned"
  - "argparse subcommands are discovered"
  - "argparse handlers resolve through set_defaults and dispatch dicts only"
  - "argparse dispatch lines become known edges"
  - "__main__ modules and packages are discovered" (Unparseable Python file)
  - "Caller-supplied seeds become entries"
  - "Discovery output is stably ordered"

## 3. Trace each discovered entry into a gated, linear path with one re-ask

`map_paths` now runs `discover_entries` and converts each entry to a `PathEntry` through `OUTPUT_KIND`. It then issues one trace request per entry through `run_tasks(..., concurrency=concurrency)`, using an in-memory answers dict.

**Trace requests.** Each call uses `call_kwargs`, with `allowed_tools=["Read","Grep","Glob"]`, `cwd=str(root)` and `budget_label=f"paths:trace:{key[:12]}"` always overwriting any caller value. The prompt comes from `paths_trace` and contains:
- the kind, location and handler symbol;
- the commit;
- the changed line ranges, or the text "no change scope; trace to the last effect";
- the known edges and `places`;
- the hop limit `20`.

**Answers.** An answer may be a `TraceAnswer`, a dict or a JSON string. `TraceAnswer`/`TraceHop` use `extra="forbid"`, so an `alternatives` field or nested `hops` fails validation as `malformed_answer`.

**`gate_hops`.** The gate is pure and makes no model call. It truncates the hops to 20, recording `hop_limit` if any were cut. It then checks each hop in this order and stops at the first broken hop:
1. `path_outside_root`
2. `missing_file`
3. `line_out_of_range`
4. `text_mismatch`, through `check_citation` with a transient head-side `Citation`
5. `base_side_citation`
6. `entry_mismatch` (hop 1 only). The range must include `entry.location.line` in the entry's file, or `handler.line` in the handler's file.
7. `unconnected_hop`. The hop is connected if any of these holds:
   - the last dotted component of hop n+1's symbol is a `tokenize` `NAME` token inside `PyIndex.enclosing_scope` of hop n's first cited line, so comments and strings never match;
   - for a non-Python hop n, that name matches as a whole word in the quote;
   - a known edge runs from hop n's scope into hop n+1's range;
   - a `place` lies in hop n+1's range.
8. `repeated_hop`

**Re-ask and cut.** Any break other than `hop_limit`, and any malformed or empty answer, gets exactly one `paths_reask` call. That call carries the broken index, the code, the detail and the accepted prefix, and uses `budget_label=f"paths:reask:{key[:12]}"`. The re-asked answer is gated from hop 1. Outcomes:
- A break at hop k retains hops 1..k−1, with `stops_at = k−1` and `break_reason = <code>`.
- `hop_limit` gives `stops_at = 20`.
- Zero retained hops becomes a `PathDrop(stage="trace", reason=<code>)`.
- A `CallFn` exception whose class is in `recoverable` becomes `reason="call_failed"`. Any other exception propagates unchanged, with no retry.
- Kept paths are sorted by `(len(hops), KIND_RANK[kind], location.path, location.line)` and get ids `p1…pN`.
- Until block 9, `tests` is `{"e2e": [], "mocks_hop_2": []}`.

**Spec and tests.** Amend these scenarios per D1 rows 2–5 in the change's spec files:
- trace-gate "Over-long trace" (`stops_at = 20`);
- "Cut after re-ask" (`stops_at = 2`);
- "Cut at hop 1" (the entry is dropped);
- path-tracing "No library-level retry" (the test passes `recoverable=(RuntimeError,)`).

The tests use a `FakeCall` that records prompt, schema, kwargs and in-flight count, answers by `budget_label` prefix, and can raise. Gate fixtures are committed to a temp git repository with a fixed author environment. The tests cover:
- a fixture argparse CLI producing a written `paths.json` whose hops re-read at the commit;
- concurrency 2 over 6 entries never having more than 2 calls in flight;
- concurrency 1 versus 8, with random delays, producing byte-identical files.

Interfaces:
- Consumes: Block 1: `async def map_paths(*, root: Path, commit: str, resolve: Resolve, call: CallFn, changed_lines: Mapping[str, Iterable[int]] | None = None, seeds: Sequence[Seed] = (), places: Sequence[Location] = (), cap: int = 10, trace_limit: int = 30, concurrency: int = 4, cache_dir: Path | None = None, fallback: bool = True, recoverable: tuple[type[BaseException], ...] = (), call_kwargs: Mapping[str, Any] | None = None, progress: Callable[[str], None] | None = None) -> PathsResult`
- Consumes: Block 1: `class PathHop(_Strict)`: the fields of walkthrough `Hop` in declaration order, then `side: Literal["head"] = "head"`
- Consumes: Block 1: `class TracedPath(_Strict): id: str; entry: PathEntry; hops: list[PathHop]; stops_at: int | None; break_reason: str | None; changed_lines_covered: list[Location]; tests: E2EListing`
- Consumes: Block 1: `class PathDrop(_Strict): stage: Literal["model_fallback_citation","model_fallback_duplicate","import_reachability","trace_limit","trace","no_changed_line","cap"]; reason: str; detail: str; entry: PathEntry`
- Consumes: Block 2: `def discover_entries(root: Path, seeds: Sequence[Seed] = (), *, index: PyIndex | None = None) -> Discovery`
- Consumes: Block 2: `PyIndex.enclosing_scope(self, path: str, line: int) -> tuple[int, int]`
- Consumes: Block 2: `class KnownEdge(_Strict): from_: Location = Field(alias="from"); to: Location`
- Produces: `OUTPUT_KIND: dict[str, str] = {"argparse_subcommand": "argparse", "click_command": "click", "typer_command": "typer", "main_module": "main", "project_script": "script", "skill_command": "skill_command", "hook_command": "hook", "seed": "seed"}`
- Produces: `KIND_RANK: dict[str, int] = {"argparse": 1, "click": 2, "typer": 3, "main": 4, "script": 5, "skill_command": 6, "hook": 7, "seed": 8, "model": 9}` (discovery kinds map to the same ranks through `OUTPUT_KIND`)
- Produces: `BREAK_CODES: tuple[str, ...] = ("malformed_answer", "empty_trace", "path_outside_root", "missing_file", "line_out_of_range", "text_mismatch", "base_side_citation", "entry_mismatch", "unconnected_hop", "repeated_hop", "hop_limit")`
- Produces: `class TraceHop(_Model)`: walkthrough `Hop` fields plus `side: Literal["head","base"] = "head"`, with `extra="forbid"`
- Produces: `class TraceAnswer(_Model): hops: list[TraceHop]`, with `extra="forbid"`
- Produces: `class GateResult(_Strict): accepted: list[PathHop]; broken_at: int | None; break_reason: str | None; detail: str`
- Produces: `def gate_hops(hops: Sequence[TraceHop], *, root: Path, resolve: Resolve, entry: Entry, known_edges: Sequence[KnownEdge], places: Sequence[Location], index: PyIndex | None = None) -> GateResult`
- Produces: `def to_path_entry(entry: Entry) -> PathEntry`
- Produces: `async def trace_entry(entry: Entry, *, ctx: TraceContext) -> TracedPath | PathDrop` (unnumbered path; `id` is assigned by `assign_ids`)
- Produces: `def assign_ids(paths: Sequence[TracedPath]) -> list[TracedPath]`
- Produces: `PATHS_TRACE_PROMPT_VERSION = "paths-trace.v1"`, `PATHS_REASK_PROMPT_VERSION = "paths-reask.v1"`
- Produces: `def paths_trace(entry: Entry, *, commit: str, changed_lines: Mapping[str, frozenset[int]] | None, known_edges: Sequence[KnownEdge], places: Sequence[Location], hop_limit: int = 20) -> str`
- Produces: `def paths_reask(entry: Entry, *, commit: str, broken_at: int, break_reason: str, detail: str, accepted: Sequence[PathHop]) -> str`

Satisfies:
- path-tracing:
  - "One trace agent per traced entry" (One request per surviving entry)
  - "Trace agents are read-only"
  - "Model calls go only through the caller's CallFn"
  - "Trace prompt carries the entry, the scope and known edges"
  - "An answer is one linear chain of hops"
  - "Hops cite head-side code only"
  - "Hop limit of 20"
  - "Empty or malformed answers are dropped, not fatal"
  - "Every answer passes through the trace gate"
  - "Bounded concurrency"
  - "Snapshot-relative locations"
  - "Deterministic trace output"
- trace-gate: every requirement except "Re-asks use the resumable answer cache", with the D1#2/#3 amendments
- paths-output-contract:
  - "Cut paths carry stop and reason together" (Complete path; Cut path)
  - "Ids are contiguous and follow array order"
  - "Byte-stable serialisation" (Concurrency does not affect output)
  - "Entry object shape" (Known discovery case keeps the add_parser line)
- path-scope-and-cap: "Path ids assigned after ordering and cap" (Contiguous ids)

## 4. Persist answers on disk so reruns resume and stay byte-identical

This block replaces the in-memory answers with `_DiskAnswers(MutableMapping)` when `cache_dir` is set, reusing `ask`/`run_tasks` unchanged.

**Storage.** Each `__setitem__` writes `<cache_dir>/paths-answers/<sha256 hex>.json` as `model_dump(mode="json")`. The write goes to a temp file in the same directory, which is `fsync`ed and then moved into place with `os.replace`. At start-up every file in the directory is loaded and re-validated into its answer model when read. Entries holding `{"error": ...}` are deleted at the start of each `map_paths` call, so a failed call is asked again on the next run and never retried within a run.

**Keys.** Each key is the sha256 of canonical JSON (`sort_keys=True`, `separators=(",", ":")`):

| Key | Inputs |
|---|---|
| `trace` | `PATHS_TRACE_PROMPT_VERSION`, `commit`, `(kind, location.path, location.line, handler)`, the sorted changed lines, the entry's sorted known edges, the sorted `places`, `20` |
| `reask` | `PATHS_REASK_PROMPT_VERSION`, the trace key, the sha256 of the first answer, the broken index, `break_reason` |

`cache_dir=None` keeps a plain dict.

**Tests.** The tests use `FakeCall` and cover the following:
- A second run on the same `cache_dir` makes zero calls, including for re-asks, and writes a byte-identical file.
- Reversing seeds, changed lines and places still makes zero calls.
- Adding one changed line re-requests the affected entries.
- Bumping `PATHS_TRACE_PROMPT_VERSION` re-requests every entry.
- A run over 5 entries interrupted after 3 cached answers makes exactly 2 trace calls on rerun, and its output equals an uninterrupted run.

Interfaces:
- Consumes: Block 3: `PATHS_TRACE_PROMPT_VERSION = "paths-trace.v1"`, `PATHS_REASK_PROMPT_VERSION = "paths-reask.v1"`
- Consumes: Block 3: `async def trace_entry(entry: Entry, *, ctx: TraceContext) -> TracedPath | PathDrop`
- Produces: `class _DiskAnswers(MutableMapping[str, Any]): def __init__(self, directory: Path | None) -> None`
- Produces: `def cache_key(kind: Literal["fallback", "trace", "reask"], payload: Mapping[str, Any]) -> str` (sha256 hex of `json.dumps(payload, sort_keys=True, separators=(",", ":"))` with `kind` included)
- Produces: `def purge_failed(answers: _DiskAnswers) -> None`

Satisfies:
- path-tracing: "Answer cache keyed independently of order"; "Resumable runs"
- trace-gate: "Re-asks use the resumable answer cache"
- paths-output-contract: "Byte-stable serialisation" (Reproducible run)
- path-scope-and-cap: "Path ids assigned after ordering and cap" (Stable ids on rerun)

## 5. Discover click, typer, `[project.scripts]` and `__main__` entries, and merge duplicate invokers

**click and typer.** `discover_entries` now emits `click_command` entries for `@click.command`, `@<group>.command`, `@click.group` and `@<group>.group`. It emits `typer_command` entries for `@<typer_app>.command` and `@<typer_app>.callback`. For each:
- `location` is the decorator line, and `handler` is the decorated `def`.
- `name` is the explicit name argument, or the function name with `_` replaced by `-`.
- The name is prefixed by parent group names and `add_typer(..., name=...)` names.

**`[project.scripts]`.** Each key in every `pyproject.toml` becomes a `project_script` entry. `location` is the key's line, and `"<module>:<function>"` is resolved under `<repo>/` and then `<repo>/src`, within the same repository only. The entry gets a known edge from the key line to the `def`. An external target yields `unresolved_target`, and invalid TOML yields `parse_error`.

**`main_module`.** These entries are anchored at the `if __name__ == "__main__":` line when it exists, and at line 1 of a package `__main__.py` otherwise (D1#12). The handler is found as follows:
1. Take the calls in the guard body, or in the module body when there is no guard.
2. Look through the transparent wrappers `sys.exit`, `exit`, `SystemExit`, `asyncio.run` and `raise SystemExit(...)` to their arguments.
3. Keep only callees that resolve within the module or the same repository.
4. If exactly one remains, it is the handler, so `sys.exit(main(sys.argv[1:]))` resolves to `main`. Otherwise the handler is `null`.

The entry gets a known edge from the location to the handler. Files named `test_*.py`, `*_test.py` or `conftest.py`, and files under `tests/`, are excluded.

**Merging.** `merge_entries` merges on `(handler.path, handler.line)` only when `handler.symbol` is not `null`:
- The kept entry has the best `KIND_RANK`, with ties going to the smallest `(location.path, location.line)`.
- The others go to `also_invoked_by`, sorted by `(path, line, kind)`, and their known edges are unioned without duplicates.
- Entries with a `null` handler, or a non-`def` handler, merge only with an identical `(kind, location)`.
- `paths.json` writes `also_invoked_by` kinds as output kinds.

**Tests.** They run against `make_tree` fixtures and through `map_paths`. Fixtures must not commit a real `pyproject.toml`. The tests cover:
- `tool = "pkg.cli:main"` plus `pkg/__main__.py` produces one `main` path whose `also_invoked_by` holds the `script` invoker and whose edges include the `pyproject.toml` key line;
- a seed duplicating a click handler is folded into `also_invoked_by`;
- two `null`-handler argparse entries at different lines stay separate.

Interfaces:
- Consumes: Block 2: `def discover_entries(root: Path, seeds: Sequence[Seed] = (), *, index: PyIndex | None = None) -> Discovery`
- Consumes: Block 2: `PyIndex.resolve_module(self, importer: str, dotted: str, level: int = 0) -> str | None`
- Consumes: Block 2: `PyIndex.resolve_name(self, path: str, line: int, name: str) -> Handler | None`
- Consumes: Block 3: `KIND_RANK: dict[str, int] = {"argparse": 1, "click": 2, "typer": 3, "main": 4, "script": 5, "skill_command": 6, "hook": 7, "seed": 8, "model": 9}`
- Consumes: Block 3: `def to_path_entry(entry: Entry) -> PathEntry`
- Produces: `def merge_entries(entries: Sequence[Entry]) -> list[Entry]`
- Produces: `def main_module_handler(index: PyIndex, path: str) -> tuple[Location, Handler | None]`

Satisfies:
- entry-point-discovery:
  - "click and typer commands are discovered"
  - "[project.scripts] entries are discovered"
  - "__main__ modules and packages are discovered", as amended by D1#12
  - "Entries resolving to the same handler are merged" (Script and __main__ resolve to the same main(); Seed duplicating a discovered entry; Unresolved handlers not merged)
- path-tracing: "One trace agent per traced entry" (De-duplicated entries share a trace)
- paths-output-contract: "Entry object shape" (Deduplicated invokers recorded)

## 6. Discover `SKILL.md` and hook commands, resolving through shell launchers

**`SKILL.md` commands.** `discover_entries` now scans every `SKILL.md` for fenced blocks whose info string is `bash`, `sh`, `shell`, `zsh` or `console`:
- Commands are tokenised with `shlex` after joining `\` line continuations.
- Accepted prefixes are environment assignments, `cd <dir> &&`, and `uv run [--directory D | --project D | flags]`.
- Each `python[3] -m <module>` command becomes a `skill_command` entry. Its `invocation` is the full command, and its `name` is `<skill dir>: <module> [<first positional>]`.

The module is searched for in these roots, in order:
1. `PYTHONPATH=` directories.
2. `cd`, `--directory` and `--project` directories.
3. The `SKILL.md` directory.
4. Its `scripts/` subdirectory.
5. The repository root.
6. `<repository root>/src`.

`${CLAUDE_SKILL_DIR}` is expanded to the `SKILL.md` directory. The known edge goes to one of two targets:
- If the first positional names a discovered subcommand of that module, the edge goes to that subcommand's handler, or to its `add_parser` line when the handler is `null`.
- Otherwise the edge goes to the `main_module` location (D1#12).

**Hook commands.** Every `"type": "command"` hook in `hooks.json` and `.claude/settings*.json` becomes a `hook_command` entry:
- `name` is `<event>:<matcher>`, or `<event>` when there is no matcher.
- `location` is the line of the command string, assigning repeated strings in document order.
- `${CLAUDE_PLUGIN_ROOT}` expands to the nearest ancestor with `.claude-plugin/plugin.json`, or to the hooks file directory's parent when there is none.
- `$CLAUDE_PROJECT_DIR` and `${CLAUDE_PROJECT_DIR}` expand to the repository root.

Launcher resolution (D1#14) then works on the expanded tokens:
1. Leading launcher tokens are stripped while they are `sh`, `bash`, `/bin/sh`, `/bin/bash`, `exec`, `env`, an existing `.sh` file, or `uv run` with its flags.
2. `-m X` resolves as for `SKILL.md`.
3. Otherwise the first existing `.py` token resolves like a `main_module` script.
4. Only when neither applies does the hook target the launcher or file itself, at line 1 with `symbol: null`.

**Diagnostics.** These cases produce diagnostics:
- A missing target gives `unresolved_target` at the command line.
- Invalid JSON gives `parse_error`.
- `"type": "prompt"` hooks are ignored.

**Spec and tests.** Amend the hook requirement and "Command naming no subcommand" in the change's spec files. The tests generate `SKILL.md` and `hooks.json` into `tmp_path`, never committing them. They cover:
- 8 `sh ${CLAUDE_PLUGIN_ROOT}/hooks/run.sh <script>.py` hooks yield 8 separate entries, each targeting its own script;
- a hook and a `SKILL.md` command running `python -m pkg` merge into one entry;
- the mixed-failure fixture returns exactly five diagnostics: `parse_error`, `parse_error`, `unresolved_target`, `dynamic_name` and `seed_not_found`.

Interfaces:
- Consumes: Block 2: `def discover_entries(root: Path, seeds: Sequence[Seed] = (), *, index: PyIndex | None = None) -> Discovery`
- Consumes: Block 5: `def merge_entries(entries: Sequence[Entry]) -> list[Entry]`
- Consumes: Block 5: `def main_module_handler(index: PyIndex, path: str) -> tuple[Location, Handler | None]`
- Produces: `def command_tokens(command: str, *, source_file: str, root: Path) -> list[str]` (variables expanded, launchers stripped)
- Produces: `def resolve_python_m(module: str, search_roots: Sequence[str], index: PyIndex) -> str | None`

Satisfies:
- entry-point-discovery:
  - "python -m commands in SKILL.md fenced shell blocks are discovered", as amended by D1#12
  - "Hook commands are discovered", as amended by D1#14
  - "Entries resolving to the same handler are merged" (Hook and SKILL.md invoke the same module)
  - "Every skipped candidate is explained"
  - "Entry record shape" (Fields present and well-formed, across every kind)

## 7. Run a single read-only model fallback for entry points that static discovery missed

When `fallback=True`, `map_paths` now makes exactly one `CallFn` call after discovery. The call uses `budget_label="paths:fallback"` and `allowed_tools=["Read","Grep","Glob"]`.

**Prompt and caching.** The `paths_fallback` prompt carries:
- the scope: the changed files, or the whole snapshot when unscoped (D1#11);
- every deterministic and seed entry, sorted by location;
- the existing citation and untrusted-content rules;
- a request for entry points not already listed.

The answer is cached under `cache_key("fallback", ...)`. The key is built from `PATHS_FALLBACK_PROMPT_VERSION`, `commit`, the sorted entry identities and the sorted scope.

**Parsing.** The answer may be a `FallbackAnswer`, a dict or a JSON string, and the model is lenient:
- An unknown `kind` becomes `other`.
- A `ValidationError` gives `fallback` `{"status": "failed", "reason": <non-empty>}`, and the run continues with deterministic entries only.
- A valid empty answer gives `{"status": "ok", "reason": null}`.
- `fallback=False` gives `skipped` and makes no call.
- An exception class in `recoverable` gives `failed`. Any other exception propagates after exactly one attempt.

**Filtering.** Each listed entry passes three checks in order. The first failure decides the drop:
1. A missing citation gives `PathDrop(stage="model_fallback_citation", reason="missing_citation")`.
2. A citation that fails `check_citation` through `resolve`, or is absolute, escapes `root` or is out of range, gives `reason="citation_unverified"`.
3. A cited line that equals a deterministic or seed entry's location, known-edge `from` line or handler line, or an earlier fallback entry's line, gives `PathDrop(stage="model_fallback_duplicate", reason="duplicate")`.

**Kept entries.** They become kind `model` with location at the cited line. `handler` is the enclosing `def` when the cited file is Python and the line is inside one, and `null` otherwise. They are sorted by location and traced through the existing trace path. Two stubs returning the same entries in different orders produce byte-identical files. A multi-repository citation such as `repo_b/cli.py:5` is recorded as written.

Interfaces:
- Consumes: Block 1: `class FallbackStatus(_Strict): status: Literal["ok","failed","skipped"]; reason: str | None`
- Consumes: Block 3: `async def trace_entry(entry: Entry, *, ctx: TraceContext) -> TracedPath | PathDrop`
- Consumes: Block 3: `def to_path_entry(entry: Entry) -> PathEntry`
- Consumes: Block 4: `def cache_key(kind: Literal["fallback", "trace", "reask"], payload: Mapping[str, Any]) -> str`
- Consumes: Block 2: `PyIndex.enclosing_scope(self, path: str, line: int) -> tuple[int, int]`
- Produces: `PATHS_FALLBACK_PROMPT_VERSION = "paths-fallback.v1"`
- Produces: `def paths_fallback(*, commit: str, scope: Sequence[str] | None, entries: Sequence[Entry]) -> str`
- Produces: `class FallbackEntry(_Model): kind: str; name: str; citation: Citation | None = None; reason: str` (kind normalised to `"other"` when it is not a known kind)
- Produces: `class FallbackAnswer(_Model): entries: list[FallbackEntry]`
- Produces: `async def run_fallback(*, ctx: TraceContext, entries: Sequence[Entry], scope: Sequence[str] | None) -> tuple[FallbackStatus, list[Entry], list[PathDrop]]`

Satisfies:
- entry-point-model-fallback: every requirement, with "Single fallback agent per run" as amended by D1#11. "Fallback entries go through the same scope filter" is completed in block 8.
- paths-output-contract: "Every drop is recorded with a reason" (Uncited model entry recorded)

## 8. Scope a run to the changed lines with reachability, trace limit, coverage and cap

When `changed_lines is not None`, `map_paths` now applies four filters. All of them also apply to seed and model entries.

**1. Import-graph reachability, before any call.** `PyIndex` gains an import graph built as follows:
- Every collected import contributes an edge, including function-local and `try` imports and excluding `TYPE_CHECKING` imports.
- `import a.b.c` adds edges to `a/__init__.py` and `a/b/__init__.py`. `from x import y` adds an edge to `x`, plus `x/y.py` when that is a module.
- Relative imports resolve by package path.
- Absolute imports resolve in `<repo>/`, then `<repo>/src/`, then the importer's directory, then the other repository roots and their `src/` in sorted order (D13). Discovery handler resolution stays same-repository.
- An unresolved third-party import adds no edge, and a module with a syntax error has no outgoing edges.

An entry's distance is 0 when its location, any `also_invoked_by` location, or its handler file is a changed file. Otherwise it is the shortest visited-set BFS distance from the handler's module, or from the location's module when the handler is `null`. With no path, the entry becomes `PathDrop(stage="import_reachability", reason="unreachable")`. An empty `{}` mapping, or only missing files, drops every entry this way.

**2. Trace limit.** Reachable entries are sorted by `(distance, KIND_RANK, location.path, location.line)`. Entries beyond `trace_limit` become `stage="trace_limit", reason="over_trace_limit"`, with no call.

**3. Coverage.** `changed_lines_covered` lists the distinct, sorted `(path, line)` pairs that fall inside a retained hop's inclusive range. Hops removed by a gate cut do not count. A traced path covering nothing becomes `stage="no_changed_line", reason="no_changed_line_covered"`.

**4. Order, cap and ids.** Kept paths are ordered by `(−len(changed_lines_covered), len(hops), KIND_RANK, location.path, location.line)`. Paths beyond `cap` become `stage="cap", reason="over_cap"`, and ids `p1…pN` are assigned last.

**Records and invariants.**
- Drops are sorted by the stage order `model_fallback_citation`, `model_fallback_duplicate`, `import_reachability`, `trace_limit`, `trace`, `no_changed_line`, `cap`, then by `entry.location.path`, `entry.location.line` and `entry.kind`.
- Unscoped runs emit no scope-stage drops and keep `changed_lines_covered: []`.
- A test asserts `len(paths) + len(dropped) == len(merged deterministic and seed entries) + len(entries the fallback agent listed)`.

**Tests.** They use fixtures with chains `pkg/cli.py → pkg/service.py → pkg/core.py`, `from . import b` / `from .sub import c`, an `x`/`y` import cycle, and a multi-repository `repo_b/src/mod.py`. `FakeCall` asserts that no trace prompt is sent for an `unreachable` or `over_trace_limit` entry.

Interfaces:
- Consumes: Block 1: `def normalise_changed_lines(root: Path, changed_lines: Mapping[str, Iterable[int]]) -> dict[str, frozenset[int]]`
- Consumes: Block 2: `class PyIndex` with `@classmethod def build(cls, root: Path) -> PyIndex`
- Consumes: Block 3: `KIND_RANK: dict[str, int] = {"argparse": 1, "click": 2, "typer": 3, "main": 4, "script": 5, "skill_command": 6, "hook": 7, "seed": 8, "model": 9}`
- Consumes: Block 3: `def assign_ids(paths: Sequence[TracedPath]) -> list[TracedPath]`
- Produces: `PyIndex.import_edges(self, path: str) -> frozenset[str]`
- Produces: `PyIndex.import_distance(self, source: str, targets: frozenset[str]) -> int | None`
- Produces: `def entry_distance(entry: Entry, index: PyIndex, changed: Mapping[str, frozenset[int]]) -> int | None`
- Produces: `def covered_lines(hops: Sequence[PathHop], changed: Mapping[str, frozenset[int]]) -> list[Location]`
- Produces: `def order_paths(paths: Sequence[TracedPath]) -> list[TracedPath]`
- Produces: `def sort_drops(drops: Sequence[PathDrop]) -> list[PathDrop]`

Satisfies:
- path-scope-and-cap: every requirement
- path-tracing: "One trace agent per traced entry" (Pre-filtered entries are not traced)
- entry-point-model-fallback: "Fallback entries go through the same scope filter"
- paths-output-contract: "Top-level document shape" (Unscoped run; Scoped run); "Changed-line coverage field"; "Every drop is recorded with a reason" (Cap drop recorded)
- trace-gate: "Hops after a gate cut do not count", through the path-scope-and-cap "Post-trace coverage filter"

## 9. List end-to-end tests that statically invoke each kept path's entry

`list_e2e_tests` fills `tests.e2e` for every kept path, and fills `test_listing_skips` with `decode_error` or `syntax_error` records, with file paths relative to the snapshot root.

**Scope.** It parses `test_*.py` and `*_test.py` under `root` with `ast` only:
- It skips `.git`, `.venv`, `venv`, `node_modules`, `__pycache__` and `site-packages`.
- It never imports, collects or spawns anything, and the tests check this by patching `subprocess.Popen` and `os.system` to raise.
- Tests are module-level `test*` functions and `test*` methods of module-level `Test*` classes. Nested functions and other classes are excluded.

**Match kinds.** A test matches when its body has one of these:
- `direct_call`: a callee that resolves through the test's imports and aliases to the handler.
- `cli_argv`: a call to the subcommand module's function whose body calls `.parse_args(` or `.parse_known_args(`, with a literal string list or tuple argv containing the subcommand token.
- `cli_runner`: `CliRunner().invoke(<resolved app>, <literal args>)`.
- `subprocess`: `subprocess.run`, `call`, `check_call`, `check_output` or `Popen`, with a literal list or string. Its tokens must match `sys.executable`/`python`/`python3` `-m <module> [<subcommand>]`, the console script name, or the hook's `command_tokens`.
- `runpy`: `runpy.run_module("<module>", ...)`.

Invoking any `also_invoked_by` invoker also counts. A same-named symbol from another module does not match.

**Helper inlining (D15).** A helper is a function defined in the test module, an in-scope `conftest.py`, or a non-test module under the same `tests/` tree. When a test calls one with literal arguments and the helper forwards a parameter into a match form (`p`, `[*prefix, *p]`, `["x", *p]` or `list(p)`), the literal is substituted. Inlining goes at most 2 levels deep. The record takes the test's call line and the inner form's match kind. A non-literal argv at the test level still never matches.

**Exclusion and records.** Argv containing `-h`, `--help` or `--version` is excluded. Each `node_id` appears at most once per path, using the earliest `invocation_line`, with ties broken in the order `direct_call`, `cli_argv`, `cli_runner`, `subprocess`, `runpy`. Records are sorted by `(file, line, node_id)`, and a test may appear on several paths.

**Spec and tests.** Amend e2e-test-listing "CLI subcommand invocation" per D1#13. The tests cover:
- a top-level side-effect fixture writes no marker file;
- reversed filesystem enumeration gives byte-identical lists;
- a live-copy test of `skills/pr/scripts/`, skipped if absent, lists tests that go through the `run(argv)` helper.

Interfaces:
- Consumes: Block 1: `class E2ETest(_Strict): node_id: str; file: str; line: int; match: Literal["direct_call","cli_argv","cli_runner","subprocess","runpy"]; invocation_line: int`
- Consumes: Block 1: `class E2EListing(_Strict): e2e: list[E2ETest]; mocks_hop_2: list[MockedE2ETest]`
- Consumes: Block 1: `class ListingSkip(_Strict): file: str; reason: Literal["decode_error","syntax_error"]`
- Consumes: Block 2: `PyIndex.resolve_name(self, path: str, line: int, name: str) -> Handler | None`
- Consumes: Block 6: `def command_tokens(command: str, *, source_file: str, root: Path) -> list[str]`
- Consumes: Block 8: `PyIndex.import_edges(self, path: str) -> frozenset[str]`
- Produces: `class TestFileIndex` is forbidden by naming rules, so: `class E2EIndex` with `@classmethod def build(cls, root: Path, index: PyIndex) -> E2EIndex` and `E2EIndex.skips(self) -> list[ListingSkip]`
- Produces: `def match_invocations(e2e_index: E2EIndex, entry: Entry, *, root: Path) -> list[E2ETest]`
- Produces: `def list_e2e_tests(root: Path, paths: Sequence[TracedPath], entries: Mapping[str, Entry], *, index: PyIndex) -> tuple[dict[str, E2EListing], list[ListingSkip]]` (keyed by path id)

Satisfies:
- e2e-test-listing:
  - "Listing runs statically with no model call and no execution"
  - "Test discovery scope"
  - "Direct handler invocation is recognised"
  - "CLI subcommand invocation is recognised", as amended by D1#13
  - "Subprocess, runpy and script invocation is recognised"
  - "Alternate invokers count"
  - "Listed test record shape" (for `e2e_tests`)
  - "Deterministic ordering and deduplication"
  - "Empty lists are explicit"
  - "Unreadable test files are skipped with a reason"

## 10. Separate tests that mock hop 2, and drop tests that patch out the handler

This block extends `list_e2e_tests` with patch recognition. The recognised forms are:
- `mock.patch` and `unittest.mock.patch` with a string target, used as a decorator, as a `with` context manager, or through `.start()`;
- `mock.patch.object(<resolved object>, "<attr>")`;
- `monkeypatch.setattr` with a dotted string target, or with an object-and-name pair.

**Where patches come from.** Patches count when they are in the test itself, or in a pytest fixture that is either requested by parameter name or marked `autouse=True`. That fixture may be defined in the test module or in any `conftest.py` from the test's directory up to `root`.

**Moving a test to `mocks_hop_2`.** A matched test moves from `tests.e2e` to `tests.mocks_hop_2` when a patch target names the symbol at hop 2 of the retained hops, in one of two ways:
- at its defining module, for example `pkg.core.run`;
- under a name bound in hop 1's module through `PyIndex` bindings, including function-local `from ... import`, for example `pkg.cli.run`.

The moved record gets `patch_file` and `patch_line`, which may point into a fixture or `conftest.py`. Three cases leave the test in `e2e`:
- a patch of any other symbol, such as `pkg.core.log`;
- a path with a single retained hop;
- a path cut after hop 1, even if it patches the model's original hop 2.

A test can never appear in both lists.

**Handler-patch exclusion (D15).** A test is not listed for a path at all when it patches the entry's handler, or a function containing one of the entry's known-edge `from` lines.

**Tests.** They use fixtures with `conftest.py` written into `tmp_path`. One scenario checks the record fields: a test at line 10 calls `main(["open"])` at line 12 under `@mock.patch("pkg.core.run")` at line 9. Its record must have `line` 10, `invocation_line` 12, `match` `cli_argv` and `patch_line` 9. A requested `fake_run` conftest fixture moves the test, and the same fixture unrequested and not autouse leaves it in `e2e`.

Interfaces:
- Consumes: Block 1: `class MockedE2ETest(_Strict): node_id: str; file: str; line: int; match: Literal["direct_call","cli_argv","cli_runner","subprocess","runpy"]; invocation_line: int; patch_file: str; patch_line: int`
- Consumes: Block 2: `PyIndex.resolve_name(self, path: str, line: int, name: str) -> Handler | None`
- Consumes: Block 9: `def match_invocations(e2e_index: E2EIndex, entry: Entry, *, root: Path) -> list[E2ETest]`
- Consumes: Block 9: `def list_e2e_tests(root: Path, paths: Sequence[TracedPath], entries: Mapping[str, Entry], *, index: PyIndex) -> tuple[dict[str, E2EListing], list[ListingSkip]]`
- Produces: `def applied_patches(e2e_index: E2EIndex, node_id: str, *, root: Path) -> list[tuple[str, str, int]]` (resolved dotted target, patch file, patch line)

Satisfies:
- e2e-test-listing:
  - "Tests that mock hop 2 are listed separately"
  - "Hop-2 patches applied by fixtures are recognised"
  - "Listing applies to cut paths"
  - "Listed test record shape" (Record fields are populated)
- paths-output-contract: "Test-listing shape" (Mocked test listed separately)