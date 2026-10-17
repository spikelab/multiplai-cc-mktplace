## Context

### What exists today

`plugins/multiplai-dev/lib/code_facts/` is a small shared library. It is a flat package at `code_facts/code_facts/`, built with hatchling and pulled into review, codebase-walkthrough, buildme and spec-extract as an editable path dependency. It is deliberately not a uv workspace member. Its public surface is:

- **`models.py`**
  - A private `_Model` base. Agent-answer models ignore unknown keys.
  - `Citation`, which defaults to the head side and rejects `line_end < line_start`.
  - `Fact`, `Term`, `ExploreOutput`, `Span`, `Scope`, `Unit` and `Dropped`.
- **`explore.py`**
  - The `Resolve` callable type, `(path, side) -> (repo, sha, rel) | None`.
  - The `CallFn` callable type, an async callable shaped like `agent_call_structured(prompt, schema, **kw)`.
  - `bounded(...)`, which runs tasks under a semaphore and re-raises the first real exception.
  - `ask(...)` and `run_tasks(...)`. These implement the answer-cache pattern: store each answer under a key and skip keys already answered.
  - `check_citation(...)`, which re-reads quotes through `multiplai_core.citations.check_quote`.
  - `gate_answers` and `citation_keeper`.
- **`prompts.py`**: the citation rules, the untrusted-content rules and `explore_unit`.
- **`partition.py`**: `partition(scope, limit)`.

The library never calls a model itself. Callers inject both `CallFn` and `Resolve`.

### Current state of path tracing

Only codebase-walkthrough traces call paths:

- `walkthrough_pipeline/stages/trace.py` seeds from webhooks, routes and Celery tasks.
- One agent per scenario returns `Hop`s (`models.py:122-128`).
- `trace_gate` (`gates.py:188-215`) checks the hops: hop 1 contains the seed, and each later hop is named in the previous hop's quote and defines that name or holds a boundary place.
- A broken trace gets one retry. If it is still broken, it is cut, with `stops_at` recording where.

On a CLI or skill repository the walkthrough finds no seeds. Review has no path view, and review-viewer has nothing to show.

### Facts that shape this design

- **pr_pipeline dispatch.** The proposal's "known discovery case" is wrong about the live code.
  - `pr_pipeline/__main__.py:53` is `sub.add_parser("edit")`.
  - `pr_pipeline/__main__.py:72` is a SPEND-marker check (`if args.command not in SPEND_COMMANDS`), not dispatch.
  - Real dispatch is a dict, `COMMANDS = {"open": cmd_open, …}` at `pr_pipeline/commands.py:400`, called as `COMMANDS[args.command](args)` at `pr_pipeline/commands.py:409`, inside `commands.run`.
  - The parser and the dispatch dict therefore sit in different modules of the same package.
- **Lockfile.** The repository has one lockfile, the root `uv.lock`. It records `code-facts` with dependencies `[multiplai-core, pydantic]`. CI installs with `uv sync --all-packages --frozen`, which does not re-check the lock against `pyproject.toml`.
- **Snapshots.** All three intended callers build read-only snapshots of a commit (`git archive`, or blob writes) and gate citations against git.
  - Review: `review_pipeline/target.py:683-697`.
  - Walkthrough: `walkthrough_pipeline/target.py:161-170`.
  - Spec-extract: `spec_extract_pipeline/target.py:626-635`.
  - Multi-repository snapshots use repo-key-prefixed, snapshot-relative paths. The caller's `Resolve` splits them (`walkthrough_pipeline/models.py:3-4`).
- **Contradictions in the specs.** The seven spec deltas contradict each other in several places: the meaning of `stops_at`, the entry-kind vocabulary, the drop vocabulary, the test-listing shape, the top-level document fields, and the pr_pipeline scenarios. This design settles each one (D3, D11, D13, D14, D16), and D18 lists the spec text that is rewritten in this same change before any implementation block starts.

## Decisions

### D1. Module layout and build order

**Decision.** The new code goes into `code_facts/code_facts/`. Each module has one responsibility so that each can be tested on its own:

| Module | Responsibility | Calls a model? | Runs git? |
|---|---|---|---|
| `snapshot.py` | `SnapshotIndex`: one walk of the root, the repo-root map, the module-name↔file resolver, and cached `ast` parses and `tokenize` streams | no | no |
| `entries.py` | Deterministic discovery and merging (D6, D7). Public `discover_entries(root, seeds=()) -> Discovery` | no | no |
| `imports.py` | Static import graph and reverse-BFS distances (D13) | no | no |
| `gate.py` | Pure `check_trace(...)` covering rules 1–8 plus the hop limit (D11) | no | no (reads through an injected reader) |
| `paths.py` | `map_paths`: orchestration of snapshot verification, the fallback, tracing, re-asks, scope, ordering, ids and e2e attachment (D2, D8, D9, D12, D13) | via `CallFn` | via `Resolve` + `git cat-file` |
| `e2e_invocations.py` | Finds the tests that structurally invoke an entry (D15a) | no | no |
| `e2e_patches.py` | Hop-2 patch and fixture analysis (D15b) | no | no |
| `path_models.py` | Output and answer models (D4, D16) | no | no |
| `prompts.py` (modified, additive only) | `trace_entry`, `retrace_entry`, `entry_fallback`, plus `TRACE_PROMPT_VERSION` and `FALLBACK_PROMPT_VERSION` | no | no |
| `schema/paths.v1.schema.json` | The JSON Schema, shipped as package data | — | — |

`__init__.py` gains `map_paths`, `write_paths_file`, `discover_entries` and the output models. Nothing existing is renamed or changed. `explore.ask`, `explore.run_tasks` and `explore.gate_answers` are not touched (D9).

The build order is eight blocks. Each block is independently testable and depends only on the blocks before it:

1. Spec amendments (D18), output models, schema, `write_paths_file`, and the dependency and lock changes (D17).
2. `SnapshotIndex` and the import graph.
3. Discovery for argparse (including the pr_pipeline shape), click/typer, `[project.scripts]`, `__main__`, and seeds, plus merging.
4. Discovery for `SKILL.md` and hooks.
5. Pure `check_trace`, tested directly with no `map_paths`.
6. `map_paths` tracing orchestration: prompts, cache, the re-ask policy, the error policy, scope, ordering and ids.
7. The model fallback.
8. E2E listing, invocation matchers first and then patch/fixture analysis.

**Alternatives considered.**

- One `paths.py` holding everything. Rejected: the gate and the e2e analysis are large, pure and separately specified.
- Splitting discovery per kind into its own modules. Rejected: merging needs every kind in one place, and the kinds share the module resolver.

**Why.** It follows the code_facts style of one concern per module. It keeps the deterministic parts free of `CallFn` and git, so the spec's "no model call" and "no subprocess" scenarios become plain unit tests. It also answers the granularity finding: no block bundles the gate rules with orchestration.

### D2. Public API and integration contracts

**Decision.**

```python
async def map_paths(
    *,
    root: Path,                                   # snapshot root (multi-repo allowed)
    commit: str,                                  # opaque id of the snapshot; goes into prompts and cache keys
    resolve: Resolve,                             # existing code_facts type; used for every git re-read
    call: CallFn,                                 # existing code_facts type
    cache_dir: Path,
    changed_lines: Mapping[str, Iterable[int]] | None = None,
    seeds: Sequence[Seed] = (),
    places: Sequence[Place] = (),
    cap: int = 5,
    trace_limit: int = 20,
    concurrency: int = DEFAULT_CONCURRENCY,       # 4
    fallback: bool = True,
    fatal: tuple[type[BaseException], ...] = (),
    call_kwargs: Mapping[str, Any] | None = None, # model/effort etc., passed through to CallFn
) -> PathsResult

def write_paths_file(result: PathsResult, path: Path) -> None
def discover_entries(root: Path, seeds: Sequence[Seed] = ()) -> Discovery
```

Contract details:

- **Required arguments.** All arguments are keyword-only, and `call`, `root`, `commit`, `resolve` and `cache_dir` have no defaults. Calling without `call` raises `TypeError` before any work starts, which satisfies contract "Missing required argument".
- **Input types.**
  - `Seed` is `{path, line | symbol, name?}`.
  - `Place` is `{path, line}`.
  - Both are frozen pydantic models. Plain dicts are accepted and validated.
- **Validation before any `CallFn` call.**
  - `changed_lines` keys are normalised to POSIX form with no leading `./`.
  - A key that is absolute or escapes the root raises `ValueError`.
  - A line number below 1 raises `ValueError`.
  - `cap < 1` and `trace_limit < 1` raise `ValueError`. This is checked only when `changed_lines` is not `None`, per path-scope.
  - A key naming a file that does not exist is accepted.
- **What `call` receives.** Every invocation is `await call(prompt, schema, allowed_tools=["Read", "Grep", "Glob"], cwd=str(root), budget_label=<label>, **call_kwargs)`.
  - The labels are `paths:fallback`, `paths:trace` and `paths:retrace`.
  - `call_kwargs` may not contain `allowed_tools`, `cwd` or `budget_label`. If it does, `ValueError` is raised before any call. This keeps the read-only guarantee out of the caller's hands.
- **Return value and writes.** `map_paths` returns the frozen `PathsResult`. It writes only under `cache_dir`. Only `write_paths_file` produces `paths.json`.
- **What `resolve` is for.** `resolve` is the same function every caller already passes to `check_citation`, so multi-repository snapshots need no new contract (paths-output-contract "Multi-repository snapshot").

**Alternatives considered.**

- Infer the commit from the root, for example from a `.review-head` marker. Rejected: each caller already knows its shas and has its own marker convention.
- Have `map_paths` write `paths.json` itself. Rejected: the spec forbids it, and callers own their output folders.

**Why.** It matches how callers already integrate `explore`: they inject `CallFn` and `Resolve`, and they own budgets and retries. Because the parameter list is a superset of what the specs list, review, codebase-walkthrough and review-viewer can adopt it in PRs 2–4 without API changes.

### D3. One entry-kind vocabulary, plus `source`

**Decision.** `EntryKind` is a closed string enum. It is used by discovery, the fallback, the output and the schema:

`argparse_subcommand`, `click_command`, `typer_command`, `main_module`, `project_script`, `skill_command`, `hook_command`, `seed`, `other`.

Each entry also carries `source ∈ {discovery, seed, model}`.

- A fallback entry whose stated kind is in the first eight values keeps that kind.
- Any other stated kind becomes `other`. Its `name` keeps the model's display name, for example kind `route` becomes kind `other` with name `GET /items`.
- `KIND_RANK` is the order of the enum as listed above. It is the merge precedence (D7) and the ordering key (D13). It is defined once, as a tuple in `path_models.py`, and a test pins it.

**Alternatives considered.**

- The contract's short names (`argparse`, `click`, `script`, `main`, `hook`, `model`). Rejected: the discovery spec, which has the most scenarios, uses the long names. A `model` kind would also collapse what an entry is into where it came from.
- Keeping `route` and other model-only kinds open-ended. Rejected: this contradicts the contract's "Unknown kind rejected" and makes the schema unbounded.

**Why.** It gives one vocabulary across five specs, and `source` keeps provenance without overloading `kind`. D18 lists the scenarios that change: the contract's "Entry object shape" and "Known discovery case", and the fallback's "Well-formed answer is parsed".

### D4. Output models: frozen, closed, derived from `_Model`

**Decision.**

- **Output models.** `PathsResult`, `PathRecord`, `EntryRecord`, `InvokerRecord`, `Hop`, `Location`, `HandlerRef`, `TestListing`, `E2ERecord`, `MockedE2ERecord`, `DropRecord`, `EntryRef`, `Diagnostic`, `FallbackStatus` and `TestListingSkip`.
  - They subclass `code_facts.models._Model`, which satisfies the contract's `issubclass` check.
  - They override `model_config = ConfigDict(extra="forbid", frozen=True)`.
  - Late fields (ids, `tests`) are set with `model_copy(update=…)` and then re-validated.
  - Block 1 asserts the observable directly: assigning any attribute on a constructed model raises `pydantic.ValidationError` (type `frozen_instance`). Building a `PathRecord` with `stops_at=2, break_reason=None` raises.
- **Answer models.** `TraceAnswer`, `HopAnswer`, `FallbackAnswer` and `FallbackEntry` are separate and keep `_Model`'s ignore-extra behaviour. The exception is `TraceAnswer`, which sets `extra="forbid"` at the top level only and declares one optional `notes: str = ""`. An answer that adds an alternative chain under any other top-level key fails validation, which is how path-tracing "Branching answer is rejected" is enforced. Inside a hop, unknown keys are still ignored.
- **`Hop` parity.** The output `Hop` has the same field names as the walkthrough `Hop` (`walkthrough_pipeline/models.py:122-128`), with the citation typed as `code_facts.models.Citation`. The parity test does not import `walkthrough_pipeline`, which is not installed in code_facts' environment. Instead it `ast`-parses that file by path, reads the class's annotated field names, and compares the sets. The gate reads the hop's callee-name field and its citation. Those are the fields `trace_gate` reads at `gates.py:188-195`, under whatever names that model uses.
- **Validators.**
  - `stops_at` and `break_reason` are both null or both set.
  - `1 ≤ stops_at ≤ len(hops)`.
  - `1 ≤ len(hops) ≤ 20`.
  - `tests.e2e` and `tests.mocks_hop_2` are disjoint on `node_id`.
  - Every location `path` is relative and POSIX, with no `.` or `..` segments, and every `line ≥ 1`.

**Alternatives considered.**

- `validate_assignment=True` (the previous draft). Rejected: it only re-validates, so a valid assignment succeeds and the "cannot change after construction" test cannot pass.
- Mutable models. Rejected: output is shared by three consumers and must be reproducible.

**Why.** Frozen models make determinism easy to reason about. The contract test then has a concrete observable to assert.

### D5. One snapshot index, verified against the commit

**Decision.**

- **One index.** `SnapshotIndex(root)` walks the root once.
  - It is deterministic: entries are sorted with `sorted(os.scandir(...))` by name.
  - It does not follow symlinks whose resolved target is outside `root`.
  - It records each file's repository root: the nearest ancestor at or below `root` that holds `.git` or `pyproject.toml`, or `root` itself.
  - It maps dotted module names to files per repository, searching `<repo>` and then `<repo>/src`.
  - It caches `ast.parse` results and `tokenize` streams. Files that fail to decode or parse are recorded once.
  - Discovery, the import graph and e2e listing all share this index, each with its own exclusion set (Global Constraints).
- **Reading contract.** `root` must be a snapshot of exactly the files `resolve` maps to the given shas. This is what `git archive` and blob-write snapshots produce. All static analysis reads `root`, and every citation check reads git.
- **Verification.** To catch callers who pass a dirty working tree, `map_paths` runs a snapshot check after discovery and before E is counted.
  - For each entry, it re-reads the entry's `location` line and, if set, its `handler` line through `resolve` + git.
  - If the file is missing at the sha, or the line text differs from the snapshot's line, the entry is removed and a `Diagnostic{reason: "snapshot_mismatch"}` is recorded with the path and line.
  - The resulting scenarios:
    - an untracked file that defines an entry is not traced and produces `snapshot_mismatch`;
    - a working-tree edit that shifts a `def` line produces `snapshot_mismatch` on the handler line, instead of silent `entry_mismatch` cuts later.

**Alternatives considered.**

- Read everything through git via an injected reader. Rejected: it means one subprocess per file across the whole tree, for discovery, the import graph and test listing.
- Document the contract only. Rejected: a mismatch would surface as unexplained gate drops, which breaks the goal of "every drop explained".

**Why.** Reading the filesystem is fast, which the deterministic layers need. A targeted git check on just the lines that anchor entries keeps the two views consistent where it matters.

### D6. Deterministic discovery rules

**Decision.** `discover_entries` implements entry-point-discovery as written, with these resolutions.

**argparse.**

- One entry is emitted per `add_parser("<literal>")` called on an object returned by `add_subparsers(...)` in the same function. Nested parsers produce space-joined names.
- `add_parser(name_var)` produces `dynamic_name`.
- Handlers resolve in three ways:
  1. **`set_defaults(func=<name>)`** on the subparser object, resolved within the same module or a same-repository import.
  2. **A dispatch dict in the parser's module.** The dict literal maps subcommand literals to function references.
  3. **A dispatch dict in another module of the same repository that is reachable from the parser's module through the static import graph (D13).** This form applies only when that module also contains a subscript call `<DICT>[args.<dest>](...)`, where `<dest>` is the `dest=` of the `add_subparsers` call. `args` may be any name.
- Known edges, all of which point to the handler's `def` line:
  - from the dispatch call line (`args.func(...)` or `<DICT>[args.<dest>](...)`) when one exists;
  - otherwise from the `set_defaults` line or the dict-entry line;
  - in every resolved case, also from the entry's `add_parser` line. This lets a trace whose hop 1 sits on the `add_parser` line legitimately reach the handler.
- On the live pr_pipeline this resolves `open`, `edit` and `render` to `cmd_open`, `cmd_edit` and `cmd_render` in `commands.py`. Each gets an edge from `commands.py:409`.
- `if args.command == "x"` comparisons are never resolved. That case keeps `handler=None`, stays anchored on its `add_parser` line, and gets its own synthetic fixture.
- Cross-function subparser registration (a helper that receives `sub`) is out of scope (D19).

**click / typer.**

- Recognised decorators are those in the spec.
- The default name is `function_name.lower().replace("_", "-")`. Click 8.1 and Typer derive names this way, so `Deploy_All` becomes `deploy-all`.
- No suffix stripping is done (D19 and Risks).
- An explicit name argument wins.
- Group and `add_typer(name=…)` prefixes are space-joined.
- `add_typer(sub_app)` with no `name=` contributes no prefix. There is no name to prefix, which is the literal reading of the spec.

**`[project.scripts]`.**

- `tomllib` parses the file. The key's line is found by a line scan for `^\s*"?<key>"?\s*=` within the `[project.scripts]` table.
- Targets resolve only in the `module:function` form, against `<repo>` and then `<repo>/src`. The function must be defined at module top level.
- `module:obj.attr` and package re-exports produce `unresolved_target` (D19).

**`__main__`.**

- As specified. A package `__main__.py` is anchored at line 1. A non-test module's `if __name__ == "__main__":` is anchored at the `if` line.
- The handler is the single called function, resolved through same-module or same-repository imports. Otherwise it is `None`.

**`SKILL.md`.**

- Only backtick fences whose info string is one of `bash`, `sh`, `shell`, `zsh`, `console` are scanned. Line continuations are joined.
- A command matches when it is `[ENV=…]* [uv run <uv-options>] python|python3 -m <module> [args…]`.
- `uv run` option peeling is kept on purpose because this repository's skills use it. The option values `--directory DIR` and `--project DIR` are added as search roots right after any `PYTHONPATH=` roots. The flags `--frozen`, `--no-sync`, `--quiet`/`-q`, `--package NAME`, `--extra NAME` and `--with SPEC` are skipped. Any other option stops matching for that command and produces `unresolved_target`.
- `${CLAUDE_SKILL_DIR}` and `${CLAUDE_PLUGIN_ROOT}` are expanded, and the search roots then follow the spec order.
- The first positional argument after the module selects the subcommand target.
- Tilde fences and interpreter flags such as `-I`, `-E` and `-P` are not recognised (D19).

**Hooks.**

- `hooks.json` and `.claude/settings*.json` are read. Each `{"type": "command"}` object with a string `command` produces one entry.
- Occurrence lines are assigned in document order by scanning for the JSON-encoded command string.
- `${CLAUDE_PLUGIN_ROOT}`, `$CLAUDE_PROJECT_DIR` and `${CLAUDE_PROJECT_DIR}` expand as specified.
- An exec-form array is not recognised (D19).

**Handlers for `skill_command` and `hook_command`.** The previous draft left these undefined, which broke merging. The handler is the `HandlerRef` at the known edge's target:

1. the resolved subcommand handler; else
2. the resolved `main_module` handler of the invoked module or script; else
3. `<module>/__main__.py:1` (or `<script>.py:1`) with symbol `__main__`; else, for a non-Python script,
4. `<file>:1` with symbol `<script>`.

**Seeds.** As specified. The handler is the enclosing `def` at the line, or the named symbol.

**Output.** Entries are sorted by `(location.path, location.line, kind, name)`. Diagnostics are sorted by `(path, line or 0, reason)`.

**Alternatives considered.**

- Resolve dispatch dicts anywhere in the repository. Rejected: it would match unrelated programs with similar keys. Requiring the import-graph link and the `args.<dest>` subscript call keeps it precise.
- Keep the spec's same-module dict rule and accept `handler=None` for pr_pipeline. Rejected: the motivating repository's CLI paths would never get past the `add_parser` line.

**Why.** It resolves the real shape this PR was written for. It also keeps everything the spec cites as deterministic and import-free (discovery "never imports or runs target code").

### D7. Merge key and precedence

**Decision.**

- **Merge key.** `merge_key = (handler.path, handler.line)` if the handler is set, else `(location.path, location.line)`. Entries with equal keys merge into the entry with the lowest `KIND_RANK`. Ties go to the smallest `(location.path, location.line)`.
- **What a merge records.**
  - Every other member is listed in `also_invoked_by` as `{kind, name, location}`, sorted by `(path, line, kind)`.
  - Their known edges are unioned into the kept entry with duplicates removed.
- **Results.**
  - A `skill_command` whose handler is `pkg/__main__.py:1` merges with the `main_module` entry anchored there, even when that entry has `handler=None`. The `main_module` entry is kept.
  - Two unresolved argparse subcommands on different `add_parser` lines stay separate (discovery "Unresolved handlers not merged").
  - A hook and a `SKILL.md` command that both run `python -m pkg` become one entry (discovery "Hook and SKILL.md invoke the same module").

**Alternatives considered.** Merge null-handler entries only when their locations are identical (the spec's literal rule). Rejected: a `SKILL.md` command and the `__main__` it runs would then never merge, because the command's target is a location rather than a handler.

**Why.** A null-handler entry is effectively anchored at its location. Using that as its key is the smallest change that makes the spec's merge scenarios and e2e "Alternate invokers count" pass.

### D8. Model fallback

**Decision.**

- **The call.** At most one `paths:fallback` call is made per run, after discovery and the snapshot check, unless `fallback=False`. In that case the status is `skipped` and no call is made.
  - The prompt lists the deterministic entries by location and kind.
  - It names the scope: the changed files if scoped, otherwise the seed paths, otherwise the whole snapshot.
  - It asks only for entries not already listed.
- **Parsing.**
  - The answer is first validated as `FallbackAnswer{entries: list[Any]}`. If that fails, the run continues with status `failed` and a non-empty reason.
  - Each raw item is then validated individually as `FallbackEntry{kind, name, citation, reason}`.
  - Each raw item counts once in E (D13), and every rejected item produces exactly one drop record with stage `model_fallback_citation`:

| Fault | Reason |
|---|---|
| No `citation` key, or `citation: null` | `missing_citation` |
| Citation fails validation (`line_start < 1`, `line_end < line_start`, absolute path or escaping path), or re-read through `check_citation` fails (missing file, out-of-range line, quote mismatch) | `citation_unverified` |
| Item is not an object, or `name` is missing or empty, or `kind` is missing | `malformed_entry` |
| Cites the same line as a deterministic or seed entry's location or handler, resolves to the same handler (the enclosing `def` via `SnapshotIndex`), or repeats an earlier kept fallback entry's line | `duplicate` |

  - A drop record for an item that has no valid location carries `location: null` in its `EntryRef` (D14).
- **Kept entries.** These get `source: "model"` and a kind per D3. Their handler is the enclosing `def`. They are ordered by location before any later stage, independent of the order the model listed them in.
- **Cache key.** The key is `sha256` of the canonical JSON of `{"v": FALLBACK_PROMPT_VERSION, "commit": commit, "entries": sorted deterministic entry identities, "scope": sorted scope paths}`.
- **Exceptions.** Any exception raised by `call` during the fallback propagates (fallback "CallFn raises"). It is not cached.

**Alternatives considered.**

- Validating the whole answer strictly. Rejected: one bad citation would discard every model entry.
- Silently skipping invalid items. Rejected: it breaks the accounting invariant and the "every drop explained" goal.

**Why.** Validating per item makes failures local. It also makes E well defined: deterministic and seed entries after merging and the snapshot check, plus one count per raw fallback item.

### D9. Calls, caching and error policy

**Decision.** `explore.ask` and `explore.run_tasks` are left unchanged. `paths.py` has a private `_cached_call(kind, key_material, prompt, schema, label)` that follows the same pattern ("key → stored answer; skip if present") against a file cache, and runs under `explore.bounded(...)`.

**Cache layout.**

- Answers live at `<cache_dir>/paths/<kind>/<sha256>.json`, where `<kind>` is `fallback`, `trace` or `retrace`.
- Each file holds `{"key": <key_material>, "answer": <answer JSON>}`. It is written to a temp file in the same directory and moved into place with `os.replace`, so a killed run leaves only complete files.
- Only answers that validate against the answer model are written.
- A file that does not parse, has a different `key`, or fails validation is treated as a miss and overwritten. Such files are never treated as errors.

**Answer normalisation.** `call` may return a model instance, a dict, or a JSON string. All three are normalised with `model_dump(mode="json")` or `json.loads` and then re-validated against our own answer model. A validation failure is a malformed answer, handled by D12.

**Exception policy.**

| Situation | Behaviour |
|---|---|
| The exception is an instance of a type in `fatal` | Always propagates, at every call site |
| Exception during the fallback call | Propagates (fallback spec) |
| Exception during a first trace call | Caught. The entry is dropped with `trace` / `trace_call_failed`. No re-ask, nothing is cached, and the call count for that entry is exactly 1 (path-tracing "No library-level retry") |
| Exception during a re-ask | Propagates unchanged (trace-gate "CallFn failures during a re-ask propagate") |

A stored `{"error": …}` never exists in the paths cache, so it can never be mistaken for a malformed answer, and it can never trigger a re-ask.

**Resume.** A resumed run makes calls only for keys with no valid cached answer. This includes traces that failed in the previous run (path-tracing "Resume after interruption").

**Alternatives considered.**

- Extend `ask` with `schema`, `budget_label` override and normalisation (the previous draft). Rejected: it changes the behaviour of an exported function for review, walkthrough, buildme and spec-extract, which the proposal forbids.
- Persist call errors as `ask` does. Rejected: a resume would then never retry them, and stored errors were ambiguous inputs to the gate.

**Why.** It reuses the proven pattern and `bounded` without touching existing exports. The error table resolves the conflict between "drop on trace failure" and "propagate on re-ask failure" exactly as the specs require.

### D10. Trace prompt

**Decision.** `prompts.trace_entry(entry, commit, scope, edges, places, hop_limit=20) -> str` composes these blocks:

- the existing citation rules and untrusted-content rules from `prompts.py`;
- the entry's kind, name, location, handler symbol and location, and invocation text;
- the commit;
- the change scope as sorted files with merged line ranges, or the sentence "There is no change scope; trace to the entry's last effect.";
- the known edges and places that apply;
- the hop limit;
- the `TraceAnswer` schema.

It always contains this anchoring instruction, verbatim apart from substitution:

> Hop 1 MUST cite `<location.path>:<location.line>` (or `<handler.path>:<handler.line>`) with its exact text, even when that file is not Python.

When there is no handler, the parenthesised alternative is omitted.

`prompts.retrace_entry(...)` adds the broken hop index, the `break_reason` code and its detail, and the accepted prefix.

`TRACE_PROMPT_VERSION` and `FALLBACK_PROMPT_VERSION` are integers. A drift-guard test hashes the rendered text of all three templates (`trace_entry`, `retrace_entry`, `entry_fallback`) for a fixed input. It compares each hash against a constant recorded next to the version it belongs to. Editing a template without bumping its version fails the test.

**Alternatives considered.** Infer anchoring from the gate's re-ask message. Rejected: it would cost a systematic re-ask for every `main_module`, `skill_command` and `hook_command` entry.

**Why.** Prompt-content tests can assert the anchoring sentence and the scope text (path-tracing "Prompt contents" and "No changed lines given").

### D11. Pure trace gate

**Decision.**

```python
check_trace(hops, entry, edges, places, read, quote_ok, hop_limit=20) -> GateOutcome
# GateOutcome(accepted: tuple[Hop, ...], broken_at: int | None, reason: BreakReason | None, detail: str)
```

- **Injected functions.** `read(path) -> list[str] | None` returns the file's lines at the commit. `quote_ok(citation) -> bool` decides the quote check. In `map_paths`, both are built from `resolve`:
  - `read` runs `git -C <repo> cat-file blob <sha>:<rel>` with a fixed argv, `shell=False`, `stdin=DEVNULL` and `check=False`, cached per `(repo, sha, rel)`.
  - `quote_ok` is `explore.check_citation`, so the whitespace normalisation is identical to existing gating.
  - Tests inject fakes. The function makes no model call and runs no subprocess itself.
- **Rule order.** Hops are checked in order, and the gate stops at the first broken hop. Within a hop, the first failing rule wins, in the spec order:
  1. `path_outside_root`
  2. `missing_file`
  3. `line_out_of_range`
  4. `text_mismatch`
  5. `base_side_citation`
  6. `entry_mismatch` (hop 1 only)
  7. `unconnected_hop` (hop 2 and later)
  8. `repeated_hop`
- **Connectivity.** Hop n+1 is connected when any of these holds:
  - its callee name appears as a `NAME` token inside hop n's cited line range, taken from the cached `tokenize` stream of the whole file. Comments and strings are therefore excluded. Non-Python files never count for this test;
  - a known edge runs from hop n's range to hop n+1's range;
  - a place falls inside hop n+1's range.
- **Hop limit.** Only the first `hop_limit` hops are checked. If all of them pass and more were supplied, the outcome is `accepted = first 20`, `broken_at = None`, `reason = hop_limit`.
- **Malformed and empty answers.** These never reach `check_trace`. The caller maps them to `broken_at = 1` with `malformed_answer` or `empty_trace` (D12).
- **`stops_at` semantics.** In the output, `stops_at` is always the number of retained hops, which is the 1-based index of the last kept hop. `broken_at` is internal and never serialised. So:
  - a re-asked answer that breaks at hop 3 yields hops 1–2 and `stops_at = 2`;
  - a 23-hop answer yields 20 hops, `stops_at = 20` and `break_reason = hop_limit`;
  - exactly 20 valid hops yields `stops_at = null`.

**Alternatives considered.** Import the walkthrough gate. Rejected: forbidden, and walkthrough moves onto the library in PR 2.

**Why.** A pure function lets every trace-gate scenario be a direct unit test (Block 5). Separating `broken_at` from `stops_at` settles the four-way contradiction in the specs in favour of the contract's "last hop kept" meaning.

### D12. Re-ask and cut policy

**Decision.** Per entry, inside one bounded task:

1. **First call.** An exception leads to D9's policy. A malformed or empty answer counts as `broken_at = 1` with that reason. Otherwise `check_trace` runs.
2. **No break.** If the first answer has no break, or its only issue is `hop_limit`, it is accepted with no re-ask.
3. **Re-ask.** Otherwise one `paths:retrace` call is made. Its cache key is `{first key, broken_at, reason, sha256(canonical first answer)}`. Any exception from it propagates.
4. **Checking the re-asked answer.** It is checked from hop 1.
   - Malformed or empty: the entry is dropped with `trace` / `malformed_answer` or `trace` / `empty_trace`.
   - Breaks at hop 1: dropped with `trace` / `cut_at_entry`.
   - Breaks at hop k > 1: kept with hops 1..k−1, `stops_at = k−1`, `break_reason = <code>`.
   - Valid but longer than 20 hops: cut by `hop_limit`.
   - Fully valid: kept with `stops_at` and `break_reason` both null.
5. **Call count.** No further call is made for that entry.

Path-tracing "Zero hops" and "Unparseable answer" therefore drop the entry after exactly two calls, with the stub returning the same answer twice. Path-tracing "No library-level retry", where `call` raises, drops after exactly one call.

**Alternatives considered.** Keep the first answer's accepted prefix when the re-ask is worse. Rejected: the spec says the re-asked answer is the one that is cut.

**Why.** It is the walkthrough's existing "one retry, then cut" behaviour, made exact.

### D13. Scope, reachability, ordering, ids, accounting

**Decision.**

**Import graph.** Built with `ast` over `.py` files in the index.

- `import a.b.c` adds edges to `a/__init__.py`, `a/b/__init__.py` and `a/b/c.py`, whichever exist.
- `from x import y` adds an edge to `x`, and also to `x/y.py` if that file exists.
- Relative imports resolve against the importing file's package.
- Unparseable modules have no outgoing edges, and unresolvable imports add none.
- Distances come from one reverse BFS seeded at the changed `.py` files, with a visited set, so cycles terminate.

**Entry distance.** Only when scoped.

- 0 if the entry's `location.path` changed, or if any `also_invoked_by[].location.path` changed. This addresses the merged-invoker finding.
- Otherwise the minimum BFS distance over the handler's file and the target files of the entry's known edges.
- An entry with no finite distance is dropped with `import_reachability` / `unreachable`, and no call is made for it.

**Trace limit.** Reachable entries are sorted by `(distance, KIND_RANK, location.path, location.line, name)`. Entries beyond `trace_limit` are dropped with `trace_limit` / `over_trace_limit`.

**Coverage.** Only when scoped. A changed line is covered when either holds:

- it falls inside the inclusive cited range of a retained hop in the same normalised file; or
- it equals the entry's `location` or an `also_invoked_by[].location`. For example, a changed `SKILL.md` command line or a `[project.scripts]` key line of a merged invoker.

A path with no covered line is dropped with `no_changed_line` / `no_changed_line_covered`. `changed_lines_covered` holds distinct `{path, line}` pairs sorted by `(path, line)`, and is `[]` when the run is unscoped.

**Order.** Kept paths are sorted by:

1. covered-line count, descending;
2. retained hop count, ascending;
3. `KIND_RANK`;
4. `location.path`;
5. `location.line`;
6. `name`.

Paths beyond `cap` are dropped with `cap` / `over_cap`. The survivors are then assigned `p1..pN`, in order and contiguously. Ordering never depends on completion order, concurrency or cache state, because traces are collected into a dict keyed by entry identity and sorted afterwards.

**Unscoped runs.** No reachability, trace-limit, coverage or cap stage runs. `cap` is `null` in the output, and every traced path is kept.

**Accounting invariant.** Kept paths plus drop records equals E, where E is defined in D8. A test asserts it on a fixture that triggers every stage.

**Alternatives considered.** Weight distance by import depth only, ignoring invoker files. Rejected: a change touching only `SKILL.md`, `hooks.json` or `pyproject.toml` would drop the very entry it modifies.

**Why.** It keeps every pre-filter deterministic and model-free. It gives the merged-invoker cases (path-scope "Entry whose definition file changed is kept") the right answer.

### D14. Drop and diagnostic vocabulary

**Decision.**

A `DropRecord` is `{stage, reason, detail, entry: EntryRef}`, where:

- `reason` is a closed enum, valid only for its stage;
- `detail` is a string and may be empty;
- `EntryRef` is `{kind, name, source, location | null}`.

| Stage (in order) | Reasons |
|---|---|
| `model_fallback_citation` | `missing_citation`, `citation_unverified`, `malformed_entry`, `duplicate` |
| `import_reachability` | `unreachable` |
| `trace_limit` | `over_trace_limit` |
| `trace` | `trace_call_failed`, `malformed_answer`, `empty_trace`, `cut_at_entry` |
| `no_changed_line` | `no_changed_line_covered` |
| `cap` | `over_cap` |

Drop records are sorted by stage order, then `location.path` (null sorts as `""`), then `location.line` (null sorts as `0`), then `kind`, then `name`. An entry has at most one drop record and never appears in both `paths` and `dropped`. When the run is unscoped, the stages `import_reachability`, `trace_limit`, `no_changed_line` and `cap` never appear.

A `Diagnostic` is `{path, line | null, reason, detail}`. The reasons are `parse_error`, `unresolved_target`, `unresolved_handler`, `dynamic_name`, `seed_not_found` and `snapshot_mismatch`. `snapshot_mismatch` is added by `map_paths` only. Direct `discover_entries` calls never produce it, so discovery "Mixed failures" still yields exactly five diagnostics.

**Alternatives considered.** Free-text reasons, as in the contract. Rejected: consumers need to filter on them, and path-scope already demands a closed set.

**Why.** It gives one table instead of two incompatible ones, and it includes the `trace` stage that neither spec had.

### D15. E2E test listing

**D15a — invocations (`e2e_invocations.py`).**

- **Scope.** Test files are `test_*.py` and `*_test.py` under the root, with the test exclusion set applied. Tests are module-level `test*` functions and `test*` methods of module-level `Test*` classes.
- **Name resolution.** Each test module's `import` and `from … import … as …` statements are resolved to snapshot files through `SnapshotIndex`, within the test file's repository.
- **Match kinds**, recognised only from literal string constants:
  - `direct_call`: the callee resolves to the entry's handler.
  - `cli_argv`: the callee resolves to a function in the subparser's module that contains a `parse_args(` or `add_subparsers(` call. The argv is a literal list or tuple containing the subcommand name. For click and typer entries, `main` is the resolved group.
  - `cli_runner`: `<runner>.invoke(<resolved group or app>, <literal args>)` where the runner comes from `CliRunner()`.
  - `subprocess`: one of the five `subprocess` functions, with tokens from a literal list or `shlex.split` of a literal string. The interpreter tokens `python`, `python3` and `sys.executable` are equivalent. The command matches `python -m <module> [<subcommand>]`, or a project-script name as the first token, or token equality with the hook's configured (unexpanded) command.
  - `runpy`: `runpy.run_module("<module>", …)`.
- A test that matches the entry or any `also_invoked_by` invoker is listed.
- A test matching several times keeps the earliest `invocation_line`. On that line, the first match kind in the order above wins.

**D15b — hop-2 patches (`e2e_patches.py`).**

- **Patch forms recognised:**
  - `mock.patch` or `unittest.mock.patch` with a string target, used as a decorator, a `with` context manager, or through `.start()`;
  - `mock.patch.object(<resolved obj>, "<name>")`;
  - `monkeypatch.setattr` with a dotted string or with an object-and-name pair.
- **Fixtures.** A patch also counts when it sits in a fixture defined in the test's module or in any `conftest.py` from the test's directory up to the root, provided the test requests the fixture by parameter name or the fixture is `autouse=True`.
- **What counts as hop 2.** Hop 2 is the second retained hop. Its module is derived from its citation path, and its symbol from its callee name. A target matches when it is `<defining module>.<symbol>`, or `<hop-1 module>.<name bound to symbol by hop-1's imports>`.
- Paths with fewer than two retained hops, including paths cut after hop 1, never move a test into `mocks_hop_2`.

**Output.**

- `tests.e2e` holds `E2ERecord{node_id, file, line, match, invocation_line}`.
- `tests.mocks_hop_2` holds `MockedE2ERecord`, which adds `patch_line` (possibly in a conftest file).
- Each list contains a `node_id` at most once and is sorted by `(file, line, node_id)`. A test may appear on several paths.
- Files that fail to decode or parse are listed in `test_listing_skips` with reason `decode_error` or `syntax_error`.
- No module is imported, no subprocess is spawned and no `CallFn` call is made.

**Alternatives considered.** Run pytest collection. Rejected: forbidden, and it would execute test modules.

**Why.** Static, literal-only matching is cheap and deterministic. It is honest about what it cannot see, as Risks explains.

### D16. Output document and `write_paths_file`

**Decision.**

`paths.json` has exactly nine required top-level fields, in this order:

1. `schema_version` (`"paths.v1"`)
2. `commit`
3. `scoped`
4. `cap`
5. `paths`
6. `dropped`
7. `fallback` (`{status: ok|failed|skipped, reason: str|null}`)
8. `diagnostics`
9. `test_listing_skips`

A path holds `id`, `entry`, `hops`, `stops_at`, `break_reason`, `changed_lines_covered` and `tests`. An entry holds `kind`, `name`, `source`, `location`, `handler`, `invocation` and `also_invoked_by`. In the output schema, `break_reason` is limited to the nine codes that can produce a kept path. `malformed_answer` and `empty_trace` only ever appear as drop reasons.

The schema lives at `code_facts/code_facts/schema/paths.v1.schema.json` and is loaded with `importlib.resources`. It is hand-written in the style of `skills/review/schema/facts.v1.schema.json`:

- the same `$schema` value;
- a `$id` and a `title`;
- `$defs` for reused shapes;
- `additionalProperties: false` on every object.

A test checks that each `$def`'s `properties` order equals the corresponding model's field order. Because serialisation follows model field order, "keys in schema-declared field order" is checked mechanically.

`write_paths_file(result, path)`:

1. Serialises with `json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"` and encodes as UTF-8.
2. Validates the serialised document with `jsonschema`, imported inside the function. On failure it raises `PathsSchemaError` naming the JSON pointer, such as `/schema_version`, and touches nothing on disk.
3. Writes a `tempfile.NamedTemporaryFile` in the target's directory, flushes and `fsync`s it, then `os.replace`s it over the target. On any exception the temp file is unlinked.

Reading a written file back with `PathsResult.model_validate_json` and writing it again reproduces the original bytes.

**Alternatives considered.**

- Generate the schema from `model_json_schema()`. Rejected: the house style is a hand-written, reviewed contract, as with facts.v1 and the viewer's committed schemas, and generated `$defs` names are unstable.
- Put the schema at `plugins/multiplai-dev/lib/code_facts/schema/` as the proposal literally says. Rejected: that location is outside the hatchling wheel's `code_facts` package, so `write_paths_file` could not find it in a non-editable install. This is a deliberate departure from the proposal's path.

**Why.** It meets every requirement in paths-output-contract under the amended shapes (D18).

### D17. Dependencies and lockfile

**Decision.**

- Add `jsonschema>=4` to `[project].dependencies` in `plugins/multiplai-dev/lib/code_facts/pyproject.toml`. It is a runtime dependency because `write_paths_file` uses it.
- Regenerate the root lockfile with `uv lock` run at the repository root and commit it, per the uv reference doc's "lockfile is the source of truth". The root `uv.lock` is the only lockfile and already records `code-facts`.
- `jsonschema` is imported only inside `write_paths_file` and inside schema tests. `import code_facts` never needs it.
- A test sets `sys.modules["jsonschema"] = None` and imports `code_facts` in a subprocess to prove this.
- Block 1's verification includes:
  - `uv lock --check`;
  - `uv sync --all-packages --frozen && uv run python -c "import code_facts"` at the root;
  - the code_facts suite via `uv run --directory plugins/multiplai-dev/lib/code_facts python -m pytest tests/`.
- No SDK dependency is added. `tomllib`, `ast`, `tokenize`, `json` and `shlex` are all stdlib on Python ≥ 3.12.

**Alternatives considered.**

- Make `jsonschema` a dev-only dependency and skip runtime validation. Rejected: the contract requires `write_paths_file` itself to validate.
- Import `jsonschema` eagerly. Rejected: a stale lock would break every skill that imports code_facts.

**Why.** It satisfies the contract and keeps "the full repository CI suite must still pass" even if a consumer's environment lacks `jsonschema`.

### D18. Spec text amended in this change, before implementation

**Decision.** Block 1 rewrites these spec scenarios and requirements in `specs/changes/<this change>/`. Implementation blocks follow the amended text only.

- **trace-gate**
  - Define the internal `broken_at`, and state that every `stops_at` means "number of retained hops".
  - "Over-long trace": `stops_at = 20`.
  - "Cut after re-ask": hops 1–2, `stops_at = 2`.
  - "Cut at hop 1": the entry is dropped at stage `trace` with reason `cut_at_entry`, and no path is produced.
  - "Malformed/empty": `broken_at = 1`, followed by a re-ask.
  - Replace "stops_at = 1" wording with `broken_at`.
- **path-tracing**
  - Drop reasons use the D14 codes (`trace_call_failed`, `malformed_answer`, `empty_trace`).
  - "Zero hops" and "Unparseable answer" expect two calls.
- **paths-output-contract**
  - Entry kinds per D3, plus `source`, `handler` and `invocation`.
  - "Known discovery case": kind `argparse_subcommand`, with `open`, `edit` and `render` resolving to `cmd_open`, `cmd_edit` and `cmd_render` through `COMMANDS` and the known edge from `commands.py:409`.
  - Top-level fields per D16.
  - "Test-listing shape" becomes `tests.e2e` and `tests.mocks_hop_2` holding records sorted by `(file, line, node_id)`.
  - Replace "Every drop is recorded" with the D14 table.
  - The schema path per D16.
- **entry-point-model-fallback**
  - "Well-formed answer is parsed": kind `other`, name `GET /items`.
  - Add a scenario: one valid entry plus one entry with `line_end < line_start` gives exactly one `citation_unverified` drop, and accounting holds.
  - Add a scenario for `malformed_entry`.
  - The cache key includes `commit` and `FALLBACK_PROMPT_VERSION`.
- **path-scope-and-cap**
  - The drop reason set becomes stage plus reason per D14.
  - Sort order: stage, path, line, kind.
  - Add a scenario: a changed `SKILL.md` line of a merged invoker keeps the path.
  - Add a scenario: a changed `[project.scripts]` key line keeps the path.
- **entry-point-discovery**
  - Replace the pr_pipeline scenarios with the live `COMMANDS` resolution.
  - Add a separate synthetic `if args.command == "x"` fixture scenario.
  - Amend the dispatch-dict rule per D6.
  - Click/typer names are lowercased (add `Deploy_All` → `deploy-all`).
  - The merge key per D7.
  - The `skill_command` and `hook_command` handler per D6.
  - Add a `uv run --directory DIR python -m …` scenario.
  - Add `snapshot_mismatch` to the diagnostic reasons, noting it is produced only by `map_paths`.
- **e2e-test-listing**
  - Rename `e2e_tests` → `tests.e2e` and `e2e_tests_mocking_hop_2` → `tests.mocks_hop_2` throughout.
- **proposal "Known discovery case"**
  - Correct the citation to `pr_pipeline/commands.py:400,409`.

**Why.** Each contradiction was already settled in one place. Leaving the other specs unchanged would make an implementer following any one spec write wrong numbers. Shipping the spec fixes first makes them the single source of truth.

### D19. Out of scope for this PR (follow-ups)

**Decision.** The following are not built, because no spec scenario needs them. Each falls back to a diagnostic or a non-match, never to a crash:

- cross-function subparser registration;
- dotted or inline-table `[project.scripts]` beyond plain keys, `module:obj.attr` targets, and `__init__` re-export chasing;
- tilde fences and interpreter flags (`-I`, `-E`, `-P`) in `SKILL.md`;
- exec-form hook commands;
- the pytest-click `cli_runner` fixture, `@pytest.mark.usefixtures`, `pytestmark`, `MonkeyPatch.context()`, class-level `@patch`, `__test__ = False`, and excluding `Test*` classes that define `__init__`;
- Click ≥ 8.2 suffix stripping.

The one extra that is kept, `uv run` option peeling (D6), has a spec scenario added in D18.

**Why.** It keeps the parser surface proportional to the fixture-test bar this PR sets.

### Reference-doc alignment and departures

- **uv-python-best-practices.md.** Followed. uv is the only tool, the single root `uv.lock` is regenerated with `uv lock`, and tests run through `uv run`. Per repository convention, dev tooling stays in the existing `dev` extra plus `dev` dependency group rather than `[tool.uv] dev-dependencies`.
- **python-project-structure.md.** Three departures, all to match the existing package:
  - **No src layout.** code_facts is already a flat package that four skills import through an editable path source, and moving it is out of scope.
  - **Stdlib logging, not structlog.** `log = logging.getLogger(__name__)` with %-style arguments, as every module in this repository does.
  - **No pydantic-settings.** A library takes explicit arguments, and configuration belongs to callers.
- Type hints follow the doc's modern syntax (`X | None`, `list[...]`).

## Risks / Trade-offs

- **Static matching misses dynamic shapes.**
  - Discovery misses dynamic `add_parser` names, plugin registries and decorator factories.
  - E2E listing misses parametrized argv, helper-wrapped invocations and fixtures that invoke the CLI.
  - Mitigations: every skipped discovery candidate yields a diagnostic, the model fallback can add entries back, and the e2e lists are labelled "structurally invoke". Absence of a test in `tests.e2e` is not proof of no coverage, and consumers must present it that way.
- **Re-ask failures propagate while first-call failures drop.** This asymmetry is spec-mandated. A transient error on a re-ask aborts the whole run. Callers should map transient errors inside their `CallFn` (they already retry once, for example `review_pipeline/sdk.py:184-200`) and pass budget and trust errors in `fatal`.
- **Unscoped runs are unbounded.** With `changed_lines=None`, every entry is traced and no `trace_limit` applies. That is spec-mandated. Callers own budgets. The walkthrough adoption in PR 2 will pass seeds or scope.
- **Snapshot verification costs git reads.** It costs one git read per distinct entry file, cached. Paths that touch only the filesystem (discovery, the import graph, e2e) still trust the snapshot. E2E listing is never cross-checked against git, so a dirty tree could list a test that does not exist at the commit.
- **The pr_pipeline handlers depend on a cross-module dispatch-dict rule.** The rule is narrow: an import-graph link plus an `args.<dest>` subscript call. A refactor to `getattr(commands, f"cmd_{args.command}")` would silently return `handler=None`. The live-shape fixture test, plus a skip-if-absent smoke test against `plugins/multiplai-dev/skills/pr/scripts`, catches that.
- **Click ≥ 8.2 naming differs from the 8.1 rule used here.** On 8.2, a function `sync_cmd` is exposed as `sync`, while discovery names it `sync-cmd`. The path is still traced (the name is a label). Only `cli_runner` and `cli_argv` matching for such commands is affected. This is a follow-up.
- **`base_side_citation` is checked after `text_mismatch` (spec order).** A base-side citation whose text does not match head reports `text_mismatch`. The base-side fixture must cite head-valid text. This is flagged as a spec oddity, not changed.
- **A strict top-level `TraceAnswer` may reject harmless extra keys** a model adds, beyond `notes`. Such answers cost a re-ask and may be dropped as `malformed_answer`. This is accepted, to make "no branching" enforceable.
- **The gate exists twice until PR 2.** The walkthrough's `trace_gate` and code_facts' `check_trace` will drift if either changes before the walkthrough moves over. PR 2 deletes the walkthrough copy.
- **Hand-written schema vs models.** These can drift. The field-order test and the "every model-produced document validates; every valid fixture parses" tests are the guard.
- **Cache keys include the whole change scope.** Adding one changed line re-requests every trace, by spec. That is cheap to reason about, and expensive on large diffs.
- **Spec amendments (D18) widen this PR's diff.** That is accepted: implementing against contradictory specs is worse.

## Global Constraints

- Python floor: `requires-python = ">=3.12"`.
- Package directory: `plugins/multiplai-dev/lib/code_facts/code_facts/`.
- Schema file: `plugins/multiplai-dev/lib/code_facts/code_facts/schema/paths.v1.schema.json`, loaded via `importlib.resources.files("code_facts") / "schema" / "paths.v1.schema.json"`.
- Schema `$schema` value: byte-for-byte equal to the `$schema` value in `plugins/multiplai-dev/skills/review/schema/facts.v1.schema.json`.
- Schema `$id` value: the `$id` of `facts.v1.schema.json` with `facts.v1` replaced by `paths.v1`.
- `schema_version` literal: `"paths.v1"`.
- Top-level field order: `schema_version`, `commit`, `scoped`, `cap`, `paths`, `dropped`, `fallback`, `diagnostics`, `test_listing_skips`.
- Path field order: `id`, `entry`, `hops`, `stops_at`, `break_reason`, `changed_lines_covered`, `tests`.
- Entry field order: `kind`, `name`, `source`, `location`, `handler`, `invocation`, `also_invoked_by`.
- Path id regex: `^p[1-9][0-9]*$`.
- Hop limit: `HOP_LIMIT = 20`.
- Defaults: `cap=5`, `trace_limit=20`, `DEFAULT_CONCURRENCY = 4`.
- Allowed tools on every model call: `["Read", "Grep", "Glob"]`.
- Budget labels: `paths:fallback`, `paths:trace`, `paths:retrace`.
- Prompt versions: `TRACE_PROMPT_VERSION = 1`, `FALLBACK_PROMPT_VERSION = 1` (integers; bumped on any template text change).
- Hop-1 anchoring sentence in `trace_entry`: `Hop 1 MUST cite <location.path>:<location.line> (or <handler.path>:<handler.line>) with its exact text, even when that file is not Python.`
- `EntryKind` values, in `KIND_RANK` order: `argparse_subcommand`, `click_command`, `typer_command`, `main_module`, `project_script`, `skill_command`, `hook_command`, `seed`, `other`.
- `source` values: `discovery`, `seed`, `model`.
- Drop stage order: `model_fallback_citation`, `import_reachability`, `trace_limit`, `trace`, `no_changed_line`, `cap`.
- Drop reasons: `missing_citation`, `citation_unverified`, `malformed_entry`, `duplicate`, `unreachable`, `over_trace_limit`, `trace_call_failed`, `malformed_answer`, `empty_trace`, `cut_at_entry`, `no_changed_line_covered`, `over_cap`.
- Gate break codes: `malformed_answer`, `empty_trace`, `path_outside_root`, `missing_file`, `line_out_of_range`, `text_mismatch`, `base_side_citation`, `entry_mismatch`, `unconnected_hop`, `repeated_hop`, `hop_limit`.
- Output `break_reason` enum: `path_outside_root`, `missing_file`, `line_out_of_range`, `text_mismatch`, `base_side_citation`, `entry_mismatch`, `unconnected_hop`, `repeated_hop`, `hop_limit`.
- Diagnostic reasons: `parse_error`, `unresolved_target`, `unresolved_handler`, `dynamic_name`, `seed_not_found`, `snapshot_mismatch`.
- Fallback status values: `ok`, `failed`, `skipped`.
- E2E match kinds, in tie-break order: `direct_call`, `cli_argv`, `cli_runner`, `subprocess`, `runpy`.
- Test-listing skip reasons: `decode_error`, `syntax_error`.
- Test-listing keys: `tests.e2e`, `tests.mocks_hop_2`.
- Discovery excluded directories: `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `site-packages`, `build`, `dist`.
- Test-listing excluded directories: `.git`, `.venv`, `venv`, `node_modules`, `__pycache__`, `site-packages`.
- Test file patterns: `test_*.py`, `*_test.py`.
- `SKILL.md` fence info strings: `bash`, `sh`, `shell`, `zsh`, `console` (backtick fences only).
- Expanded variables: `${CLAUDE_SKILL_DIR}`, `${CLAUDE_PLUGIN_ROOT}`, `$CLAUDE_PROJECT_DIR`, `${CLAUDE_PROJECT_DIR}`.
- Click/typer default name: `function_name.lower().replace("_", "-")`.
- Cache layout: `<cache_dir>/paths/{fallback,trace,retrace}/<sha256 hex>.json`, each `{"key": …, "answer": …}`.
- Serialisation: `json.dumps(obj, indent=2, ensure_ascii=False) + "\n"`, UTF-8.
- All file writes: temp file in the same directory, then `os.replace`. The temp file is removed on failure.
- Output model config: `ConfigDict(extra="forbid", frozen=True)` on subclasses of `code_facts.models._Model`.
- No module under `plugins/multiplai-dev/lib/code_facts/` imports `walkthrough_pipeline`.
- `explore.ask`, `explore.run_tasks`, `explore.gate_answers` and every existing export keep their current signatures and behaviour.
- Every module starts with `from __future__ import annotations` and logs through `log = logging.getLogger(__name__)` with %-style arguments.
- No library function or class name starts with `test` (pytest collection).
- Subprocesses (git only): a fixed argv list, `shell=False`, `stdin=subprocess.DEVNULL`, `check=False`.
- Runtime dependency added: `jsonschema>=4` in `plugins/multiplai-dev/lib/code_facts/pyproject.toml`, imported only inside `write_paths_file`.
- Lockfile: regenerate the root `uv.lock` with `uv lock` run at the repository root.
- Test command: `uv run --directory plugins/multiplai-dev/lib/code_facts python -m pytest tests/` (pytest, `asyncio_mode = "auto"`).