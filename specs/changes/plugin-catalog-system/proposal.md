## Why

Catalog generation currently lives outside the plugin — in `claude-code-multiplai/dotfiles/hooks/` as standalone Python scripts. This creates three problems:

1. **Fragile coupling**: The dotfiles scripts depend on plugin internals (paths, config, diary format) but aren't versioned or tested with the plugin. Changes to either side break silently.

2. **No diary catalog**: Memory, skills, and resources all have catalogs that let `context_manager` make fast routing decisions without scanning raw files. Diary entries — the fastest-growing context source — have no catalog. Every diary lookup requires scanning and parsing individual day files, which degrades as history grows.

3. **Naming mismatch**: `context_router.py` manages context assembly, fallback logic, and catalog reads — it routes as one sub-task, but its responsibilities are broader. The name actively misleads contributors about where to put new context logic.

Moving generation into the plugin, adding diary catalogs, and renaming the manager makes the system self-contained, testable, and honest about what each piece does.

## What Changes

- **Catalog generation moves into the plugin** as `scripts/generators/`, with a shared base providing LLM calls (via `model_client`), state tracking, and content hashing. The external dotfiles scripts become dead code.
- **A new diary catalog** summarizes per-day entries (sessions, projects, topics, word count) so context assembly can select relevant days without reading raw files.
- **State-aware regeneration** tracks source hashes per entry; unchanged sources are skipped on re-run. Deleted sources are pruned from catalogs.
- **`context_router.py` becomes `context_manager.py`**, with all hook references and imports updated.
- **Catalog generation piggybacks on `/dream`**, so catalogs stay fresh as part of the existing reflection cycle. A separate `/refresh-catalogs` skill provides manual control with dry-run support.
- **Fail-open behavior**: if a mandatory catalog (memory, diary) is missing or corrupt, `context_manager` falls back to live file scanning rather than failing the context call.
- **New config surface** in `plugin.json` for enabling optional catalogs (skills, resources), setting model/effort for generation, diary window size, and catalog TTL.

## Capabilities

### New Capabilities

- `catalog-generation-base`: Shared infrastructure for catalog generators — LLM client via model_client, retry logic, content hashing, state file I/O, and schema versioning.
- `memory-catalog-generator`: Port memory catalog generation into the plugin, preserving hand-authored fields (sections, bundle, co_retrieve_for) across regeneration.
- `diary-catalog-generator`: New per-day diary catalog with configurable lookback window, producing session/project/topic summaries and word counts.
- `skills-catalog-generator`: Port skills catalog generation into the plugin, gated on `enable_skills` config.
- `resources-catalog-generator`: Port resources catalog generation into the plugin, gated on `enable_resources` and `resources_dir` config.
- `catalog-dispatcher`: Unified entry point (`generate_catalog.py`) that orchestrates all generators with state-aware skipping and deletion pruning.
- `catalog-dream-integration`: Wire catalog regeneration into the `/dream` lifecycle and `autodream.py` call chain.
- `refresh-catalogs-skill`: Manual `/refresh-catalogs` skill with force-regenerate and dry-run modes.
- `catalog-config-surface`: New `plugin.json` userConfig entries for catalog model, reasoning effort, TTL, diary window, and optional catalog gates.

### Modified Capabilities

- `context-manager-rename`: Rename `context_router.py` to `context_manager.py`, updating hooks.json, imports, and tests.
- `context-manager-catalog-read`: Add catalog-first read path to context manager with fail-open fallback to live scanning when mandatory catalogs are missing.

## Impact

- **Files renamed**: `scripts/context_router.py` → `scripts/context_manager.py`; `hooks.json` path reference updated.
- **New files**: 6 generator modules under `scripts/generators/`, 1 new skill under `skills/`.
- **Modified files**: `plugin.json` (config schema), `scripts/generate_catalog.py` (stub → dispatcher), `scripts/lib/paths.py` and `scripts/lib/config.py` (catalog/state helpers), `skills/dream.md` (catalog regen hook).
- **Runtime dependency**: All LLM calls route through `lib/model_client.py` using `claude-sonnet-4-6` at medium reasoning effort (configurable). No new external dependencies.
- **Data**: New `$CLAUDE_PLUGIN_DATA/catalogs/` directory with up to 4 JSON catalogs and a `.generation-state.json` tracking file.
- **Backward compatibility**: Fail-open design means missing catalogs degrade to current behavior (live scan), not errors. External dotfiles scripts are superseded but not deleted (they live in a different repo).