## ADDED Requirements

### Requirement: LLM client access via model_client
The catalog generation base MUST route all LLM calls through `lib/model_client.py`, using the configured model and reasoning effort settings rather than making direct API calls.

#### Scenario: Default model and effort used when no config override
- **WHEN** a generator invokes the base LLM client without any config overrides
- **THEN** the call is routed through `model_client` using `claude-sonnet-4-6` at medium reasoning effort

#### Scenario: Config-specified model and effort are respected
- **WHEN** `plugin.json` userConfig specifies a different `catalog_model` and `catalog_reasoning_effort`
- **THEN** the base LLM client uses those values for all generation calls

#### Scenario: model_client is the sole LLM interface
- **WHEN** any generator module under `scripts/generators/` makes an LLM call
- **THEN** the call passes through `lib/model_client.py` — no generator imports or instantiates an LLM client directly

---

### Requirement: Retry logic for LLM calls
The base infrastructure MUST retry transient LLM failures with backoff so that a single flaky API call does not abort catalog generation.

#### Scenario: Transient failure followed by success
- **WHEN** an LLM call fails with a retryable error (e.g., 429, 500, 503) on the first attempt
- **THEN** the base retries the call after a backoff delay and returns the successful result

#### Scenario: Persistent failure exhausts retries
- **WHEN** an LLM call fails on every attempt up to the maximum retry count
- **THEN** the base raises an exception to the caller with the last error details, and does not silently return partial or empty results

#### Scenario: Non-retryable errors are not retried
- **WHEN** an LLM call fails with a non-retryable error (e.g., 400 bad request, 401 auth failure)
- **THEN** the base raises the error immediately without retrying

---

### Requirement: Content hashing for source change detection
The base MUST compute deterministic content hashes of source files so that generators can skip unchanged sources on re-run.

#### Scenario: Identical content produces identical hash
- **WHEN** the same source file content is hashed on two separate runs
- **THEN** the resulting hash values are identical

#### Scenario: Modified content produces a different hash
- **WHEN** a source file is modified (even by a single character) between runs
- **THEN** the hash computed on the second run differs from the first

#### Scenario: Hash is computed on file content, not metadata
- **WHEN** a source file's modification timestamp changes but its content does not
- **THEN** the computed hash remains the same as before

---

### Requirement: State file I/O
The base MUST read and write a `.generation-state.json` file in `$CLAUDE_PLUGIN_DATA/catalogs/` that tracks per-entry source hashes and generation metadata.

#### Scenario: State file created on first run
- **WHEN** catalog generation runs for the first time and no `.generation-state.json` exists
- **THEN** the base creates `.generation-state.json` in `$CLAUDE_PLUGIN_DATA/catalogs/` containing the hashes and metadata for all processed entries

#### Scenario: State file read on subsequent run
- **WHEN** catalog generation runs and `.generation-state.json` already exists
- **THEN** the base reads the existing state and makes it available to generators for skip/regenerate decisions

#### Scenario: State file updated after generation
- **WHEN** a generator processes one or more entries (skipped or regenerated)
- **THEN** the base writes the updated state (new hashes, timestamps) back to `.generation-state.json` atomically — a crash mid-write does not corrupt the file

#### Scenario: Corrupt state file triggers fresh regeneration
- **WHEN** `.generation-state.json` exists but contains invalid JSON or does not conform to the expected schema
- **THEN** the base logs a warning, discards the corrupt state, and proceeds as if no state file existed (full regeneration)

---

### Requirement: State-aware skip for unchanged sources
The base MUST compare current source hashes against stored state to skip regeneration of entries whose sources have not changed.

#### Scenario: Unchanged source is skipped
- **WHEN** a source file's content hash matches the hash stored in `.generation-state.json`
- **THEN** the base signals the generator to skip that entry, and the existing catalog entry is preserved unchanged

#### Scenario: Changed source triggers regeneration
- **WHEN** a source file's content hash differs from the hash stored in `.generation-state.json`
- **THEN** the base signals the generator to regenerate that entry

#### Scenario: New source with no prior state triggers generation
- **WHEN** a source file exists but has no corresponding entry in `.generation-state.json`
- **THEN** the base treats it as new and signals the generator to generate a catalog entry for it

---

### Requirement: Deletion pruning for removed sources
The base MUST detect when source files have been deleted since the last run and remove their corresponding entries from both the catalog and the state file.

#### Scenario: Deleted source is pruned from state
- **WHEN** `.generation-state.json` contains an entry for a source file that no longer exists on disk
- **THEN** the base removes that entry from the state file on the next run

#### Scenario: Deleted source is pruned from catalog output
- **WHEN** a catalog contains an entry for a source file that no longer exists on disk
- **THEN** the base removes that entry from the catalog output on the next run

#### Scenario: All sources deleted results in empty catalog
- **WHEN** every source file tracked in the state has been deleted
- **THEN** the base produces an empty catalog (valid JSON structure with zero entries) and clears the state file entries

---

### Requirement: Schema versioning for catalogs
Each catalog MUST include a schema version so that consumers can detect and handle format changes.

#### Scenario: Catalog includes schema version field
- **WHEN** the base produces a catalog JSON file
- **THEN** the output contains a top-level `schema_version` field with a string value following semver format (e.g., `"1.0.0"`)

#### Scenario: Schema version is bumped on breaking format change
- **WHEN** the catalog format changes in a backward-incompatible way (e.g., field removed or renamed)
- **THEN** the `schema_version` major version is incremented

#### Scenario: Consumer can read schema version before parsing entries
- **WHEN** a consumer (e.g., `context_manager.py`) reads a catalog file
- **THEN** it can access `schema_version` without first parsing the entries array, enabling version-gate logic before full deserialization

---

### Requirement: Shared base class or module interface for generators
The base MUST provide a common interface that individual generators implement, ensuring consistent lifecycle (load state → detect changes → generate → write catalog → save state).

#### Scenario: Generator implements required interface methods
- **WHEN** a new generator is created under `scripts/generators/`
- **THEN** it implements the base interface methods (at minimum: enumerate sources, generate entry, merge into catalog) and the base orchestrates the lifecycle

#### Scenario: Base handles lifecycle even if generator raises
- **WHEN** a generator's `generate entry` method raises an exception for a specific source
- **THEN** the base logs the error, skips that entry, and continues processing remaining sources — it does not abort the entire catalog

#### Scenario: Multiple generators can coexist without interference
- **WHEN** two different generators (e.g., memory and skills) run in sequence using the same base
- **THEN** each maintains its own state entries and catalog output with no cross-contamination of hashes or entries

---

### Requirement: Catalogs directory initialization
The base MUST ensure the `$CLAUDE_PLUGIN_DATA/catalogs/` directory exists before any read or write operations.

#### Scenario: Directory created if missing
- **WHEN** catalog generation runs and `$CLAUDE_PLUGIN_DATA/catalogs/` does not exist
- **THEN** the base creates the directory (including any missing parent directories) before writing any files

#### Scenario: Existing directory is left untouched
- **WHEN** catalog generation runs and `$CLAUDE_PLUGIN_DATA/catalogs/` already exists with prior catalog files
- **THEN** the base does not delete or recreate the directory — existing files are preserved unless explicitly overwritten by the generation process