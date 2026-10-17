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
