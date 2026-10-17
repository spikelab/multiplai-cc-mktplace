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
