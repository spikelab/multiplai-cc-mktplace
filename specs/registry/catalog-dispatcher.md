## ADDED Requirements

### Requirement: Unified entry point dispatches all registered generators
`generate_catalog.py` serves as the single entry point for catalog generation, discovering and invoking all registered generators (memory, diary, skills, resources) in a defined order.

#### Scenario: All generators invoked on full run
- **WHEN** `generate_catalog.py` is executed with no filter arguments
- **THEN** it invokes the memory, diary, skills, and resources generators in that order, and each generator's `generate()` method is called exactly once.

#### Scenario: Execution order is deterministic
- **WHEN** `generate_catalog.py` is executed multiple times under identical conditions
- **THEN** generators always run in the fixed order: memory → diary → skills → resources.

### Requirement: Selective generator execution via filter argument
The dispatcher accepts an optional filter to run only a subset of generators.

#### Scenario: Single generator filter
- **WHEN** `generate_catalog.py` is invoked with `--only diary`
- **THEN** only the diary catalog generator runs; memory, skills, and resources generators are not invoked.

#### Scenario: Multiple generator filter
- **WHEN** `generate_catalog.py` is invoked with `--only memory,diary`
- **THEN** only the memory and diary generators run, in their canonical order (memory before diary).

#### Scenario: Invalid generator name in filter
- **WHEN** `generate_catalog.py` is invoked with `--only nonexistent`
- **THEN** it exits with a non-zero status and logs an error identifying `nonexistent` as an unrecognized generator name.

### Requirement: State-aware skipping of unchanged sources
The dispatcher coordinates with each generator's state tracking to skip regeneration when source content has not changed.

#### Scenario: No source changes since last run
- **WHEN** `generate_catalog.py` runs and `.generation-state.json` shows the content hash for a generator's sources matches the current source hashes
- **THEN** that generator's LLM-based generation is skipped, the existing catalog file is left untouched, and the dispatcher logs that the generator was skipped due to no changes.

#### Scenario: Source changes detected
- **WHEN** `generate_catalog.py` runs and a generator's source files have changed (content hash differs from `.generation-state.json`)
- **THEN** that generator's `generate()` method is called, and on success the new content hashes are written back to `.generation-state.json`.

#### Scenario: Force regeneration bypasses state check
- **WHEN** `generate_catalog.py` is invoked with `--force`
- **THEN** all generators run regardless of source hash state, and `.generation-state.json` is updated with new hashes after each successful generation.

### Requirement: Deletion pruning for removed sources
When source files that contributed to a catalog are deleted, the dispatcher ensures stale entries are pruned from both the catalog and the state file.

#### Scenario: Source file deleted between runs
- **WHEN** a source file (e.g., a memory file or skill file) existed during the previous generation but has since been deleted, and `generate_catalog.py` runs
- **THEN** the corresponding entry is removed from the catalog JSON output, and the source's hash entry is removed from `.generation-state.json`.

#### Scenario: All sources for a generator deleted
- **WHEN** every source file for a generator (e.g., all skill files) has been deleted and `generate_catalog.py` runs
- **THEN** the generator produces an empty catalog (valid JSON with zero entries), and all hash entries for that generator are cleared from `.generation-state.json`.

### Requirement: Config-gated generators are skipped when disabled
Generators gated behind config flags (skills, resources) are only invoked when their respective config is enabled.

#### Scenario: Skills generator disabled in config
- **WHEN** `plugin.json` userConfig has `enable_skills` set to `false` and `generate_catalog.py` runs without `--only`
- **THEN** the skills generator is not invoked, and no skills catalog file is written or modified.

#### Scenario: Resources generator disabled due to missing resources_dir
- **WHEN** `plugin.json` userConfig has `enable_resources` set to `true` but `resources_dir` is not configured or points to a nonexistent directory
- **THEN** the resources generator is skipped, a warning is logged, and no resources catalog file is written.

#### Scenario: Mandatory generators always run
- **WHEN** `generate_catalog.py` runs regardless of config values
- **THEN** the memory and diary generators are always invoked (they are not gated by config flags).

### Requirement: Generation state file management
The dispatcher manages `.generation-state.json` in `$CLAUDE_PLUGIN_DATA/catalogs/`, creating it on first run and updating it atomically.

#### Scenario: First run with no existing state file
- **WHEN** `generate_catalog.py` runs and `$CLAUDE_PLUGIN_DATA/catalogs/.generation-state.json` does not exist
- **THEN** all generators run unconditionally, and a new `.generation-state.json` is created containing per-generator source hashes and timestamps.

#### Scenario: Corrupt state file
- **WHEN** `generate_catalog.py` runs and `.generation-state.json` exists but contains invalid JSON
- **THEN** the dispatcher logs a warning, treats all generators as needing regeneration (equivalent to `--force`), and overwrites the corrupt file with a valid state file after generation completes.

#### Scenario: State file updated atomically
- **WHEN** a generator completes successfully
- **THEN** `.generation-state.json` is written atomically (write-to-temp-then-rename) so a crash mid-write does not leave a corrupt state file.

### Requirement: Catalogs directory auto-creation
The dispatcher ensures the output directory exists before any generator writes.

#### Scenario: Catalogs directory does not exist
- **WHEN** `generate_catalog.py` runs and `$CLAUDE_PLUGIN_DATA/catalogs/` does not exist
- **THEN** the directory is created (including intermediate directories) before any generator is invoked.

#### Scenario: Catalogs directory already exists
- **WHEN** `generate_catalog.py` runs and `$CLAUDE_PLUGIN_DATA/catalogs/` already exists
- **THEN** the existing directory and its contents are left untouched; no error is raised.

### Requirement: Generator failure isolation
A failure in one generator does not prevent other generators from running.

#### Scenario: One generator fails, others succeed
- **WHEN** the skills generator raises an exception during `generate()` but memory, diary, and resources generators succeed
- **THEN** the memory, diary, and resources catalogs are written successfully, `.generation-state.json` is updated for the successful generators only, and the dispatcher logs the skills generator error with full traceback.

#### Scenario: All generators fail
- **WHEN** every generator raises an exception during `generate()`
- **THEN** `generate_catalog.py` exits with a non-zero status, logs all errors, and `.generation-state.json` retains its pre-run state (no partial updates for failed generators).

### Requirement: Dry-run mode reports actions without side effects
The dispatcher supports a dry-run mode that reports what would happen without writing any files or calling LLMs.

#### Scenario: Dry run with pending changes
- **WHEN** `generate_catalog.py` is invoked with `--dry-run` and source files have changed since the last run
- **THEN** it outputs which generators would run and which would be skipped, without modifying any catalog files, `.generation-state.json`, or making any LLM calls.

#### Scenario: Dry run with no pending changes
- **WHEN** `generate_catalog.py` is invoked with `--dry-run` and no source files have changed
- **THEN** it reports that all generators would be skipped, and exits with a zero status.

### Requirement: Exit code reflects generation outcome
The dispatcher communicates success or failure via its exit code.

#### Scenario: All generators succeed
- **WHEN** all invoked generators complete without error
- **THEN** `generate_catalog.py` exits with status `0`.

#### Scenario: At least one generator fails
- **WHEN** one or more generators fail but at least one succeeds
- **THEN** `generate_catalog.py` exits with a non-zero status (e.g., `1`) to signal partial failure.

#### Scenario: Dry run always exits zero
- **WHEN** `generate_catalog.py` is invoked with `--dry-run`
- **THEN** it always exits with status `0` regardless of what actions would have been taken.