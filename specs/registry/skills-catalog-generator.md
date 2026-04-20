## ADDED Requirements

### Requirement: Skills catalog generator produces a valid catalog from skill files
The skills catalog generator MUST scan all `.md` files in the `skills/` directory, send each skill's content to the LLM via `model_client`, and produce a `skills.json` catalog in `$CLAUDE_PLUGIN_DATA/catalogs/` containing a summary, trigger phrases, and metadata for each skill.

#### Scenario: Generate skills catalog from existing skill files
- **WHEN** the skills catalog generator is invoked and the `skills/` directory contains one or more `.md` skill files
- **THEN** a `skills.json` catalog is written to `$CLAUDE_PLUGIN_DATA/catalogs/` with one entry per skill file, each entry containing at minimum the skill name, a summary, and trigger phrases

#### Scenario: Skills catalog entry structure
- **WHEN** a skill file `skills/dream.md` is processed by the generator
- **THEN** the resulting catalog entry includes the fields: `name`, `file`, `summary`, `triggers`, and `content_hash`

### Requirement: Skills catalog generation is gated on enable_skills config
The skills catalog generator MUST only run when the `enable_skills` configuration option is set to `true` in `plugin.json` userConfig. When disabled, the generator MUST skip execution without error.

#### Scenario: Generator skips when enable_skills is false
- **WHEN** the skills catalog generator is invoked and `enable_skills` is `false` in plugin config
- **THEN** the generator returns early without producing or modifying any catalog file, and no LLM calls are made

#### Scenario: Generator skips when enable_skills is not set
- **WHEN** the skills catalog generator is invoked and `enable_skills` is not present in plugin config
- **THEN** the generator treats the value as `false` and skips execution without error

#### Scenario: Generator runs when enable_skills is true
- **WHEN** the skills catalog generator is invoked and `enable_skills` is `true` in plugin config
- **THEN** the generator proceeds to scan skill files and generate the catalog

### Requirement: Skills catalog generator uses shared base infrastructure
The skills catalog generator MUST extend the shared catalog generation base class, using `model_client` for LLM calls, content hashing for state tracking, and the base's retry logic and schema versioning.

#### Scenario: LLM calls route through model_client
- **WHEN** the skills catalog generator processes a skill file
- **THEN** the LLM summarization call is made via `model_client` using the configured model and reasoning effort, not through direct API calls

#### Scenario: Generator uses base class content hashing
- **WHEN** the skills catalog generator processes a skill file
- **THEN** it computes a content hash of the source file using the shared base's hashing method and stores it in the generation state

#### Scenario: Catalog includes schema version
- **WHEN** a skills catalog is generated
- **THEN** the output JSON includes a `schema_version` field provided by the base infrastructure

### Requirement: Skills catalog generator performs state-aware regeneration
The generator MUST track content hashes per skill file in `.generation-state.json`. Unchanged skill files MUST be skipped on re-run. Only modified or new skill files trigger LLM calls.

#### Scenario: Skip unchanged skill files
- **WHEN** the skills catalog generator runs and a skill file's content hash matches the hash stored in `.generation-state.json`
- **THEN** the existing catalog entry for that skill is preserved without making an LLM call

#### Scenario: Regenerate modified skill files
- **WHEN** the skills catalog generator runs and a skill file's content has changed since the last run (hash mismatch)
- **THEN** the generator makes an LLM call to re-summarize that skill and updates both the catalog entry and the stored hash in `.generation-state.json`

#### Scenario: Generate entry for new skill files
- **WHEN** the skills catalog generator runs and a new `.md` file exists in `skills/` that has no entry in `.generation-state.json`
- **THEN** the generator makes an LLM call to summarize the new skill, adds an entry to the catalog, and records the hash in `.generation-state.json`

#### Scenario: State file records per-skill hashes
- **WHEN** the skills catalog generator completes a run
- **THEN** `.generation-state.json` contains a content hash entry for every skill file that was processed, keyed by file path

### Requirement: Skills catalog generator prunes deleted skill files
When a skill file that previously had a catalog entry no longer exists in `skills/`, the generator MUST remove its entry from the catalog and from `.generation-state.json`.

#### Scenario: Prune catalog entry for deleted skill
- **WHEN** the skills catalog generator runs and `.generation-state.json` references a skill file `skills/old-skill.md` that no longer exists on disk
- **THEN** the entry for `old-skill.md` is removed from `skills.json` and its hash is removed from `.generation-state.json`

#### Scenario: Pruning does not trigger LLM calls
- **WHEN** a skill file is deleted and the generator runs
- **THEN** the pruning of the deleted entry completes without making any LLM calls

### Requirement: Skills catalog generator handles empty skills directory
The generator MUST handle the case where the `skills/` directory exists but contains no `.md` files, and the case where the directory does not exist.

#### Scenario: Empty skills directory produces empty catalog
- **WHEN** the skills catalog generator runs with `enable_skills` set to `true` and the `skills/` directory contains no `.md` files
- **THEN** a valid `skills.json` is written with an empty entries array and the schema version field

#### Scenario: Missing skills directory produces empty catalog without error
- **WHEN** the skills catalog generator runs with `enable_skills` set to `true` and the `skills/` directory does not exist
- **THEN** a valid `skills.json` is written with an empty entries array and no exception is raised

### Requirement: Skills catalog generator uses configured model and reasoning effort
The generator MUST respect the catalog model and reasoning effort settings from `plugin.json` userConfig, defaulting to `claude-sonnet-4-6` at medium reasoning effort.

#### Scenario: Default model and effort
- **WHEN** the skills catalog generator runs and no custom `catalog_model` or `catalog_reasoning_effort` is set in plugin config
- **THEN** LLM calls are made using `claude-sonnet-4-6` with medium reasoning effort

#### Scenario: Custom model override
- **WHEN** `catalog_model` is set to `claude-haiku-4-5` in plugin config
- **THEN** the skills catalog generator's LLM calls use `claude-haiku-4-5`

#### Scenario: Custom reasoning effort override
- **WHEN** `catalog_reasoning_effort` is set to `low` in plugin config
- **THEN** the skills catalog generator's LLM calls use `low` reasoning effort

### Requirement: Skills catalog generator handles LLM call failures gracefully
When an LLM call fails for a particular skill file, the generator MUST retry according to the base class retry logic. If retries are exhausted, the generator MUST skip that entry, log the failure, and continue processing remaining skill files without aborting.

#### Scenario: Retry on transient LLM failure
- **WHEN** the LLM call for a skill file fails on the first attempt but succeeds on retry
- **THEN** the catalog entry is generated successfully and included in the output

#### Scenario: Skip entry after exhausting retries
- **WHEN** the LLM call for a skill file fails on all retry attempts
- **THEN** that skill file's entry is omitted from the catalog, the failure is logged, and the generator continues processing remaining skill files

#### Scenario: Partial failure produces partial catalog
- **WHEN** 3 skill files exist and the LLM call fails permanently for 1 of them
- **THEN** the resulting `skills.json` contains entries for the 2 successful skills and the generator exits without raising an exception

### Requirement: Skills catalog generator writes atomic output
The generator MUST write the catalog file atomically (write to temp file, then rename) to prevent corrupt partial writes from being read by `context_manager`.

#### Scenario: Atomic write on success
- **WHEN** the skills catalog generator finishes generating all entries
- **THEN** the catalog is written to a temporary file first, then atomically moved to `$CLAUDE_PLUGIN_DATA/catalogs/skills.json`

#### Scenario: No partial catalog on crash
- **WHEN** the generator process is interrupted mid-write
- **THEN** the previous `skills.json` (if any) remains intact and readable