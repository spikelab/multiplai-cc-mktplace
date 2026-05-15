---
name: dream
description: "Manual AutoDream trigger — consolidates learnings into memory"
---

# Multiplai Dream — Manual AutoDream Trigger

You are the multiplai dream consolidation skill. Your job is to trigger the AutoDream pipeline on demand, synthesizing accumulated learnings from recent sessions into updated memory files.

## Steps

1. **Check for pending learnings:**
   Run `python scripts/autodream.py --check` to inspect what needs consolidation.
   - If the output says nothing to consolidate (empty or no new learnings), inform the user and exit — do not proceed to step 2.

2. **Run the consolidation pipeline:**
   Run `python scripts/autodream.py --run` to extract learnings and synthesize them into memory file updates via the model client abstraction.

3. **Report a summary of results** as markdown:
   - **Number of learnings processed** — how many learning lines were consumed
   - **Memory files updated** — which files received new content
   - **Items skipped** (if any) — files that were not updated and why
   - If an error or partial failure occurred, report it clearly so the user can decide next steps

<!-- catalog-regen -->
## Catalog Regeneration

After the consolidation and diary write completes, regenerate catalogs to keep indexes fresh:

4. **Refresh catalogs after consolidation:**
   Run `python scripts/generate_catalog.py` to invoke the catalog dispatcher (`generate_catalogs`) and regenerate all enabled catalog indexes (memory, diary, and any optional catalogs like skills/resources).

   - The dispatcher uses state-aware skipping — only catalogs whose source content has changed since the last run are regenerated. This keeps dream execution fast.
   - If catalog generation fails or errors occur, the dream cycle still completes successfully. Catalog failures are logged but do not block or prevent the dream from finishing.
   - The catalog dispatcher handles deletion pruning automatically — removed source files are cleaned up from catalog entries.
   - All LLM calls for catalog generation route through model_client using the configured catalog_model and reasoning effort.

## Memory Version Control

If the memory directory is under git version control, `autodream.py`
automatically commits memory file changes after consolidation and catalog
regeneration complete. The commit message is `dream: consolidate YYYY-MM-DD`.

If the memory directory is **not** a git repository, auto-commit is skipped
with a warning in the log — this is non-fatal. The user can initialize
git tracking at any time via `git init` in their memory directory (see
`/multiplai:setup` for the guided prompt).

## Constraints
- The autodream script uses the path resolver for all file locations — never hardcode paths.
- All LLM calls go through the model client abstraction — never import the SDK directly.
- If an error occurs during synthesis, the script rolls back gracefully and does not leave memory files in a partial or corrupted state.
- Auto-commit failures (permissions, detached HEAD, etc.) are logged as warnings and never block the dream from completing.
