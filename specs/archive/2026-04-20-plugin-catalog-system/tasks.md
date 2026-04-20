## 1. Rename `context_router.py` → `context_manager.py`

Atomic rename of the context routing module to reflect its actual responsibilities. All internal references in `hooks.json`, imports, and tests must be updated in lockstep. Verified complete by grepping for any remaining `context_router` references across the plugin.

Satisfies: Design Decision 9 (Rename strategy)

- [ ] Rename `context_router.py` to `context_manager.py`
- [ ] Update `hooks.json` to reference the new module name
- [ ] Update all internal imports across the plugin to use `context_manager`
- [ ] Update all test files referencing `context_router`
- [ ] Run `grep -r context_router` across the plugin to confirm zero remaining references
- [ ] Add `_read_catalog_or_scan()` stub method with fail-open fallback signature per Decision 8

## 2. Catalog config schema and plugin configuration

Add the `catalogs` configuration block to `plugin.json` `userConfig` with model, reasoning effort, TTL, diary lookback, and opt-in toggles for skills/resources catalogs. Implement a `CatalogConfig` dataclass that reads and validates these settings at runtime. Memory and diary catalogs are always enabled; skills and resources are gated by boolean flags.

Satisfies: Design Decision 10 (Config schema)

- [ ] Add `catalogs` nested object to `plugin.json` `userConfig` with all fields and defaults
- [ ] Create `CatalogConfig` dataclass in `scripts/generators/config.py`
- [ ] Implement config loading from plugin settings with default fallbacks
- [ ] Add validation for enum fields (`reasoning_effort`) and numeric bounds (`ttl_hours`, `diary_lookback_days`)
- [ ] Write unit tests for config loading and validation edge cases

## 3. Generator base class and state management

Implement `GeneratorBase` in `scripts/generators/base.py` with the template-method lifecycle: discover sources, hash, diff against state, call LLM for changed entries, merge results, prune deletions, and write state. Includes atomic state file writes (write-to-tmp then `os.replace()`), the `GenerationResult` dataclass, and `ModelClient` integration with retry logic. This is the foundation all four generators inherit from.

Satisfies: Design Decision 1 (Generator architecture), Decision 2 (Content hashing), Decision 3 (State tracking)

- [ ] Create `scripts/generators/` package with `__init__.py`
- [ ] Implement `GenerationResult` and `GenerationState` dataclasses
- [ ] Implement `GeneratorBase` with template method `run()` orchestrating the full lifecycle
- [ ] Implement `_load_state()` / `_save_state()` with atomic writes and schema versioning
- [ ] Implement `_read_catalog()` / `_write_catalog()` with JSON serialization
- [ ] Implement `_call_llm()` with retry logic via `ModelClient`
- [ ] Implement hash comparison logic: skip unchanged, detect new, detect deleted
- [ ] Implement `dry_run` mode that reports what would change without calling LLM or writing state
- [ ] Implement `force` mode that ignores hashes and regenerates all entries
- [ ] Write unit tests for lifecycle, state persistence, hash diffing, pruning, and error handling

## 4. Memory catalog generator

Implement `MemoryGenerator` inheriting from `GeneratorBase`. Discovers the memory source file, hashes it, builds an LLM prompt to extract structured metadata, and parses the response into a catalog entry. Overrides `merge_entry()` to preserve hand-authored fields (`sections`, `bundle`, `co_retrieve_for`) from existing catalog entries during regeneration.

Satisfies: Design Decision 5 (Memory catalog hand-authored fields), Decision 2 (per-file hashing)

- [ ] Create `scripts/generators/memory.py` with `MemoryGenerator` class
- [ ] Implement `discover_sources()` to locate the memory file
- [ ] Implement `hash_source()` with SHA-256 of file contents
- [ ] Implement `build_prompt()` with memory-specific LLM prompt
- [ ] Implement `parse_response()` to extract structured catalog entry from LLM output
- [ ] Implement `merge_entry()` preserving `sections`, `bundle`, `co_retrieve_for` from existing entries
- [ ] Write unit tests including merge preservation and first-run behavior

## 5. Diary catalog generator

Implement `DiaryGenerator` inheriting from `GeneratorBase`. Discovers per-day directories under `$CLAUDE_PLUGIN_DATA/diary/`, hashes each day as `sha256(sorted(file_contents))`, and generates per-day entries with session summaries, project references, topic tags, and word count. Respects `diary_lookback_days` config to bound the catalog window.

Satisfies: Design Decision 4 (Diary catalog structure), Decision 2 (per-day-directory hashing)

- [ ] Create `scripts/generators/diary.py` with `DiaryGenerator` class
- [ ] Implement `discover_sources()` to enumerate day directories within the lookback window
- [ ] Implement `hash_source()` computing SHA-256 over sorted file contents of a day directory
- [ ] Implement `build_prompt()` with diary-specific LLM prompt for day summarization
- [ ] Implement `parse_response()` producing entries matching the diary catalog schema (date, sessions, topics, projects, word_count)
- [ ] Implement pruning of days that fall outside the lookback window
- [ ] Write unit tests for discovery, hashing across multiple files, lookback windowing, and schema compliance

## 6. Skills and resources catalog generators

Implement `SkillsGenerator` and `ResourcesGenerator`, both inheriting from `GeneratorBase`. Skills discovers one file per skill and generates per-skill entries. Resources discovers files under the configured `resources_dir`. Both are opt-in (disabled by default) and skip entirely when their config flag is false.

Satisfies: Design Decision 10 (enable_skills, enable_resources config flags), Decision 2 (per-file hashing)

- [ ] Create `scripts/generators/skills.py` with `SkillsGenerator` class
- [ ] Create `scripts/generators/resources.py` with `ResourcesGenerator` class
- [ ] Implement `discover_sources()`, `build_prompt()`, and `parse_response()` for each
- [ ] Implement early-exit when the corresponding config flag is disabled
- [ ] Write unit tests for both generators including disabled-state behavior

## 7. Catalog dispatcher

Implement `generate_catalogs()` dispatcher function that sequentially runs enabled generators, collects `GenerationResult` from each, logs progress, and handles early termination on critical failure while continuing past non-critical errors. Supports filtering to specific generators, force mode, and dry-run mode.

Satisfies: Design Decision 6 (Dispatcher design)

- [ ] Create `scripts/generators/dispatcher.py` with `generate_catalogs()` function
- [ ] Implement sequential generator execution with progress logging
- [ ] Implement generator filtering via `generators` parameter
- [ ] Implement error classification: critical (state file corruption) vs. non-critical (single entry LLM failure)
- [ ] Aggregate and return `list[GenerationResult]`
- [ ] Write unit tests for sequencing, filtering, error propagation, and dry-run aggregation

## 8. `context_manager.py` catalog-first read paths

Update `context_manager.py` to read from catalogs as the primary data source, with fail-open fallback to live file scanning. Implement `_read_catalog_or_scan()` for each context type. Missing or corrupt catalogs log a once-per-session warning and degrade gracefully. Diary context assembly uses the new diary catalog for O(1) day selection instead of scanning individual files.

Satisfies: Design Decision 8 (Fail-open strategy)

- [ ] Implement `_read_catalog_or_scan()` with try/catalog-first, catch/fallback-to-scan logic
- [ ] Add catalog read paths for memory, diary, skills, and resources
- [ ] Implement once-per-session warning logging for catalog failures
- [ ] Implement schema version checking — reject catalogs with unknown schema versions and fall back
- [ ] Write unit tests for catalog hit, catalog miss, corrupt JSON, and schema mismatch scenarios

## 9. `/dream` integration and `/refresh-catalogs` skill

Wire catalog regeneration as a post-step in the `/dream` skill, called after reflection completes. Create the `/refresh-catalogs` skill for manual force-regeneration and dry-run inspection, supporting `--dry-run`, `--force`, and `--generators` flags. Both skills invoke the dispatcher and surface `GenerationResult` summaries to the user.

Satisfies: Design Decision 7 (`/dream` integration)

- [ ] Add `<!-- catalog-regen -->` section to `dream.md` skill file calling the dispatcher
- [ ] Create `refresh-catalogs.md` skill file with flag parsing and dispatcher invocation
- [ ] Implement `--dry-run` output formatting showing what would be generated/pruned
- [ ] Implement `--force` flag passthrough to dispatcher
- [ ] Implement `--generators` flag to selectively regenerate specific catalogs
- [ ] Write integration tests verifying `/dream` triggers catalog regeneration
- [ ] Write integration tests for `/refresh-catalogs` with each flag combination