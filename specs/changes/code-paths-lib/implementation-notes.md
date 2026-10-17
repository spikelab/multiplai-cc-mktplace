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
