## ADDED Requirements

### Requirement: Resources catalog generator is gated on configuration
The resources catalog generator MUST only run when both `enable_resources` is `true` and `resources_dir` is set to a non-empty string in `plugin.json` userConfig.

#### Scenario: Both config flags enabled
- **WHEN** `enable_resources` is `true` and `resources_dir` is set to a valid directory path in plugin config
- **THEN** the resources catalog generator executes and produces a catalog file at `$CLAUDE_PLUGIN_DATA/catalogs/resources.json`

#### Scenario: enable_resources is false
- **WHEN** `enable_resources` is `false` and `resources_dir` is set to a valid directory path
- **THEN** the resources catalog generator is skipped and no resources catalog file is created or updated

#### Scenario: resources_dir is not set
- **WHEN** `enable_resources` is `true` but `resources_dir` is empty or not configured
- **THEN** the resources catalog generator is skipped and no resources catalog file is created or updated

#### Scenario: Both config flags disabled/missing
- **WHEN** `enable_resources` is `false` and `resources_dir` is empty or not configured
- **THEN** the resources catalog generator is skipped

---

### Requirement: Resources catalog generator discovers resource files from configured directory
The generator MUST recursively scan the directory specified by `resources_dir` and catalog all resource files found.

#### Scenario: Flat directory of resource files
- **WHEN** `resources_dir` points to a directory containing 3 resource files at the top level
- **THEN** the generated catalog contains exactly 3 entries, one per resource file

#### Scenario: Nested directory structure
- **WHEN** `resources_dir` points to a directory containing resource files in subdirectories (e.g., `apis/openai.md`, `docs/setup.md`)
- **THEN** the generated catalog contains entries for all resource files across all subdirectories, with paths preserved relative to `resources_dir`

#### Scenario: Empty resources directory
- **WHEN** `resources_dir` points to a valid but empty directory
- **THEN** the generated catalog contains an empty entries array and the catalog file is still written with valid schema

#### Scenario: resources_dir does not exist
- **WHEN** `resources_dir` points to a path that does not exist on disk
- **THEN** the generator logs a warning and produces no catalog file, without raising an unhandled exception

---

### Requirement: Resources catalog entries contain LLM-generated summaries
Each resource entry in the catalog MUST include an LLM-generated summary describing the resource's content and purpose, produced via `model_client`.

#### Scenario: Resource file with clear content
- **WHEN** a resource file contains documentation about an API integration
- **THEN** the catalog entry for that file includes a `summary` field with a non-empty string describing the resource's content

#### Scenario: LLM call uses configured model and effort
- **WHEN** the resources catalog generator calls the LLM to summarize a resource
- **THEN** the call is routed through `model_client` using the model and reasoning effort specified in plugin config (defaulting to `claude-sonnet-4-6` at medium effort)

#### Scenario: LLM call fails with retryable error
- **WHEN** the LLM call for a specific resource fails due to a transient error (e.g., rate limit, timeout)
- **THEN** the generator retries according to the shared base retry logic before failing that entry

#### Scenario: LLM call fails after retries exhausted
- **WHEN** the LLM call for a specific resource fails after all retries are exhausted
- **THEN** the generator skips that resource entry, logs the failure, and continues processing remaining resources without aborting the entire catalog generation

---

### Requirement: Resources catalog uses content hashing for state-aware regeneration
The generator MUST compute a content hash for each resource file and skip regeneration for entries whose source content has not changed since the last run.

#### Scenario: Resource file unchanged since last generation
- **WHEN** a resource file's content hash matches the hash stored in `.generation-state.json` from the previous run
- **THEN** the generator skips LLM summarization for that file and carries forward the existing catalog entry

#### Scenario: Resource file modified since last generation
- **WHEN** a resource file's content hash differs from the hash stored in `.generation-state.json`
- **THEN** the generator re-runs LLM summarization for that file and updates both the catalog entry and the stored hash

#### Scenario: New resource file added
- **WHEN** a resource file exists in `resources_dir` but has no corresponding entry in `.generation-state.json`
- **THEN** the generator runs LLM summarization for that file and adds entries to both the catalog and the state file

#### Scenario: First run with no prior state
- **WHEN** `.generation-state.json` does not exist or contains no resources section
- **THEN** the generator processes all resource files and creates the state tracking entries

---

### Requirement: Deleted resource files are pruned from the catalog
When a resource file that was previously cataloged no longer exists in `resources_dir`, the generator MUST remove its entry from both the catalog and the state file.

#### Scenario: Resource file deleted between runs
- **WHEN** a resource file existed during the previous generation run but has since been deleted from `resources_dir`
- **THEN** the catalog no longer contains an entry for that file and its hash is removed from `.generation-state.json`

#### Scenario: Multiple files deleted simultaneously
- **WHEN** 3 previously cataloged resource files are all deleted before the next generation run
- **THEN** all 3 entries are removed from the catalog and state file, while remaining entries are preserved unchanged

---

### Requirement: Resources catalog output conforms to a versioned JSON schema
The generated resources catalog MUST include a schema version field and follow a consistent structure.

#### Scenario: Catalog file structure
- **WHEN** the resources catalog generator completes successfully
- **THEN** the output file at `$CLAUDE_PLUGIN_DATA/catalogs/resources.json` contains a JSON object with at minimum: a `schema_version` field (string), a `generated_at` timestamp (ISO 8601), and an `entries` array

#### Scenario: Each entry contains required fields
- **WHEN** the resources catalog contains entries
- **THEN** each entry includes at minimum: `path` (relative to `resources_dir`), `summary` (string), and `content_hash` (string)

#### Scenario: Catalog is valid JSON
- **WHEN** the resources catalog generator completes, including after partial failures
- **THEN** the output file is valid, parseable JSON

---

### Requirement: Resources catalog generator extends the shared generator base
The resources catalog generator MUST inherit from the shared catalog generation base class, reusing its LLM client integration, retry logic, state file I/O, and content hashing utilities.

#### Scenario: Generator inherits base class
- **WHEN** the resources catalog generator module is loaded
- **THEN** its generator class is a subclass of the shared catalog generator base from `scripts/generators/`

#### Scenario: State file I/O uses shared infrastructure
- **WHEN** the resources catalog generator reads or writes `.generation-state.json`
- **THEN** it uses the base class state file I/O methods, sharing the same state file as other generators with a resources-specific namespace/section

---

### Requirement: Resources catalog generator is invocable by the catalog dispatcher
The resources catalog generator MUST be registered with and callable from `generate_catalog.py` (the catalog dispatcher).

#### Scenario: Dispatcher invokes resources generator when enabled
- **WHEN** the catalog dispatcher runs with `enable_resources` set to `true` and `resources_dir` configured
- **THEN** the dispatcher calls the resources catalog generator and includes its result in the overall generation report

#### Scenario: Dispatcher skips resources generator when disabled
- **WHEN** the catalog dispatcher runs with `enable_resources` set to `false`
- **THEN** the dispatcher does not invoke the resources catalog generator

---

### Requirement: Resources catalog generator handles large resource files gracefully
The generator MUST handle resource files of varying sizes without crashing or producing malformed output.

#### Scenario: Resource file exceeds LLM context window
- **WHEN** a resource file's content is too large to fit in a single LLM call's context window
- **THEN** the generator truncates or chunks the content before sending to the LLM, and still produces a valid summary entry

#### Scenario: Binary or non-text resource file
- **WHEN** `resources_dir` contains a binary file (e.g., an image or compiled artifact)
- **THEN** the generator skips the file or produces a minimal entry noting it is non-text, without crashing