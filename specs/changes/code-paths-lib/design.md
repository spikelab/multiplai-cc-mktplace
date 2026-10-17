## Context

`plugins/multiplai-dev/lib/code_facts/` is the shared library behind codebase-walkthrough, review, buildme and spec-extract.

- **What it does today.** It exports pydantic answer models (`Citation`, `Fact`, `Unit`, …) on a private `_Model` base that ignores unknown keys (`code_facts/models.py:21-23`). It provides an explore loop built on `ask` / `run_tasks` / `bounded`, which stores each answer as it arrives and skips keys that already have one (`code_facts/explore.py:109-142`). It provides `check_citation`, which resolves a path through the caller's `Resolve` and re-reads the quote with `git show` (`code_facts/explore.py:76-81`).
- **What callers supply.** Every caller passes two functions: a `CallFn` shaped like `agent_call_structured(prompt, schema, **kw)` and a `Resolve` (`code_facts/explore.py:31-32`). The library never calls a model SDK itself.
- **How it is installed.** It is not a uv workspace member. Four skills pull it in as an editable path dependency (`plugins/multiplai-dev/skills/buildme/scripts/pyproject.toml:51-55`).

The only path tracing today lives in codebase-walkthrough.

- **Seeds.** `stages/trace.py` seeds only from webhooks, routes and Celery tasks (`trace.py:35-38`).
- **Gate.** It checks hops with `trace_gate` (`gates.py:188-195`). Hop 1 must contain the seed line. Every later hop must be named in the previous hop's quote, and its lines must define that name or hold a boundary entry.
- **Gap.** On a CLI or skill repository such as this one, it finds nothing.

A prototype of this change ran against this repository. It established three things:

- **Output shape.** The `paths.v1` document shape, which D4 records as normative.
- **Live dispatch resolution.** pr_pipeline's `open` / `edit` / `render` resolve to `cmd_*` through the `COMMANDS` dict and the edge at `commands.py:409`.
- **Real traces on a CLI repo.** A CLI repo yields traced paths whose citations re-read at the commit.

The prototype also disproved six points of the draft design. Each is fixed below, using the same decision numbers as the draft so the fixes can be traced:

| Draft decision | What the prototype showed | Fixed in |
|---|---|---|
| D4 | Hop parity is impossible: the walkthrough `Hop` is flat and has no citation or side. A nested `Citation` is neither frozen nor `forbid`, and it leaks `id`. | D4 |
| D6 | 1. Hooks run as `sh run.sh <script>.py` all resolve to `run.sh`. 2. Anchoring `__main__` at line 1, combined with the "single called function" rule, makes `main_module` untraceable. | D6 |
| D7 | Merging on handler `(path, line)` collapses all 8 real hooks into one entry. | D7 |
| D11 | The name-in-cited-text rule forces a hop to cite its whole function (118 lines for `cmd_edit`). | D11 |
| D13 | Function-local imports are ignored. pr_pipeline needs them for dispatch-dict resolution and for reachability. | D13 |
| D15a | Literal-only argv matching finds none of the ~15 real pr e2e tests, which use a `run(argv)` helper. It also lists one test that never reaches the handler. | D15 |

## Decisions

### D1. Reading of the specs where they disagree

The seven capability specs contradict each other in several places. The implementation follows the readings in the table below. Each named spec scenario is amended to match, and the test for that scenario asserts the amended value.

| # | Conflict | Reading adopted | Spec text amended |
|---|---|---|---|
| 1 | Entry kinds. Discovery uses `argparse_subcommand` and similar names. The output contract uses `argparse` and similar. The fallback spec uses "`other`" and accepts `route`. | `discover_entries()` returns the discovery-spec kinds. `paths.json` uses the contract kinds through a fixed map (D5). A fallback answer's kind becomes `other` inside the fallback answer model. Every fallback entry is written as kind `model`. | None. Each spec holds at its own layer. |
| 2 | `stops_at`. path-tracing (`20` at the hop limit) and the contract ("index of last hop kept, ≤ len(hops)") disagree with trace-gate (`21`, and "index of first broken hop"). | `stops_at` is the 1-based index of the **last retained hop** (D12). | trace-gate "Over-long trace" changes from `stops_at = 21` to `stops_at = 20`. "Cut after re-ask" changes from `stops_at = 3` to `stops_at = 2`. |
| 3 | trace-gate "Cut at hop 1" returns a path with zero hops. The contract requires `hops` to have 1 to 20 items. | A trace with zero retained hops is never a path. It becomes a drop with stage `trace` (D12). | trace-gate "Cut at hop 1": the path is dropped, not returned. |
| 4 | Malformed answers. path-tracing drops them. trace-gate breaks at hop 1 and re-asks once. | Re-ask once. If the re-asked answer is still malformed, drop the entry with stage `trace` and reason `malformed_answer`. | None. path-tracing's scenarios still hold, because the entry is dropped and the run returns. |
| 5 | `CallFn` exceptions. path-tracing drops the entry. Fallback and re-ask let the exception propagate. | One rule at every call site: an exception whose class is in the caller's `recoverable` tuple is recorded, and any other exception propagates. `recoverable` defaults to `()` (D17). | path-tracing "No library-level retry": the test passes `recoverable=(RuntimeError,)`. |
| 6 | Drop vocabulary. scope reasons, the contract's five stages, the fallback reasons and the tracing reasons all differ. | A drop is `{stage, reason, detail, entry}`. `stage` and `reason` are closed codes (D4). Two stages are added to the contract's five: `model_fallback_duplicate` and `trace`. | paths-output-contract: stage enum and sort order. |
| 7 | Test listing. The e2e spec has records sorted by file, line and node_id. The contract has bare test ids sorted lexically. | Records under `tests.e2e` and `tests.mocks_hop_2`, sorted by `(file, line, node_id)`, as the prototype emitted. A mocked record also gets `patch_file`, because `patch_line` "may be in a fixture file". | paths-output-contract "Test-listing shape". |
| 8 | Proposal and discovery spec: pr_pipeline handlers are `null` because it dispatches on `args.command`. The live code dispatches through `COMMANDS[args.command](args)` in `commands.py`. | Both cases are tested. A comparison-dispatch fixture expects `handler == null`. A live-shaped fixture, with `COMMANDS` in a sibling module imported function-locally, expects `cmd_*`. `location.line` is the `add_parser` line in both cases. | None. The spec fixture is literally comparison-dispatch. |
| 9 | Contract: hop field names equal the walkthrough `Hop`'s, "except that the citation field is typed as `code_facts.Citation`". The walkthrough `Hop` has no citation field, so this is infeasible as written. | Flat hop fields: the walkthrough `Hop` fields, then `side` (D4). | paths-output-contract "Field names match". |
| 10 | The contract's `map_paths` signature has no way to re-read citations at a commit in a snapshot that may hold several repositories. | `map_paths` takes a required `resolve: Resolve`, as `explore` does. `commit` is the label written to `paths.json`. | paths-output-contract `map_paths` inputs. |
| 11 | Fallback scope "otherwise the caller's scope paths". No such parameter exists. | When unscoped, the fallback scope is the whole snapshot. | entry-point-model-fallback "Single fallback agent per run". |
| 12 | Package `__main__.py` is located at line 1. | The location is the `if __name__ == "__main__":` line when one exists, and line 1 otherwise (D6). | discovery table. Also "Command naming no subcommand", which now points at the `main_module` location. |
| 13 | The e2e spec says a test "SHALL be listed" whenever it invokes the entry. | Narrowed: tests that cannot reach the handler are excluded (D15). | e2e-test-listing "CLI subcommand invocation". |
| 14 | A hook target that is "any other existing file" gets handler line 1, so `run.sh` swallows every hook. | Hook commands are resolved through shell launchers to the script they run (D6). | entry-point-discovery hook requirement. |

### D2. Module layout

All new modules go under `plugins/multiplai-dev/lib/code_facts/code_facts/`:

| Module | Contents |
|---|---|
| `pyindex.py` | Single `ast` pass over the snapshot. Holds module naming, import resolution (including function-local imports), symbol tables, enclosing-scope spans, the import graph and reachability (D13). |
| `entries.py` | `discover_entries()`, the internal `Entry`, `Diagnostic` and `KnownEdge` records, and the merge logic (D6, D7). |
| `trace_gate.py` | `gate_hops()`, a pure function (D11). |
| `e2e_tests.py` | `list_e2e_tests()`, which parses tests with `ast` only (D15). |
| `paths.py` | Output models (D4), `map_paths`, fallback, scope, cap, ids and `write_paths_file`. |
| `prompts.py` (modified) | Adds `paths_fallback()`, `paths_trace()` and `paths_reask()`, plus the version constants. |
| `__init__.py` (modified) | Adds exports. Every existing export is unchanged. |
| `schema/paths.v1.schema.json` | Inside the package (D16). |

- **Alternatives considered.**
  - The two modules the proposal named (`entries.py`, `paths.py`). This gives about 3 kLOC in two files, and entries, gate, scope and e2e listing would each re-parse Python.
  - Additions to `models.py`. That mixes strict output models into a file of lenient answer models.
- **Why chosen.**
  - `pyindex` is needed by four consumers: discovery, reachability, the gate's scope rule and test listing. Parsing once keeps them consistent.
  - The gate stays importable without pydantic output models, so PR 2 can replace walkthrough's `trace_gate` with it.
  - The package keeps its existing flat `code_facts/code_facts/` layout. This departs from `python-project-structure.md`'s src layout, because four members already install it as an editable path dependency and moving it is out of scope.

### D3. Public API

```python
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
) -> PathsResult

def write_paths_file(result: PathsResult, path: Path) -> Path

def discover_entries(root: Path, seeds: Sequence[Seed] = (), *, index: PyIndex | None = None) -> Discovery
```

**Calling contract.**
- `map_paths` is `async`, because `CallFn` is async. Every argument is keyword-only.
- If `call` or `resolve` is missing, the call raises `TypeError` before any work starts (contract "Missing required argument").
- The following raise `ValueError` before any I/O or `CallFn` call:
  - A `changed_lines` path that is absolute, escapes `root`, or has a line below 1.
  - `cap` or `trace_limit` below 1 when `changed_lines is not None`.
- Paths in `changed_lines` are normalised to POSIX form with no leading `./`.
- `call_kwargs` (model, effort, max_turns, and so on) is passed to every `CallFn` call. The library then overwrites three keys:
  - `allowed_tools = ["Read", "Grep", "Glob"]`
  - `cwd = str(root)`
  - `budget_label`

  Read-only tools therefore cannot be widened through `call_kwargs`.
- `map_paths` writes nothing outside `cache_dir`.

**Pipeline order.**
1. Validate the arguments.
2. Build the `PyIndex`.
3. Run discovery.
4. Run the fallback.
5. Apply reachability and `trace_limit` (scoped runs only).
6. Run the first trace asks.
7. Gate the first answers.
8. Re-ask broken traces.
9. Gate the re-asked answers.
10. Cut the traces.
11. Apply coverage, ordering and cap, then assign ids.
12. List e2e tests for the kept paths.
13. Assemble the result.

**Rejected alternatives.**
- A synchronous wrapper that runs its own event loop. Callers are already async (review, walkthrough and spec-extract stages).
- Reading files through git instead of `root`. Discovery and test listing would then shell out once per file.

### D4. Output models and the `paths.v1` shape

**Model bases.**
- Output models derive from `class _Strict(_Model)` with `model_config = ConfigDict(extra="forbid", frozen=True)`. They are therefore still subclasses of `_Model` (contract "Model base").
- Answer models stay lenient, with two exceptions:
  - `FallbackAnswer` is lenient, because unknown fields and kinds must be tolerated.
  - `TraceAnswer` and `TraceHop` use `extra="forbid"`, so a branching answer (`alternatives`, nested `hops`) fails validation. That failure leads to one re-ask (D1#4). This departs from the "answer models ignore unknown keys" convention, because the path-tracing spec requires that branching be rejected.

**Hop (fixes draft D4).**
- `PathHop` declares the fields of `walkthrough_pipeline/models.py:122-128` `Hop`, with the same names, types and order, followed by `side: Literal["head"] = "head"`.
- No `Citation` is embedded. Citation checks build a throwaway `Citation` inside `trace_gate`, so `Citation.id` never reaches output.
- The contract test asserts: `set(PathHop.model_fields) == set(walkthrough Hop fields) | {"side"}`. The test reads walkthrough's field names by parsing `models.py` with `ast`, so the library never imports walkthrough.
- **Rejected alternative:** a nested `citation: Citation`. It is not frozen and not `forbid`, it serialises `id`, and it has no parity with walkthrough. PR 2 would have to convert it anyway.

**Normative field order.** This is the shape the prototype emitted. The serialiser order equals the model declaration order, which equals the schema `properties` order.

```json
{
  "schema_version": "paths.v1",
  "commit": "<sha>",
  "scoped": true,
  "cap": 10,
  "fallback": {"status": "ok", "reason": null},
  "paths": [{
    "id": "p1",
    "entry": {
      "kind": "argparse",
      "name": "open",
      "location": {"path": "plugins/…/pr_pipeline/__main__.py", "line": 41},
      "handler": {"path": "plugins/…/pr_pipeline/commands.py", "line": 180, "symbol": "cmd_open"},
      "invocation": null,
      "also_invoked_by": [{"kind": "skill_command", "name": "pr: pr_pipeline open", "location": {"path": "…/SKILL.md", "line": 30}}]
    },
    "hops": [{"<walkthrough Hop fields, in order>": "…", "side": "head"}],
    "stops_at": null,
    "break_reason": null,
    "changed_lines_covered": [{"path": "…", "line": 10}],
    "tests": {
      "e2e": [{"node_id": "…::test_open", "file": "…", "line": 10, "match": "cli_argv", "invocation_line": 12}],
      "mocks_hop_2": [{"node_id": "…", "file": "…", "line": 10, "match": "cli_argv", "invocation_line": 12, "patch_file": "…", "patch_line": 9}]
    }
  }],
  "dropped": [{"stage": "cap", "reason": "over_cap", "detail": "rank 11 of 14", "entry": {"…": "entry object"}}],
  "diagnostics": [{"path": "…", "line": 12, "reason": "dynamic_name", "detail": "…"}],
  "test_listing_skips": [{"file": "tests/test_broken.py", "reason": "syntax_error"}]
}
```

**Field rules.**
- `fallback.status` is one of `ok`, `failed`, `skipped`. `fallback.reason` is a non-empty string when the status is `failed`, and `null` otherwise.
- `handler.symbol` is nullable (see D7).
- `known_edges` is internal and is not written.
- Drop `stage` is one of the following, in sort order. Each stage has a closed set of `reason` codes:

| Stage | Reasons |
|---|---|
| `model_fallback_citation` | `missing_citation`, `citation_unverified` |
| `model_fallback_duplicate` | `duplicate` |
| `import_reachability` | `unreachable` |
| `trace_limit` | `over_trace_limit` |
| `trace` | `call_failed`, or any trace-gate break code |
| `no_changed_line` | `no_changed_line_covered` |
| `cap` | `over_cap` |

- `detail` is free text for humans and is never parsed.
- Drops are sorted by stage (order above), then `entry.location.path`, then `entry.location.line`, then `entry.kind`.
- In unscoped runs, no drop has stage `import_reachability`, `trace_limit`, `no_changed_line` or `cap`.

### D5. Kind vocabularies

| Discovery kind (`discover_entries`) | Output kind (`paths.json`) | Merge precedence and ordering rank |
|---|---|---|
| `argparse_subcommand` | `argparse` | 1 |
| `click_command` | `click` | 2 |
| `typer_command` | `typer` | 3 |
| `main_module` | `main` | 4 |
| `project_script` | `script` | 5 |
| `skill_command` | `skill_command` | 6 |
| `hook_command` | `hook` | 7 |
| `seed` | `seed` | 8 |
| (fallback) | `model` | 9 |

- One constant, `KIND_RANK`, drives three things: merge precedence (D7), pre-trace ordering (D9 in scope) and output ordering (D14). A test covers the ranking.
- The merge precedence is the proposal's "assumption to confirm". It is kept, because the most specific invoker should name the path.
- `also_invoked_by` records use output kinds in `paths.json`, and discovery kinds in `discover_entries()`.

### D6. Discovery rules (fixes draft D6)

Discovery reads files from `root` only. It never imports or executes target code.

- **Skipped directories:** `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `site-packages`, `build`, `dist`.
- **Traversal:** `os.walk(followlinks=False)`. A symlinked file whose `resolve()` falls outside `root` is skipped.

**Rule 1: argparse.**
- An entry is emitted per `add_parser(<str literal>)` on a value bound from `add_subparsers(...)`. Nested names are joined with a space.
- A non-literal name produces a `dynamic_name` diagnostic.

**Rule 2: argparse handlers.**
- Resolved through `set_defaults(func=<name>)` on the subparser.

**Rule 3: dispatch dicts.**
- A dict literal maps the subcommand's literal name to a function reference.
- The search covers the parser's module and every same-repository module it imports, including function-local imports (D13). This is how the live `COMMANDS` in `commands.py` is found.
- The known edge runs from the dispatch call (`args.func(...)` or `<DICT>[...](...)`, for example `commands.py:409`) to the handler's `def`.
- `args.command == "x"` comparisons are never resolved.

**Rule 4: click and typer.**
- As specified. `location` is the decorator line, and `handler` is the decorated `def`.

**Rule 5: `[project.scripts]`.**
- As specified. The module search runs under the repository root, then `<root>/src`, same repository only.

**Rule 6: `main_module`.**
- **Location.** For a package `__main__.py`, the location is the `if __name__ == "__main__":` line when present, and line 1 otherwise. A non-test module with a guard uses the guard line.
- **Handler.** Take the calls in the guard body, or in the module body when there is no guard. Remove calls whose callee is one of the transparent wrappers: `sys.exit`, `exit`, `SystemExit`, `asyncio.run`, `raise SystemExit(...)`. Look inside their arguments instead. Keep only callees that resolve to a function in this module or in a same-repository module. If exactly one remains, it is the handler (`sys.exit(main(sys.argv[1:]))` resolves to `main`). Otherwise the handler is `null`.
- **Known edge.** From the location line to the handler's `def`.
- **Why.** Line-1 anchoring combined with a raw "single call" rule made pr_pipeline untraceable.
- Test files are excluded as specified.

**Rule 7: `SKILL.md`.**
- **Blocks scanned.** Fenced blocks with info strings `bash`, `sh`, `shell`, `zsh` or `console`. Commands are tokenised with `shlex` after joining `\`-continuations.
- **Prefixes accepted.** Environment assignments, `cd <dir> &&`, and `uv run [--directory D | --project D | other flags]`. `--directory` and `--project` add `D` as a search root at the same priority as `cd`.
- **Variables.** `${CLAUDE_SKILL_DIR}` expands to the `SKILL.md` directory. `${CLAUDE_PLUGIN_ROOT}` expands as for hooks.
- **Known edge target.**
  - If the first positional argument after the module names a discovered subcommand of that module, the edge goes to that subcommand's handler, or to its `add_parser` line when the handler is `null`.
  - Otherwise the edge goes to the `main_module` location.
- **Handler.** The edge target's `def` when it is one, and `null` otherwise.

**Rule 8: hooks.**
- **Sources.** `hooks.json` and `.claude/settings*.json`, for `"type": "command"` hooks only.
- **Launcher resolution (new).** The command is tokenised after variable expansion. Leading tokens are stripped while they are an interpreter or launcher: `sh`, `bash`, `/bin/sh`, `/bin/bash`, `exec`, `env`, a `.sh` file that exists under `root`, or `uv run` with its flags. Resolution then continues on the remaining tokens:
  - `python[3] -m X`, or `-m X` after a launcher, resolves as for `SKILL.md`.
  - The first `.py` token that exists resolves as a `main_module`-style script: guard line and single-call handler.
  - Only when no `.py` or `-m` target exists does the hook target the launcher or other file itself, at line 1 with `symbol: null`.
- **Effect.** The 8 real `sh run.sh <script>.py` hooks resolve to 8 different scripts.

**Rule 9: seeds.**
- As specified: `handler` is the enclosing `def` at the seed's line, or the named symbol.
- Failures produce a `seed_not_found` diagnostic.

**Diagnostics.**
- Every examined-but-not-emitted candidate produces exactly one diagnostic. Reasons: `parse_error`, `unresolved_target`, `unresolved_handler`, `dynamic_name`, `seed_not_found`.
- An emitted entry with a `null` handler gets one `unresolved_handler` diagnostic, except argparse subcommands dispatched by comparison. Those are expected, so they get no diagnostic. This means the spec's mixed-failure scenario still counts exactly five.

**Alternatives considered.**
- Model-only discovery. Not deterministic, and costs more.
- Importing modules to introspect parsers. This executes target code, which is forbidden.

### D7. Merging entries (fixes draft D7)

- **Merge key.** Entries are merged on `(handler.path, handler.line)` only when `handler.symbol` is not `null`, meaning the handler is a Python `def`.
- **No merge.** Entries whose handler is `null`, or whose handler is a non-`def` target (launcher or file line 1), merge only with entries that have an identical `(kind, location)`. Two hooks that share a launcher therefore stay separate.
- **Kept entry.** The entry with the best `KIND_RANK`. Ties go to the smallest `(location.path, location.line)`.
- **Other entries.** The rest go to `also_invoked_by`, sorted by `(path, line, kind)`. Their known edges are unioned with duplicates removed.

**Rejected alternative:** merging on any resolved target. The prototype showed this collapses unrelated hooks into one entry.

### D8. Model fallback

- **When it runs.** One `CallFn` call after discovery. It is skipped (status `skipped`) when `fallback=False`.
- **Prompt (`paths_fallback`).** It contains:
  - the scope (the changed files, or the snapshot when unscoped);
  - every deterministic entry, sorted by location;
  - the existing citation and untrusted-content rules from `prompts.py`;
  - a request for entry points that are not already listed.
- **Answer.** `FallbackAnswer.entries: list[FallbackEntry]`, where each entry has `kind`, `name`, `citation: Citation`, `reason`. A kind outside the known set is normalised to `other`.
- **Processing order. The first failing step decides the drop:**
  1. No citation: `model_fallback_citation` / `missing_citation`.
  2. The citation does not re-read via `check_citation` and `resolve`, its path is absolute or escapes `root`, or it is out of range: `model_fallback_citation` / `citation_unverified`.
  3. Duplicate: `model_fallback_duplicate` / `duplicate`. This applies when the cited line equals the location of a deterministic or seed entry. It also applies when the cited line equals any known-edge `from` line or handler line of such an entry, or when the cited line equals the line of an earlier fallback entry in location order.
- **Kept entries.** Written as kind `model`, with location set to the cited line. `handler` is the enclosing `def` when the cited file is Python and the line is inside one, and `null` otherwise. Kept entries are sorted by location.
- **Answer handling.** The answer `CallFn` returns is accepted in three forms: a `FallbackAnswer` instance, a dict, or a JSON string. Dicts and strings are validated here. A `ValidationError` gives status `failed` with a reason, and the run continues with deterministic entries only.

**Rejected alternative:** one fallback agent per scope file. This multiplies cost, and the spec caps it at one call.

### D9. Answer cache, built on `ask` / `run_tasks`

- **Storage.** `ask` and `run_tasks` are reused unchanged. The `answers` mapping passed to them is a `_DiskAnswers(MutableMapping)`. Each `__setitem__` writes `<cache_dir>/paths-answers/<sha256>.json` atomically (temp file in the same directory, `fsync`, `os.replace`). At start-up, every file in that directory is loaded. When `cache_dir is None`, the mapping is a plain in-memory dict.
- **Stored format.** Answers are stored as `model_dump(mode="json")`. They are re-validated into answer models whenever they are read.
- **Failed answers.** `{"error": …}` entries are deleted at the start of each `map_paths` call. A failed call is therefore re-asked on the next run, and never retried within a run. This avoids the "failed key is never asked again" trap noted for `explore.ask`.
- **Keys.** Each key is the sha256 of canonical JSON (`sort_keys=True`, `separators=(",", ":")`):

| Key | Inputs |
|---|---|
| `fallback` | `PATHS_FALLBACK_PROMPT_VERSION`, `commit`, the sorted deterministic entry identities, the sorted scope |
| `trace` | `PATHS_TRACE_PROMPT_VERSION`, `commit`, `(kind, location.path, location.line, handler)`, the sorted changed lines, the entry's sorted known edges, the sorted `places`, `20` |
| `reask` | `PATHS_REASK_PROMPT_VERSION`, the trace key, the sha256 of the first answer, the broken index, `break_reason` |

- **Effect.** Input order, completion order and concurrency never change a key. Byte-identical reruns and interrupted runs resume, as the path-tracing "Resumable runs" and contract "Reproducible run" scenarios require.

### D10. Trace requests

- **When.** One request per entry that survives scope, through `run_tasks(…, concurrency=concurrency)`. `run_tasks` uses `bounded(...)`. The default `concurrency=4` matches the existing callers.
- **Budget labels.** `paths:fallback`, `paths:trace:<key[:12]>`, `paths:reask:<key[:12]>`.
- **Prompt (`paths_trace`).** It contains:
  - the entry's kind, location and handler symbol;
  - the commit;
  - the changed files and line ranges, or the statement "no change scope; trace to the last effect";
  - the known edges and `places`;
  - the hop limit of 20;
  - an instruction to return one linear `hops` list that cites head-side code.
- **Effect.** Hop citations may now be short (the call site plus the callee's `def`), because the gate checks connectivity against the enclosing scope (D11).

### D11. Trace gate (fixes draft D11)

`gate_hops(hops, *, root, resolve, entry, known_edges, places) -> GateResult(accepted: list[PathHop], broken_at: int | None, break_reason: str | None, detail: str)` is pure and makes no model call.

- **Before the checks.** It truncates to 20 hops, recording `hop_limit` if any hops were cut.
- **Checks per hop, in this order:**
  1. `path_outside_root`: the path is absolute, contains a `..` or `\`, or normalises outside `root`.
  2. `missing_file`: the path is not a file under `root`.
  3. `line_out_of_range`: start is below 1, end is below start, or end is past EOF.
  4. `text_mismatch`: `check_citation` through `resolve`, using a transient `Citation` with side `head`.
  5. `base_side_citation`.
  6. `entry_mismatch` (hop 1 only). The cited range must include `entry.location.line`, or the handler's `def` line when it lives in the same file.
  7. `unconnected_hop` (hop n+1). The hop passes if any of these holds:
     - **Name reference.** The last dotted component of hop n+1's symbol appears as a `NAME` token in the scope that encloses hop n's first cited line. The enclosing scope is the innermost `def`/`class`, or the module, from `pyindex`. Tokens come from `tokenize`, so comments and strings never match. When hop n's file is not Python, the hop n quote is matched on word boundaries instead.
     - **Known edge.** A known edge whose `from` line lies in hop n's enclosing scope (or cited range, for non-Python) and whose `to` line lies in hop n+1's cited range.
     - **Place.** A `place` lies in hop n+1's cited range. The citation must still re-read, because checks 1–4 run first.
  8. `repeated_hop`: same path and same line range as an earlier hop.
- **Stopping.** The first broken hop stops the check.
- **Not imported.** The gate does not import `walkthrough_pipeline`.

**Alternatives considered.**
- The walkthrough rule (name in hop n's quote). Disproved: it forces whole-function citations.
- Requiring a separate `call_site` citation per hop. This breaks parity with the walkthrough `Hop`.

### D12. Re-ask, cut and `stops_at`

| Situation | What happens | Result |
|---|---|---|
| The first answer passes. | Nothing more. | Path with `stops_at` and `break_reason` both `null`. |
| The only defect is `hop_limit`. | No re-ask. | `stops_at = 20`, `break_reason = "hop_limit"`. |
| Any other break, or a malformed or empty answer. | One `reask`. The prompt carries the broken index, the code, the detail and the accepted prefix. The re-asked answer is gated from hop 1 and replaces the first answer. | See the next two rows. |
| The re-asked answer passes. | Nothing more. | The re-asked hops, with no cut. |
| The re-asked answer breaks at hop k. | Hops 1..k−1 are retained. | `stops_at = k−1`, `break_reason = <code>`. |
| k−1 = 0. | The entry is dropped. | Stage `trace`, `reason = <code>`. |
| A recoverable `CallFn` exception on the first ask or the re-ask. | The entry is dropped. | Stage `trace`, `reason = "call_failed"`. |

- **Coverage and listing.** Coverage (D14) and test listing (D15) see only retained hops, so hop 2 is taken from the retained hops.
- **Model check.** The model validator enforces that `stops_at` and `break_reason` are both `null` or both set, with `1 ≤ stops_at ≤ len(hops)`.

### D13. `PyIndex`: imports, resolution and reachability (fixes draft D13)

- **Single pass.** One `ast.parse` per `.py` file, with the same directory exclusions as D6. A file that cannot be decoded or parsed has no outgoing imports, and produces a discovery `parse_error`.
- **Repository root.** The nearest ancestor at or below `root` that contains `.git` or `pyproject.toml`, falling back to `root`. Each skill's `scripts/` directory is therefore its own repository (D19).
- **Imports collected at every nesting level.** This covers function bodies, `try` blocks and methods. Imports inside `if TYPE_CHECKING:` are excluded, because they never run.
  - **For the import graph:** every collected import adds an edge from the file.
  - **For name resolution:** a function-local import binds only inside that function's scope. Resolving `COMMANDS`, `cmd_open` or `commands` inside `main()` uses the function's bindings first, then the module's.
- **Absolute import resolution.** Search `<repo>/`, then `<repo>/src/`, then the importing file's own directory (script-style `sys.path[0]`, as in multiplai-context's `from lib import …`).
  - **Discovery handler resolution:** stops there (same repository only, per the spec).
  - **Reachability and test-listing resolution:** fall back to the other repository roots (and their `src/`) in sorted order. This lets a skill reach `code_facts` through its path dependency.
- **Relative imports.** Resolved by package path.
- **Package edges.** `import a.b.c` also adds edges to `a/__init__.py` and `a/b/__init__.py`. `from x import y` adds an edge to `x`, and also to `x/y.py` when that is a module.
- **Reachability.** For each entry:
  - Distance is `0` when `location.path`, any `also_invoked_by` location, or the handler file is a changed file.
  - Otherwise, a BFS runs from the handler's module (or the location's module when the handler is `null` and the location is Python) over import edges. The distance is the length of the shortest path to a changed `.py` file.
  - With no path, the entry is dropped as `unreachable` before any call.
  - The BFS tracks visited modules, so import cycles terminate.

### D14. Scope, ordering, cap and ids

- **Pre-trace order (scoped runs only).** Reachable entries are sorted by `(distance, KIND_RANK, location.path, location.line)`. Entries beyond `trace_limit` are dropped as `over_trace_limit`.
- **Coverage.** A hop covers a changed line when its path equals the normalised changed path and the line falls within the hop's range, inclusive. Only retained hops count.
  - `changed_lines_covered` is the distinct `(path, line)` set, sorted.
  - A traced path that covers nothing is dropped as `no_changed_line_covered`.
- **Output order.** Kept paths are sorted by `(−len(changed_lines_covered), len(hops), KIND_RANK, location.path, location.line)`.
  - In unscoped runs, coverage is empty for every path, so the first key is a tie.
- **Cap.** Paths beyond `cap` (scoped runs only) are dropped as `over_cap`.
- **Ids.** `p1…pN` are assigned last, in array order.
- **Accounting invariant (tested).** `len(paths) + len(dropped) == len(merged deterministic and seed entries) + len(entries the fallback agent listed)`.

### D15. End-to-end test listing (fixes draft D15a)

- **Inputs.** `test_*.py` and `*_test.py` under `root`, skipping `.git`, `.venv`, `venv`, `node_modules`, `__pycache__` and `site-packages`.
- **Unreadable files.** `decode_error` and `syntax_error` go to `test_listing_skips`.
- **Not run.** Nothing is imported, executed, collected or spawned.
- **Which functions count as tests.** As specified: module-level `test*` functions and `test*` methods of module-level `Test*` classes.

**15a. Invocation matching.**

- **Match forms.** Each match kind is as specified: `direct_call`, `cli_argv`, `cli_runner`, `subprocess`, `runpy`.
  - For `cli_argv`, the program's function is one defined in the subcommand's module whose body calls `.parse_args(` or `.parse_known_args(`.
- **Helper inlining (new).**
  - **What counts as a helper.** A function defined in the test module, in a `conftest.py` in scope, or in a non-test module under the same `tests/` tree.
  - **When it applies.** The test calls the helper with literal string-constant list or tuple arguments, and the helper's body contains a match form whose argv is one of its parameters. That includes `p`, `[*prefix, *p]`, `["x", *p]` and `list(p)`.
  - **Effect.** The literal is substituted and matched. Inlining goes at most 2 levels deep.
  - **What is recorded.** `invocation_line` is the test's call line, and `match` is the inner form's kind.
  - **Unchanged.** A non-literal argv at the test level still does not match, so "Non-literal argv does not match" holds.
- **Exclusions (new). A test is not listed for a path when any of these holds:**
  1. Its argv contains `-h`, `--help` or `--version`.
  2. It patches (D15b forms) the entry's handler, or a function containing one of the entry's known-edge `from` lines. In both cases the handler never runs.
- **Alternate invokers.** As specified: invoking any `also_invoked_by` invoker counts.

**15b. Hop-2 mocks.**
- **Recognised patch forms.** As specified: `mock.patch` / `unittest.mock.patch` (as decorator, context manager or `.start()`), `mock.patch.object`, and `monkeypatch.setattr`. The `autouse` and requested-fixture rules apply across the test module and every `conftest.py` up to `root`.
- **Matching hop 2.** A target matches hop 2 when it names hop 2's symbol either at its defining module or under a name bound in hop 1's module. Bindings come from `pyindex`, including function-local `from … import`.
- **Effect.** The test moves to `mocks_hop_2` with `patch_file` and `patch_line`.

**Order and dedup.** As specified: each node_id appears at most once, the record uses the earliest `invocation_line`, ties are broken by the match-kind order, and records are sorted by `(file, line, node_id)`. A test cannot appear in both lists.

**Rejected alternative:** running pytest collection. Forbidden: it imports and executes test modules.

### D16. Schema and writing

- **Location.** The schema file is `code_facts/schema/paths.v1.schema.json`, inside the package, so editable and wheel installs both find it through `importlib.resources.files("code_facts")`.
  - **Departure:** review keeps `facts.v1` outside its package (`skills/review/schema/`). That is fine for a skill but not for a library loaded at runtime.
- **Header.** `$schema` is copied verbatim from `facts.v1.schema.json`. `$id` is facts.v1's `$id` with `facts.v1` replaced by `paths.v1`. It also has a `title`.
- **Structure.**
  - `$defs` cover `location`, `handler`, `entry`, `invoker`, `hop`, `path`, `e2eTest`, `mockedE2eTest`, `drop`, `diagnostic` and `skip`.
  - Every object sets `additionalProperties: false`.
  - `location.path` matches `^(?!/)(?!.*(^|/)\.\.?(/|$))[^\\]+$`, and `line` has `minimum: 1`.
- **`write_paths_file` steps.**
  1. `text = json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"`.
  2. Validate `json.loads(text)` with `jsonschema.validators.validator_for(schema)`. On failure, raise `ValueError` naming `"/" + "/".join(map(str, err.absolute_path))`. Nothing is written.
  3. Write a temp file in the target's directory, `fsync` it, then `os.replace` it over the target. Any exception unlinks the temp file and re-raises.
- **Round trip.** `model_validate_json` followed by a re-write gives identical bytes, because the field order is the declaration order and there are no float or dict ordering hazards.

### D17. Error handling

| Source | Behaviour |
|---|---|
| Discovery parse or resolution failures | Diagnostics. Never raised. |
| Unreadable test files | `test_listing_skips`. Never raised. |
| Bad arguments | `ValueError` or `TypeError` before any call. |
| `CallFn` raises a class in `recoverable` | Fallback: status `failed`. Trace or re-ask: drop with stage `trace` and reason `call_failed`. |
| `CallFn` raises anything else | Propagates unchanged, for example the caller's `RepoTrustError` or `BudgetExceededError`. No retry, no budget. |
| `CallFn` returns an invalid answer | Fallback: `failed`. Trace: one re-ask, then a drop. |

- Callers whose `CallFn` raises its own parse error (`AgentCallError`, `CallFailed`) put that class in `recoverable`.
- Logging uses `log = logging.getLogger(__name__)` with %-style arguments, as `code_facts/explore.py:26` does. This departs from `python-project-structure.md`'s structlog guidance, because the codebase convention wins inside an existing package.

### D18. Dependencies and tests

**Dependencies.**
- Add `jsonschema>=4` to the code_facts `[project] dependencies`. It is a runtime dependency, because `write_paths_file` validates at runtime.
- Run `uv lock` at the repository root. The root `uv.lock` changes as a consequence, and nothing else does.
- Stdlib modules used: `ast`, `tokenize`, `tomllib`, `json`, `shlex`, `hashlib`.
- Commands follow `uv-python-best-practices.md` (`uv run`, lockfile).

**Tests.**
- Location: `plugins/multiplai-dev/lib/code_facts/tests/`.
- Run with: `uv run --directory plugins/multiplai-dev/lib/code_facts python -m pytest tests/`, with `asyncio_mode="auto"` already configured.

**Fixtures.**
- Fixture trees are built in `tmp_path` from Python dicts by a `make_tree(tmp_path, files)` helper. No committed fixture contains a real `pyproject.toml`, `SKILL.md` or `hooks.json`, because `scripts/lint_workspace.py` and `scripts/lint_skills.py` would pick them up and fail CI.
- Gate tests commit their trees to a temp git repo with a fixed author environment, and use a `resolve` that maps paths to `(repo, sha, rel)`.
- One test copies the live `plugins/multiplai-dev/skills/pr/scripts/` into `tmp_path`. It asserts `open`, `edit` and `render` resolve to `cmd_*`, with the edge from `commands.py:409`. It is skipped if that directory is absent.

**Fakes.**
- `FakeCall` records the prompt, schema, kwargs and in-flight count, and returns canned answers keyed by the `budget_label` prefix. It can be set to raise.

### D19. Decisions on the prototype's open questions

| Question | Decision | Why |
|---|---|---|
| Are seeds exempt from `cap`? | **No.** Seeds rank 8 and are capped like any entry. | One deterministic rule. A caller that needs every seed passes `cap ≥ len(seeds)`, or runs unscoped. |
| Deduplicate paths with an identical hop suffix? | **No.** Every entry keeps its own path. The fallback does treat a cited line that equals a known invoker or edge line as `duplicate` (D8). | Paths are per entry by contract. Grouping by suffix is a consumer concern, for example in review-viewer. |
| Hook entry `name`? | `<event>:<matcher>`, or `<event>`, per the spec. Names are labels, not unique keys. Identity is `(kind, location)`. | Keeps the spec scenarios. The `location` line and `invocation` tell entries apart. |
| argparse `invocation`? | `null`. Only `skill_command` and `hook` carry `invocation`. | It is literal text from the source. Synthesising `python -m pr_pipeline open` would be a guess. |
| Structured `stops_at` / `break_reason` / `also_invoked_by` on drops? | **No new fields.** `reason` is a closed code (the break code for `trace` drops). `also_invoked_by` is already inside `entry`. `detail` is human text. Hops of dropped traces are not kept. | No consumer needs a dropped path's hops. A fixed four-field shape keeps the schema small. |
| `Citation.id` in hop output? | **No.** Hops are flat (D4). | Ids are an explore concept, and the walkthrough `Hop` has none. |
| Does each skill's `scripts/pyproject.toml` make it a separate repository? | **Yes, for discovery handler resolution.** The import graph and test listing fall back across repositories (D13). | Same-repository handler resolution prevents `tool` collisions (spec scenario). Cross-repository reachability keeps path dependencies such as `code_facts` reachable. |

## Risks / Trade-offs

- **Static analysis is approximate.**
  - These are invisible: `getattr` dispatch, plugin registries, `importlib` and `sys.path` edits outside the script directory.
  - The fallback agent covers part of the gap. An entry missed by both is silently absent, and recall on a real repository is not proven in this PR (non-goal).
- **Cross-repository import fallback can bind the wrong module** when two repositories share a top-level package name. The first sorted root wins. The cost is extra reachable entries, never fewer.
- **The enclosing-scope connectivity rule is weaker than "cited call site".** A hop is accepted if the callee's name appears anywhere in the caller's function, even on a branch the path does not take. This is accepted to allow short citations. Review finders still read the code.
- **Snapshot and commit must match.** Structure checks read `root`, while text checks re-read via git. If `root` is not the extraction of `commit`, citations fail as `text_mismatch`. Callers already snapshot with `git archive`.
- **The cache key excludes the model and effort** (spec-defined inputs). After a model switch, cached answers are reused until the prompt version is bumped or `cache_dir` is cleared.
- **Test listing over-approximates.** Examples: tests that expect a non-zero exit on bad flags, and helpers deeper than 2 levels. Consumers must treat `tests.e2e` as candidates, not as proof of coverage.
- **A model entry's claimed kind is not kept.** Every fallback entry is written as `model`, and its `name` carries the agent's label.
- **Cost.** There is one agent per traced entry, plus at most one re-ask. `trace_limit=30` and `cap=10` bound a scoped run. Unscoped runs trace every entry, which on this repository is all CLI subcommands, hooks and skills.
- **Duplicated gate logic.** `trace_gate.py` and walkthrough's `gates.py:188-215` diverge (D11) until PR 2 moves walkthrough onto the library.
- **Lockfile change.** Adding `jsonschema` regenerates the root `uv.lock`, a file outside `code_facts/`. This is the only change outside the library directory.
- **Spec amendments (D1).** Scenarios rewritten by D1 (rows 2, 3, 5, 6, 7, 9–14) must be updated in the change's spec files in the same PR. Otherwise tests and specs disagree.
- **Live pr_pipeline test coupling.** The live-copy discovery test depends on another skill's layout. It is skipped when the directory is absent, but a refactor of pr_pipeline can break it.

## Global Constraints

- Python floor: `requires-python = ">=3.12"`. Every new module starts with `from __future__ import annotations` and defines `log = logging.getLogger(__name__)`.
- New runtime dependency, exactly: `"jsonschema>=4"` in `plugins/multiplai-dev/lib/code_facts/pyproject.toml` `[project] dependencies`. Existing floors are unchanged: `pydantic>=2`, `multiplai-core[sdk]`.
- Schema file path: `plugins/multiplai-dev/lib/code_facts/code_facts/schema/paths.v1.schema.json`.
- Schema version literal: `"paths.v1"`.
- `$schema` value: copied byte-for-byte from `plugins/multiplai-dev/skills/review/schema/facts.v1.schema.json`. `$id`: facts.v1's `$id` with `facts.v1` replaced by `paths.v1`.
- Top-level field order: `schema_version`, `commit`, `scoped`, `cap`, `fallback`, `paths`, `dropped`, `diagnostics`, `test_listing_skips`.
- Path field order: `id`, `entry`, `hops`, `stops_at`, `break_reason`, `changed_lines_covered`, `tests`.
- Entry field order: `kind`, `name`, `location`, `handler`, `invocation`, `also_invoked_by`.
- Invoker field order: `kind`, `name`, `location`.
- Location field order: `path`, `line`.
- Handler field order: `path`, `line`, `symbol`.
- Hop fields: the fields of `walkthrough_pipeline/models.py:122-128` `Hop`, in that order, then `side` (literal `"head"`).
- E2E record field order: `node_id`, `file`, `line`, `match`, `invocation_line`.
- Mocked record field order: `node_id`, `file`, `line`, `match`, `invocation_line`, `patch_file`, `patch_line`.
- Drop field order: `stage`, `reason`, `detail`, `entry`.
- Diagnostic field order: `path`, `line`, `reason`, `detail`.
- Skip field order: `file`, `reason`.
- `fallback` object: `{"status": "ok" | "failed" | "skipped", "reason": string | null}`.
- Path id pattern: `^p[1-9][0-9]*$`, assigned `p1`…`pN` after ordering and cap.
- Location path pattern: `^(?!/)(?!.*(^|/)\.\.?(/|$))[^\\]+$`. Lines are 1-based integers with `minimum: 1`.
- Hop limit: `20`.
- Discovery kinds: `argparse_subcommand`, `click_command`, `typer_command`, `main_module`, `project_script`, `skill_command`, `hook_command`, `seed`.
- Output kinds and `KIND_RANK` order: `argparse`, `click`, `typer`, `main`, `script`, `skill_command`, `hook`, `seed`, `model`.
- Drop stages, in sort order: `model_fallback_citation`, `model_fallback_duplicate`, `import_reachability`, `trace_limit`, `trace`, `no_changed_line`, `cap`.
- Drop reasons outside the `trace` stage: `missing_citation`, `citation_unverified`, `duplicate`, `unreachable`, `over_trace_limit`, `no_changed_line_covered`, `over_cap`.
- Drop reasons in the `trace` stage: `call_failed`, or any break code.
- Break codes: `malformed_answer`, `empty_trace`, `path_outside_root`, `missing_file`, `line_out_of_range`, `text_mismatch`, `base_side_citation`, `entry_mismatch`, `unconnected_hop`, `repeated_hop`, `hop_limit`.
- Per-hop check order: `path_outside_root`, `missing_file`, `line_out_of_range`, `text_mismatch`, `base_side_citation`, `entry_mismatch`, `unconnected_hop`, `repeated_hop`.
- Discovery diagnostic reasons: `parse_error`, `unresolved_target`, `unresolved_handler`, `dynamic_name`, `seed_not_found`.
- Test-listing skip reasons: `decode_error`, `syntax_error`.
- Match kinds, in tie-break order: `direct_call`, `cli_argv`, `cli_runner`, `subprocess`, `runpy`.
- Discovery skips directories named: `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `site-packages`, `build`, `dist`.
- Test listing skips directories named: `.git`, `.venv`, `venv`, `node_modules`, `__pycache__`, `site-packages`.
- Test file patterns: `test_*.py`, `*_test.py`.
- `SKILL.md` fence info strings: `bash`, `sh`, `shell`, `zsh`, `console`.
- Agent tools, exactly: `["Read", "Grep", "Glob"]`. This always overrides any `allowed_tools` in `call_kwargs`.
- `budget_label` values: `paths:fallback`, `paths:trace:<first 12 hex of key>`, `paths:reask:<first 12 hex of key>`.
- Prompt version constants: `PATHS_FALLBACK_PROMPT_VERSION = "paths-fallback.v1"`, `PATHS_TRACE_PROMPT_VERSION = "paths-trace.v1"`, `PATHS_REASK_PROMPT_VERSION = "paths-reask.v1"`.
- `map_paths` defaults: `cap=10`, `trace_limit=30`, `concurrency=4`, `cache_dir=None`, `fallback=True`, `recoverable=()`.
- Cache files: `<cache_dir>/paths-answers/<sha256 hex>.json`, written as temp file, then `fsync`, then `os.replace`.
- Serialisation: `json.dumps(..., indent=2, ensure_ascii=False)` followed by exactly one trailing `"\n"`, UTF-8.
- No module under `plugins/multiplai-dev/lib/code_facts/` imports `walkthrough_pipeline`.
- No exported name starts with `Test`, and no library function name starts with `test`.
- No committed test fixture file is named `pyproject.toml`, `SKILL.md`, `hooks.json` or `settings*.json`. Fixtures are generated into `tmp_path`.
- New exports in `code_facts/__init__.py`: `map_paths`, `write_paths_file`, `discover_entries`, `PathsResult`, `TracedPath`, `PathEntry`, `PathHop`, `PathDrop`, `E2ETest`, `MockedE2ETest`, `E2EListing`, `Location`, `Seed`. Every existing export is unchanged.
- Test command: `uv run --directory plugins/multiplai-dev/lib/code_facts python -m pytest tests/`.