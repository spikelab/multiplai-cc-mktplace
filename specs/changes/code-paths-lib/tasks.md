## 1. Walking skeleton: the `paths.json` contract, `write_paths_file`, a validating `map_paths`, and the amended specs

This block adds the first runnable `map_paths`.
- **Output models.** The frozen output models live in `plugins/multiplai-dev/lib/code_facts/code_facts/path_models.py`. They subclass `code_facts.models._Model` and use `ConfigDict(extra="forbid", frozen=True, regex_engine="python-re")`.
- **Schema.** A hand-written `code_facts/code_facts/schema/paths.v1.schema.json` is loaded through `importlib.resources.files("code_facts") / "schema" / "paths.v1.schema.json"`.
  - `$schema` equals the value in `facts.v1.schema.json` byte for byte. `$id` is `paths.v1.schema.json`.
  - Every object has `additionalProperties: false`, including the citation `$def`.
  - Each `$def`'s `properties` order equals its model's field order.
  - `schema_version` (`"paths.v1"`, required, no default) and every other top-level field appear in `required`, in this order: `schema_version, commit, scoped, cap, paths, dropped, fallback, diagnostics, test_listing_skips`.
- **`map_paths` skeleton.** It checks every input before any `CallFn` call:
  - a missing `call` raises `TypeError`;
  - `changed_lines` keys are normalised to POSIX form with any leading `./` removed;
  - keys that are absolute, escape the root, or contain `..`, `\`, or NUL raise `ValueError`;
  - values are materialised once into sorted, de-duplicated tuples, so a generator works;
  - a `bool`, non-`int`, or `int < 1` value raises `ValueError`;
  - keys whose line set is empty are dropped, and a key naming a missing file is accepted;
  - when scoped, `cap < 1` or `trace_limit < 1` raises `ValueError`;
  - `call_kwargs` containing `allowed_tools`, `cwd`, or `budget_label` raises `ValueError`.

  On a repository with no entries and `fallback=False` (the only fallback mode until Block 7), it returns `paths=()`, `dropped=()`, `fallback={"status": "skipped", "reason": "disabled"}`, `cap=None` when unscoped, and `scoped` set to `changed_lines is not None`. It writes nothing outside `cache_dir`.
- **Model validators.**
  - `stops_at` and `break_reason` are both null or both set, with `1 ≤ stops_at ≤ len(hops) ≤ 20`.
  - Ids match `^p[1-9][0-9]*$` and are contiguous; an empty `paths` is contiguous.
  - `tests.e2e` and `tests.mocks_hop_2` share no `node_id`.
  - Each drop `reason` is legal for its `stage`, per the D14 table.
  - `scoped=False` forces `cap=None`, and drop stages `import_reachability`, `trace_limit`, `no_changed_line` and `cap` are not allowed.
  - `scoped` is a `StrictBool`, so `"false"` is rejected by both the model and the schema.
  - Paths match `^(?!/)(?!.*(^|/)\.\.?(/|$))[^\\\n]+$` in the model and in the schema. So `"a/b.py\n"`, `/abs/x.py`, `../x.py`, `./a.py` and `a\b.py` are rejected by both. `line ≥ 1` is enforced.
  - Assigning any attribute raises `ValidationError` of type `frozen_instance`. Late fields are set with `model_copy(update=…)`, followed by re-validation.
  - `f"{EntryKind.seed}" == "seed"` holds, and `KIND_RANK` (not alphabetical `sorted(EntryKind)`) defines rank.
- **`write_paths_file`.**
  - It serialises with `json.dumps(result.model_dump(mode="json", by_alias=True), indent=2, ensure_ascii=False) + "\n"`.
  - It validates with `jsonschema.Draft202012Validator`, imported inside the function. It then re-validates through `PathsResult.model_validate`, so instances built with `model_construct` cannot get past the model invariants.
  - On failure it raises `PathsSchemaError` naming an RFC 6901 pointer:
    - each `absolute_path` element is converted with `str()`, and `~` and `/` are escaped as `~0` and `~1`;
    - for `required` and `additionalProperties` errors the offending key is appended, so a missing `dropped` reports `/dropped` and a `model_construct` `schema_version="bad"` reports `/schema_version`;
    - an error under an array (`/paths/0/hops/3/line`) never raises `TypeError`;
    - errors are sorted by `tuple(str(p) for p in absolute_path)`.
  - On failure it leaves any existing target byte-for-byte unchanged and creates no file.
  - Writing goes through `atomic_write_text`:
    - `tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")`, UTF-8, `fsync`, and `chmod` to the existing target's mode or `0o666 & ~umask` (never the `mkstemp` `0600`);
    - then `os.replace`;
    - the temp file is unlinked on any `BaseException`, so a serialiser patched to raise mid-write leaves no temp file.
  - The output ends in exactly one `\n`, and a read-back plus rewrite is byte-identical.
  - Validation needs no network: it passes with `socket.socket` patched to raise.
- **Hop parity.** A test `ast`-parses `walkthrough_pipeline/models.py` by file path (code_facts never imports `walkthrough_pipeline`). It asserts that every walkthrough `Hop` field name is either a field of the output `Hop` (`symbol_called`, `citation`, `note`) or a field of its `code_facts.models.Citation`. A serialised citation without an explicit side carries `"side": "head"`.
- **Dependency and lock.** `jsonschema>=4` is added to `[project].dependencies` in `plugins/multiplai-dev/lib/code_facts/pyproject.toml`. The root `uv.lock` is regenerated with `uv lock` at the repository root. The block proves all of the following:
  - `uv lock --check` passes;
  - `uv sync --all-packages --frozen && uv run python -c "import code_facts"` succeeds;
  - an isolated `python -I` subprocess that sets `sys.modules["jsonschema"] = None` can still import `code_facts`.

  No package named after a field or keyword (`file`, `line`, `kind`, `other`, `start`, `with`, `route`, ...) appears in either file.
- **Spec amendments, written first.** Every spec text that D18 lists is rewritten under `specs/changes/<this change>/`:
  - trace-gate: an internal `broken_at`; `stops_at` = retained-hop count (hops 1–2 → 2; over-long → 20); cut at hop 1 → `trace`/`cut_at_entry` drop;
  - path-tracing: D14 drop codes, and two-call scenarios for zero and unparseable answers;
  - contract: D3 `EntryKind` plus `source`/`handler`/`invocation`; nine top-level fields; `tests.e2e`/`tests.mocks_hop_2` records; the D14 drop table; the schema path;
  - fallback: `other`/`GET /items`, a `malformed_entry` scenario, and the `citation_unverified` accounting scenario;
  - path-scope: stage-ordered drops and the merged-invoker coverage scenarios;
  - discovery: live `COMMANDS` resolution for pr_pipeline, plus a synthetic `if args.command == "x"` fixture; lower-cased click names; the D7 merge key; `uv run --directory`; `snapshot_mismatch`;
  - e2e: the key renames;
  - proposal: the `pr_pipeline/commands.py:400,409` citation.
- **How to run it.** `uv run --directory plugins/multiplai-dev/lib/code_facts python -m pytest tests/` runs `map_paths` on an empty `tmp_path` repository and `write_paths_file`s the result, and the existing code_facts suite passes unmodified.

Interfaces:
- Produces: `async def map_paths(*, root: Path, commit: str, resolve: Resolve, call: CallFn, cache_dir: Path, changed_lines: Mapping[str, Iterable[int]] | None = None, seeds: Sequence[Seed] = (), places: Sequence[Place] = (), cap: int = 5, trace_limit: int = 20, concurrency: int = DEFAULT_CONCURRENCY, fallback: bool = True, fatal: tuple[type[BaseException], ...] = (), call_kwargs: Mapping[str, Any] | None = None) -> PathsResult`
- Produces: `def write_paths_file(result: PathsResult, path: Path) -> None`
- Produces: `class PathsSchemaError(ValueError)` with attribute `pointer: str`
- Produces: `def paths_schema() -> dict[str, Any]`
- Produces: `def atomic_write_text(path: Path, text: str) -> None`
- Produces: `def normalise_rel_path(root: Path, path: str) -> str` (raises `ValueError` for empty, `.`, absolute, `..`, backslash or NUL paths; strips leading `./` and duplicate slashes)
- Produces: `def normalise_changed_lines(root: Path, changed_lines: Mapping[str, Iterable[int]]) -> dict[str, tuple[int, ...]]`
- Produces: `class EntryKind(StrEnum)` with values `argparse_subcommand, click_command, typer_command, main_module, project_script, skill_command, hook_command, seed, other`; `KIND_RANK: tuple[EntryKind, ...]` in that order
- Produces: `class DropStage(StrEnum)` with values `model_fallback_citation, import_reachability, trace_limit, trace, no_changed_line, cap`; `class DropReason(StrEnum)` with values `missing_citation, citation_unverified, malformed_entry, duplicate, unreachable, over_trace_limit, trace_call_failed, malformed_answer, empty_trace, cut_at_entry, no_changed_line_covered, over_cap`
- Produces: `class BreakReason(StrEnum)` with values `malformed_answer, empty_trace, path_outside_root, missing_file, line_out_of_range, text_mismatch, base_side_citation, entry_mismatch, unconnected_hop, repeated_hop, hop_limit`; `class OutputBreakReason(StrEnum)` (the same values minus `malformed_answer` and `empty_trace`)
- Produces: `class DiagnosticReason(StrEnum)` with values `parse_error, unresolved_target, unresolved_handler, dynamic_name, seed_not_found, snapshot_mismatch`; `class E2EMatch(StrEnum)` with values `direct_call, cli_argv, cli_runner, subprocess, runpy`
- Produces: `class Location(path: str, line: int)`; `class HandlerRef(path: str, line: int, symbol: str)`; `class InvokerRecord(kind: EntryKind, name: str, location: Location)`
- Produces: `class EntryRecord(kind: EntryKind, name: str, source: Literal["discovery", "seed", "model"], location: Location, handler: HandlerRef | None, invocation: str | None, also_invoked_by: tuple[InvokerRecord, ...])`
- Produces: `class Hop(symbol_called: str, citation: Citation, note: str = "")` where `Citation` is `code_facts.models.Citation`
- Produces: `class E2ERecord(node_id: str, file: str, line: int, match: E2EMatch, invocation_line: int)`; `class MockedE2ERecord(E2ERecord)` adding `patch_line: int`; `class TestListing(e2e: tuple[E2ERecord, ...] = (), mocks_hop_2: tuple[MockedE2ERecord, ...] = ())`
- Produces: `class PathRecord(id: str, entry: EntryRecord, hops: tuple[Hop, ...], stops_at: int | None, break_reason: OutputBreakReason | None, changed_lines_covered: tuple[Location, ...], tests: TestListing)`
- Produces: `class EntryRef(kind: EntryKind, name: str, source: Literal["discovery", "seed", "model"], location: Location | None)`; `class DropRecord(stage: DropStage, reason: DropReason, detail: str, entry: EntryRef)`
- Produces: `class Diagnostic(path: str, line: int | None, reason: DiagnosticReason, detail: str)`; `class FallbackStatus(status: Literal["ok", "failed", "skipped"], reason: str | None)`; `class TestListingSkip(file: str, reason: Literal["decode_error", "syntax_error"])`
- Produces: `class PathsResult(schema_version: Literal["paths.v1"], commit: str, scoped: StrictBool, cap: int | None, paths: tuple[PathRecord, ...], dropped: tuple[DropRecord, ...], fallback: FallbackStatus, diagnostics: tuple[Diagnostic, ...], test_listing_skips: tuple[TestListingSkip, ...])`
- Produces: `class Seed(path: str, line: int | None = None, symbol: str | None = None, name: str | None = None)`; `class Place(path: str, line: int)`
- Produces: `HOP_LIMIT = 20`; `DEFAULT_CONCURRENCY = 4`; `ALLOWED_TOOLS = ("Read", "Grep", "Glob")`

Satisfies: paths-output-contract (Public entry points exported from the library; `map_paths` returns a model and writes only cache files; Schema file exists and follows the review facts schema style; Top-level document shape; Path object shape; Entry object shape; Hop object shape mirrors the walkthrough Hop; Locations are relative to the snapshot root; Cut paths carry stop and reason together — "Mismatched pair rejected"; Test-listing shape — "Overlap rejected"; Every drop is recorded with a reason — "Empty reason rejected"; Empty results are valid; `write_paths_file` validates before writing; `write_paths_file` writes atomically; Byte-stable serialisation — "Trailing newline"; Round-trip between file and model; Schema and models agree; Schema validation dependency is declared); path-scope-and-cap (Changed-line paths are validated and normalised; Cap limits kept paths — "Invalid cap rejected"; Trace limit bounds the number of entries traced — "Invalid trace limit rejected"); entry-point-model-fallback (Fallback can be disabled by the caller)

## 2. Discovery of entries declared in Python source: argparse, click, typer, `__main__`, seeds, merging

This block delivers `pyindex.py`, `imports.py` and `entries.discover_entries`. Together they return sorted entries and diagnostics for the Python-declared kinds. Discovery never calls a model, never imports or executes snapshot code (sentinel-writing, raising modules leave no file and raise nothing), and never spawns a subprocess (a test passes with `subprocess.Popen` and `os.system` patched to raise).

**`SnapshotIndex(root)`: walking the tree**
- One sorted `os.scandir` walk.
- It skips the directories `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `site-packages`, `build` and `dist` at any depth, matching names exactly and case-sensitively. A root that is itself named `site-packages` is still walked.
- It does not follow a symlink whose `resolve()` is outside `root`. It keeps a `(st_dev, st_ino)` visited set, so a loop inside `root` (`a/loop -> ..`) terminates and each file appears once. Python 3.12's `RuntimeError` from `resolve()` on a symlink loop is caught.

**`SnapshotIndex(root)`: repository roots and module lookup**
- A file's repository root is the nearest ancestor at or below `root` that contains `.git` (a file or a directory) or `pyproject.toml`.
- Dotted names resolve against `<repo>`, then `<repo>/src`, and never cross into another repository. A package (`a/__init__.py`) wins over a same-named module (`a.py`), matching `importlib.util.find_spec`.

**`SnapshotIndex(root)`: parsing**
- Each file is parsed once: its bytes go to `ast.parse`, so PEP 263 cookies and BOMs are honoured.
- The whole-file `tokenize` stream is cached alongside.
- Any of `SyntaxError`, `UnicodeDecodeError`, `ValueError`, `RecursionError`, `MemoryError` or `OSError` becomes exactly one `parse_error` diagnostic with `line=None`. That holds for NUL bytes, 300-deep brackets, a `-`×200000 expression and a `chmod 000` file, and no file aborts discovery.
- An empty `.py` file parses to zero entries and produces no diagnostic.

**`ImportGraph`**
- `import a.b.c` adds edges to `a/__init__.py`, `a/b/__init__.py` and `a/b/c.py`.
- `from x import y` adds edges to `x`, and to `x/y.py` when that file exists.
- `from . import b` is handled (`module=None`).
- A relative import resolves against the importer's package: the file's own directory for an `__init__.py`.
- A `level` deeper than the package adds no edge, as do unparseable modules and third-party imports.
- `reaches` terminates on cycles.

**argparse**
- One entry per `add_parser(<literal>)` on an `add_subparsers()` result in the same function. Nested names are space-joined, and `name="x"` given as a keyword is handled.
- A non-literal first argument (`Name`, f-string, `BinOp`, `Starred`) produces exactly one `dynamic_name` diagnostic.
- Handlers resolve through `set_defaults(func=<Name>)`, or through a dispatch-dict literal in the same module.
- A dispatch dict in another module of the same repository also counts, but only when the parser module `reaches` it and that module calls `<DICT>[args.<dest>](...)`.
- Known edges run to the handler's `def` line:
  - from the dispatch call line, or else the `set_defaults` or dict-entry line;
  - and in every resolved case, also from the `add_parser` line.

  On the live pr_pipeline shape, `open`, `edit` and `render` resolve to `cmd_open`, `cmd_edit` and `cmd_render` in `commands.py`, with edges from `commands.py:409`.
- A synthetic `if args.command == "x"` fixture keeps `handler=None` at its `add_parser` line.
- A handler imported from a third-party package becomes `handler=None` plus an `unresolved_handler` diagnostic.

**`main_module`**
- A package `__main__.py` produces an entry at line 1 named with the dotted package name.
- A non-test top-level `if __name__ == "__main__":` produces an entry at the `if` line. Test files (`test_*.py`, `*_test.py`, `conftest.py`, anything under `tests/`) are excluded.
- The handler is the single called function. `sys.exit(main())`, `raise SystemExit(main())` and `asyncio.run(main())` are unwrapped to `main`. A callee that resolves outside the repository is never a handler.
- A `__main__.py` whose body also has a guard produces a single entry.

**Seeds**
- A seed resolves to the innermost `def` or `async def` whose span, counting from its first decorator line, contains the line. A seed can also name a symbol directly.
- A missing file, a line outside `1..len(lines)`, an unknown symbol, an absolute or escaping path, or no enclosing function each produce one `seed_not_found` diagnostic and no exception.
- The output does not depend on the order the seeds are given in.

**click and typer**
- The decorator receiver must resolve to a `click.group`/`click.command` binding, a `@<group>.group` result, `app.cli` (Flask), or `typer.Typer(...)`. So Dash `@app.callback(...)` and discord `@bot.command()` produce no entry.
- Click accepts both `@cli.command` and `@cli.command()`. For Typer, only the call form registers a command.
- Default names are `function_name.lower().replace("_", "-")`, with no suffix stripping, so `Deploy_All` becomes `deploy-all`.
- Explicit literal names are kept verbatim. An empty explicit name falls back to the function name, and a non-literal name gives `dynamic_name`.
- Group prefixes and `add_typer(name=…)` prefixes are space-joined. A nameless `add_typer` adds no prefix.
- The handler is the decorated `def`, and the known edge runs from the decorator line to the `def` line.

**Output**
- Merging keys on `(handler.path, handler.line)`, or on `location` when there is no handler. The entry with the lowest `KIND_RANK`, then the smallest location, wins. The others go into `also_invoked_by`, sorted by `(path, line, kind)`, and their edges are de-duplicated and unioned. Unresolved subcommands on different lines stay separate.
- Entries are sorted by `(location.path, location.line, kind, name)` and diagnostics by `(path, line or 0, reason)`.
- Every path is POSIX and relative to the snapshot root, including in multi-repository fixtures.
- Two runs, and runs with seeds `[A, B]` and `[B, A]`, serialise byte-identically.

Exercise this with `discover_entries(fixture_root)` on the fixtures under `tests/fixtures/paths/`.

Interfaces:
- Produces: `class SnapshotIndex` with `def __init__(self, root: Path) -> None`, `def files(self) -> tuple[str, ...]`, `def repo_root(self, rel: str) -> str`, `def module_file(self, repo: str, dotted: str, extra_roots: Sequence[str] = ()) -> str | None`, `def module_name(self, rel: str) -> str | None`, `def parse(self, rel: str) -> ast.Module | None`, `def tokens(self, rel: str) -> list[tokenize.TokenInfo] | None`, `def text_lines(self, rel: str) -> list[str] | None`, `def parse_failure(self, rel: str) -> Diagnostic | None`, `def enclosing_def(self, rel: str, line: int) -> HandlerRef | None`
- Produces: `def build_import_graph(index: SnapshotIndex) -> ImportGraph`; `class ImportGraph` with `def imports(self, rel: str) -> frozenset[str]` and `def reaches(self, src: str, dst: str) -> bool`
- Produces: `class KnownEdge(from_: Location = Field(alias="from"), to: Location)`
- Produces: `class DiscoveredEntry(kind: EntryKind, name: str, source: Literal["discovery", "seed"], location: Location, handler: HandlerRef | None, invocation: str | None, known_edges: tuple[KnownEdge, ...], also_invoked_by: tuple[InvokerRecord, ...])`
- Produces: `class Discovery(entries: tuple[DiscoveredEntry, ...], diagnostics: tuple[Diagnostic, ...])`
- Produces: `def discover_entries(root: Path, seeds: Sequence[Seed] = ()) -> Discovery`
- Produces: `def merge_entries(entries: Iterable[DiscoveredEntry]) -> tuple[DiscoveredEntry, ...]`
- Produces: `def to_entry_record(entry: DiscoveredEntry) -> EntryRecord`
- Consumes: Block 1: `class Location(path: str, line: int)`; `class HandlerRef(path: str, line: int, symbol: str)`; `class InvokerRecord(kind: EntryKind, name: str, location: Location)`; `class Diagnostic(path: str, line: int | None, reason: DiagnosticReason, detail: str)`; `class Seed(path: str, line: int | None = None, symbol: str | None = None, name: str | None = None)`; `class EntryKind(StrEnum)`; `KIND_RANK: tuple[EntryKind, ...]`; `class EntryRecord(kind: EntryKind, name: str, source: Literal["discovery", "seed", "model"], location: Location, handler: HandlerRef | None, invocation: str | None, also_invoked_by: tuple[InvokerRecord, ...])`; `def normalise_rel_path(root: Path, path: str) -> str`

Satisfies: entry-point-discovery (Discovery is deterministic and makes no model call; Discovery never imports or runs target code; Entry record shape; Paths are relative to the snapshot root, including multi-repository snapshots; Excluded and unsafe locations are not scanned; argparse subcommands are discovered; argparse handlers resolve through set_defaults and dispatch dicts only, as amended; argparse dispatch lines become known edges; click and typer commands are discovered; __main__ modules and packages are discovered; Caller-supplied seeds become entries; Entries resolving to the same handler are merged — "Unresolved handlers not merged" and "Seed duplicating a discovered entry"; Discovery output is stably ordered; Every skipped candidate is explained)

## 3. Discovery of entries declared in config and docs: `[project.scripts]`, `SKILL.md` commands, hook commands

This block extends `discover_entries` with three new entry kinds. Each kind resolves against the module index from Block 2, carries a handler as defined in D6, and merges across kinds.

**`[project.scripts]`**
- Every `pyproject.toml` is read as bytes, decoded as `utf-8-sig`, and parsed with `tomllib.loads`.
- These all become one `parse_error` diagnostic for the whole file, and other files still yield their entries:
  - `TOMLDecodeError`, including duplicate keys;
  - `UnicodeDecodeError`;
  - `RecursionError`.
- Each plain key under a `[project.scripts]` header becomes a `project_script` entry:
  - the key's line is found by a line scan that handles quoted keys;
  - extras suffixes are stripped, and spaces around `:` are allowed (`"pkg.mod : main [x]"` resolves like `pkg.mod:main`);
  - a known edge runs from the key line to the target `def`.
- These shapes produce one `unresolved_target` diagnostic and never crash:
  - a module-only value, a non-string value, or a `module:obj.attr` value;
  - an inline-table or dotted-key `scripts`, reported at the `[project]` header line;
  - a target that does not resolve under the declaring project's own roots (nested `scripts/` projects never bind to another project's same-named package).
- `dynamic = ["scripts"]` and `[tool.poetry.scripts]` yield no entries and no diagnostic.

**`SKILL.md` commands**
- Only backtick fences whose first info-string word is exactly `bash`, `sh`, `shell`, `zsh` or `console` are scanned. Prose, `python` fences, untagged fences and tilde fences are ignored.
- Inside a `console` fence, only lines that start with `$ ` are commands. The prompt is stripped and output lines are never matched.
- Line continuations (`\`, plus `> ` inside `console`) are joined.
- If `shlex.split` raises `ValueError` (an unbalanced quote), that line gets one `parse_error` diagnostic and scanning continues.
- An empty token list is not a candidate.
- Tokens from the first token that starts with `#` onward are discarded, so an inline comment never becomes the subcommand.
- Handling of command prefixes:
  - leading `NAME=value` assignments are removed;
  - a `PYTHONPATH=` value is split on `:`, empty components are dropped, and each component becomes a search root, left to right;
  - `cd <dir> &&` adds a search root;
  - `uv run --directory DIR` and `--project DIR` add their roots right after the `PYTHONPATH` roots;
  - `--frozen`, `--no-sync`, `--quiet`/`-q`, `--package NAME`, `--extra NAME` and `--with SPEC` are skipped;
  - any other `uv run` option, an `-mpkg` token, `python -I`/`-E`/`-P`, or an unknown `$VAR` gives `unresolved_target`.
- Only `${CLAUDE_SKILL_DIR}` and `${CLAUDE_PLUGIN_ROOT}` are expanded.
- The search-root order is the one in the spec.
- The entry is named `<skill dir>: <module> [<first positional>]`, with `invocation` holding the joined command.
- The known edge goes to the resolved subcommand's handler if there is one. Otherwise it goes to that subcommand's `add_parser` line. Otherwise it goes to `<module>/__main__.py:1`; for example, a placeholder `<command>` argument matches no subcommand.
- `handler` is the edge target as a `HandlerRef`, with symbol `__main__` for a line-1 fallback. This lets a `SKILL.md` command merge with the package's `main_module`.

**Hook commands**
- Only `hooks.<event>[].hooks[]` objects in `hooks.json` and `.claude/settings*.json` are read, and only those with `"type": "command"` and a string `command`.
- `statusLine` and `prompt` hooks are ignored, as is a file without the `"hooks"` wrapper.
- These become `parse_error` and do not crash:
  - an empty file, invalid JSON, or a BOM-prefixed file (it is decoded as `utf-8-sig`);
  - `RecursionError`;
  - wrongly typed shapes such as `"hooks": null` or a list.
- Each command's line is found by searching the raw text for `json.dumps(cmd, ensure_ascii=False)` and then for `json.dumps(cmd)`. Repeated strings are assigned in document order. If neither form is found, the result is `unresolved_target` at line 1.
- The entry is named `<event>:<matcher>`, or `<event>` when there is no matcher.
- Variable expansion:
  - `${CLAUDE_PLUGIN_ROOT}` becomes the nearest ancestor that contains `.claude-plugin/plugin.json`, or else the parent of the hooks file's directory;
  - `$CLAUDE_PROJECT_DIR` and `${CLAUDE_PROJECT_DIR}` become the repository root, including the quoted form `"$CLAUDE_PROJECT_DIR"/x.sh`;
  - any other variable, such as `$CLAUDE_CONFIG_DIR`, gives `unresolved_target`.
- Target resolution:
  - only the first simple command, after an optional `cd <dir> &&`, is considered, so `mkdir …; bash x.sh` resolves nothing and gives `unresolved_target`;
  - a `python -m` target uses the `SKILL.md` rules;
  - a Python script uses the `main_module` handler rules;
  - any other existing file resolves to line 1;
  - a missing file gives `unresolved_target`.

**Merging across kinds and the end-to-end check**
- `[project.scripts] tool = "pkg.cli:main"` together with `pkg/__main__.py` produces one `main_module` entry. Its `also_invoked_by` holds `tool`, and its `known_edges` include the edge from the `pyproject.toml` key line.
- A hook and a `SKILL.md` command that both run `python -m pkg` produce one entry.
- On a fixture with one entry of every kind:
  - every entry has all seven fields;
  - paths are relative and contain no `..`;
  - every `line` is between 1 and the file's line count;
  - a fixture with one broken `.py` file, one invalid `hooks.json`, one external script target, one `add_parser(name_var)` and one missing seed returns exactly five diagnostics, with reasons `parse_error`, `parse_error`, `unresolved_target`, `dynamic_name` and `seed_not_found`.

Interfaces:
- Produces: (none)
- Consumes: Block 2: `def discover_entries(root: Path, seeds: Sequence[Seed] = ()) -> Discovery`; `def merge_entries(entries: Iterable[DiscoveredEntry]) -> tuple[DiscoveredEntry, ...]`; `class SnapshotIndex` with `def module_file(self, repo: str, dotted: str, extra_roots: Sequence[str] = ()) -> str | None` and `def repo_root(self, rel: str) -> str`; `class DiscoveredEntry(kind: EntryKind, name: str, source: Literal["discovery", "seed"], location: Location, handler: HandlerRef | None, invocation: str | None, known_edges: tuple[KnownEdge, ...], also_invoked_by: tuple[InvokerRecord, ...])`; `class KnownEdge(from_: Location = Field(alias="from"), to: Location)`
- Consumes: Block 1: `class Diagnostic(path: str, line: int | None, reason: DiagnosticReason, detail: str)`; `class EntryKind(StrEnum)`

Satisfies: entry-point-discovery ([project.scripts] entries are discovered; python -m commands in SKILL.md fenced shell blocks are discovered, including the amended `uv run --directory` scenario; Hook commands are discovered; Entries resolving to the same handler are merged — "Script and __main__ resolve to the same main()" and "Hook and SKILL.md invoke the same module"; Entry record shape — "Fields present and well-formed"; Every skipped candidate is explained — "Mixed failures in one fixture")

## 4. Pure trace gate `check_trace`

This block delivers `code_facts/gate.py`. Its `check_trace` checks hops in order and stops at the first hop that breaks. A broken hop reports the first rule it fails, in this order: `path_outside_root`, `missing_file`, `line_out_of_range`, `text_mismatch`, `base_side_citation`, `entry_mismatch` (hop 1 only), `unconnected_hop` (hop 2 onward), `repeated_hop`. The function is pure:
- it reads files only through the injected `read`, `quote_ok` and `tokens` callables;
- it never imports `walkthrough_pipeline` (an AST scan of every `code_facts` module asserts this);
- it never calls a model (a stub `CallFn` that fails the test if called is never invoked);
- it gives equal outcomes for equal inputs.

**Path rule (rule 1).**
- The check is lexical, on `PurePosixPath(path).parts`. It never uses `is_relative_to` after a join.
- These paths break as `path_outside_root`: empty, `.`, absolute, `//x`, any `..` segment, a backslash, or a NUL.
- Leading `./` and duplicate slashes are normalised away, and the normalised path is what accepted hops store. So `./pkg/a.py` and `pkg/a.py` compare equal everywhere, and every accepted path passes the output path pattern.
- `repo_b/pkg/mod.py` passes in a multi-repo snapshot.

**Reading and quoting (rules 2–5).**
- `read` returns `None` for a missing file or a non-blob, and that becomes `missing_file`.
- `line_out_of_range` is computed from `len(lines)` before any quote check, because `check_quote` silently clamps `line_end` at end of file. So line 500 of a 40-line file breaks, and so does `line_end` past EOF.
- An empty or whitespace-only quote, or a quote that differs from the file, is `text_mismatch`.
- An explicit `side == "base"` is `base_side_citation`. It is checked after the head-side text rules, so a base-side citation of text that does not exist at head reports `text_mismatch`. That oddity is pinned by a test.

**Anchoring (rule 6).** Hop 1 passes `entry_mismatch` if either holds:
- it cites the entry's `location.path` with a range that includes `location.line`;
- it cites `handler.path` with a range that includes `handler.line`. The path must match: `other.py` at the handler's line number still fails.

**Connectivity (rule 7).** Hop n+1 connects to hop n if any of these holds:
- **Name.** The last identifier of `symbol_called`, taken after dropping any argument list (`self._ack(rev)` gives `_ack`, `cli.handle_open` gives `handle_open`), appears as a `NAME` token inside hop n's range.
  - Tokens come from the whole-file `tokenize` stream, not from a fragment. This means a range that starts mid-block or inside a docstring never raises `IndentationError` or `TabError`, and docstring prose is never treated as code.
  - If the whole file fails to tokenize (`TokenError` or any `SyntaxError` subclass), a fallback strips quoted spans and `#` comments from the cited lines and matches identifiers in what is left.
  - A name that appears only in a comment or a plain string does not connect.
  - Non-Python files never connect by name.
- **Known edge.** An edge's `from` line is inside hop n's range and its `to` line is inside hop n+1's range.
- **Place.** A place falls inside hop n+1's range. A matching place does not excuse a bad citation: a mismatched quote still breaks as `text_mismatch`.

**Repeats, limits and degenerate input.**
- A hop whose normalised `(path, line_start, line_end)` equals any earlier hop's breaks as `repeated_hop`. An immediate self-repeat breaks at `broken_at=2`. Ranges that overlap but are not equal do not count as repeats.
- Only the first `hop_limit` hops are checked:
  - 21 or 25 valid hops give 20 accepted hops, `broken_at=21` and `reason=hop_limit`;
  - a broken hop at position 21 or later is never seen;
  - exactly 20 valid hops give `broken_at=None`.
- An empty list gives `broken_at=1`, `reason=empty_trace`.

**How to run it.** Use direct unit tests with fake `read`, `quote_ok` and `tokens`. Cover every trace-gate rule scenario and the "first broken hop decides" and multiple-failure ordering scenarios.

Interfaces:
- Produces: `class GateOutcome(accepted: tuple[Hop, ...], broken_at: int | None, reason: BreakReason | None, detail: str)`
- Produces: `def check_trace(hops: Sequence[Hop], entry: EntryRecord, edges: Sequence[KnownEdge], places: Sequence[Place], read: Callable[[str], list[str] | None], quote_ok: Callable[[Citation], bool], tokens: Callable[[str], list[tokenize.TokenInfo] | None], hop_limit: int = HOP_LIMIT) -> GateOutcome`
- Consumes: Block 1: `class Hop(symbol_called: str, citation: Citation, note: str = "")`; `class EntryRecord(kind: EntryKind, name: str, source: Literal["discovery", "seed", "model"], location: Location, handler: HandlerRef | None, invocation: str | None, also_invoked_by: tuple[InvokerRecord, ...])`; `class Place(path: str, line: int)`; `class BreakReason(StrEnum)`; `HOP_LIMIT = 20`
- Consumes: Block 2: `class KnownEdge(from_: Location = Field(alias="from"), to: Location)`

Satisfies: trace-gate (Gate is deterministic and makes no model call; Gate does not import the walkthrough pipeline; Break reasons come from a fixed set; Hop paths stay inside the snapshot root; Every hop citation re-reads at the commit; Citations are head-side only; Hop 1 is anchored at the entry; Consecutive hops are connected by a reference or a known edge; Caller-supplied places are accepted as connections; Discovery edges are accepted as connections; Repeated hops break the path; Traces are capped at 20 hops, as amended; The first broken hop decides the outcome); path-tracing (Hops cite head-side code only; Snapshot-relative locations — "Path escaping the root")

## 5. Unscoped tracing through `map_paths`: discovery, snapshot check, one trace per entry, one re-ask, cut or drop, ordered `paths.json`

**What the block does.** `map_paths` runs the whole unscoped path for real, still with `fallback=False`.
- **Git check.** It runs `shutil.which("git")` before any call. If git is missing it raises `RuntimeError`; it does not misreport every hop as `missing_file`.
- **Discovery.** It calls `discover_entries(root, seeds)` and copies the diagnostics into the result.
- **Snapshot check.** For each entry it compares the snapshot's `location` line, and `handler` line when there is one, with `make_git_reader(resolve)`. A missing file or a differing line removes the entry and adds a `snapshot_mismatch` diagnostic. Example: an untracked file that defines an entry, or a working-tree edit that shifts a `def`.
- **Git reader.** It runs a fixed argv `git -C <repo> cat-file blob <sha>:<rel>` with `shell=False`, `stdin=DEVNULL` and `check=False`. Results are cached per `(repo, sha, rel)` within the run.
  - It returns `None` for a non-zero exit (a bad sha, a directory, or a path that is not a blob) and for a path containing NUL.
  - It decodes with `errors="replace"`.
  - It splits on `"\n"` only and drops one trailing empty element, so `"a\nb\n"` has 2 lines and an empty blob has 0.
- **Tracing.** Each surviving entry gets exactly one `paths:trace` call through `explore.bounded(concurrency)`, so peak in-flight calls never exceed the limit and `concurrency=1` never overlaps.
  - Each call is `await call(prompt, TraceAnswer, allowed_tools=["Read", "Grep", "Glob"], cwd=str(root), budget_label="paths:trace", **call_kwargs)`.
  - Re-asks are identical except for the label, which is `paths:retrace`.
  - Entries that share a handler share one trace.

**Prompts.**
- `trace_entry` includes the citation rules and the untrusted-content rules.
- It includes the entry's kind, name, location, handler and invocation. These are fenced with `multiplai_core.untrusted.fence`, which is stated to transform backticks and injection-shaped text.
- It includes the commit and the sentence `There is no change scope; trace to the entry's last effect.`
- It includes the applicable edges and places, the number `20`, the `TraceAnswer` schema, and verbatim the line `Hop 1 MUST cite <location.path>:<location.line> (or <handler.path>:<handler.line>) with its exact text, even when that file is not Python.`. The parenthesised part is omitted when there is no handler.
- `retrace_entry` adds the broken hop index, the literal reason code, the detail and the accepted prefix.
- `TRACE_PROMPT_VERSION = 1`. A drift-guard test pins a SHA-256 of the rendered `trace_entry` and `retrace_entry` text next to the version.

**Answers and cache.** `cached_call` replaces `explore.ask`/`run_tasks`, which stay unchanged. An inspect-signature test confirms this.
- **Normalisation.** It turns a model instance (via `model_dump` then `model_validate`, so `model_construct` instances are re-checked), a dict, or a JSON string (`model_validate_json`) into the answer model.
  - `TraceAnswer` is `extra="forbid"` at the top level and requires `hops`. So `{}`, a misspelt key, a second alternative chain and Markdown-fenced JSON are all `malformed_answer`.
  - `HopAnswer` keeps `side`, so `side: "base"` survives parsing.
- **Storage.**
  - Only valid answers are stored, at `<cache_dir>/paths/<kind>/<sha256 hex>.json` as `{"key": …, "answer": …}`, written through `atomic_write_text`. `cache_dir` is created with `mkdir(parents=True, exist_ok=True)`.
  - A zero-byte, truncated, mismatched-key or invalid file counts as a miss and is overwritten, never raised.
  - `.tmp` files are never read.
- **Keys.** Keys are `canonical_key` = SHA-256 of `json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`, built from str-keyed dicts of builtins and lists sorted explicitly.
  - The trace key covers the commit, entry identity, sorted edges and places, and the version.
  - The re-ask key adds `broken_at`, the reason and the SHA-256 of the canonical first answer.

**Policy.**
- **First-call exception.** An exception whose type is in `fatal` propagates. Any other exception on the first trace call drops the entry with stage `trace`, reason `trace_call_failed`, after exactly one call. Nothing is cached and no re-ask is made, so a rerun retries it.
- **Re-ask.** A malformed or empty answer counts as `broken_at=1`. Any break other than `hop_limit` gets exactly one re-ask. Its exceptions propagate unchanged, and no further call is made.
- **Re-answer.** The re-answer replaces the first answer:
  - a re-answer that is malformed or empty drops the entry as `trace`/`malformed_answer` or `trace`/`empty_trace`, after exactly two calls;
  - one that breaks at hop 1 drops the entry as `trace`/`cut_at_entry`;
  - one that breaks at hop k > 1 keeps hops `1..k-1` with `stops_at=k-1` and its reason;
  - a 21-hop answer keeps 20 hops with `stops_at=20` and `hop_limit`, after one call.
- **Ordering and ids.**
  - Kept paths are sorted by hop count ascending, then `KIND_RANK`, then `location.path`, then `location.line`, then `name`. The sort runs on results gathered into a dict, so completion order doesn't matter.
  - Ids `p1..pN` are assigned last.
  - `changed_lines_covered` is `[]`.
  - Kept paths plus drops equal the number of entries after merging and the snapshot check.

**How to verify.** Use a recording `FakeCall` keyed on prompt markers, with random delays, an in-flight counter decremented in `finally`, and chosen exceptions, together with a prefix-splitting `resolve` over a `git init` `tmp_path` fixture. The following all hold:
- A full run writes a schema-valid `paths.json`.
- A rerun from the same cache makes zero calls and gives byte-identical output.
- Concurrency 1 and concurrency 8 give byte-identical output.
- Reversing seeds and places gives byte-identical output.
- A run interrupted after 3 of 5 traces makes exactly 2 calls on rerun.
- Bumping `TRACE_PROMPT_VERSION` re-asks every entry.

Interfaces:
- Produces: `def make_git_reader(resolve: Resolve) -> Callable[[str], list[str] | None]`
- Produces: `def make_quote_check(resolve: Resolve) -> Callable[[Citation], bool]`
- Produces: `class HopAnswer(_Model)` with fields `symbol_called: str`, `citation: Citation`, `note: str = ""`; `class TraceAnswer(_Model)` with fields `hops: list[HopAnswer]` (required) and `notes: str = ""`, top-level `extra="forbid"`
- Produces: `def trace_entry(entry: EntryRecord, commit: str, scope: Mapping[str, tuple[int, ...]] | None, edges: Sequence[KnownEdge], places: Sequence[Place], hop_limit: int = HOP_LIMIT) -> str`
- Produces: `def retrace_entry(entry: EntryRecord, commit: str, scope: Mapping[str, tuple[int, ...]] | None, edges: Sequence[KnownEdge], places: Sequence[Place], broken_at: int, reason: BreakReason, detail: str, accepted: Sequence[Hop], hop_limit: int = HOP_LIMIT) -> str`
- Produces: `TRACE_PROMPT_VERSION = 1`
- Produces: `def canonical_key(material: Mapping[str, Any]) -> str`
- Produces: `async def cached_call(*, cache_dir: Path, kind: Literal["fallback", "trace", "retrace"], key_material: Mapping[str, Any], prompt: str, schema: type[_Model], label: str, call: CallFn, root: Path, call_kwargs: Mapping[str, Any]) -> _Model`
- Produces: `def path_sort_key(path: PathRecord) -> tuple[int, int, int, str, int, str]` returning `(-len(path.changed_lines_covered), len(path.hops), KIND_RANK.index(path.entry.kind), path.entry.location.path, path.entry.location.line, path.entry.name)`
- Consumes: Block 1: `async def map_paths(*, root: Path, commit: str, resolve: Resolve, call: CallFn, cache_dir: Path, changed_lines: Mapping[str, Iterable[int]] | None = None, seeds: Sequence[Seed] = (), places: Sequence[Place] = (), cap: int = 5, trace_limit: int = 20, concurrency: int = DEFAULT_CONCURRENCY, fallback: bool = True, fatal: tuple[type[BaseException], ...] = (), call_kwargs: Mapping[str, Any] | None = None) -> PathsResult`; `def atomic_write_text(path: Path, text: str) -> None`; `class PathRecord(id: str, entry: EntryRecord, hops: tuple[Hop, ...], stops_at: int | None, break_reason: OutputBreakReason | None, changed_lines_covered: tuple[Location, ...], tests: TestListing)`; `class DropRecord(stage: DropStage, reason: DropReason, detail: str, entry: EntryRef)`; `ALLOWED_TOOLS = ("Read", "Grep", "Glob")`
- Consumes: Block 2: `def discover_entries(root: Path, seeds: Sequence[Seed] = ()) -> Discovery`; `def to_entry_record(entry: DiscoveredEntry) -> EntryRecord`; `class SnapshotIndex` with `def tokens(self, rel: str) -> list[tokenize.TokenInfo] | None` and `def text_lines(self, rel: str) -> list[str] | None`
- Consumes: Block 4: `def check_trace(hops: Sequence[Hop], entry: EntryRecord, edges: Sequence[KnownEdge], places: Sequence[Place], read: Callable[[str], list[str] | None], quote_ok: Callable[[Citation], bool], tokens: Callable[[str], list[tokenize.TokenInfo] | None], hop_limit: int = HOP_LIMIT) -> GateOutcome`

Satisfies: path-tracing (One trace agent per traced entry; Trace agents are read-only; Model calls go only through the caller's CallFn; Trace prompt carries the entry, the scope and known edges — "No changed lines given"; An answer is one linear chain of hops; Hop limit of 20; Empty or malformed answers are dropped, not fatal, as amended; Every answer passes through the trace gate; Bounded concurrency; Answer cache keyed independently of order; Resumable runs; Snapshot-relative locations; Deterministic trace output); trace-gate (Malformed or empty answers break at hop 1; A broken trace gets exactly one re-ask; An unrepaired trace is cut at the break; Re-asks use the resumable answer cache; CallFn failures during a re-ask propagate); paths-output-contract (Cut paths carry stop and reason together; Ids are contiguous and follow array order; Byte-stable serialisation — "Reproducible run" and "Concurrency does not affect output"; Entry object shape — "Known discovery case keeps the add_parser line", as amended; Changed-line coverage field — "Unscoped coverage empty"); path-scope-and-cap (Scoping is active only when changed lines are supplied — "No changed lines means no scope filtering")

## 6. Change scope: import reachability, trace limit, changed-line coverage, cap, accounting

With `changed_lines` supplied, `map_paths` writes `scoped: true` and the integer `cap`, and filters entries and paths as follows.

**Reachability distance.**
- `reverse_distances` runs one reverse BFS over the Block 2 import graph. It is seeded at the changed `.py` files and keeps a visited set, so import cycles terminate.
- An entry's distance is 0 when either its `location.path` or any `also_invoked_by[].location.path` is a changed file. This covers a change to only `SKILL.md`, `hooks.json` or a `[project.scripts]` key line, which keeps the merged path.
- Otherwise its distance is the minimum BFS distance over its handler file and the targets of its known edges.
- An entry with no finite distance is dropped at stage `import_reachability` with reason `unreachable`, and no call is made for it. This includes:
  - an entry reached only through `importlib.import_module`;
  - an entry whose importer has a syntax error;
  - an entry whose handler only reads a changed JSON file.

**Trace limit.**
- Reachable entries are sorted by `(distance, KIND_RANK, location.path, location.line, name)`.
- Entries past `trace_limit` are dropped at stage `trace_limit` with reason `over_trace_limit`, and no call is made for them.
- The limit counts attempts. A failed trace does not free its slot for the next entry.

**Coverage and cap.**
- A changed line is covered when either holds:
  - it falls in the inclusive cited range of a retained hop in the same normalised file;
  - it equals the entry's `location`, or an `also_invoked_by` location.
- Hops dropped by a cut or past the 20-hop limit never count.
- `changed_lines_covered` holds distinct `{path, line}` values, sorted.
- A path with no coverage is dropped at stage `no_changed_line` with reason `no_changed_line_covered`.
- Kept paths are ordered by `path_sort_key`: covered count descending, then hop count ascending, then kind rank, path, line and name.
- Paths past `cap` are dropped at stage `cap` with reason `over_cap`, before ids `p1..pN` are assigned.

**Prompt, keys and boundary cases.**
- When scoped, the trace prompt and the trace key carry the changed lines as sorted, merged ranges. A 50,000-line rewrite therefore renders as one range.
- Reordered `changed_lines` keys and values hit the same keys, so the second run makes zero calls. Adding one changed line re-asks every entry whose prompt includes that file.
- `{}` (or a mapping in which every key has no lines) is scoped. It gives zero paths, every entry `unreachable`, and zero `CallFn` calls.
- A changed path naming a deleted file is accepted, and every entry is `unreachable`.
- With `cap=2` on 7 qualifying paths, 5 `over_cap` drops are recorded and the ids are exactly `p1`, `p2`.

**Accounting check.** A fixture that triggers every stage proves that kept paths plus drop records equal E. Each entry has at most one drop record, never also appears as a kept path, and the drops are sorted in stage order, then path, line and kind. Run it with `FakeCall` and `write_paths_file`, and assert on the drops and `changed_lines_covered`.

Interfaces:
- Produces: `def reverse_distances(graph: ImportGraph, seeds: Iterable[str]) -> dict[str, int]`
- Produces: `def entry_distance(entry: DiscoveredEntry, changed: Mapping[str, tuple[int, ...]], distances: Mapping[str, int]) -> int | None`
- Produces: `def covered_lines(hops: Sequence[Hop], entry: EntryRecord, changed: Mapping[str, tuple[int, ...]]) -> tuple[Location, ...]`
- Consumes: Block 1: `def normalise_changed_lines(root: Path, changed_lines: Mapping[str, Iterable[int]]) -> dict[str, tuple[int, ...]]`; `class DropRecord(stage: DropStage, reason: DropReason, detail: str, entry: EntryRef)`
- Consumes: Block 2: `def build_import_graph(index: SnapshotIndex) -> ImportGraph`; `class ImportGraph` with `def imports(self, rel: str) -> frozenset[str]` and `def reaches(self, src: str, dst: str) -> bool`; `class DiscoveredEntry(kind: EntryKind, name: str, source: Literal["discovery", "seed"], location: Location, handler: HandlerRef | None, invocation: str | None, known_edges: tuple[KnownEdge, ...], also_invoked_by: tuple[InvokerRecord, ...])`
- Consumes: Block 5: `def path_sort_key(path: PathRecord) -> tuple[int, int, int, str, int, str]`; `def trace_entry(entry: EntryRecord, commit: str, scope: Mapping[str, tuple[int, ...]] | None, edges: Sequence[KnownEdge], places: Sequence[Place], hop_limit: int = HOP_LIMIT) -> str`; `def canonical_key(material: Mapping[str, Any]) -> str`

Satisfies: path-scope-and-cap (Scoping is active only when changed lines are supplied — "Changed lines given activates scoping"; Changed-line paths are validated and normalised — "Missing file tolerated" and "Multi-repository snapshot paths"; Static import-graph reachability pre-filter; Trace limit bounds the number of entries traced; Post-trace coverage filter; Deterministic path ordering; Cap limits kept paths; Path ids assigned after ordering and cap; Every scope drop records a reason, with the amended merged-invoker scenarios); path-tracing (One trace agent per traced entry — "Pre-filtered entries are not traced"; Trace prompt carries the entry, the scope and known edges — "Prompt contents"; Answer cache keyed independently of order — "Changed scope misses the cache"); paths-output-contract (Top-level document shape — "Scoped run"; Changed-line coverage field — "Coverage listed"; Every drop is recorded with a reason — "Cap drop recorded")

## 7. Model fallback for entries that deterministic discovery missed

**What runs.** With `fallback=True` (the default), `map_paths` makes at most one `paths:fallback` call after discovery and the snapshot check, using the same read-only call shape as tracing.
- The `entry_fallback` prompt lists the deterministic entries by location and kind, asks only for new ones, and names the scope:
  - the changed files when scoped;
  - otherwise the seed paths;
  - otherwise the whole snapshot.
- A scoped run with no changed files skips the call. It records `fallback={"status": "skipped", "reason": "empty_scope"}`.

**Parsing and status.**
- The answer is parsed in two levels:
  - The outer `FallbackAnswer{entries: list[Any]}` must validate. If it doesn't, the run continues with deterministic entries only and records `status: failed`. The reason is the first error rendered as `"<loc>: <msg>"` from `errors(include_url=False)`, cut to 200 characters, so model input is never echoed.
  - Each raw item is then validated on its own as `FallbackEntry`. One bad item costs only that item.
- `{"entries": []}` gives `status: ok`.

**What each raw item becomes.** Each raw item counts once in E, and is either kept or recorded with stage `model_fallback_citation`:

| Item | Result |
|---|---|
| Absent or `null` citation | `missing_citation` |
| Non-object item, missing `kind`, or missing or empty `name` | `malformed_entry` |
| Absolute, `..` or unresolvable path | `citation_unverified` |
| `line_end < line_start`, or `line_start < 1` | `citation_unverified` |
| `line_end` past the end of the file (checked explicitly, because `check_quote` clamps) | `citation_unverified` |
| Empty or mismatched quote | `citation_unverified` |
| `side: "base"` | `citation_unverified` |
| A line or enclosing handler equal to a deterministic or seed entry's location or handler | `duplicate` |
| A repeat of an earlier kept fallback line | `duplicate` |

- Absolute paths are rejected on the raw string, before any `ws_path` normalisation.
- A drop for an item with no valid location carries `location: null`.

**Kept entries.**
- Kind matching is exact: an unknown kind such as `route`, `argparse` or `Route` becomes `other`, and `name` keeps the model's display name, e.g. `GET /items`.
- Kept entries get `source: "model"`, a handler set to the enclosing `def`, and a location relative to the snapshot root. In a multi-repo snapshot that looks like `repo_b/cli.py:5`.
- They are ordered by location, whatever order the model listed them in.
- They then go through the same reachability, trace-limit, coverage and cap stages as deterministic entries, with the same drop reasons.

**Cache.**
- The cache key is `canonical_key({"v": FALLBACK_PROMPT_VERSION, "commit": commit, "entries": sorted deterministic identities, "scope": sorted scope})`.
- A resumed run makes zero fallback calls. Two stubs that return the same entries in a different order give byte-identical `paths.json`.
- The drift-guard test also pins a hash of the rendered `entry_fallback` text next to `FALLBACK_PROMPT_VERSION = 1`. Editing the template without bumping the version fails the test.

**Errors.** Any exception the `CallFn` raises during the fallback propagates. It is not cached, and the stub records exactly one attempt.

**How to exercise it.** Use a `FakeCall` fallback stub against a fixture with two argparse subcommands, then `write_paths_file`. Accounting must hold with one valid item plus one `line_end < line_start` item.

Interfaces:
- Produces: `class FallbackEntry(_Model)` with fields `kind: str`, `name: str = Field(min_length=1)`, `citation: Citation | None = None`, `reason: str = ""`; `class FallbackAnswer(_Model)` with field `entries: list[Any]` (required)
- Produces: `def entry_fallback(entries: Sequence[EntryRecord], scope: Sequence[str]) -> str`
- Produces: `FALLBACK_PROMPT_VERSION = 1`
- Consumes: Block 5: `async def cached_call(*, cache_dir: Path, kind: Literal["fallback", "trace", "retrace"], key_material: Mapping[str, Any], prompt: str, schema: type[_Model], label: str, call: CallFn, root: Path, call_kwargs: Mapping[str, Any]) -> _Model`; `def canonical_key(material: Mapping[str, Any]) -> str`; `def make_quote_check(resolve: Resolve) -> Callable[[Citation], bool]`; `def make_git_reader(resolve: Resolve) -> Callable[[str], list[str] | None]`
- Consumes: Block 2: `class SnapshotIndex` with `def enclosing_def(self, rel: str, line: int) -> HandlerRef | None`
- Consumes: Block 1: `def normalise_rel_path(root: Path, path: str) -> str`; `class FallbackStatus(status: Literal["ok", "failed", "skipped"], reason: str | None)`; `class EntryKind(StrEnum)`; `class DropRecord(stage: DropStage, reason: DropReason, detail: str, entry: EntryRef)`

Satisfies: entry-point-model-fallback (Single fallback agent per run; Fallback agent is read-only; Fallback response shape, as amended; Malformed fallback answer does not fail the run; CallFn errors are not retried; Citation must re-read at the commit; Paths are relative to the snapshot root; Fallback entries do not duplicate deterministic entries; Fallback entries go through the same scope filter; Fallback is deterministic and resumable); paths-output-contract (Every drop is recorded with a reason — "Uncited model entry recorded"; Empty results are valid — "Repository with no entry points"); entry-point-discovery (Discovery is deterministic and makes no model call — "No model call during discovery")

## 8. Static e2e listing: tests that structurally invoke each path's entry

**What it adds.** `code_facts/e2e.py` fills `tests.e2e` on every kept path. It does this by reading test sources only. It never imports a test module, never collects tests, and never runs a subprocess or a `CallFn`. A guard test patches `subprocess.Popen` and `os.system` to raise during listing. A test file whose top-level code raises and writes a marker leaves no marker, and its tests are still considered.

**Which files and tests are scanned.**
- Files matching `test_*.py` or `*_test.py` under the snapshot root.
- These directories are skipped: `.git`, `.venv`, `venv`, `node_modules`, `__pycache__`, `site-packages`.
- Tests are module-level `test*` `def`/`async def` functions, and `test*` methods of module-level `Test*` classes. Nested functions and helper functions are excluded.

**Reading each file.**
- Bytes are decoded as strict UTF-8 first. A failure becomes `decode_error`.
- The text then goes to `ast.parse`. `SyntaxError` (NUL bytes included), `ValueError`, `RecursionError` and `MemoryError` all become `syntax_error`. Both reasons go into `test_listing_skips` with a root-relative `file`.
- An empty file gives no tests and no skip entry.

**Name resolution.** Callee names are resolved through:
- module-level imports;
- imports inside the test function body;
- `import a.b as c`;
- `from a.b import x as y`;
- attribute chains on imported modules;
- `from .x import y` re-exports in a package `__init__`, so `from pkg import run` matches a handler in `pkg/impl.py`.

Extra roots are the nearest `pyproject.toml` ancestor, the parent of the test's `tests/` directory, and the test file's own directory. A name rebound locally inside the test (`main = Mock()`) does not resolve to the import.

**Match kinds, in tie-break order.**
- `direct_call`: the callee resolves to `(handler.path, handler.symbol)`. A same-named symbol from another module does not match.
- `cli_argv`:
  - The callee is the program's resolved `main`, or `<parser builder>().parse_args`, in the subparser's module.
  - The argv is a literal list or tuple. Only its string-constant elements are compared, so `str(repo)` elements are allowed.
  - Those constants must contain the subcommand tokens as an ordered subsequence, so global options may come first.
  - A non-literal argv, or an empty `main([])`, never matches a subcommand entry.
- `cli_runner`:
  - The receiver is a `CliRunner()` call, or a name bound to one, imported from `click.testing` or `typer.testing`.
  - The first argument resolves to the click group or Typer app.
  - The args are a literal list, or a string constant split with `shlex`.
- `subprocess`:
  - Covers `subprocess.run`, `call`, `check_call`, `check_output` and `Popen`.
  - The command is a literal list, or a string constant split with `shlex`. The interpreters `sys.executable`, `python` and `python3` are treated as equal.
  - It matches `python -m <module> [<subcommand>]` for `main_module` and `skill_command` entries, so `otherpkg` never matches `pkg`.
  - It matches the project script name as the first token.
  - It matches a token-equal hook command.
- `runpy`: `runpy.run_module("<module>", …)`.

A test that matches the entry, or any `also_invoked_by` invoker, is listed. A test can appear on several paths.

**Records and output.**
- Each test is listed once per path. Its record keeps the minimum `Call.lineno` as `invocation_line`, chosen explicitly rather than by `ast.walk` order. On that line, the first match kind in the order above wins.
- Records are sorted by `(file, line, node_id)`. `node_id` is `<root-relative file>::[Class::]name`, so it carries the `repo-b/` prefix in multi-repository snapshots.
- Every path always has both lists. With no test files, both lists are `[]`.
- Two runs, including one with `os.scandir` order reversed, give byte-identical output.

**How to exercise it.** Run `map_paths` on fixtures holding argparse, click, `__main__`, project-script and hook entries plus their test suites. Then assert on `tests.e2e` and `test_listing_skips` in the written `paths.json`.

Interfaces:
- Produces: `def list_e2e_tests(index: SnapshotIndex, paths: Sequence[PathRecord]) -> tuple[dict[str, TestListing], tuple[TestListingSkip, ...]]` (keyed by path id)
- Consumes: Block 2: `class SnapshotIndex` with `def files(self) -> tuple[str, ...]`, `def parse(self, rel: str) -> ast.Module | None`, `def module_file(self, repo: str, dotted: str, extra_roots: Sequence[str] = ()) -> str | None`, `def module_name(self, rel: str) -> str | None` and `def repo_root(self, rel: str) -> str`
- Consumes: Block 1: `class PathRecord(id: str, entry: EntryRecord, hops: tuple[Hop, ...], stops_at: int | None, break_reason: OutputBreakReason | None, changed_lines_covered: tuple[Location, ...], tests: TestListing)`; `class TestListing(e2e: tuple[E2ERecord, ...] = (), mocks_hop_2: tuple[MockedE2ERecord, ...] = ())`; `class E2ERecord(node_id: str, file: str, line: int, match: E2EMatch, invocation_line: int)`; `class TestListingSkip(file: str, reason: Literal["decode_error", "syntax_error"])`; `class E2EMatch(StrEnum)`

Satisfies: e2e-test-listing (Listing runs statically with no model call and no execution; Test discovery scope; Direct handler invocation is recognised; CLI subcommand invocation is recognised; Subprocess, runpy and script invocation is recognised; Alternate invokers count; Listed test record shape; Deterministic ordering and deduplication; Empty lists are explicit; Unreadable test files are skipped with a reason); paths-output-contract (Test-listing shape, as amended)

## 9. Hop-2 patch classification: tests that mock the second hop move to `tests.mocks_hop_2`

This block extends `list_e2e_tests` so that a test is moved into `tests.mocks_hop_2` when it patches the path's hop-2 symbol. Such a test is written as a `MockedE2ERecord`, it is removed from `tests.e2e`, and its `patch_line` is the patch call's own line, which may be in a `conftest.py`. The hop-2 symbol comes from the second retained hop: the last identifier of `symbol_called`, together with the module named by its `citation.path`. If a path has fewer than two retained hops (a single-hop path, or a path cut after hop 1), no test moves, even when the test patches what the model originally proposed as hop 2.

**Which targets count as hop 2**
- `<defining module>.<symbol>`.
- `<hop-1 module>.<name bound by hop 1's imports>`.
- A test that patches only another symbol, such as `pkg.core.log`, stays in `tests.e2e`.

**Patch forms recognised**
- `mock.patch` / `unittest.mock.patch` / `patch` with a dotted string literal, used as a decorator, as a `with` item, or through `.start()` on a patcher created in the same function or fixture:
  - every item of a multi-item `with` counts, and each one uses its own `context_expr.lineno`, never `With.lineno`;
  - `async with` items count.
- `patch.object(<resolved obj>, "<name>")`.
- `monkeypatch.setattr` in either form:
  - dotted string: two positional arguments where the first is a string containing a dot;
  - object form: three positional arguments;
  - keyword arguments are accepted in both.

**Ignored, and leaving the test in `tests.e2e`**
- A `patch(...)` that is constructed but never started.
- A dotless string target.
- A non-literal or f-string target.
- A three-argument `setattr` whose first argument is a string.

**Patches applied by fixtures**
A patch applied in a fixture counts when the test either:
- requests the fixture by parameter name, honouring `name=` aliases, or
- the fixture is autouse: `autouse` is any truthy constant.

The fixture decorator may be written as `pytest.fixture`, `import pytest as pt`, or `from pytest import fixture as fx`. Lookup rules:
- Fixtures are looked up in the test module, then in each `conftest.py` from the test's directory up to the snapshot root.
- Each fixture name resolves to its nearest definition. A module-level, non-patching `foo` therefore overrides an autouse conftest `foo` that patches.
- Fixture-to-fixture requests are followed transitively with a visited set, so cycles terminate.
- A patch wrapped around a fixture's `yield` counts.
- A `conftest.py` that fails to decode or parse is recorded in `test_listing_skips` and contributes no fixtures.

**Exercising and verifying**
Run `map_paths` on a fixture suite containing:
- decorator, `with`, `.start()`, `patch.object` and both `setattr` patches;
- an unrequested non-autouse fixture;
- a requested `tests/conftest.py` fixture that calls `monkeypatch.setattr("pkg.core.run", ...)`.

Assert that every `mocks_hop_2` record has the expected `line`, `invocation_line`, `match` and `patch_line`. For example, a `@mock.patch("pkg.core.run")` decorator at line 9 on a test at line 10 that calls `main(["open"])` at line 12 gives `line` 10, `invocation_line` 12, `match` `cli_argv` and `patch_line` 9. Also assert that the two lists stay disjoint, which is checked by `write_paths_file`.

Interfaces:
- Produces: (none)
- Consumes: Block 8: `def list_e2e_tests(index: SnapshotIndex, paths: Sequence[PathRecord]) -> tuple[dict[str, TestListing], tuple[TestListingSkip, ...]]`
- Consumes: Block 1: `class MockedE2ERecord(E2ERecord)` adding `patch_line: int`; `class Hop(symbol_called: str, citation: Citation, note: str = "")`
- Consumes: Block 2: `class SnapshotIndex` with `def parse(self, rel: str) -> ast.Module | None`, `def module_name(self, rel: str) -> str | None` and `def module_file(self, repo: str, dotted: str, extra_roots: Sequence[str] = ()) -> str | None`

Satisfies: e2e-test-listing (Tests that mock hop 2 are listed separately; Hop-2 patches applied by fixtures are recognised; Listing applies to cut paths; Listed test record shape — "Record fields are populated"); paths-output-contract (Test-listing shape — "Mocked test listed separately")