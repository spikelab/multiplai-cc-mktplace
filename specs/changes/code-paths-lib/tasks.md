## 1. Walking skeleton: the `paths.json` contract and an empty `map_paths`

This block adds `code_facts/paths.py` with these pieces:
- every output model, built on `_Strict(_Model)` with `ConfigDict(extra="forbid", regex_engine="python-re", validate_assignment=True, populate_by_name=True)`;
- the `EntryKind` StrEnum and `KIND_RANK`;
- `schema_text()` and the committed `code_facts/schema/paths.v1.schema.json`, loaded through `importlib.resources`, with `$schema` hard-coded to `https://json-schema.org/draft/2020-12/schema` and `$id` set to `paths.v1.schema.json`;
- `write_paths_file`;
- a keyword-only `map_paths` that returns an empty `PathsResult` that passes the schema. It sets `scoped = changed_lines is not None` and echoes `cap` only when scoped, otherwise `null`. The fallback is always `skipped`, with one of these reasons:
  - `"disabled"` when `model_fallback=False`;
  - `"empty_scope"` when `changed_lines == {}`, or when unscoped with empty `scope_paths`;
  - the interim `"not_implemented"` in every other case.

It also runs `uv add 'jsonschema>=4'` as a runtime dependency followed by `uv lock`, and adds the new exports to `code_facts/__init__.py` while keeping every earlier export.

`write_paths_file` works in this order:
1. `model_dump(mode="json", by_alias=True)`.
2. `jsonschema.Draft202012Validator`.
3. `PathsResult.model_validate` on the dumped dict, to apply the cross-field invariants the schema cannot express.
4. `json.dumps(indent=2, ensure_ascii=False) + "\n"`, written as UTF-8 to a same-directory `tempfile.mkstemp(prefix="." + name + ".", suffix=".tmp")`.
5. `os.replace` onto the target, chmod to `0o666 & ~umask`, and `unlink(missing_ok=True)` of the tempfile in `finally`.

Any failure raises `PathsSchemaError`. Its `.pointer` is an RFC 6901 pointer built from `str(part)` for each `absolute_path` element: integer indices are stringified, `~` and `/` are escaped, and `required` / `additionalProperties` errors also name the offending key. It must never raise `TypeError`, must leave the target untouched, and must leave no `*.tmp` file behind. A serialiser patched to raise mid-write must leave the old bytes in place.

The acceptance tests cover these edge cases:
- The path rule `^(?!/)(?!.*\n)(?!.*(^|/)\.\.?(/|$))[^\\]+$` gives the same verdict in pydantic and in jsonschema:
  - accepts `a/.hidden/b.py`;
  - rejects `""`, `./a.py`, `a/../b`, `/abs/x.py`, `a\b.py` and `a/b.py\n`.
- `"scoped": "false"` is rejected, because the field is `StrictBool`.
- All nine top-level fields appear in the schema's `required` list, because none has a default.
- These are rejected:
  - `stops_at=2` together with `break_reason=None`;
  - `stops_at > len(hops)`;
  - `scoped=False` with a non-null `cap`;
  - an unscoped result that contains a `cap` or `no_changed_line` drop;
  - a node id that appears in both `tests.e2e` and `tests.mocks_hop_2`;
  - non-contiguous ids;
  - `"kind": "route"`.
- Changing a model after it is constructed raises.
- The schema's `$schema` equals the value in `skills/review/schema/facts.v1.schema.json` (read in the test only).
- `Draft202012Validator.check_schema` passes.
- The committed schema file matches `schema_text()` byte for byte.
- A default unscoped call records `skipped` / `"empty_scope"` even though `model_fallback=True` is the default.

Guard tests:
- the `Hop` field names equal the walkthrough `Hop` fields, read by AST-parsing `walkthrough_pipeline/models.py`, plus `side`;
- no module under `code_facts/` imports `walkthrough_pipeline`;
- no library function or class name starts with `test` or `Test`.

Run it with `uv run pytest tests/test_paths_contract.py` from `plugins/multiplai-dev/lib/code_facts`. The existing suite must pass unchanged.

Interfaces:
- Produces: `class EntryKind(enum.StrEnum)` with members `argparse_subcommand, click_command, typer_command, main_module, project_script, skill_command, hook_command, seed, other`
- Produces: `KIND_RANK: Mapping[EntryKind, int]` (a `MappingProxyType` built from `list(EntryKind)`; `KIND_RANK[EntryKind.other] == 8`)
- Produces: `MAX_HOPS: int = 20`
- Produces: `class Location(_Strict): path: str; line: int`
- Produces: `class HandlerRef(_Strict): path: str; line: int; symbol: str`
- Produces: `class KnownEdge(_Strict): from_: Location = Field(alias="from"); to: Location`
- Produces: `class Invoker(_Strict): kind: EntryKind; name: str; location: Location`
- Produces: `class Entry(_Strict): kind: EntryKind; name: str; location: Location; handler: HandlerRef | None; invocation: str | None; known_edges: list[KnownEdge]; also_invoked_by: list[Invoker]; source: Literal["discovered", "seed", "model"]`
- Produces: `class Hop(_Strict): symbol_called: str; path: str; line_start: int; line_end: int; quote: str; note: str = ""; side: Literal["base", "head"] = "head"`
- Produces: `BreakReason = Literal["malformed_answer", "empty_trace", "path_outside_root", "missing_file", "line_out_of_range", "text_mismatch", "base_side_citation", "entry_mismatch", "unconnected_hop", "repeated_hop", "hop_limit"]`
- Produces: `PublishedBreakReason = Literal["path_outside_root", "missing_file", "line_out_of_range", "text_mismatch", "base_side_citation", "unconnected_hop", "repeated_hop", "hop_limit"]`
- Produces: `class E2ERecord(_Strict): node_id: str; file: str; line: int; match: Literal["direct_call", "cli_argv", "cli_runner", "subprocess", "runpy"]; invocation_line: int`
- Produces: `class MockedE2ERecord(E2ERecord): patch_line: int`
- Produces: `class E2EListing(_Strict): e2e: list[E2ERecord]; mocks_hop_2: list[MockedE2ERecord]`
- Produces: `class TracedPath(_Strict): id: str; entry: Entry; hops: list[Hop]; stops_at: int | None; break_reason: PublishedBreakReason | None; changed_lines_covered: list[Location]; tests: E2EListing`
- Produces: `DropStage = Literal["model_fallback_citation", "import_reachability", "trace_limit", "trace", "no_changed_line", "cap"]`
- Produces: `DropReason = Literal["missing_citation", "citation_unverified", "duplicate", "unreachable", "over_trace_limit", "trace_call_failed", "malformed_trace", "empty_trace", "cut_at_entry", "no_changed_line_covered", "over_cap"]`
- Produces: `class DropRecord(_Strict): stage: DropStage; entry: Entry; reason: DropReason` (a validator enforces the D11 reason set allowed for each stage)
- Produces: `class Diagnostic(_Strict): path: str; line: int | None; reason: Literal["parse_error", "unresolved_target", "unresolved_handler", "dynamic_name", "seed_not_found"]; detail: str`
- Produces: `class ListingSkip(_Strict): file: str; reason: Literal["decode_error", "syntax_error"]`
- Produces: `class FallbackOutcome(_Strict): status: Literal["ok", "failed", "skipped"]; reason: str | None`
- Produces: `class PathsResult(_Strict): schema_version: Literal["paths.v1"]; commit: str; scoped: StrictBool; cap: int | None; fallback: FallbackOutcome; paths: list[TracedPath]; dropped: list[DropRecord]; diagnostics: list[Diagnostic]; test_listing_skips: list[ListingSkip]`
- Produces: `class Seed(_Model): path: str; line: int | None = None; symbol: str | None = None; name: str | None = None`
- Produces: `class Place(_Model): path: str; line: int`
- Produces: `class PathsSchemaError(ValueError): pointer: str`
- Produces: `def schema_text() -> str`
- Produces: `def write_paths_file(result: PathsResult, path: Path) -> None`
- Produces: `async def map_paths(*, root: Path, commit: str, call: CallFn, resolve: Resolve, changed_lines: Mapping[str, Iterable[int]] | None = None, seeds: Sequence[Seed] = (), places: Sequence[Place] = (), cap: int = 6, trace_limit: int | None = None, scope_paths: Sequence[str] = (), model_fallback: bool = True, concurrency: int | None = None, recoverable: tuple[type[BaseException], ...] = (), fatal: tuple[type[BaseException], ...] = (), cache_dir: Path | None = None, call_kwargs: Mapping[str, Any] | None = None) -> PathsResult`

Satisfies:
- paths-output-contract:
  - Public entry points exported from the library: both scenarios
  - `map_paths` returns a model and writes only cache files: both
  - Schema file exists and follows the review facts schema style: all three
  - Top-level document shape: all four
  - Path object shape: both
  - Entry object shape: Unknown kind rejected
  - Hop object shape mirrors the walkthrough Hop: all three
  - Locations are relative to the snapshot root: Absolute or escaping path rejected; Line zero rejected
  - Cut paths carry stop and reason together: Mismatched pair rejected
  - Test-listing shape: Overlap rejected
  - Every drop is recorded with a reason: Empty reason rejected
  - Empty results are valid
  - `write_paths_file` validates before writing: both
  - `write_paths_file` writes atomically
  - Byte-stable serialisation: Trailing newline
  - Round-trip between file and model: both
  - Schema and models agree: both
  - Schema validation dependency is declared
- entry-point-model-fallback: Fallback can be disabled by the caller

## 2. Discovery core: snapshot walker, module index, `__main__` entries, seeds, merge and diagnostics

This block adds `code_facts/pyindex.py` and `code_facts/entries.py`. `discover_entries` is synchronous, takes no `CallFn`, and never imports snapshot code. `map_paths` now copies `Discovery.diagnostics` into `PathsResult.diagnostics`.

**Walker.**
- Uses `os.scandir`, sorted by name.
- Prunes `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `site-packages`, `build` and `dist` at any depth before descending. Matching is exact and case-sensitive, and the root's own basename is never tested.
- Does not follow a symlink whose `resolve()` is outside the root.
- Keeps a visited set of `(st_dev, st_ino)`, so symlink loops inside the root terminate. `RuntimeError` / `OSError` from `resolve()` on a loop is caught. `PermissionError` on an unreadable directory is logged and that directory is skipped.

**Repository roots.** A repository root is the nearest directory at or below the snapshot root that contains `.git` (file or directory) or `pyproject.toml`.

**Module index.**
- Searches, in order: the repo root, then `<repo>/src`, then each nested `pyproject.toml` directory and its `src/`. The first root that matches wins, and resolution never crosses into another repository.
- When both `a.py` and `a/__init__.py` exist, it picks `a/__init__.py`, as CPython does.

**Parse cache.** Files are passed to `ast.parse` as bytes, so PEP 263 cookies and BOMs are honoured. `SyntaxError` (including NUL bytes), `UnicodeDecodeError`, `ValueError`, `RecursionError` and `MemoryError` each produce exactly one `parse_error` diagnostic with `line=None`. One bad file never aborts discovery.

**`__main__` scanner.**
- A package `__main__.py` gives an entry at line 1, named with the dotted package name.
- A top-level `if __name__ == "__main__":` gives an entry at the `if` line. It is recognised with either operand order and either quote style, only directly in `Module.body`, and only in non-test files. Test files are `test_*.py`, `*_test.py`, `conftest.py` and anything under `tests/`.
- When one `__main__.py` has both forms, it emits only the line-1 package entry and resolves the handler from the guard body.
- A zero-byte `__main__.py` emits no entry. It records `parse_error`, `line=None`, detail `"empty file"`.
- The handler resolves when the body's single top-level statement is a call to `Name(...)` or `mod.Name(...)`. It also resolves through `sys.exit(X())`, `raise SystemExit(X())` and `asyncio.run(X())`, picking `X`, not `sys.exit`.
- A single call whose target is outside the repository gives `handler=None` plus an `unresolved_handler` diagnostic.

**Seeds.**
- Seeds are sorted before processing.
- A `line` seed resolves to the innermost `FunctionDef` / `AsyncFunctionDef` whose span, including its decorator lines, contains that line.
- A `symbol` seed resolves to the last top-level definition with that name.
- A missing file, a line out of range, an unknown symbol, a module-level line with no enclosing function, an absolute path, a `..` path, or a path that reaches outside the root through a symlink each give `seed_not_found`.
- A seed into an unparseable file gives only that file's `parse_error`.
- Two identical seeds give one entry.

**Merge.**
- Entries are grouped by `(handler.path, handler.line)`. Entries with `handler=None` are grouped only when their `location` is identical.
- The kept entry is the one with the smallest `(KIND_RANK, location.path, location.line)`.
- `also_invoked_by` is sorted by `(path, line, kind, name)`.
- `known_edges` are unioned, de-duplicated and sorted.

**Output order.** Entries are sorted by `(location.path, location.line, kind, name)`. Diagnostics are sorted by `(path, -1 if line is None else line, reason, detail)`.

**Tests.**
- A spy `CallFn` records zero calls.
- `subprocess.Popen` and `os.system` are patched to raise, and discovery still runs.
- A sentinel `__main__.py` that writes a file and raises at import still yields its entry, and the file is never created.
- A seed duplicating a `main_module` handler merges into it.
- Test helper `tests/repo_fixture.py::build_repo` writes trees with `shutil.copytree(..., dirs_exist_ok=True)` semantics and runs `git init` plus a commit with a fixed author and `GIT_CEILING_DIRECTORIES`.
- `tests/conftest.py` sets `collect_ignore_glob = ["fixtures/*"]`, so fixture `test_*.py` and `conftest.py` files are never collected.

Run it with `uv run pytest tests/test_discovery_core.py`.

Interfaces:
- Produces: `def build_index(root: Path) -> PyIndex`
- Produces: `class PyIndex` with `root: Path`, `def files(self) -> list[str]`, `def parse(self, rel: str) -> ast.Module | None`, `def parse_error(self, rel: str) -> str | None`, `def repo_root(self, rel: str) -> str`, `def module_name(self, rel: str) -> str | None`, `def resolve_module(self, dotted: str, *, from_file: str, extra_roots: Sequence[str] = ()) -> str | None`, `def resolve_symbol(self, rel: str, name: str) -> HandlerRef | None`
- Produces: `class Discovery(NamedTuple): entries: list[Entry]; diagnostics: list[Diagnostic]`
- Produces: `def discover_entries(root: Path, *, seeds: Sequence[Seed] = (), index: PyIndex | None = None) -> Discovery`
- Produces: `def build_repo(dest: Path, tree: Mapping[str, str | bytes], *, git: bool = True) -> Path` (in `tests/repo_fixture.py`)
- Consumes: Block 1: `class Entry(_Strict): kind: EntryKind; name: str; location: Location; handler: HandlerRef | None; invocation: str | None; known_edges: list[KnownEdge]; also_invoked_by: list[Invoker]; source: Literal["discovered", "seed", "model"]`
- Consumes: Block 1: `class Diagnostic(_Strict): path: str; line: int | None; reason: Literal["parse_error", "unresolved_target", "unresolved_handler", "dynamic_name", "seed_not_found"]; detail: str`
- Consumes: Block 1: `class Seed(_Model): path: str; line: int | None = None; symbol: str | None = None; name: str | None = None`
- Consumes: Block 1: `KIND_RANK: Mapping[EntryKind, int]`
- Consumes: Block 1: `async def map_paths(*, root: Path, commit: str, call: CallFn, resolve: Resolve, changed_lines: Mapping[str, Iterable[int]] | None = None, seeds: Sequence[Seed] = (), places: Sequence[Place] = (), cap: int = 6, trace_limit: int | None = None, scope_paths: Sequence[str] = (), model_fallback: bool = True, concurrency: int | None = None, recoverable: tuple[type[BaseException], ...] = (), fatal: tuple[type[BaseException], ...] = (), cache_dir: Path | None = None, call_kwargs: Mapping[str, Any] | None = None) -> PathsResult`

Satisfies:
- entry-point-discovery:
  - Discovery is deterministic and makes no model call: both scenarios
  - Discovery never imports or runs target code: the `__main__.py` half of "Module with import-time side effects"
  - Excluded and unsafe locations are not scanned: both
  - `__main__` modules and packages are discovered: all four
  - Caller-supplied seeds become entries: all three
  - Discovery output is stably ordered: both

## 3. Trace slice: one gated trace per entry, re-ask, drops, ordering and ids

**Calling the model.**
- `map_paths` now traces every discovered entry, under `bounded(concurrency or DEFAULT_CONCURRENCY)`. `concurrency < 1` raises `ValueError`.
- Each entry gets one `ask(..., schema=TraceAnswer)`. `allowed_tools` is forced to exactly `["Read", "Grep", "Glob"]` and `cwd=str(root)`, both layered over `call_kwargs`.
- Answers are kept in an in-memory dict keyed by `canonical_digest`. The digest is `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)` over builtins with str keys only, then SHA-256. The key covers commit, entry `(kind, location, handler)`, sorted compressed changed ranges, sorted edges and places, and `TRACE_PROMPT_VERSION`.

**Changes to `code_facts.explore.ask`.** All are backward compatible, and the existing explore tests stay green:
- a new keyword `schema`, defaulting to `ExploreOutput`;
- `dict`, `str` and `bytes` results are normalised with `model_validate` / `model_validate_json` before `model_dump`;
- a `budget_label` in `call_kwargs` overrides the default instead of raising `TypeError`;
- a stored error is the first non-empty line, cut to 200 characters, or the exception type name when there is none.

**Validation before any `CallFn` call.**
- `normalise_changed_lines` materialises each iterable once, so generators work.
- It raises `ValueError` for:
  - `str` / `bytes` values, `bool` or non-`int` lines, and lines below 1;
  - absolute, escaping or NUL-containing paths.
- It strips a leading `./` and tolerates missing files.
- Places are validated the same way.

**Prompt and gate.**
- `trace_entry` starts with `TRACE_MARKER`. It gives the kind, location, handler symbol and commit, the changed ranges or the literal "There is no change scope; trace the chain to the entry's last effect.", the fenced edges and places, the cap of 20, `citation_rules()`, `UNTRUSTED_RULES` and `schema_json(TraceAnswer)`. An empty `invocation` is rendered as `(empty)`, because `fence("")` yields nothing.
- `TraceAnswer.hops` is required. A `{}`, a misspelt key or a stored `{"error": …}` is therefore `malformed_answer` or `trace_call_failed`, never `empty_trace`.
- `check_trace` applies rules 1–6 in order:
  1. `path_outside_root`: empty path, `.`, an absolute path (even inside the root), a `..` part, a backslash, NUL, or a leading `//`. Accepted paths are stored normalised.
  2. `missing_file`: `resolve(path, "head")` returns `None`; a bad sha; or `git cat-file -t <sha>:<rel>` is not `blob`, which covers directories and gitlinks. A missing `git` binary raises `FileNotFoundError` instead.
  3. `line_out_of_range`: uses the git line count `text.count("\n") + (1 if text and not text.endswith("\n") else 0)`, so a zero-byte file has 0 lines.
  4. `text_mismatch`: through `check_quote`. Empty or whitespace-only quotes fail here.
  5. `base_side_citation`.
  6. `entry_mismatch`: hop 1 only. Hop 1 must match the location file and line, or the handler path and line.
- `hop_limit`: when there are more than 20 hops, the first 20 are kept and checked. If they all pass, the result is `broken_at=21`, published `stops_at=20`, and there is no re-ask.

**Re-ask and drops.**
- Every other break gets exactly one `retrace_entry` call, starting with `RETRACE_MARKER`. It carries the broken index, the reason literal, the detail and the accepted prefix.
- The re-answer replaces the first answer, even when it is worse. A test documents this.
- If the re-answer still breaks at hop 1, the entry is dropped at stage `trace` with reason `malformed_trace`, `empty_trace` or `cut_at_entry`.
- An error type listed in `recoverable` gives a `trace_call_failed` drop.
- Any other error, or a type in `fatal`, propagates unchanged, never as an `ExceptionGroup`, and nothing further is called for that entry.

**Ordering and ids.** Kept paths are sorted by `-len(changed_lines_covered)` (always 0 at this stage), then hop count ascending, then `KIND_RANK`, then path, then line. Ids `p1..pN` are given after sorting. Kept plus dropped equals the number of entries.

**Test helpers and conventions.**
- `tests/fakes.py::FakeCall` keys answers by marker and by the entry's `path:line`. It answers any unmarked prompt with `schema.model_validate({"entries": []})`. It records `calls`, `trace_calls` and `max_in_flight`, and can add random delays or raise chosen errors.
- Fixture hop chains name hop n+1's symbol inside hop n's cited lines, so these tests still pass once Block 4 adds connectivity.
- Scoped tests choose changed lines that the faked hops cover and import-reach. They also pass `model_fallback=False` whenever they assert total call counts, so they still pass once Blocks 11 and 12 land.

**Tests run in this block.**
- One end-to-end run on seed and `__main__` fixtures, including a `repo_b/cli.py` multi-repo root.
- Byte-identical `paths.json` across concurrency 1 and 8 with random delays.
- Byte-identical `paths.json` for entries traced in two different orders with fresh caches.

Run it with `uv run pytest tests/test_trace_slice.py`.

Interfaces:
- Produces: `class AnswerHop(_Model): symbol_called: str; path: str; line_start: int; line_end: int; quote: str; note: str = ""; side: Literal["base", "head"] = "head"`
- Produces: `class TraceAnswer(_Model): hops: list[AnswerHop]`
- Produces: `class GateOutcome(NamedTuple): accepted: list[Hop]; broken_at: int | None; reason: BreakReason | None; detail: str`
- Produces: `def check_trace(hops: Sequence[AnswerHop], *, entry: Entry, edges: Sequence[KnownEdge], places: Sequence[Place], resolve: Resolve, root: Path) -> GateOutcome`
- Produces: `TRACE_PROMPT_VERSION: str = "trace.v1"`, `TRACE_MARKER: str = "<!-- code_facts:trace_entry -->"`, `RETRACE_MARKER: str = "<!-- code_facts:retrace_entry -->"`
- Produces: `def trace_entry(*, entry: Entry, commit: str, changed: Mapping[str, Sequence[int]] | None, edges: Sequence[KnownEdge], places: Sequence[Place]) -> str`
- Produces: `def retrace_entry(*, entry: Entry, commit: str, changed: Mapping[str, Sequence[int]] | None, edges: Sequence[KnownEdge], places: Sequence[Place], broken_at: int, reason: BreakReason, detail: str, accepted: Sequence[Hop]) -> str`
- Produces: `async def ask(key: str, prompt: str, call: CallFn, answers: MutableMapping[str, dict], *, call_kwargs: dict[str, Any], errors: list[str], recoverable: tuple[type[BaseException], ...] = (Exception,), fatal: tuple[type[BaseException], ...] = (), progress: Callable[[str], None] | None = None, schema: type[BaseModel] = ExploreOutput) -> None`
- Produces: `def canonical_digest(obj: Any) -> str`
- Produces: `def normalise_changed_lines(root: Path, changed_lines: Mapping[str, Iterable[int]]) -> dict[str, list[int]]`
- Produces: `def normalise_places(root: Path, places: Sequence[Place]) -> list[Place]`
- Produces: `DEFAULT_CONCURRENCY: int = 4`
- Produces: `class FakeCall` with `async def __call__(self, prompt: str, schema: type[BaseModel], **kwargs: Any) -> Any` and attributes `calls: list[tuple[str, type[BaseModel], dict]]`, `trace_calls: int`, `max_in_flight: int` (in `tests/fakes.py`)
- Produces: `def prefix_resolver(root: Path) -> Resolve` (in `tests/repo_fixture.py`)
- Consumes: Block 1: `class Hop(_Strict): symbol_called: str; path: str; line_start: int; line_end: int; quote: str; note: str = ""; side: Literal["base", "head"] = "head"`
- Consumes: Block 1: `class TracedPath(_Strict): id: str; entry: Entry; hops: list[Hop]; stops_at: int | None; break_reason: PublishedBreakReason | None; changed_lines_covered: list[Location]; tests: E2EListing`
- Consumes: Block 1: `class DropRecord(_Strict): stage: DropStage; entry: Entry; reason: DropReason`
- Consumes: Block 1: `class Place(_Model): path: str; line: int`
- Consumes: Block 2: `def discover_entries(root: Path, *, seeds: Sequence[Seed] = (), index: PyIndex | None = None) -> Discovery`
- Consumes: Block 2: `def build_repo(dest: Path, tree: Mapping[str, str | bytes], *, git: bool = True) -> Path`

Satisfies:
- path-tracing:
  - One trace agent per traced entry: One request per surviving entry
  - Trace agents are read-only: both
  - Model calls go only through the caller's CallFn: both
  - Trace prompt carries the entry, the scope and known edges: No changed lines given
  - An answer is one linear chain of hops: all three
  - Hops cite head-side code only: both
  - Hop limit of 20: both
  - Empty or malformed answers are dropped, not fatal: both
  - Every answer passes through the trace gate
  - Bounded concurrency: both
  - Snapshot-relative locations: both
  - Deterministic trace output
- trace-gate:
  - Gate is deterministic and makes no model call: both
  - Gate does not import the walkthrough pipeline
  - Break reasons come from a fixed set
  - Malformed or empty answers break at hop 1: both
  - Hop paths stay inside the snapshot root: all three
  - Every hop citation re-reads at the commit: all four
  - Citations are head-side only: both
  - Hop 1 is anchored at the entry: all three
  - Traces are capped at 20 hops: both
  - The first broken hop decides the outcome: Later hops ignored after a break
  - A broken trace gets exactly one re-ask: all four
  - An unrepaired trace is cut at the break: all three
  - Re-asks use the resumable answer cache: Order independence
  - CallFn failures during a re-ask propagate
- paths-output-contract:
  - Locations are relative to the snapshot root: Multi-repository snapshot
  - Cut paths carry stop and reason together: Complete path; Cut path
  - Ids are contiguous and follow array order: both
  - Changed-line coverage field: Unscoped coverage empty
  - Byte-stable serialisation: Concurrency does not affect output
- path-scope-and-cap:
  - Changed-line paths are validated and normalised: Absolute path rejected; Path escaping the snapshot rejected; Non-positive line rejected; Equivalent spellings normalise to one file
  - Deterministic path ordering: Tie broken by kind then location; Independent of completion order

## 4. Gate connectivity: name references, known edges, caller places and repeated hops

`check_trace` gains rule 7, `unconnected_hop`, and rule 8, `repeated_hop`. They run after rules 1–6, for each hop n+1 where n ≥ 1.

Hop n+1 is connected if any of these holds:
- **(a) Name reference.** One of the name candidates of `symbol_called` is a `NAME` token inside hop n's cited line range. The candidates are: the full dotted text with call arguments stripped; each dotted suffix; and the last segment, with `.delay`, `.apply_async`, `.s`, `.si` and `.signature` removed.
  - For `.py` files, the whole file at the commit is tokenized once with `tokenize.generate_tokens`, and tokens are filtered by `tok.start[0]`. That avoids fragment problems such as dedents, ranges that start inside docstrings, and stray `IndentationError`.
  - If the whole file raises `TokenError` or any `SyntaxError`, including `IndentationError` and `TabError`, the cited lines are matched by regex instead, with quoted spans and `#` comments stripped first.
  - Names inside f-string replacement fields count on Python 3.12+. Names only in comments, plain strings or docstrings do not.
  - Non-`.py` files never connect by name.
  - An empty `symbol_called` can only connect through (b) or (c).
- **(b) Known edge.** An edge in the `edges` argument runs from a line inside hop n's range to a line inside hop n+1's range. `map_paths` passes `entry.known_edges`, which already includes the edges of merged invokers.
- **(c) Caller place.** Hop n+1's range contains a place from `places`. A place never excuses a failed citation.

Rule 8 breaks a hop whose normalised `(path, line_start, line_end)` equals that of any earlier hop. For example, A→B→A breaks at hop 3. Ranges that overlap but are not identical are accepted.

The rule-order test needs two cases. A hop that is both out of range and unconnected reports `line_out_of_range`. As a control, the same hop with an in-range line reports `unconnected_hop`. Re-asks triggered by `unconnected_hop` and `repeated_hop` reuse the Block 3 machinery.

Run it with `uv run pytest tests/test_gate_connectivity.py`. The tests call `check_trace` directly and also go through `map_paths` with places.

Interfaces:
- Produces: `def check_trace(hops: Sequence[AnswerHop], *, entry: Entry, edges: Sequence[KnownEdge], places: Sequence[Place], resolve: Resolve, root: Path) -> GateOutcome` (now applies rules 7 and 8)
- Consumes: Block 3: `def check_trace(hops: Sequence[AnswerHop], *, entry: Entry, edges: Sequence[KnownEdge], places: Sequence[Place], resolve: Resolve, root: Path) -> GateOutcome`
- Consumes: Block 3: `class FakeCall` with `async def __call__(self, prompt: str, schema: type[BaseModel], **kwargs: Any) -> Any`
- Consumes: Block 1: `class KnownEdge(_Strict): from_: Location = Field(alias="from"); to: Location`

Satisfies:
- trace-gate:
  - Consecutive hops are connected by a reference or a known edge: all three scenarios
  - Caller-supplied places are accepted as connections: both
  - Repeated hops break the path
  - The first broken hop decides the outcome: Multiple failures on one hop

## 5. argparse subcommands: entries, handlers, dispatch edges and CLI targets

This block adds the argparse scanner to `entries.py`.

**Which `add_parser` calls it recognises.**
- Calls on names bound to `add_subparsers(...)` in the same function or module scope.
- Chained calls of the form `parser.add_subparsers(...).add_parser(...)`.
- One level of helper registration, where a function parameter receives a bound subparsers object at a call site in the same repository.

**Naming.**
- Nested names are joined with single spaces.
- A name counts as literal when it is an `ast.Constant` str (which includes folded implicit concatenation), a `JoinedStr` made only of constants, or a `name=` keyword.
- Any other name, a missing name, or `""` gives `dynamic_name` and no entry.
- Duplicate literal names give two entries. A test pins this.

**Handlers.**
- Resolved only from `set_defaults(func=Name)`, `set_defaults(func=mod.attr)`, or a dispatch dict literal in the same module as the `add_parser` call. The target is followed through same-repo imports.
- `lambda`, `partial`, other keys, third-party targets and dicts in other modules give `handler=None` plus `unresolved_handler`.
- `args.command == "x"` comparisons are ignored.

**Edges.** A known edge runs from the `args.func(...)` or `HANDLERS[...](...)` call line in that module to the `def` line. When no such call exists, it runs from the `set_defaults` line or the dict entry line instead.

**Two `pr_pipeline` fixtures.** Both yield `open`, `edit` and `render` with `handler=None` at their `add_parser` lines:
- the spec shape, which dispatches on `args.command`;
- the live shape, with a `COMMANDS` dict in a sibling `commands.py`.

**End-to-end tests.**
- A `FakeCall` answers hop 1 at an `add_parser` line and hop 2 at `cmd_open`. With no place, the result is two calls and a path with `stops_at=1` and `unconnected_hop`. With a place at `cmd_open`, the path is accepted.
- `write_paths_file` output keeps kind `argparse_subcommand` and the `add_parser` line.
- The trace prompt for a resolved subcommand contains the dispatch edge.

Run it with `uv run pytest tests/test_argparse.py`.

Interfaces:
- Produces: `class SymbolRef(NamedTuple): module: str; path: str; symbol: str`
- Produces: `class CliTargets(NamedTuple): programs: list[SymbolRef]; apps: list[SymbolRef]; tokens: tuple[str, ...]; aliases: tuple[str, ...]`
- Produces: `def cli_targets(entry: Entry, index: PyIndex) -> CliTargets | None` (for `argparse_subcommand`:
  - `programs` holds the function that builds the parser and calls `add_subparsers`, plus a module-level `main` that calls it;
  - `apps=[]`;
  - `tokens` is `entry.name.split(" ")`;
  - `aliases` are the literal `aliases=` values;
  - other kinds return `None`)
- Produces: `def find_subcommand(entries: Sequence[Entry], package_dir: str, token: str) -> Entry | None` (returns the `argparse_subcommand` entry whose `location.path` starts with `package_dir + "/"` and whose `name` or alias equals `token`; ties go to the smallest `(location.path, location.line)`)
- Consumes: Block 2: `class PyIndex` with `def resolve_symbol(self, rel: str, name: str) -> HandlerRef | None` and `def parse(self, rel: str) -> ast.Module | None`
- Consumes: Block 2: `def discover_entries(root: Path, *, seeds: Sequence[Seed] = (), index: PyIndex | None = None) -> Discovery`
- Consumes: Block 3: `def trace_entry(*, entry: Entry, commit: str, changed: Mapping[str, Sequence[int]] | None, edges: Sequence[KnownEdge], places: Sequence[Place]) -> str`
- Consumes: Block 4: `def check_trace(hops: Sequence[AnswerHop], *, entry: Entry, edges: Sequence[KnownEdge], places: Sequence[Place], resolve: Resolve, root: Path) -> GateOutcome`

Satisfies:
- entry-point-discovery:
  - argparse subcommands are discovered: all three scenarios
  - argparse handlers resolve through set_defaults and dispatch dicts only: all four
  - argparse dispatch lines become known edges: both
  - Entries resolving to the same handler are merged: Unresolved handlers not merged
- path-tracing: Trace prompt carries the entry, the scope and known edges: Prompt contents
- paths-output-contract: Entry object shape: Known discovery case keeps the add_parser line
- trace-gate: Discovery edges are accepted as connections: `args.command` dispatch

## 6. click and typer commands

This block adds the click/typer scanner. A decorator only counts when its receiver can be traced back to one of:
- a `click.group`-decorated function;
- `click.Group(...)` or `typer.Typer(...)`, including the aliases `import click as c`, `from click import command, group` and `from typer import Typer`.

So discord.py `@bot.command()`, Dash `@app.callback(...)` and Slack `@app.command("/x")` produce no entries.

**Accepted forms.**
- The bare `@cli.command` and `@cli.group` forms count for click (8.1+).
- A bare `@app.command` counts for typer and registers nothing.
- `async def` is recognised.

**Naming.**
- A literal name given positionally or as `name=` is used verbatim. `""` falls back to the derived name.
- Click derived names use `f.__name__.lower().replace("_", "-")`, then strip a trailing `-command`, `-cmd`, `-group` or `-grp` (Click ≥ 8.2). So `sync_cmd` becomes `sync`, and `Deploy_All` becomes `deploy-all`.
- Typer derived names lowercase and replace `_` with `-` but strip nothing.
- Entry names are prefixed by the parent group names, including the root group's own name, as the spec expects for `cli build-all`.
- Typer group names come from `add_typer(name=)`, then `Typer(name=)`. A sub-app with neither has its commands merged into the parent (Typer ≥ 0.15).
- A name that is not a literal gives `dynamic_name`.
- Duplicate names under one group give two entries. A test pins this.

**Location and edges.** The location is the decorator line. The handler is the `def` line. The known edge runs from the decorator line to the `def` line.

**`cli_targets` for click/typer entries.** It returns:
- `apps=[<root group/app SymbolRef>]`;
- `tokens` = the name path after the root token;
- `tokens=()` for a single-command typer app with no callback, because Typer omits the token there;
- `programs=[]`.

**Tests.** The sentinel fixture's click module, which writes a file and raises at import, still yields its entries. A caller seed on a `click_command` handler is folded into that entry's `also_invoked_by`.

Run it with `uv run pytest tests/test_click_typer.py`.

Interfaces:
- Produces: `def cli_targets(entry: Entry, index: PyIndex) -> CliTargets | None` (now also returns `CliTargets` for `click_command` and `typer_command`, as described above)
- Consumes: Block 5: `def cli_targets(entry: Entry, index: PyIndex) -> CliTargets | None`
- Consumes: Block 5: `class CliTargets(NamedTuple): programs: list[SymbolRef]; apps: list[SymbolRef]; tokens: tuple[str, ...]; aliases: tuple[str, ...]`
- Consumes: Block 2: `def discover_entries(root: Path, *, seeds: Sequence[Seed] = (), index: PyIndex | None = None) -> Discovery`

Satisfies:
- entry-point-discovery:
  - click and typer commands are discovered: all three scenarios
  - Discovery never imports or runs target code: the click-module half of "Module with import-time side effects"
  - Entries resolving to the same handler are merged: Seed duplicating a discovered entry

## 7. `[project.scripts]` entries and multi-repository resolution

This block adds the `[project.scripts]` scanner.

**Parsing errors.** Each `pyproject.toml` is read with `tomllib.load` on a binary file. Each of these gives one `parse_error` with `line=None` and leaves other files unaffected:
- `TOMLDecodeError`, including duplicate keys;
- `UnicodeDecodeError`;
- `RecursionError`.

**Finding each key's line.** A line scan finds each key in these places:
- under `[project.scripts]`, including the spaced header `[ project . scripts ]` and the quoted header `[project."scripts"]`;
- quoted keys;
- dotted `scripts.<key> =` lines under `[project]`;
- an inline `scripts = {…}`, which gives the `scripts =` line.

The scan skips comments. When no line can be found, the entry uses the `[project]` header line.

**Values.**
- Extras suffixes and spaces around the colon are stripped, so `pkg.mod : main [x]` behaves like `pkg.mod:main`.
- `module:obj.attr` resolves to the definition of `obj`, and the symbol is `obj.attr`.
- A target assigned at module level, such as `app = typer.Typer()`, resolves to its assignment line.
- A function re-exported through a package `__init__` is followed.
- Non-string values and module-only references give `unresolved_target` at the key line.
- `dynamic = ["scripts"]` gives no entries and no diagnostic. A test pins this.

**Module search.** Modules are looked up under the file's own repository root, then `src/`, then nested roots, and never in another repository. A third-party target gives `unresolved_target`. The known edge runs from the key line to the handler `def` line.

**End-to-end tests.**
- Two repositories each declaring a script, with `run = "tool.cli:main"` resolving only inside `repo_a`.
- A script and `pkg/__main__.py` sharing `def main` merge into one `main_module` entry.
- A script and an argparse subcommand sharing a handler produce exactly one trace request. The written path has exactly one `also_invoked_by` record, for the `project_script` invoker.
- A hop at the script target line is accepted through the script known edge.

Run it with `uv run pytest tests/test_project_scripts.py`.

Interfaces:
- Produces: (none)
- Consumes: Block 2: `class PyIndex` with `def repo_root(self, rel: str) -> str`, `def resolve_module(self, dotted: str, *, from_file: str, extra_roots: Sequence[str] = ()) -> str | None` and `def resolve_symbol(self, rel: str, name: str) -> HandlerRef | None`
- Consumes: Block 2: `def discover_entries(root: Path, *, seeds: Sequence[Seed] = (), index: PyIndex | None = None) -> Discovery`
- Consumes: Block 1: `def write_paths_file(result: PathsResult, path: Path) -> None`
- Consumes: Block 3: `class FakeCall` with `async def __call__(self, prompt: str, schema: type[BaseModel], **kwargs: Any) -> Any`

Satisfies:
- entry-point-discovery:
  - [project.scripts] entries are discovered: all three scenarios
  - Paths are relative to the snapshot root, including multi-repository snapshots: both
  - Entries resolving to the same handler are merged: Script and __main__ resolve to the same main()
- paths-output-contract: Entry object shape: Deduplicated invokers recorded
- path-tracing: One trace agent per traced entry: De-duplicated entries share a trace
- trace-gate: Discovery edges are accepted as connections: Script target edge

## 8. `python -m` commands in `SKILL.md`

This block adds the `SKILL.md` scanner and the shared command parser.

**Fenced blocks.** CommonMark fences are parsed: either backticks or tildes, at least 3 characters, indented up to 3 spaces. The info string's first word must match exactly one of `bash`, `sh`, `shell`, `zsh` or `console`. A closing fence must use the same character and be at least as long. An unclosed fence runs to end of file.

**Command lines.**
- Lines ending in `\` are joined.
- In `console` blocks only lines starting with `$ ` are commands, and `> ` continuation prompts are stripped.
- `parse_command_line` tokenizes with `shlex.shlex(posix=True, punctuation_chars=True)` and splits on `;`, `&&`, `||` and `|`, so a glued `cd x&&python3` is handled.
- An unbalanced quote raises `ValueError`, which is recorded as `parse_error` at that line.

**Resolving the target.** `resolve_command` works in this order:
1. Peels env assignments, `env`, and `uv run` with its value-taking options (`--directory`, `--project`, `--with`, `--python`/`-p`, `--package`).
2. Accepts `python`, `python3` or `python3.X` with `-I`, `-E` (both ignore `PYTHONPATH`), `-P` (drops the cwd root) and `-u`.
3. Matches `-m <mod>` and `-m<mod>`. The first positional is the first later token that does not start with `-` or `#`.
4. Expands `${CLAUDE_SKILL_DIR}`, and `${CLAUDE_PLUGIN_ROOT}` to the nearest ancestor holding `.claude-plugin/plugin.json`. Other `$VAR`, `$(…)` and `~` stay literal.
5. Searches `PYTHONPATH` entries left to right (empty segments skipped), then the `cd` / `--directory` directory, then the `SKILL.md` directory, then `scripts/`, then the repository root, then `<repo>/src`.

**Entries and edges.**
- Each matched command becomes a `skill_command` entry named `<skill dir>: <module> [<first positional>]`, with `invocation` set to the joined command.
- The known edge points to:
  1. `find_subcommand`'s handler;
  2. otherwise its `add_parser` line;
  3. otherwise line 1 of `__main__.py`, which also covers placeholders like `<command>`.
- An unresolvable module gives `unresolved_target`. Python fences, unlabelled fences and prose give nothing.

Run it with `uv run pytest tests/test_skill_commands.py`.

Interfaces:
- Produces: `class CommandTarget(NamedTuple): module: str | None; script: str | None; path: str; first_positional: str | None`
- Produces: `def parse_command_line(text: str) -> list[list[str]]`
- Produces: `def resolve_command(segments: list[list[str]], *, index: PyIndex, source_file: str, default_roots: Sequence[str], variables: Mapping[str, str]) -> CommandTarget | None`
- Produces: `def command_edge(target: CommandTarget, entries: Sequence[Entry]) -> Location`
- Consumes: Block 5: `def find_subcommand(entries: Sequence[Entry], package_dir: str, token: str) -> Entry | None`
- Consumes: Block 2: `class PyIndex` with `def resolve_module(self, dotted: str, *, from_file: str, extra_roots: Sequence[str] = ()) -> str | None` and `def repo_root(self, rel: str) -> str`
- Consumes: Block 2: `def discover_entries(root: Path, *, seeds: Sequence[Seed] = (), index: PyIndex | None = None) -> Discovery`

Satisfies:
- entry-point-discovery: python -m commands in SKILL.md fenced shell blocks are discovered: all six scenarios

## 9. Hook commands, and discovery across all entry kinds

This block adds the hook scanner for `hooks.json` and `.claude/settings*.json`.

**Reading and walking files.**
- Files are decoded as `utf-8-sig`, so a BOM is tolerated, and parsed with `json.loads`.
- `JSONDecodeError`, `RecursionError` and `UnicodeDecodeError` each give a `parse_error` with `line=None`.
- A missing `"hooks"` wrapper, `"hooks": null`, or a list where an object is expected gives `parse_error` with a shape detail, never a crash.
- The scanner walks `hooks.<event>[].{matcher, hooks[]}` and keeps only `"type": "command"`. `statusLine` is ignored.

**Lines and tokens.**
- A command's line is found by matching `"command"\s*:\s*<encoded>` in the raw text. Both `json.dumps(cmd)` and `json.dumps(cmd, ensure_ascii=False)` encodings are tried, and repeated commands are assigned in document order. If neither encoding is found, the line falls back to the `"hooks"` key line.
- Tokens are `[command, *args]` in exec form. In shell form they come from Block 8's `parse_command_line`, so commands like `mkdir …; bash script.sh || true` resolve per segment.

**Variables and targets.**
- `${CLAUDE_PLUGIN_ROOT}` expands to the nearest ancestor holding `.claude-plugin/plugin.json`, or else the parent of the hooks file's directory.
- `$CLAUDE_PROJECT_DIR` and `${CLAUDE_PROJECT_DIR}` expand to the repository root.
- `python -m` targets use `resolve_command`.
- Python script targets get `main_module` handler rules.
- Other existing files, including the script argument of `bash` or `sh`, resolve to line 1.
- Missing targets give `unresolved_target` at the command line.
- Entries are named `<event>:<matcher>` or `<event>`, with `invocation` set to the command string. The known edge runs from the command line to the target.

**Fixtures closing out discovery.**
- A hook and a `SKILL.md` command running `python -m pkg` merge into one entry with the other recorded in `also_invoked_by`.
- A mixed-failure fixture gives exactly five diagnostics: two `parse_error`, one `unresolved_target`, one `dynamic_name`, one `seed_not_found`. Every valid entry is still returned.
- An every-kind fixture checks for all entries:
  - all seven fields are present;
  - every path is POSIX, relative and has no `..`;
  - every line is between 1 and the file's line count.
- A hop at a hook target is accepted through the hook known edge.

Run it with `uv run pytest tests/test_hooks.py`.

Interfaces:
- Produces: (none)
- Consumes: Block 8: `def parse_command_line(text: str) -> list[list[str]]`
- Consumes: Block 8: `def resolve_command(segments: list[list[str]], *, index: PyIndex, source_file: str, default_roots: Sequence[str], variables: Mapping[str, str]) -> CommandTarget | None`
- Consumes: Block 8: `def command_edge(target: CommandTarget, entries: Sequence[Entry]) -> Location`
- Consumes: Block 2: `def discover_entries(root: Path, *, seeds: Sequence[Seed] = (), index: PyIndex | None = None) -> Discovery`
- Consumes: Block 4: `def check_trace(hops: Sequence[AnswerHop], *, entry: Entry, edges: Sequence[KnownEdge], places: Sequence[Place], resolve: Resolve, root: Path) -> GateOutcome`

Satisfies:
- entry-point-discovery:
  - Hook commands are discovered: all five scenarios
  - Entries resolving to the same handler are merged: Hook and SKILL.md invoke the same module
  - Every skipped candidate is explained: Mixed failures in one fixture
  - Entry record shape: Fields present and well-formed
- trace-gate: Discovery edges are accepted as connections: Hook command edge

## 10. Persistent answer cache and resumable runs

**`DirAnswers`.** Passing `cache_dir` backs answers with `DirAnswers`, which stores one file per key: `cache_dir/<sha256(key)>.json` containing `{"key": key, "answer": answer}`.
- The directory is created with `mkdir(parents=True, exist_ok=True)`. `Path("")` and a `cache_dir` that is a file raise `ValueError`.
- A probe write at the start of `map_paths` means a read-only directory raises `OSError` before the first paid call.
- `__setitem__` writes a `.`-prefixed `.tmp` tempfile in the same directory, then `fsync`, then `os.replace`.
- `__getitem__` treats a missing, zero-byte, truncated or invalid-JSON file, or a key mismatch, as `KeyError`. The entry is asked again rather than aborting the run.
- `__iter__` lists only `*.json` files and never stray `.tmp` files.

**Run behaviour.**
- At the start of a run, every `{"error": …}` entry under this run's keys is deleted, so recoverable failures are asked again on the next run.
- Output is rebuilt by looking up this run's own computed keys in canonical order, never by iterating the directory. Answers from other commits in a shared directory are ignored.
- With `cache_dir=None`, nothing is written anywhere, including the working directory.

**Tests.**
- A drift guard pins `TRACE_PROMPT_VERSION` together with a SHA-256 of `trace_entry` and `retrace_entry` rendered for a fixed entry. Editing any prompt rule fails the test until the version is bumped.
- Bumping the version re-asks every entry.
- Reordered seeds, changed lines and places give zero calls and byte-identical output.
- Adding a changed line re-asks the entries whose prompts include that file.
- A run with re-asks, repeated with the same cache, makes zero calls.
- A run over 5 entries interrupted after 3 answers makes exactly 2 calls on rerun, and its output equals an uninterrupted run.
- Two runs from a shared cache write byte-identical `paths.json`, with ids stable.

Run it with `uv run pytest tests/test_answer_cache.py`.

Interfaces:
- Produces: `class DirAnswers(MutableMapping[str, dict])` with `def __init__(self, directory: Path) -> None`
- Consumes: Block 3: `def canonical_digest(obj: Any) -> str`
- Consumes: Block 3: `async def ask(key: str, prompt: str, call: CallFn, answers: MutableMapping[str, dict], *, call_kwargs: dict[str, Any], errors: list[str], recoverable: tuple[type[BaseException], ...] = (Exception,), fatal: tuple[type[BaseException], ...] = (), progress: Callable[[str], None] | None = None, schema: type[BaseModel] = ExploreOutput) -> None`
- Consumes: Block 3: `TRACE_PROMPT_VERSION: str = "trace.v1"`
- Consumes: Block 3: `def trace_entry(*, entry: Entry, commit: str, changed: Mapping[str, Sequence[int]] | None, edges: Sequence[KnownEdge], places: Sequence[Place]) -> str`
- Consumes: Block 3: `def retrace_entry(*, entry: Entry, commit: str, changed: Mapping[str, Sequence[int]] | None, edges: Sequence[KnownEdge], places: Sequence[Place], broken_at: int, reason: BreakReason, detail: str, accepted: Sequence[Hop]) -> str`

Satisfies:
- path-tracing:
  - Answer cache keyed independently of order: all three scenarios
  - Resumable runs: Resume after interruption
- trace-gate: Re-asks use the resumable answer cache: Resume makes no calls
- paths-output-contract: Byte-stable serialisation: Reproducible run
- path-scope-and-cap: Path ids assigned after ordering and cap: Stable ids on rerun

## 11. Change scope: import reachability, trace limit, coverage filter and cap

This block applies only when `changed_lines is not None`.

**Import graph.** `PyIndex.import_distances` builds a static import graph from `Import` and `ImportFrom`:
- `level` and `module=None` are handled, and `from a import b` resolves to the submodule when one exists, otherwise to `a/__init__.py`.
- `import a.b.c` also adds edges to the parent `__init__.py` files.
- A star import adds an edge to its module.
- Unparseable modules have no out-edges, and unresolved or third-party imports add none.
- It runs a reverse BFS from the changed `.py` files and terminates on cycles.

**Distances and drops.**
- An entry's distance is 0 when its location file changed. Otherwise it is the distance of its handler module, or of its location module when the location is `.py`.
- Entries with no distance are dropped as `import_reachability` / `unreachable` before any call. That covers missing files, non-`.py` changes that only affect other files, and dynamic imports.
- `changed_lines={}` drops every entry this way and makes zero calls.

**Trace limit.** Reachable entries are ordered by `(distance, KIND_RANK, path, line)`. Entries beyond `trace_limit` (default `4 * cap`) are dropped as `trace_limit` / `over_trace_limit`. A scoped `cap < 1` or `trace_limit < 1`, or a `bool` value, raises `ValueError` before any call. When unscoped, both are ignored, and `trace_limit=0` traces everything; a test pins this.

**Coverage.**
- Only retained hops count, so hops after a cut and hops past 20 never count.
- Normalised hop paths are compared with the changed-line keys, inclusive at both ends.
- `changed_lines_covered` is sorted and de-duplicated.
- A path covering nothing is dropped as `no_changed_line` / `no_changed_line_covered`.

**Ordering, cap and ids.** Paths are ordered with real coverage counts, then capped; paths beyond the cap are dropped as `cap` / `over_cap`. Ids `p1..pN` are given after the cap. Drops are sorted by stage order, then `(location.path, location.line, kind)`. Kept plus all drops equals E.

**Tests also cover:**
- missing-file tolerance;
- the multi-repo `repo_b/src/mod.py` case;
- tie-breaking by hop count when coverage is equal;
- schema validation of all four scope drop reasons.

Run it with `uv run pytest tests/test_scope.py`.

Interfaces:
- Produces: `def import_distances(self, changed: Iterable[str]) -> dict[str, int]` (a method on `PyIndex`; keys are root-relative `.py` paths, values are BFS distances to a changed file)
- Consumes: Block 2: `def build_index(root: Path) -> PyIndex`
- Consumes: Block 3: `def normalise_changed_lines(root: Path, changed_lines: Mapping[str, Iterable[int]]) -> dict[str, list[int]]`
- Consumes: Block 1: `class DropRecord(_Strict): stage: DropStage; entry: Entry; reason: DropReason`
- Consumes: Block 1: `KIND_RANK: Mapping[EntryKind, int]`

Satisfies:
- path-scope-and-cap:
  - Scoping is active only when changed lines are supplied: both scenarios
  - Changed-line paths are validated and normalised: Missing file tolerated; Multi-repository snapshot paths
  - Static import-graph reachability pre-filter: all seven
  - Trace limit bounds the number of entries traced: all three
  - Post-trace coverage filter: all four
  - Deterministic path ordering: More coverage ranks first; Duplicate coverage counted once; Tie broken by hop count
  - Cap limits kept paths: all three
  - Path ids assigned after ordering and cap: Contiguous ids
  - Every scope drop records a reason: all three
- path-tracing: One trace agent per traced entry: Pre-filtered entries are not traced
- paths-output-contract:
  - Changed-line coverage field: Coverage listed
  - Every drop is recorded with a reason: Cap drop recorded

## 12. Model fallback for entry points discovery missed

**The call.** This replaces Block 1's interim `"not_implemented"` reason with a real fallback. At most one `ask(..., schema=FallbackAnswer)` is made per run:
- The prompt starts with `FALLBACK_MARKER`, lists the sorted deterministic entries, names the scope, and asks only for new entry points.
- Tools are exactly `Read`, `Grep` and `Glob`.
- The cache key is `fallback:` plus `canonical_digest` of the sorted entries `(kind, name, location)`, the sorted normalised scope, the commit and `TRACE_PROMPT_VERSION`.
- `scope_paths` is validated like changed lines. A bare `str` or an absolute or escaping path raises `ValueError` before any call.
- `model_fallback=False` gives `skipped`/`"disabled"`. An empty scope gives `skipped`/`"empty_scope"`.

**Answers and status.**
- `FallbackAnswer.entries` is a list of raw dicts. Each is validated on its own as `FallbackEntry`, so one bad citation, such as `line_end < line_start` or `line_start: 0`, costs only that entry.
- An answer that fails as a whole gives `failed`, with a reason of at most 200 characters: the first error's location and message, `include_url=False`.
- A `recoverable` error gives `failed`. A non-recoverable or `fatal` error propagates after exactly one attempt.

**Post-processing, in order:**
1. A missing citation gives `missing_citation`.
2. These give `citation_unverified`: a raw path that is absolute (even inside the root), contains `..` or does not resolve; `side == "base"`; a `line_end` past the end of the file by git line count; or a failed `check_citation`.
3. A non-string or unknown `kind` becomes `other`.
4. Entries citing the same `(path, line_start)` are collapsed, keeping the first by `(path, line, kind, name)`.
5. An entry is dropped as `duplicate` when its `(path, line)` equals a deterministic or seed location or handler line, or when its enclosing `def` is a deterministic handler.

**Survivors.** Survivors are sorted by location, get `source="model"`, and then go through reachability, trace limit, tracing, coverage and cap like any other entry.

**Tests.**
- Fallback drop records are present.
- Two stubs listing the same entries in different orders write byte-identical `paths.json`.
- A rerun with the same `cache_dir` makes zero fallback calls.

Run it with `uv run pytest tests/test_fallback.py`.

Interfaces:
- Produces: `FALLBACK_MARKER: str = "<!-- code_facts:entry_fallback -->"`
- Produces: `class FallbackEntry(_Model): kind: str; name: str; citation: Citation | None = None; reason: str = ""`
- Produces: `class FallbackAnswer(_Model): entries: list[dict[str, Any]]`
- Produces: `def entry_fallback(*, entries: Sequence[Entry], scope: Sequence[str], commit: str) -> str`
- Consumes: Block 3: `async def ask(key: str, prompt: str, call: CallFn, answers: MutableMapping[str, dict], *, call_kwargs: dict[str, Any], errors: list[str], recoverable: tuple[type[BaseException], ...] = (Exception,), fatal: tuple[type[BaseException], ...] = (), progress: Callable[[str], None] | None = None, schema: type[BaseModel] = ExploreOutput) -> None`
- Consumes: Block 3: `def canonical_digest(obj: Any) -> str`
- Consumes: Block 3: `def normalise_changed_lines(root: Path, changed_lines: Mapping[str, Iterable[int]]) -> dict[str, list[int]]`
- Consumes: Block 11: `def import_distances(self, changed: Iterable[str]) -> dict[str, int]`
- Consumes: Block 10: `class DirAnswers(MutableMapping[str, dict])` with `def __init__(self, directory: Path) -> None`

Satisfies:
- entry-point-model-fallback:
  - Single fallback agent per run: all three scenarios
  - Fallback agent is read-only
  - Fallback response shape: all three
  - Malformed fallback answer does not fail the run: both
  - CallFn errors are not retried
  - Citation must re-read at the commit: all five
  - Paths are relative to the snapshot root
  - Fallback entries do not duplicate deterministic entries: both
  - Fallback entries go through the same scope filter
  - Fallback is deterministic and resumable: both
- paths-output-contract: Every drop is recorded with a reason: Uncited model entry recorded

## 13. Static end-to-end test listing per path

This block adds `code_facts/e2e.py`. `map_paths` now fills each path's `tests` and `PathsResult.test_listing_skips`.

**Finding test files.**
- Files named `test_*.py` and `*_test.py` are found under the root, skipping `.git`, `.venv`, `venv`, `node_modules`, `__pycache__` and `site-packages`. `build` and `dist` are walked.
- Each file is read as bytes and decoded strictly as `utf-8-sig`. A decode failure gives `decode_error`.
- These failures from `ast.parse` give `syntax_error`: `SyntaxError` (including NUL bytes), `ValueError`, `RecursionError`, `MemoryError`.
- A zero-byte file gives no tests and no skip.
- Tests are module-level `test*` functions, including `async def`, and `test*` methods of module-level `Test*` classes that have no `__init__` / `__new__` and do not set `__test__ = False`.
- Node ids are `<root-relative file>::[Class::]name`.

**Resolving names.** Names resolve through module-level imports and imports inside the test body. The extra roots are the nearest `pyproject.toml` ancestor, the parent of `tests/`, and the test file's own directory. Re-exports through a package `__init__` are followed. A name rebound locally (`main = Mock()`) never matches.

**Matchers.** Each matcher is checked against the entry and against every `also_invoked_by` invoker:
- `direct_call`
- `cli_argv`:
  - the callee is a program or owning function from `cli_targets`, or `X().parse_args` on one;
  - the first argument is a literal list or tuple;
  - only its string constants are compared, against the tokens as an ordered subsequence;
  - an empty argv never matches a subcommand entry.
- `cli_runner`:
  - `.invoke(<app>, args)` on a receiver that is any of: a `CliRunner()` call from `click.testing` or `typer.testing`; a name bound to one; a fixture parameter returning one; or pytest-click's `cli_runner`;
  - `args` may be a literal list, tuple or string (string split with `shlex`), given positionally or as `args=`;
  - `tokens=()` matches single-command typer apps.
- `subprocess`: `run`, `call`, `check_call`, `check_output` and `Popen` with a literal list or string; interpreters `sys.executable`, `python` and `python3` are normalised; matches `-m <mod> [sub]`, a script name, or the hook command.
- `runpy`

**Hop-2 patches.** These move a test to `mocks_hop_2`, with `patch_line` set to the patch call's own line:
- a patch on the retained hop 2's symbol, at its defining module or at hop 1's import alias, as:
  - a decorator;
  - a class-level `@patch` on `Test*` classes;
  - a `with` item (each `context_expr` line, tuple items, `AsyncWith`);
  - `.start()` on a patcher bound earlier. A patcher that is never started does not count.
- `patch.object(<resolved>, "<name>")`;
- `monkeypatch.setattr` (two positional arguments with a string first is the dotted form; three positional arguments with a string first is not a patch) on `monkeypatch`, `pytest.MonkeyPatch()` or a `MonkeyPatch.context()` receiver.

Patches count inside the test, and inside fixtures that reach it:
- requested by parameter, `@pytest.mark.usefixtures`, `pytestmark` or truthy literal `autouse`;
- defined with `pytest.fixture`, an aliased `fixture` import, or `name=`;
- coming from the module or from `conftest.py` files up to the root, with the nearest definition winning by name, followed transitively with a visited set.

**Edge rules, output and run command.**
- A path with one retained hop always has empty `mocks_hop_2`.
- Each test is de-duplicated to its earliest `invocation_line`, then by match order `direct_call`, `cli_argv`, `cli_runner`, `subprocess`, `runpy`.
- Lists are sorted by `(file, line, node_id)`, and the result is byte-identical when filesystem order is reversed.
- Run it with `uv run pytest tests/test_e2e_listing.py`. Fixture suites live under `tests/fixtures/paths/` and are kept out of collection.

Interfaces:
- Produces: `def list_e2e_tests(root: Path, paths: Sequence[TracedPath], entries: Sequence[Entry], index: PyIndex) -> tuple[dict[str, E2EListing], list[ListingSkip]]` (keyed by path id)
- Consumes: Block 6: `def cli_targets(entry: Entry, index: PyIndex) -> CliTargets | None` (the click/typer contract: `apps=[<root group/app>]`, `tokens` as the name path after the root token, `()` for single-command typer apps)
- Consumes: Block 5: `class CliTargets(NamedTuple): programs: list[SymbolRef]; apps: list[SymbolRef]; tokens: tuple[str, ...]; aliases: tuple[str, ...]`
- Consumes: Block 8: `def parse_command_line(text: str) -> list[list[str]]`
- Consumes: Block 2: `class PyIndex` with `def resolve_module(self, dotted: str, *, from_file: str, extra_roots: Sequence[str] = ()) -> str | None` and `def resolve_symbol(self, rel: str, name: str) -> HandlerRef | None`
- Consumes: Block 1: `class E2EListing(_Strict): e2e: list[E2ERecord]; mocks_hop_2: list[MockedE2ERecord]`
- Consumes: Block 1: `class ListingSkip(_Strict): file: str; reason: Literal["decode_error", "syntax_error"]`

Satisfies:
- e2e-test-listing:
  - Listing runs statically with no model call and no execution: all three scenarios
  - Test discovery scope: all three
  - Direct handler invocation is recognised: all three
  - CLI subcommand invocation is recognised: all four
  - Subprocess, runpy and script invocation is recognised: all five
  - Alternate invokers count
  - Tests that mock hop 2 are listed separately: all five
  - Hop-2 patches applied by fixtures are recognised: both
  - Listing applies to cut paths
  - Listed test record shape
  - Deterministic ordering and deduplication: all three
  - Empty lists are explicit
  - Unreadable test files are skipped with a reason: both
- paths-output-contract: Test-listing shape: Mocked test listed separately