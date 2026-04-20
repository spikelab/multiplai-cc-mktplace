## ADDED Requirements

### Requirement: Skill registration
The `/refresh-catalogs` skill MUST be registered in `skills/` as a discoverable skill so that users can invoke it from the Claude Code prompt.

#### Scenario: Skill appears in available skills
- **WHEN** the plugin is loaded and a user types `/refresh-catalogs`
- **THEN** the skill is recognized and executes the catalog refresh workflow

#### Scenario: Skill file exists at expected path
- **WHEN** the plugin directory is inspected
- **THEN** a skill definition file exists at `skills/refresh-catalogs.md` (or equivalent skill registration path)

---

### Requirement: Default invocation triggers all enabled catalogs
When invoked without arguments, `/refresh-catalogs` MUST regenerate all catalogs that are enabled in the current `plugin.json` configuration.

#### Scenario: All enabled catalogs regenerated
- **WHEN** the user runs `/refresh-catalogs` with no arguments
- **AND** `plugin.json` has memory and diary catalogs enabled (mandatory) and skills catalog enabled via `enable_skills`
- **THEN** the memory, diary, and skills catalog generators are each invoked
- **AND** the resources catalog generator is NOT invoked (since `enable_resources` is not set)

#### Scenario: Only mandatory catalogs when optional catalogs disabled
- **WHEN** the user runs `/refresh-catalogs` with no arguments
- **AND** `enable_skills` and `enable_resources` are both `false` in config
- **THEN** only the memory and diary catalog generators are invoked

---

### Requirement: Force-regenerate mode
`/refresh-catalogs` MUST support a `--force` (or equivalent) flag that bypasses state-aware skipping and regenerates all catalogs regardless of whether source content has changed.

#### Scenario: Unchanged sources are regenerated under force mode
- **WHEN** all catalog sources have unchanged content hashes since the last generation
- **AND** the user runs `/refresh-catalogs --force`
- **THEN** every enabled catalog generator runs and produces updated catalog output
- **AND** `.generation-state.json` is updated with new timestamps

#### Scenario: Normal run skips unchanged sources
- **WHEN** all catalog sources have unchanged content hashes since the last generation
- **AND** the user runs `/refresh-catalogs` without `--force`
- **THEN** generators report that sources are unchanged and skip regeneration
- **AND** existing catalog files are not modified

---

### Requirement: Dry-run mode
`/refresh-catalogs` MUST support a `--dry-run` (or equivalent) flag that reports what would be regenerated without actually performing any generation or writing any files.

#### Scenario: Dry-run reports pending work without side effects
- **WHEN** the memory catalog source has changed since last generation
- **AND** the diary catalog source has not changed
- **AND** the user runs `/refresh-catalogs --dry-run`
- **THEN** the output indicates the memory catalog WOULD be regenerated
- **AND** the output indicates the diary catalog would be SKIPPED
- **AND** no catalog files are written or modified
- **AND** `.generation-state.json` is not modified

#### Scenario: Dry-run with force shows all catalogs as pending
- **WHEN** no catalog sources have changed
- **AND** the user runs `/refresh-catalogs --dry-run --force`
- **THEN** the output indicates ALL enabled catalogs would be regenerated
- **AND** no catalog files are written or modified

---

### Requirement: Output reports per-catalog status
`/refresh-catalogs` MUST report the outcome for each catalog individually so the user can see what was generated, skipped, or failed.

#### Scenario: Mixed results reported clearly
- **WHEN** the user runs `/refresh-catalogs`
- **AND** the memory catalog is regenerated successfully
- **AND** the diary catalog is skipped (unchanged)
- **AND** the skills catalog generator fails with an LLM error
- **THEN** the output shows memory as "regenerated" (or equivalent success status)
- **AND** the output shows diary as "skipped" (or equivalent unchanged status)
- **AND** the output shows skills as "failed" with the error reason

#### Scenario: All catalogs succeed
- **WHEN** the user runs `/refresh-catalogs --force`
- **AND** all generators succeed
- **THEN** each enabled catalog is listed with a success status

---

### Requirement: Invocation delegates to catalog-dispatcher
`/refresh-catalogs` MUST invoke the catalog dispatcher (`scripts/generate_catalog.py`) rather than calling individual generators directly, ensuring consistent orchestration logic.

#### Scenario: Skill calls dispatcher with correct flags
- **WHEN** the user runs `/refresh-catalogs --force`
- **THEN** the skill invokes `generate_catalog.py` (or the dispatcher module) with the force-regenerate flag passed through
- **AND** the dispatcher handles generator orchestration, state tracking, and deletion pruning

---

### Requirement: Handles missing or corrupt state file gracefully
`/refresh-catalogs` MUST not fail if `.generation-state.json` is missing, empty, or contains invalid JSON.

#### Scenario: Missing state file triggers full regeneration
- **WHEN** `$CLAUDE_PLUGIN_DATA/catalogs/.generation-state.json` does not exist
- **AND** the user runs `/refresh-catalogs`
- **THEN** all enabled catalogs are regenerated (since no prior state exists to compare)
- **AND** a new `.generation-state.json` is created with current generation state

#### Scenario: Corrupt state file triggers full regeneration
- **WHEN** `$CLAUDE_PLUGIN_DATA/catalogs/.generation-state.json` contains invalid JSON
- **AND** the user runs `/refresh-catalogs`
- **THEN** all enabled catalogs are regenerated
- **AND** the corrupt state file is replaced with a valid new one

---

### Requirement: Handles missing source directories gracefully
`/refresh-catalogs` MUST not crash if expected source directories (e.g., memory dir, diary dir) do not exist. It should report the issue and continue with other catalogs.

#### Scenario: Missing diary directory skips diary catalog
- **WHEN** the diary source directory does not exist
- **AND** the user runs `/refresh-catalogs`
- **THEN** the diary catalog is reported as skipped or errored with a clear message about the missing directory
- **AND** other enabled catalogs (memory, skills, resources) are still processed

---

### Requirement: No new external dependencies
`/refresh-catalogs` MUST NOT introduce any external dependencies beyond what the plugin already uses. All LLM calls go through `lib/model_client.py`.

#### Scenario: Skill uses existing model_client for LLM calls
- **WHEN** `/refresh-catalogs` triggers catalog generation that requires LLM summarization
- **THEN** all LLM calls are routed through `lib/model_client.py`
- **AND** no direct API calls or new SDK imports are introduced

---

### Requirement: Respects configured model and reasoning effort
`/refresh-catalogs` MUST use the catalog model and reasoning effort settings from `plugin.json` userConfig, defaulting to `claude-sonnet-4-6` at medium reasoning effort.

#### Scenario: Custom model config is respected
- **WHEN** `plugin.json` userConfig specifies `catalog_model: "claude-haiku-4-5"` and `catalog_reasoning_effort: "low"`
- **AND** the user runs `/refresh-catalogs --force`
- **THEN** all LLM calls during catalog generation use `claude-haiku-4-5` with low reasoning effort

#### Scenario: Defaults applied when config is absent
- **WHEN** `plugin.json` userConfig does not specify `catalog_model` or `catalog_reasoning_effort`
- **AND** the user runs `/refresh-catalogs`
- **THEN** LLM calls use `claude-sonnet-4-6` with medium reasoning effort