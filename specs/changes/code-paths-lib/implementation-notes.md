# Implementation Notes

Surprises reported by the build's agents in their `SURPRISES:` / `SPEC_IMPACT:`
slots, appended as they happened. `contradicts` means the block could only be
built by doing something the spec/design does not say (or says otherwise).
These notes are the input to the end-of-build respec proposal (`respec.md`);
no spec file is edited from them automatically.

## Block 1 — Write an empty but valid `paths.v1` document through `map_paths` and `write_paths_file` (test_writer)
- SPEC_IMPACT: clarify
- SURPRISES:
  (1) The block says `fallback=True` is recorded as skipped "until block 7", so testing it would lock in behaviour that block 7 replaces; I left it untested. (2) The spec doesn't say what happens with `cap`/`trace_limit` below 1 on an unscoped run; I pinned "accepted, cap written as null". (3) The interfaces don't say which module holds the non-exported names, so the tests look them up by name. (4) The empty-drop-reason scenario is listed as satisfied but the block's schema bullet list has no minLength; I test the schema rejects `""` and don't require the model to. (5) The schema reject list includes `scoped: "false"`, which assumes a strict boolean in the schema.

## Block 1 — Write an empty but valid `paths.v1` document through `map_paths` and `write_paths_file` (implementer)
- SPEC_IMPACT: clarify
- SURPRISES:
  (1) The schema is generated from the models by `paths_schema_text()`, as review's facts.v1 is, so `$defs` use class names rather than D16's lowercase names. Fields with defaults (`note`, `side`) are always serialised, so the schema requires them (`json_schema_serialization_defaults_required`). (2) The models also check id contiguity, that `cap` is set exactly when scoped, no scope-stage drops or coverage when unscoped, and that a fallback reason is given exactly when the status is `failed` (all from D4/D14). (3) The interface types drop `reason`, `break_reason` and `Invoker.kind` as `str`; the models narrow them to closed Literals (`DropReason`, checked per stage; `CutReason`, the break codes that can cut a kept path; `DiscoveredKind`), so the schema has enums for all three. (4) The design doesn't cover changed-line paths with `\` or non-integer lines; I raise ValueError and TypeError. (5) The block's test command (`uv run --directory …`) creates an untracked `code_facts/uv.lock` on every run; I deleted it. (6) The spec's "Top-level document shape" table still lists 6 of the 9 required fields; it's outside D1 rows 6/7/9/10, so I left it.

## Block 1 — Write an empty but valid `paths.v1` document through `map_paths` and `write_paths_file` (refactorer)
- SPEC_IMPACT: clarify
- SURPRISES:
  The block's test command `uv run --directory plugins/multiplai-dev/lib/code_facts python -m pytest tests/` creates an untracked `code_facts/uv.lock` because the library isn't a workspace member, which goes against D18's "only the root uv.lock changes"; I deleted it. Model docstrings are written into the committed schema as `description`s, so any docstring edit means regenerating the schema. `normalise_changed_lines` raises `TypeError` (not `ValueError`) for a line that isn't an int; I left that as it is.

## Block 2 — Discover argparse subcommands and caller seeds from a static `PyIndex` (test_writer)
- SPEC_IMPACT: clarify
- SURPRISES:
  (1) The specs don't say whether an argparse subcommand with no handler mechanism at all (no set_defaults, no dict, no comparison) gets an `unresolved_handler` diagnostic: D6 implies yes, while the block text lists only the comparison and out-of-repository cases. Left unpinned. (2) Block 1's `Invoker.kind` is the output-kind Literal, so it cannot hold discovery kinds in `Entry.also_invoked_by`; the merge block will need to resolve this. (3) Not specified for seeds: `location.line` of a line seed, and the `seed_not_found` line when the file is missing, even though "line is null only for parse errors" implies one is required. (4) The `repo_root` fallback could be spelt `""` or `"."`; the test accepts both. (5) The live pr_pipeline gets `commands` through a function-local `from . import commands` inside `_main`, not `main`; the tests mirror that.

## Block 2 — Discover argparse subcommands and caller seeds from a static `PyIndex` (implementer)
- SPEC_IMPACT: clarify
- SURPRISES:
  (1) The test's `_ReversedScandir` stand-in had no `__next__`, and Python 3.12's `os.walk` calls `next()` on it; fixed in the test. (2) Block 1's `PathDiagnostic` requires `line ≥ 1` or null, so `seed_not_found` for a symbol seed on a missing file has `line: null`, against "line is null only for parse errors"; an absolute or escaping seed path raises `ValueError` instead of producing a diagnostic. (3) `unresolved_handler` is emitted only when a `set_defaults(func=…)` or a dispatch-dict value fails to resolve; a subcommand with no handler mechanism gets no diagnostic, unlike D6's "every null handler" wording. (4) Block 1's `Invoker.kind` is the output-kind Literal, so it can't hold discovery kinds in `also_invoked_by`. (5) Seed rules the spec leaves open: symbol wins over line; a line seed's location is the seed line; a line seed with no enclosing def gives `handler: null` and the name `<path>:<line>`. (6) Dispatch dicts match the `add_parser` literal, not the space-joined name, and are searched in the parser module plus its directly imported same-repo modules. (7) `entries.py` and `pyindex.py` import models from `paths.py`, so `map_paths` will need a local import of `entries` (or the models moved) to avoid a circular import.

## Block 2 — Discover argparse subcommands and caller seeds from a static `PyIndex` (refactorer)
- SPEC_IMPACT: none
- SURPRISES:
  `Diagnostic` in `entries.py` is an alias of block 1's `PathDiagnostic`, not a separate class. That works because the fields are identical. Also, `ruff check` flags `entries.py`'s import block (`I001`), which was there before this refactor.

## Block 3 — Trace each discovered entry into a gated, linear path with one re-ask (test_writer)
- SPEC_IMPACT: clarify
- SURPRISES:
  (1) The block doesn't say what an empty hop list gives at the gate level; the tests expect `gate_hops([])` to return broken_at 1 with `empty_trace`, matching trace-gate's "Empty hop list". (2) `TraceContext` is undefined, so `trace_entry` is tested only through `map_paths`. (3) The fake needs `paths_reask` to name the entry (its handler symbol or name) even when there are no accepted hops, because re-ask labels can't be linked back to an entry. (4) trace-gate's "Malformed or empty answers break at hop 1" scenarios still say "stops_at = 1"; that is gate-level `broken_at`, and I left them alone because they are outside D1 rows 2–5. (5) Whether `assign_ids` itself sorts is left open: the ordering is tested through `map_paths`, and `assign_ids` only on input that is already ordered.

## Block 3 — Trace each discovered entry into a gated, linear path with one re-ask (implementer)
- SPEC_IMPACT: clarify
- SURPRISES:
  (1) D9 says `ask` and `run_tasks` are "reused unchanged", but `ask` hard-coded `ExploreOutput`, an `explore:` budget label and `model_dump()`. It now takes optional `schema`, `budget_label` (a function of the key) and `encode`, with defaults that keep the old behaviour. `budget_label` now replaces any value in `call_kwargs` instead of raising `TypeError`. (2) Circular import: `map_paths` lives in `paths.py`, which `entries`, `pyindex` and `tracing` import, so it imports them inside the function. (3) `TracedPath` requires a valid id, so `trace_entry` uses the placeholder "p1" until `assign_ids` renumbers. (4) `TraceHop` lines are plain ints, unlike walkthrough `Hop` (`ge=1`), so that `line_out_of_range` can cover start below 1 or end before start, as D11 lists. (5) Answers are validated as they are stored; one that doesn't parse becomes `{"malformed": …}`. (6) The drop sort's last key is the kind string, per D4's wording; `KIND_RANK` would be the other reading. (7) `UNTRUSTED_RULES` says "report it as a gotcha", but trace answers have no gotchas, so the trace prompts use a variant that points at `note`. The original constant is byte-identical. (8) `GateResult.broken_at` is 21 for `hop_limit`. (9) The constraint's test command (`uv run --directory …/code_facts`) creates a stray `code_facts/uv.lock`; it was deleted.

## Block 4 — Persist answers on disk so reruns resume and stay byte-identical (test_writer)
- SPEC_IMPACT: clarify
- SURPRISES:
  The interface gives no module for `_DiskAnswers`, `cache_key` or `purge_failed`, and doesn't say whether `_DiskAnswers` takes `cache_dir` or `cache_dir/paths-answers`. "Kind included" in `cache_key` doesn't say how. The behaviour for an empty or corrupt cache file at start-up isn't specified; I test it as a skipped key. `map_paths` doesn't do reachability yet, so "affected entries" for a new changed line means every entry, which the test asserts.

## Block 4 — Persist answers on disk so reruns resume and stay byte-identical (implementer)
- SPEC_IMPACT: clarify
- SURPRISES:
  The interface names no module for `_DiskAnswers`, `cache_key` or `purge_failed`; I added `code_facts/cache.py`, which D2's module table doesn't list. "Kind included" in `cache_key` doesn't say how; I hash `{"kind": kind, "payload": payload}` as canonical JSON. "`cache_dir=None` keeps a plain dict": I use the memory-only `_DiskAnswers(None)` so `purge_failed` gets its declared type. The spec doesn't cover unreadable cache files or the cache file format; I treat a bad file as missing and write files in the global serialisation format. Running `uv run --directory plugins/multiplai-dev/lib/code_facts …` created an untracked `code_facts/uv.lock`, which I deleted.

## Block 4 — Persist answers on disk so reruns resume and stay byte-identical (refactorer)
- SPEC_IMPACT: none
- SURPRISES:
  `cache_key` hashes through `canonical_digest`, which uses `ensure_ascii=False`; the interface text names only `sort_keys` and `separators`. This gives the same keys for ASCII inputs and differs only for non-ASCII paths, so I left it as is.

## Block 5 — Discover click, typer, `[project.scripts]` and `__main__` entries, and merge duplicate invokers (test_writer)
- SPEC_IMPACT: clarify
- SURPRISES:
  No naming rule for a typer `@callback` entry, so I left the name unasserted. D6 says every null-handler entry other than comparison-dispatch argparse gets an `unresolved_handler` diagnostic, which would apply to a `main_module` with two calls; the spec's mixed-failure count only works if it doesn't, so I didn't assert either way. The "root group function name as prefix" rule (`cli build-all`) gives names nobody types on the command line. `to_path_entry` currently copies discovery-kind invoker kinds unchanged, so the "output kinds" requirement means it has to change.

## Block 5 — Discover click, typer, `[project.scripts]` and `__main__` entries, and merge duplicate invokers (implementer)
- SPEC_IMPACT: clarify
- SURPRISES:
  (1) D7's null-handler merge on identical (kind, location) contradicts the Block 2 test where one helper declares `list` under two parents on the same line; the Block 2 test was changed to accept the merged form. (2) The Block 2 interface types `Entry.also_invoked_by` as the paths.v1 `Invoker`, whose kinds are output kinds, while D5 and the tests need discovery kinds; I added an `EntryInvoker` model and `to_path_entry` converts it. (3) D6 asks for `unresolved_handler` on every null-handler entry; a `main_module` only gets one when it calls several functions, in line with Block 2's partial approach. (4) The spec has no naming rule for typer callbacks; the command rule is used. (5) click and typer groups and apps are resolved within one module only; click 8.2 suffix stripping and lowercasing are not done. (6) `tomllib` keeps no line numbers, so `[project.scripts]` key lines are found by scanning the text.

## Block 5 — Discover click, typer, `[project.scripts]` and `__main__` entries, and merge duplicate invokers (refactorer)
- SPEC_IMPACT: clarify
- SURPRISES:
  The block-5 implementation commit edited a Block 2 test (`tests/test_entries_argparse.py`) and added a lockfile at `plugins/multiplai-dev/lib/code_facts/uv.lock`, though the design says only the root `uv.lock` changes. `Entry.also_invoked_by` uses a new `EntryInvoker` model with discovery kinds, not the `Invoker` type the Block 2 interface states. My refactor changed none of this.

## Block 6 — Discover `SKILL.md` and hook commands, resolving through shell launchers (test_writer)
- SPEC_IMPACT: clarify
- SURPRISES:
  A command naming no subcommand edges to the main_module location (guard line or line 1), and "handler = the edge target's def, else null" then makes its handler null, so the spec's hook + SKILL.md `python -m pkg` merge scenario could never happen. The tests expect such commands to take the main_module's handler (the def main() that the guard calls). Unspecified details pinned by the tests: command_tokens may return absolute or root-relative paths; resolve_python_m returns `<pkg>/__main__.py` or `<mod>.py`, with "" meaning the snapshot root. The spec-file amendments the block asks for were not made, since this agent writes tests only.

## Block 6 — Discover `SKILL.md` and hook commands, resolving through shell launchers (implementer)
- SPEC_IMPACT: clarify
- SURPRISES:
  (1) D6 rule 7 says a command's handler is "the edge target's def, else null", but that makes a no-subcommand command's handler null, so the spec's "hook and SKILL.md invoke the same module → one entry" could never happen; command entries instead take the main_module handler of the target file (spec amended to say so). (2) D2's module layout has no home for command parsing; it went into a new code_facts/commands.py so later e2e subprocess matching can share it, which meant adding that module to the test helper's lookup list. (3) Unspecified choices: hook `python -m` uses the hooks file's directory where SKILL.md uses its own; relative paths run from the repository root; a named but missing `.py` or `-m` target is `unresolved_target` rather than a launcher fallback; only top-level argparse subcommands are matched by a command's first positional.

## Block 6 — Discover `SKILL.md` and hook commands, resolving through shell launchers (refactorer)
- SPEC_IMPACT: clarify
- SURPRISES:
  A command that can't be tokenised (for example, an unclosed quote) is skipped with no diagnostic in a `SKILL.md`, but gets an `unresolved_target` diagnostic in a hooks file. The spec doesn't cover this case for either source.

## Block 7 — Run a single read-only model fallback for entry points that static discovery missed (test_writer)
- SPEC_IMPACT: clarify
- SURPRISES:
  The capability spec drops a fallback entry that resolves to the same handler as a deterministic one; the block description and D8 compare only lines, so the tests follow the block. No location is defined for drops of entries with no citation, a `../` path or an absolute path, yet `PathEntry.location` is required and must be a valid relative path. The duplicate check is unclear on whether it compares path and line or the line alone; the tests use path and line. `check_quote` passes a range that runs past the end of the file, so a separate range check is needed. The `fallback=False` test already passes because today's stub always reports `skipped`.

## Block 7 — Run a single read-only model fallback for entry points that static discovery missed (implementer)
- SPEC_IMPACT: clarify
- SURPRISES:
  The `Entry` interface lists only the 8 discovery kinds, but `run_fallback` must return `Entry` objects that publish as `model`, so `Entry.kind` was widened to accept `"model"`. A dropped entry with no in-root citation needs a placeholder location (`(unplaced)`, line 1) because `PathEntry.location` is required and the spec gives none. The capability spec's same-handler duplicate rule is missing from D8 and the block; I implemented it. `check_quote` doesn't check whether a range runs past the end of the file, so the fallback reuses the gate's range check. A circular import (the prompt needs `FallbackAnswer`'s schema, and `fallback.py` needs the prompt) is broken by a deferred import inside `paths_fallback`.

## Block 7 — Run a single read-only model fallback for entry points that static discovery missed (refactorer)
- SPEC_IMPACT: clarify
- SURPRISES:
  Besides the cited-line rule, `_Filter._duplicate` also drops a fallback entry whose enclosing `def` is a discovered entry's handler. That follows the requirement "resolves to the same handler as such an entry" (entry-point-model-fallback), but the block text and design D8 list only line matches. I kept the behaviour as it was.

## Block 8 — Scope a run to the changed lines with reachability, trace limit, coverage and cap (test_writer)
- SPEC_IMPACT: clarify
- SURPRISES:
  (1) The spec doesn't say whether the cross-repository fallback order is "repo_b/, repo_b/src/, repo_c/, …" or "all repository roots first, then all src/ directories". I picked fixtures that give the same answer under both readings. (2) Read literally, `from x import y` adds an edge to `x` only, not to `x`'s parent packages; the test follows that wording exactly. (3) The current `path_order` sorts by hop count only, and coverage-first ordering is new, so `order_paths` should replace it. (4) One test passes before implementation (the unscoped scenario), because that behaviour already exists.

## Block 8 — Scope a run to the changed lines with reachability, trace limit, coverage and cap (implementer)
- SPEC_IMPACT: clarify
- SURPRISES:
  (1) D2 places scope in `paths.py`, but `entry_distance` needs `Entry` and `PyIndex`, and both of those import `paths.py`. So the scope filters went into a new `scope.py`, the module name the test helper already looks for. `order_paths` and `sort_drops` stay in `paths.py` because unscoped runs use them too. (2) D13 doesn't say whether the snapshot root counts as a fallback repository when it has no `.git` or `pyproject.toml`. I included it, as `""`, sorted before the other roots, and a module found there must belong to that root's repository. (3) As the spec words it, `from a.b import c` adds edges to `a/b/__init__.py` and `a/b/c.py` but not to `a/__init__.py`, even though Python does run `a/__init__.py`. I followed the spec.

## Block 9 — List end-to-end tests that statically invoke each kept path's entry (test_writer)
- SPEC_IMPACT: clarify
- SURPRISES:
  (1) The live pr tests pass argv as literal lists with computed elements (`str(repo)`), and their `run(argv)` helper calls `build_parser().parse_args(argv)` rather than a function that itself calls parse_args. The tests require both to match as cli_argv, which goes beyond "literal list of string constants". (2) The spec's console-script scenario uses `--help`, which the D15 exclusion forbids; the test uses `build` instead. (3) Unspecified behaviour pinned: build/ and dist/ are searched by listing; a zero-byte test file is neither a skip nor a test; the module may be `code_facts.e2e_tests` or `code_facts.e2e`. (4) The D1#13 spec amendment was not made; this agent writes tests only.

## Block 9 — List end-to-end tests that statically invoke each kept path's entry (implementer)
- SPEC_IMPACT: clarify
- SURPRISES:
  (1) The live pr `run(argv)` helper calls `build_parser().parse_args(argv)`, so `<builder>().parse_args(argv)` with the builder in the parser's module also counts as cli_argv. (2) Literal lists with computed elements match on their constant tokens; spec text said "string constants" only. (3) Typer apps are module-level variables that resolve_name cannot see, so the runner matcher falls back to the new PyIndex.qualified_name plus resolve_module. Typer words are relative to the app's mount prefix, so a single-command typer app invoked without its name is missed. (4) A console script that targets an argparse program's main does not match that program's subcommand paths, only the project_script entry. (5) The reversed-scandir test fake was not an iterator, so Python 3.12 os.walk failed inside the pre-existing PyIndex.build. (6) build/ and dist/ test files are outside PyIndex, so they are added through the new PyIndex.including(). (7) Function-local name scoping for stdlib callees comes from the new qualified_name, so other PyIndex internals were not duplicated.
