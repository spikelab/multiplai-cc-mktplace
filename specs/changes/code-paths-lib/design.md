## Context

`plugins/multiplai-dev/lib/code_facts/` is the shared library behind review, codebase-walkthrough, buildme and spec-extract. Today it provides:

- **Read-only explore agents.** `explore_unit` builds a prompt, `run_tasks` / `ask` run it, and every answer is stored under a key as it arrives. A key that already holds an answer is skipped ([`explore.py:109-142`](../../../plugins/multiplai-dev/lib/code_facts/code_facts/explore.py#L109)).
- **Bounded concurrency.** `bounded(...)` runs tasks under a semaphore in a TaskGroup and re-raises the first underlying exception ([`explore.py:52-57`](../../../plugins/multiplai-dev/lib/code_facts/code_facts/explore.py#L52)).
- **Citation re-reading.** `check_citation` resolves a path through the caller's `Resolve` and re-reads the quote at the commit with `multiplai_core.citations.check_quote` ([`explore.py:76-81`](../../../plugins/multiplai-dev/lib/code_facts/code_facts/explore.py#L76)).
- **Pydantic models.** These sit on a private `_Model` base. `Citation` has a `side` field, defaulting to head ([`models.py:21-43`](../../../plugins/multiplai-dev/lib/code_facts/code_facts/models.py#L21)).
- **Caller-supplied seams.** The caller passes in `Resolve` and an async `CallFn` shaped like `agent_call_structured(prompt, schema, **kw)` ([`explore.py:31-32`](../../../plugins/multiplai-dev/lib/code_facts/code_facts/explore.py#L31)). The library itself never calls an SDK.

The only path tracer today lives in `walkthrough_pipeline`. It seeds from webhooks, routes and Celery tasks ([`stages/trace.py:35-38`](../../../plugins/multiplai-dev/skills/codebase-walkthrough/scripts/walkthrough_pipeline/stages/trace.py#L35)). Its `trace_gate` requires that:

- hop 1 holds the seed;
- each later hop is named in the previous hop's quote.

A broken trace gets one retry. If it still breaks, the trace is cut and `stops_at` is recorded ([`gates.py:188-195`](../../../plugins/multiplai-dev/skills/codebase-walkthrough/scripts/walkthrough_pipeline/gates.py#L188), [`trace.py:111-114`](../../../plugins/multiplai-dev/skills/codebase-walkthrough/scripts/walkthrough_pipeline/stages/trace.py#L111)). None of this finds CLI, script, hook or `SKILL.md` entry points. This repository is made almost entirely of those, for example `pr_pipeline`'s argparse CLI ([`pr_pipeline/__main__.py:1`](../../../plugins/multiplai-dev/skills/pr/scripts/pr_pipeline/__main__.py#L1)) and the `run.sh`-launched hooks.

Repository facts that constrain the design:

- **code_facts packaging.** code_facts has its own `pyproject.toml` and is pulled in by four skills as an editable path dependency. It is not a workspace member ([`buildme/scripts/pyproject.toml:51-55`](../../../plugins/multiplai-dev/skills/buildme/scripts/pyproject.toml#L51)). Its current runtime dependencies are `multiplai-core[sdk]` and `pydantic>=2` ([`code_facts/pyproject.toml:6-11`](../../../plugins/multiplai-dev/lib/code_facts/pyproject.toml#L6)).
- **jsonschema.** It is already used in buildme's dev extra to validate `facts.v1` ([`buildme/scripts/pyproject.toml:28-31`](../../../plugins/multiplai-dev/skills/buildme/scripts/pyproject.toml#L28)). It is not a code_facts dependency yet.
- **Schema convention.** Review-viewer treats pydantic models as the source of truth and writes committed JSON Schemas from them ([`review_viewer/models.py:1-8`](../../../plugins/multiplai-dev/skills/review-viewer/scripts/review_viewer/models.py#L1)).
- **Test conventions.** Tests:
  - build real git repos under `tmp_path`;
  - fake every model call with a hand-written async callable;
  - use sentence-style test names and plain `assert`;
  - never name helpers with a `test` prefix ([`review/scripts/tests/test_tree.py:23-30`](../../../plugins/multiplai-dev/skills/review/scripts/tests/test_tree.py#L23), [`walkthrough gates.py:8`](../../../plugins/multiplai-dev/skills/codebase-walkthrough/scripts/walkthrough_pipeline/gates.py#L8)).

This change is additive. It adds new modules, prompts, exports, one schema file and fixtures. It does not change any existing export, and no skill is modified.

## Decisions

### D1. Module layout inside `code_facts/`

New modules under `plugins/multiplai-dev/lib/code_facts/code_facts/`:

| Module | Holds |
|---|---|
| `pyindex.py` | Snapshot walker (exclusions, symlink guard), repository-root detection, dotted-module ↔ file index, `ast` parse cache, static import graph with BFS distance. Shared by discovery, scope and e2e listing. |
| `entries.py` | Deterministic discovery: `discover_entries(root, *, seeds=()) -> Discovery`. Also argparse, click/typer, `[project.scripts]`, `__main__`, `SKILL.md` and hook scanners, merge and diagnostics. Synchronous, with no `CallFn` parameter. |
| `gate.py` | `check_trace(hops, *, entry, edges, places, resolve, root) -> GateOutcome`. Pure, synchronous, no model. |
| `e2e.py` | Static test listing: `list_e2e_tests(root, paths, entries, index) -> (per-path lists, skips)`. |
| `paths.py` | Output and answer models, `map_paths`, scope/cap/ordering, `write_paths_file` and `schema_text()`. |
| `prompts.py` (modified) | Adds `entry_fallback(...)` and `trace_entry(...)`, `retrace_entry(...)`, plus `TRACE_PROMPT_VERSION`. |
| `__init__.py` (modified) | Adds the new exports only. |

**Alternatives considered:**

- **Put everything in `entries.py` + `paths.py` + `models.py`, as the proposal sketches.** Rejected. Discovery, e2e listing and the reachability pre-filter all need the same module index and import graph. Putting that in either file would create an import tangle.
- **A separate `code_facts.paths` sub-package.** Rejected because the library is flat today.

The proposal allows "or additions", and the extra module boundaries are internal.

### D2. Public signatures (integration contract for PRs 2–4)

```python
async def map_paths(
    *,
    root: Path,                       # snapshot root (may hold several repos)
    commit: str,                      # caller's label for the snapshot; echoed into paths.json
    call: CallFn,                     # required
    resolve: Resolve,                 # required; maps snapshot-relative path+side -> (repo, sha, rel)
    changed_lines: Mapping[str, Iterable[int]] | None = None,
    seeds: Sequence[Seed] = (),
    places: Sequence[Place] = (),
    cap: int = 6,
    trace_limit: int | None = None,   # default 4 * cap
    scope_paths: Sequence[str] = (),  # fallback scope when changed_lines is None
    model_fallback: bool = True,
    concurrency: int | None = None,   # None -> existing code_facts default
    recoverable: tuple[type[BaseException], ...] = (),
    cache_dir: Path | None = None,    # None -> in-memory answers, nothing written
    call_kwargs: Mapping[str, Any] | None = None,  # e.g. model/effort/budget_label, passed through
) -> PathsResult

def write_paths_file(result: PathsResult, path: Path) -> None
def discover_entries(root: Path, *, seeds: Sequence[Seed] = ()) -> Discovery
```

Every parameter is keyword-only and has no positional defaults for `call` or `resolve`. Calling `map_paths()` without `call` therefore raises `TypeError` at call time, before any discovery runs. This satisfies the "Missing required argument" scenario.

> **Spec gap:** the output contract's keyword list (root, commit, CallFn, changed lines, seeds, places, cap, concurrency, cache) is not exhaustive. It omits `resolve`, `trace_limit`, `model_fallback`, `scope_paths` and `recoverable`, all of which other specs require. These are added as keywords. The contract list is read as "at least these".

`resolve` is required rather than derived. In a multi-repository snapshot, each top-level directory maps to a different repo and sha, and only the caller knows that mapping. This matches how `check_citation` already works. Tests build one git repo per fixture repository and pass a prefix-splitting resolver.

**Alternative considered:** derive a default resolver from `root` + `commit` when `root` is a git checkout. Rejected, because it silently misbehaves on multi-repo snapshots.

### D3. Kind vocabulary and `source` (resolves a contradiction)

> **Contradiction:** the specs use three different kind vocabularies:
> - entry-point-discovery uses `argparse_subcommand`, `click_command`, `typer_command`, `project_script`, `main_module`, `skill_command`, `hook_command` and `seed`;
> - paths-output-contract uses `argparse`, `click`, `typer`, `script`, `main`, `skill_command`, `hook`, `seed` and `model`;
> - the fallback spec uses "discovery set or `other`", but its example keeps kind `route`.

**Decision:** there is one `EntryKind` enum, used in models, schema and `paths.json`:

`argparse_subcommand`, `click_command`, `typer_command`, `main_module`, `project_script`, `skill_command`, `hook_command`, `seed`, `other`

There is also a separate `source` field: `discovered | seed | model`. Model-fallback entries carry their validated kind, and any kind the model invents becomes `other`. So the fallback "Well-formed answer" scenario's `route` is stored as `other`, and the agent's original label goes into `name`.

The output contract's short names, and its `model` kind, are superseded. Those scenarios are implemented against the enum above.

**Why:** discovery has by far the most scenarios that assert kind literals. Merging "how it was found" (`model`) into `kind` would lose the kind of a model-found argparse command.

**Kind rank:** `KIND_RANK` is the discovery precedence list, with `other` last. It is used for:

- merge precedence;
- pre-trace order;
- output ordering.

A test pins this ranking.

### D4. Deterministic discovery (`entries.py`, `pyindex.py`)

**Walk.** `pyindex.walk(root)` uses `os.scandir`. It:

- sorts names;
- skips the directories `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `site-packages`, `build`, `dist`;
- does not follow a symlink whose `resolve()` is outside `root`.

The repository root of a file is the nearest ancestor at or below `root` that contains `.git` or `pyproject.toml`, falling back to `root`.

**Module index.** Each repository has search roots, in this order:

1. the repository root;
2. `<repo>/src`;
3. every directory under the repository that holds a `pyproject.toml`, and its `src/`.

The third root handles this repository's layout, where each `scripts/` directory is its own project. A dotted name resolves to `<root>/a/b.py` or `<root>/a/b/__init__.py`. The first root that matches wins. Resolution never crosses into another repository, which covers the "Module resolution stays within the right repository" scenario.

**Parsing.** Files are parsed once with `ast.parse` and cached. `SyntaxError` or `UnicodeDecodeError` produces a `parse_error` diagnostic with `line=None`. No snapshot code is ever imported or executed, which covers the sentinel scenario.

**Scanners.** All scanners are pure functions over the parse cache:

- **argparse.**
  - Track names bound to `add_subparsers(...)` results, and the parser each came from, within a function or module scope.
  - Each `<sub>.add_parser("<literal>")` emits an entry. Nested names are built by walking the parent subparser chain.
  - A non-literal name emits a `dynamic_name` diagnostic.
  - Handler resolution happens only through `<p>.set_defaults(func=<Name>)` or a dict literal mapping the literal name to a `Name`. The name is followed through same-module `def` or same-repository `import` / `from … import`.
  - `args.command == "x"` comparisons are deliberately ignored. In the `pr_pipeline` case, the `open`, `edit` and `render` entries keep `handler=None` at their `add_parser` lines.
  - Known edge: from the `args.func(...)` or `HANDLERS[...](...)` call line when present, otherwise from the `set_defaults` line or dict entry line, to the `def` line.
- **click/typer.**
  - Recognise decorators by attribute shape: `click.command`, `click.group`, `<g>.command`, `<g>.group`, `<app>.command`, `<app>.callback`.
  - Group names are built from `@click.group` functions and `app.add_typer(sub, name=…)`.
  - Default names convert `_` to `-`.
  - Known edge: decorator line to `def` line.
- **`[project.scripts]`.**
  - Parse with `tomllib`.
  - Key lines are found by a line scan inside the `[project.scripts]` table, because `tomllib` does not report line numbers.
  - Targets resolve via the module index.
  - Unresolved targets emit `unresolved_target`. Invalid TOML emits `parse_error`.
- **`__main__`.**
  - Covers package `__main__.py` (line 1, dotted package name) and top-level `if __name__ == "__main__":` in non-test files.
  - The handler is resolved only when the body's sole top-level call is a single `Name(...)` or `mod.Name(...)`.
- **`SKILL.md`.**
  - A line scanner tracks fence state and info strings, accepting only `bash`, `sh`, `shell`, `zsh`, `console`.
  - It joins `\` continuations, strips a `$ ` prompt for `console`, and tokenises with `shlex`.
  - It then peels off env assignments and `uv run` (with its options), and records `PYTHONPATH=` and `cd <dir> &&` prefixes.
  - It matches `python|python3 -m <mod> [first-positional]`.
  - It expands `${CLAUDE_SKILL_DIR}` and `${CLAUDE_PLUGIN_ROOT}`.
  - It searches the six roots in the specified order.
  - Known edge:
    1. to the matching argparse subcommand's handler if that subcommand was discovered and resolved;
    2. otherwise to its `add_parser` line;
    3. otherwise to line 1 of `__main__.py`.

  This means `SKILL.md` resolution runs after the argparse pass. Discovery uses two passes: Python first, then text and JSON sources.
- **Hooks.**
  - `json.loads` each `hooks.json` and `.claude/settings*.json` file.
  - Walk `hooks.<event>[].{matcher, hooks[]}` and keep objects with `type == "command"`.
  - The line of each command string is found by scanning the raw text for the JSON-encoded string, assigning occurrences in document order.
  - Variables expand as specified (`${CLAUDE_PLUGIN_ROOT}` through the nearest `.claude-plugin/plugin.json` ancestor).
  - Python `-m` targets use the `SKILL.md` rules. Script paths use the `main_module` rules. Other existing files resolve to line 1.
- **Seeds.**
  - Validated against the snapshot.
  - The handler is the innermost `def` enclosing the line, or the named symbol.
  - Failures emit `seed_not_found`.

**Merge.**

- Group entries by `(handler.path, handler.line)`. Entries with `handler=None` group by identical `location`.
- Keep the entry with the smallest `(KIND_RANK, location.path, location.line)`.
- Move the others into `also_invoked_by`, sorted by `(path, line, kind)`.
- Union the `known_edges` of the group, with duplicates removed and sorted.

The proposal's "most specific kind" rule is the precedence list, as the spec states (assumption to confirm).

**Output.** Entries are sorted by `(location.path, location.line, kind, name)`. Diagnostics are sorted by `(path, line or 0, reason)`, with `None` sorting first. Each skipped candidate produces exactly one diagnostic. Seeds are sorted before processing, so the order the caller passes them in does not matter.

### D5. Model fallback

The fallback is one `ask(...)` under the key `fallback:<sha256(prompt-inputs)>`. The prompt inputs are canonical JSON of:

- the sorted deterministic entries (kind, name, location);
- the sorted scope;
- `TRACE_PROMPT_VERSION`.

The answer model `FallbackAnswer(_Model)` holds `entries: list[FallbackEntry]`. Each `FallbackEntry` has `kind: str`, `name`, `citation: Citation | None` and `reason`.

**Post-processing**, in this order:

1. A missing citation becomes `missing_citation`.
2. A path that is absolute, contains `..`, or does not resolve becomes `citation_unverified`. Otherwise the citation is re-read through `check_citation`, and a failed re-read also becomes `citation_unverified`.
3. Unknown `kind` becomes `other`.
4. Fallback entries citing the same line are collapsed. The first in `(path, line)` order is kept and later ones are silently merged.
5. An entry is dropped as `duplicate` if its line equals any deterministic or seed `location` or `handler` line, or if its enclosing `def` equals a deterministic handler.
6. The survivors are sorted by location and get `source="model"`.

The fallback is skipped (status `skipped`) in these cases:

- when `model_fallback=False`;
- when the scope is empty, meaning `changed_lines == {}` or no `scope_paths` while unscoped (reason `empty_scope`).

A parse failure of the answer gives status `failed` with the pydantic error summary cut to 200 characters, matching the repository's error-truncation convention. The run then continues with deterministic entries only.

**`CallFn` return handling.** `CallFn` may return:

- a schema instance;
- a `dict`;
- a JSON `str`.

The library normalises all three with `Schema.model_validate` / `model_validate_json`. This is what makes "stub returns invalid text" a content failure, separate from `CallFn` raising.

### D6. CallFn error policy (resolves a contradiction)

> **Contradiction:** the specs treat a raising `CallFn` in three different ways:
> - fallback: `RuntimeError` propagates;
> - re-ask: `RuntimeError("budget")` propagates;
> - first trace call: "the fake `CallFn` raises an exception → entry dropped, `CallFn` called once".

**Decision:** use the existing `ask` split between `recoverable` and fatal, with one rule everywhere. On any call (fallback, trace, re-ask), an exception whose type is in the caller's `recoverable` tuple is recorded:

- for a trace, as a `trace` / `trace_call_failed` drop;
- for the fallback, as fallback `failed`.

Every other exception propagates unchanged out of `map_paths`.

`bounded(...)` already re-raises the first underlying exception, not an `ExceptionGroup`. The default `recoverable=()` means "propagate everything". The path-tracing scenario's test therefore passes `recoverable=(FakeCallError,)` and raises that type. Nothing is retried within a run.

Recoverable error answers are stored by `ask` as `{"error": …}`. At the start of each `map_paths` run, `{"error": …}` entries under this run's keys are deleted. This means a later run asks again, which is the caller's retry, not the library's. It avoids the existing trap where a cached error is never re-asked ([`explore.py:127-142`](../../../plugins/multiplai-dev/lib/code_facts/code_facts/explore.py#L127)).

**Alternative considered:** treat every first-trace exception as a drop. Rejected, because a budget stop must never be swallowed, and that must hold on first asks as well as re-asks.

### D7. Answer cache and resumability

`cache_dir` is backed by a `DirAnswers(MutableMapping[str, dict])`. It stores one file per key, `cache_dir/<sha256(key)>.json`, written atomically with a tempfile in the same directory plus `os.replace`. Because it implements the mapping `ask` and `run_tasks` already accept, both are reused unchanged.

One file per key means:

- concurrent completions never rewrite a shared file;
- an interrupted run keeps every finished answer, which covers the "Resume after interruption → exactly 2 calls" scenario.

With `cache_dir=None` a plain `dict` is used, and `map_paths` writes nothing to disk. Writing `paths.json` is only ever done by `write_paths_file`.

**Keys** are canonical JSON (`sort_keys=True`, sorted lists) hashed with SHA-256:

- **Trace:** `trace` | commit | `(kind, location, handler)` | sorted changed ranges | sorted applicable known edges and places | `TRACE_PROMPT_VERSION`.
- **Re-ask:** the trace key | first-answer digest | `broken_at` | `break_reason`.

Inputs are sorted before hashing, so reordered seeds, changed lines or places hit the same keys. This covers "Reordered inputs hit the cache". The output is rebuilt from stored answers in canonical order, never in completion order.

**Alternative considered:** a single `answers.json` rewritten on every answer, like spec-extract's state. Rejected. Concurrent traces would serialise on it, and a kill during a rewrite could lose everything.

### D8. Tracing

Each distinct surviving entry gets one `run_tasks` task under `bounded(concurrency)`. When the caller passes no `concurrency`, the library's existing default applies. If code_facts has no named default, `DEFAULT_CONCURRENCY = 4` is added, matching the pipelines' configured default.

**Call kwargs:** `allowed_tools=["Read", "Grep", "Glob"]` and `cwd=root`, merged under the caller's `call_kwargs`. The tool list always overrides.

**`trace_entry` prompt** contains:

- the entry kind, location, handler symbol and commit;
- the changed ranges, or the literal statement that there is no change scope;
- the applicable known edges and places;
- the cap of 20;
- the existing citation and untrusted-content rule blocks from `prompts.py`.

Repository text quoted in the prompt (`invocation` strings, hook commands) is wrapped with `multiplai_core.untrusted.fence`.

**`TraceAnswer(_Model)`** has exactly `hops: list[Hop]`. `Hop(_Model)` copies the field names of the walkthrough `Hop` (`walkthrough_pipeline/models.py:122-128`), with the citation typed as `code_facts.Citation`. There is no alternative-chain field. A branching answer either fails validation or carries an unknown key that `_Model` ignores, so the hop list it ends up with must still be linear. A test checks the field names by `ast`-parsing the walkthrough file from the repository. It does not import the walkthrough, so there is no cross-import.

### D9. Trace gate and `stops_at` semantics (resolves contradictions)

`check_trace` runs the hops in order and stops at the first broken one. The hop rules are applied in the specified order:

1. `path_outside_root`: absolute path, a `..` segment, or a normalised path outside `root`.
2. `missing_file`
3. `line_out_of_range`
4. `text_mismatch`
5. `base_side_citation`
6. `entry_mismatch` (hop 1 only): same file as the entry's location and its range includes `location.line`, or alternatively the resolved `handler` line.
7. `unconnected_hop`
8. `repeated_hop`: same `(path, line_start, line_end)` as an earlier hop.

Two outcomes apply to the answer as a whole, before any hop rule: `malformed_answer` and `empty_trace`. Both give `broken_at=1`.

**Reading rules 2–4.** Rules 2–3 read the file through `resolve(path, "head")` and `git show <sha>:<rel>`. Rule 4 then delegates to `multiplai_core.citations.check_quote`, so whitespace normalisation is identical to existing gating.

**Connectivity (rule 7).** For n ≥ 1, hop n+1 is connected if any of these holds:

- the hop's symbol name is a `NAME` token in hop n's cited lines. For `.py` files, tokens come from `tokenize.generate_tokens`, which excludes comments and strings. On a `TokenError` from a partial fragment, the fallback strips quoted spans and `#` comments by regex before matching identifiers.
- there is a known edge from hop n's location to a line in hop n+1's range;
- hop n+1's range contains a caller place.

Non-Python files (`SKILL.md`, JSON) have no name tokens, so they only connect through edges and places.

The spec rules are followed, not the walkthrough's extra "its lines must define that name" rule. The two copies of the gate therefore differ in that one respect until PR 2 moves the walkthrough onto this gate.

**Gate result.** The result is `GateOutcome(accepted: list[Hop], broken_at: int | None, reason: BreakReason | None, detail: str)`.

> **Contradictions:**
> - trace-gate defines `stops_at` as the index of the first broken hop, and over-long traces get `stops_at=21`;
> - path-tracing says `stops_at=20` for the hop limit;
> - the output contract defines `stops_at` as the index of the last hop kept, with `1 ≤ stops_at ≤ len(hops)` and "third hop fails → `stops_at: 2`, 2 hops".
>
> **Decision:** the gate reports `broken_at` (first broken index) internally. The published `stops_at` is the number of hops kept, which equals `broken_at - 1`, or 20 for the hop limit. The trace-gate scenarios are implemented as assertions on `broken_at` plus `reason`. For example, "breaks at hop 3 → stops_at 3" becomes `broken_at == 3`, `stops_at == 2`. The contract's invariant holds for every written path.

**Hop limit.** When there are more than 20 hops, the first 20 are kept and checked. If they pass, the outcome is `broken_at=21`, `reason="hop_limit"`, and there is no re-ask. Exactly 20 hops is not a cut.

**Re-ask.** This is the gate's rule, adopted over path-tracing's "malformed → drop" wording. Any outcome with a reason other than `hop_limit`, including `malformed_answer` and `empty_trace`, gets exactly one `retrace_entry` re-ask through `ask`. The re-ask prompt states:

- the broken hop index;
- the reason code literal, e.g. `text_mismatch`;
- the detail;
- the accepted prefix.

The re-answer is gated from hop 1 and is never re-asked again. If it still breaks:

- with `broken_at ≥ 2`: the path keeps the re-answer's accepted prefix, with `stops_at = broken_at - 1` and `break_reason = reason`;
- with `broken_at == 1`: no path exists, because the contract requires 1–20 hops. The entry is dropped at stage `trace`, with reason `malformed_trace`, `empty_trace` or `cut_at_entry` as appropriate.

For path-tracing's "Unparseable answer" and "Zero hops" scenarios, this means two `CallFn` calls, then a drop.

### D10. Scope, cap, ordering and ids

**Validation.** All of this happens before any `CallFn` call:

- changed-line paths are normalised to POSIX with no leading `./`;
- absolute paths or paths escaping `root` raise `ValueError`;
- lines below 1 raise `ValueError`;
- when scoped, `cap < 1` or `trace_limit < 1` raises `ValueError`.

A missing file is tolerated.

**Reachability** applies only when `changed_lines is not None`. Changed `.py` files are seeds for a reverse BFS over `pyindex`'s import graph, which handles relative imports, `from . import b` and package `__init__`. Unparseable modules have no out-edges, and unresolved imports add none. The BFS terminates on cycles.

An entry's distance is:

- **0** if its location file changed;
- otherwise the BFS distance of its handler module, or of its location module when there is no handler and the location is `.py`. The module's own change counts as distance 0.

`None` means the entry is dropped at stage `import_reachability` with reason `unreachable`, before tracing.

**Trace limit.** Reachable entries are ordered by `(distance, KIND_RANK, path, line)`. Entries past `trace_limit` are dropped at stage `trace_limit` with reason `over_trace_limit`. The default is `4 × cap` (assumption to confirm, as is `cap=6`, which mirrors the walkthrough's default scenario count).

**Coverage.** A traced path covers a changed line when:

- the hop is one of the retained hops, so hops after a cut never count; and
- the hop's file equals the changed file; and
- the line falls in `[line_start, line_end]`.

`changed_lines_covered` is the sorted, de-duplicated set. A path with no coverage is dropped at stage `no_changed_line` with reason `no_changed_line_covered`.

**Ordering** applies in both modes:

1. `-len(covered)`
2. retained hop count
3. `KIND_RANK`
4. path
5. line

When scoped, paths past `cap` are dropped at stage `cap` with reason `over_cap`. Ids `p1..pN` are assigned last, so dropped paths never consume ids. Unscoped runs record no scope drops and set `cap: null`.

**Accounting.** E is defined as:

- deterministic and seed entries after merge; plus
- fallback entries after same-line collapse.

kept + drops always equals E. A test asserts this.

### D11. Drop record vocabulary (resolves a contradiction)

> **Contradiction:**
> - path-scope-and-cap gives reasons `unreachable`, `over_trace_limit`, `no_changed_line_covered`, `over_cap`, sorted by path, line, kind;
> - the contract gives `stage ∈ {model_fallback_citation, import_reachability, trace_limit, no_changed_line, cap}` with a free-text `reason`, sorted by stage first;
> - neither covers trace failures or fallback duplicates.

**Decision:** `DropRecord(stage, entry, reason)` uses closed enums per stage:

| stage (sort order) | reasons |
|---|---|
| `model_fallback_citation` | `missing_citation`, `citation_unverified`, `duplicate` |
| `import_reachability` | `unreachable` |
| `trace_limit` | `over_trace_limit` |
| `trace` | `trace_call_failed`, `malformed_trace`, `empty_trace`, `cut_at_entry` |
| `no_changed_line` | `no_changed_line_covered` |
| `cap` | `over_cap` |

Sorting is by stage order, then `(location.path, location.line, kind)`. Within a stage this equals the scope spec's ordering. The `trace` stage is new and is added to the contract. When unscoped, only `model_fallback_citation` and `trace` can appear.

### D12. Static e2e test listing (`e2e.py`)

**Discovery.** Test files match `test_*.py` or `*_test.py` under `root`. The directories `.git`, `.venv`, `venv`, `node_modules`, `__pycache__` and `site-packages` are skipped. This is the e2e spec's list, which deliberately does not exclude `build`/`dist`.

**Parsing.**

- Undecodable files give skip reason `decode_error`. Files that fail to parse give `syntax_error`.
- Tests are module-level `test*` functions, or `test*` methods of module-level `Test*` classes.
- Node ids are `<root-relative file>::[Class::]name`.

**Name resolution.**

- A per-module import table maps local names to dotted targets: `import a.b as c`, `from a.b import x as y`, and attribute chains on imported modules.
- Dotted names map to files via `pyindex`.
- To support this repository's convention of tests inserting `scripts/` on `sys.path`, the test file's nearest ancestor holding a `pyproject.toml`, and the parent of its `tests/` directory, are added as extra roots.

**Matchers**, evaluated per call node in the test body. Nested `def`s are excluded.

| match | rule |
|---|---|
| `direct_call` | callee resolves to `(handler.path, handler.symbol)` |
| `cli_argv` | callee resolves to the program's `main` or the function owning the subparser setup; first arg is a literal list or tuple of `str` constants containing the subcommand's name tokens, in order |
| `cli_runner` | `<x>.invoke(<resolved group/app>, <literal args>)` where `<x>` is `CliRunner()` or a name bound to it |
| `subprocess` | `subprocess.{run, call, check_call, check_output, Popen}` with a literal list or string. Tokens are compared after normalising the interpreter (`sys.executable`, `python`, `python3`): `-m <module> [sub]`, script name first, or the hook command string after `shlex` split |
| `runpy` | `runpy.run_module("<module>")` |

Matching applies to the entry and every `also_invoked_by` invoker.

**Hop-2 patches.** The hop-2 symbol is taken from retained hops only. A test is moved to `mocks_hop_2` when a patch targets the hop-2 symbol at either:

- its defining module; or
- its import alias in hop 1's module.

Patches are recognised in these forms:

- decorator, `with`, or `.start()` forms of `mock.patch` / `unittest.mock.patch` / `patch` with a string target;
- `patch.object(<resolved>, "<name>")`;
- `monkeypatch.setattr` with a dotted string or `(obj, "name")`.

A patch counts if it appears in the test or in a fixture the test requests by parameter, or an `autouse=True` fixture. Fixtures come from the test's own module or from `conftest.py` files from the test's directory up to `root`. Fixture-to-fixture requests are followed transitively, with a visited set. `patch_line` is the patch's own line, which may be in `conftest.py`.

> **Contradiction:** the output contract gives `tests: {e2e: [str], mocks_hop_2: [str]}`, sorted lexically. The e2e spec gives `e2e_tests` / `e2e_tests_mocking_hop_2` arrays of records, sorted by `(file, line, node_id)`.
>
> **Decision:** keep the contract's container and key names (`tests.e2e`, `tests.mocks_hop_2`). Fill them with the e2e spec's records: `node_id`, `file`, `line`, `match`, `invocation_line`, plus `patch_line` for mocks. Sort by `(file, line, node_id)`. Each test is deduplicated to its earliest `invocation_line`, then by match-kind order. No `node_id` appears in both lists.

### D13. Output models, schema and writing

**Models.** Output models subclass `_Strict(_Model)` with `extra="forbid"`. The agent-answer models stay on `_Model`, which ignores extra keys. Both satisfy the `issubclass(_Model)` scenario. Model validators enforce:

- `stops_at` and `break_reason` are both null or both set, with `1 ≤ stops_at ≤ len(hops)`;
- no test id overlaps between the two lists;
- id contiguity;
- path rules: the pattern `^(?!/)(?!.*(^|/)\.\.?(/|$))[^\\]+$` and `line ≥ 1`.

**Top-level document** (`PathsResult`) has these required fields, in this order:

1. `schema_version`
2. `commit`
3. `scoped`
4. `cap`
5. `fallback` (`{status, reason}`)
6. `paths`
7. `dropped`
8. `diagnostics`
9. `test_listing_skips`

> **Spec gap:** the contract lists only six top-level fields with `additionalProperties: false`. Discovery diagnostics, fallback status and test-listing skips must also be recorded. They are declared as additional required fields so that empty arrays stay explicit. The `Entry` object likewise carries `handler`, `invocation`, `known_edges` and `source` alongside the contract's fields.

**Schema.** The schema is generated, following the review-viewer convention. `paths.schema_text()` takes `PathsResult.model_json_schema()` and injects:

- the `$schema` value copied from `facts.v1.schema.json`;
- `$id`;
- `title`.

It is serialised with the same writer as the paths file.

The committed file sits at `plugins/multiplai-dev/lib/code_facts/code_facts/schema/paths.v1.schema.json`. It is inside the package so `importlib.resources` can load it at runtime under the editable install. The proposal's `schema/…` is read relative to the package. A test asserts the committed file equals `schema_text()`.

**Alternative considered:** a hand-written schema. Rejected, because it would drift from the models.

**`write_paths_file`** works as follows:

1. Serialise with `model_dump(mode="json")`, which keeps declaration order, then `json.dumps(indent=2, ensure_ascii=False) + "\n"`.
2. Validate with `jsonschema.Draft202012Validator`, or whichever draft `facts.v1` declares. On failure, raise `PathsSchemaError` naming the first error's JSON pointer, `"/" + "/".join(error.absolute_path)`, chosen by sorting errors by path. Nothing is written.
3. Write a tempfile in the target's directory, then `os.replace` it over the target. The tempfile is unlinked in `finally` on any failure.

This gives byte stability, the round-trip property and atomicity.

**Dependency.** `jsonschema>=4` becomes a runtime dependency of code-facts, because validation happens at write time. The lockfile is regenerated.

**Alternative considered:** validating only in tests. Rejected, because the spec requires validation before every write.

### D14. Tests

All tests live under `plugins/multiplai-dev/lib/code_facts/tests/` and are run with `uv run` from the code_facts directory.

**Fixtures** are small repository trees under `tests/fixtures/paths/`. Each one is copied to `tmp_path` and `git init`-committed with the fixed author environment the review tests use. Fixtures cover:

- each entry kind;
- the `pr_pipeline` `args.command` shape (a fixture copy, not the live file);
- a multi-repo root;
- side-effect sentinel modules;
- broken TOML, JSON and Python;
- test suites with patches and conftest fixtures.

**Fake `CallFn`.** A recording `FakeCall` class keyed on prompt markers. It can:

- return canned answers;
- add random delays to vary completion order;
- count in-flight calls;
- raise chosen exceptions.

**Guard tests:**

- AST-scan `code_facts/` for any `walkthrough_pipeline` import.
- Compare the hop model's fields with the walkthrough `Hop` via AST.
- Check `__init__` exports are a superset of the previous list.
- Patch `subprocess.Popen` and `os.system` to raise during discovery and listing.

## Risks / Trade-offs

- **Gate logic exists twice until PR 2.** The code_facts gate also intentionally differs from the walkthrough gate: it has no "lines define the name" check, and it adds known edges and places. Mitigation: both are pinned by tests, and PR 2 deletes the walkthrough copy.
- **Static name resolution is heuristic.** Two kinds of misses are expected:
  - **e2e listing:** tests that build argv dynamically, call through wrappers, or rely on `sys.path` manipulation outside the root heuristics will be missed. This is acceptable, because the spec makes non-literal argv non-matching by design, and false negatives only shrink the lists.
  - **reachability:** dynamic imports (`importlib`, `__import__`) and `sys.path` hacks are invisible to the import-graph pre-filter. An entry that really does reach changed code can be dropped as `unreachable`. This is recall loss, which the PR #295 check in the PR description is meant to surface. It is the main reason recall is not claimed in this PR.
- **Multiple search roots can make module resolution ambiguous.** For example, two `scripts/` projects can both contain a `lib` package. The first root in a fixed order wins, which is deterministic but can be wrong. This is mitigated by staying inside the file's own repository, and, for test imports, by preferring the test's own project root first.
- **`tokenize` on cited fragments.** Fragments can be syntactically incomplete. The regex fallback for string and comment stripping can mis-classify unusual literals, such as triple-quoted strings starting mid-range, and so accept or reject a connection wrongly.
- **Line finding in TOML and JSON is a text scan**, not parser positions. Unusual formatting can mislocate a line. Examples are a key quoted differently from its parsed form, or a command string with escapes that encodes differently. A mislocated line still points into the right file, and the gate re-reads citations anyway.
- **Re-asking malformed and empty answers costs up to one extra call per entry.** This was a deliberate choice of the gate spec over path-tracing's immediate drop. Budgeting stays with the caller's `CallFn`.
- **Cached `{"error"}` answers are cleared at run start.** This deviates from `ask`'s skip-on-present behaviour elsewhere in code_facts. It is scoped to this run's keys only, and existing callers are unaffected.
- **Adding `jsonschema` at runtime** pulls a dependency into every skill that uses code_facts: buildme, review, walkthrough and spec-extract. It is small and pure-Python, and buildme already resolves it.
- **Defaults are assumptions to confirm:**
  - `cap=6`;
  - `trace_limit = 4 × cap`;
  - discovery merge precedence;
  - `model_fallback=True`;
  - skipping the fallback on an empty scope.
- **Spec conflicts resolved in this design** (D3, D6, D9, D11, D12, D13) mean some scenario literals are implemented in translated form. These are the kind names, `stops_at` values in the trace-gate scenarios, drop vocabulary, test-list field names and top-level fields. The spec text should be updated to match before or alongside implementation, so that reviewers do not flag the translations as deviations.

## Global Constraints

- Python floor: `requires-python = ">=3.12"`.
- Every new module starts with `from __future__ import annotations` and defines `log = logging.getLogger(__name__)`.
- No module under `plugins/multiplai-dev/lib/code_facts/` imports `walkthrough_pipeline`.
- No function or class in library code has a name starting with `test` or `Test`.
- Agent tool allow-list on every fallback, trace and re-ask call: `["Read", "Grep", "Glob"]`.
- Model calls go only through the caller's `call` argument. There is no SDK import, no retry, and no budget in the library.
- Hop cap constant: `MAX_HOPS = 20`.
- Path id format: `^p[1-9][0-9]*$`, assigned `p1`, `p2`, … in output order.
- `schema_version` literal: `"paths.v1"`.
- Schema file: `plugins/multiplai-dev/lib/code_facts/code_facts/schema/paths.v1.schema.json`. Its `$schema` value must equal the one in `plugins/multiplai-dev/skills/review/schema/facts.v1.schema.json`.
- Runtime dependency added to code-facts: `jsonschema>=4`.
- `EntryKind` values: `argparse_subcommand`, `click_command`, `typer_command`, `main_module`, `project_script`, `skill_command`, `hook_command`, `seed`, `other`.
- `KIND_RANK` order: `argparse_subcommand`, `click_command`, `typer_command`, `main_module`, `project_script`, `skill_command`, `hook_command`, `seed`, `other`.
- Entry `source` values: `discovered`, `seed`, `model`.
- Discovery diagnostic reasons: `parse_error`, `unresolved_target`, `unresolved_handler`, `dynamic_name`, `seed_not_found`.
- Discovery excluded directories: `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `site-packages`, `build`, `dist`.
- `SKILL.md` fence info strings scanned: `bash`, `sh`, `shell`, `zsh`, `console`.
- Hook files read: `hooks.json` and `.claude/settings*.json`. Only `"type": "command"` hooks are read.
- Fallback status values: `ok`, `failed`, `skipped`.
- Break reasons: `malformed_answer`, `empty_trace`, `path_outside_root`, `missing_file`, `line_out_of_range`, `text_mismatch`, `base_side_citation`, `entry_mismatch`, `unconnected_hop`, `repeated_hop`, `hop_limit`.
- Gate rule order for a single hop: `path_outside_root`, `missing_file`, `line_out_of_range`, `text_mismatch`, `base_side_citation`, `entry_mismatch`, `unconnected_hop`, `repeated_hop`.
- Published `stops_at` is the count of retained hops (`1 ≤ stops_at ≤ len(hops)`). It is `20` for `hop_limit`.
- Drop stages, in sort order: `model_fallback_citation`, `import_reachability`, `trace_limit`, `trace`, `no_changed_line`, `cap`.
- Drop reasons: `missing_citation`, `citation_unverified`, `duplicate`, `unreachable`, `over_trace_limit`, `trace_call_failed`, `malformed_trace`, `empty_trace`, `cut_at_entry`, `no_changed_line_covered`, `over_cap`.
- Test-listing file patterns: `test_*.py` and `*_test.py`. Excluded directories: `.git`, `.venv`, `venv`, `node_modules`, `__pycache__`, `site-packages`.
- Test match kinds, in tie-break order: `direct_call`, `cli_argv`, `cli_runner`, `subprocess`, `runpy`.
- Test-listing skip reasons: `decode_error`, `syntax_error`.
- Test lists live at `tests.e2e` and `tests.mocks_hop_2`. Records are sorted by `(file, line, node_id)`.
- Every path in the output is POSIX, relative to the snapshot root, has no leading `/` and no `.` or `..` segment, and has `line ≥ 1`.
- `paths.json` serialisation: UTF-8, `json.dumps(..., indent=2, ensure_ascii=False)`, model field order, exactly one trailing `"\n"`, written to a same-directory tempfile and then `os.replace`d.
- `map_paths` writes nothing outside `cache_dir`. Cache entries are `cache_dir/<sha256(key)>.json`, each written atomically.
- `TRACE_PROMPT_VERSION` is part of every trace and re-ask cache key, and must be bumped whenever the trace prompt text changes.