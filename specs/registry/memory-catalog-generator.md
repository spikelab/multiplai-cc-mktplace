## ADDED Requirements

### Requirement: Generator module location and entry point
The memory catalog generator MUST be implemented as a module at `scripts/generators/memory_catalog.py` and expose a callable entry point that the catalog dispatcher can invoke.

#### Scenario: Module exists and is importable
- **WHEN** the plugin is installed and `scripts/generators/memory_catalog.py` exists
- **THEN** the module can be imported without error and exposes a generation function (e.g., `generate`) that the catalog dispatcher can call

#### Scenario: Dispatcher invokes memory catalog generator
- **WHEN** the catalog dispatcher runs with memory generation enabled
- **THEN** it calls the memory catalog generator's entry point and receives a success/failure result

---

### Requirement: Shared base infrastructure usage
The memory catalog generator MUST use the shared catalog generation base for LLM calls (via `model_client`), content hashing, state file I/O, and retry logic — not its own implementations.

#### Scenario: LLM calls route through model_client
- **WHEN** the memory catalog generator needs to summarize or classify memory entries
- **THEN** it issues LLM calls through `lib/model_client.py` using the configured model (default `claude-sonnet-4-6`) and reasoning effort (default medium)

#### Scenario: Content hashing uses shared base
- **WHEN** the generator computes a hash for a memory source file
- **THEN** it uses the same hashing function provided by the catalog generation base, not a local reimplementation

---

### Requirement: Output catalog format and location
The memory catalog generator MUST produce a JSON catalog file at `$CLAUDE_PLUGIN_DATA/catalogs/memory.json` conforming to the catalog schema with version metadata.

#### Scenario: Catalog file is written to correct path
- **WHEN** the memory catalog generator completes successfully
- **THEN** a file exists at `$CLAUDE_PLUGIN_DATA/catalogs/memory.json` containing valid JSON

#### Scenario: Catalog includes schema version
- **WHEN** the memory catalog JSON is read
- **THEN** it contains a top-level `schema_version` field with a semver-compatible string

#### Scenario: Catalog entries correspond to memory sources
- **WHEN** three memory files exist in the memory directory
- **THEN** the generated catalog contains entries summarizing all three memory files

---

### Requirement: Preserve hand-authored fields across regeneration
The memory catalog generator MUST preserve hand-authored fields — `sections`, `bundle`, and `co_retrieve_for` — from the existing catalog when regenerating entries whose source content has changed.

#### Scenario: Hand-authored sections field is preserved
- **WHEN** an existing catalog entry has a `sections` field manually set to `["projects", "preferences"]` and the source memory file is modified
- **THEN** after regeneration, the catalog entry retains `sections` equal to `["projects", "preferences"]` while LLM-generated fields (e.g., summary, topics) are updated

#### Scenario: Hand-authored bundle field is preserved
- **WHEN** an existing catalog entry has a `bundle` field set to `"work-context"` and the source memory file is modified
- **THEN** after regeneration, the catalog entry retains `bundle` equal to `"work-context"`

#### Scenario: Hand-authored co_retrieve_for field is preserved
- **WHEN** an existing catalog entry has `co_retrieve_for` set to `["diary", "skills"]` and the source memory file is modified
- **THEN** after regeneration, the catalog entry retains `co_retrieve_for` equal to `["diary", "skills"]`

#### Scenario: Hand-authored fields are preserved when source is unchanged
- **WHEN** an existing catalog entry has hand-authored fields and the source memory file has not changed
- **THEN** the entire entry is skipped (not regenerated) and all fields including hand-authored ones remain intact

#### Scenario: New entries have no hand-authored fields
- **WHEN** a new memory file is added that has no prior catalog entry
- **THEN** the generated entry does not contain `sections`, `bundle`, or `co_retrieve_for` fields (or they are set to null/empty defaults) — they are not invented by the LLM

---

### Requirement: State-aware skipping of unchanged sources
The memory catalog generator MUST track content hashes per memory source in the shared generation state file and skip regeneration for entries whose source has not changed.

#### Scenario: Unchanged memory file is skipped
- **WHEN** a memory file's content hash matches the hash stored in `.generation-state.json`
- **THEN** the generator does not make an LLM call for that file and the existing catalog entry is retained unchanged

#### Scenario: Changed memory file triggers regeneration
- **WHEN** a memory file's content hash differs from the hash stored in `.generation-state.json`
- **THEN** the generator makes an LLM call to regenerate that entry, merges hand-authored fields from the old entry, and updates the hash in the state file

#### Scenario: New memory file triggers generation
- **WHEN** a memory file exists that has no corresponding entry in `.generation-state.json`
- **THEN** the generator makes an LLM call to generate a new catalog entry and records the hash in the state file

#### Scenario: State file is updated after successful generation
- **WHEN** the generator completes a run that regenerated two entries and skipped three
- **THEN** `.generation-state.json` contains current hashes for all five memory files

---

### Requirement: Deletion pruning of removed sources
The memory catalog generator MUST remove catalog entries and state tracking for memory files that no longer exist on disk.

#### Scenario: Deleted memory file is pruned from catalog
- **WHEN** a memory file previously tracked in `.generation-state.json` no longer exists on disk
- **THEN** after generation, the catalog no longer contains an entry for that file and the state file no longer tracks its hash

#### Scenario: Multiple deletions are pruned in one run
- **WHEN** three previously tracked memory files have been deleted
- **THEN** all three are removed from both the catalog and the state file in a single generation run

---

### Requirement: Parity with external dotfiles memory catalog script
The memory catalog generator MUST produce output functionally equivalent to the existing `claude-code-multiplai/dotfiles/hooks/` memory catalog script — same catalog fields, same summary quality expectations.

#### Scenario: Generated fields match expected schema
- **WHEN** the memory catalog generator processes a memory file
- **THEN** the resulting catalog entry contains at minimum: a source file path/identifier, an LLM-generated summary, and any topic/keyword metadata that the prior external script produced

#### Scenario: Summaries are useful for routing
- **WHEN** the `context_manager` reads a memory catalog entry
- **THEN** the entry contains enough information (summary, topics, keywords) to make a routing decision without reading the raw memory file

---

### Requirement: Configurable model and reasoning effort
The memory catalog generator MUST respect the `plugin.json` userConfig settings for catalog generation model and reasoning effort.

#### Scenario: Custom model is used when configured
- **WHEN** `plugin.json` userConfig sets the catalog model to `claude-haiku-4-5`
- **THEN** the memory catalog generator's LLM calls use `claude-haiku-4-5` instead of the default

#### Scenario: Custom reasoning effort is used when configured
- **WHEN** `plugin.json` userConfig sets reasoning effort to `low`
- **THEN** the memory catalog generator's LLM calls use low reasoning effort

#### Scenario: Defaults are used when no config is set
- **WHEN** `plugin.json` userConfig has no catalog model or effort overrides
- **THEN** the memory catalog generator uses `claude-sonnet-4-6` at medium reasoning effort

---

### Requirement: Graceful handling of empty or missing memory directory
The memory catalog generator MUST handle the case where the memory directory is empty or does not exist without raising an error.

#### Scenario: Empty memory directory produces empty catalog
- **WHEN** the memory directory exists but contains no memory files
- **THEN** the generator produces a valid catalog JSON with an empty entries list and exits successfully

#### Scenario: Missing memory directory produces empty catalog
- **WHEN** the memory directory path does not exist on disk
- **THEN** the generator produces a valid catalog JSON with an empty entries list and exits successfully (does not raise an exception)

---

### Requirement: LLM call failure handling with retry
The memory catalog generator MUST retry failed LLM calls using the shared base retry logic and skip entries that fail after retries without aborting the entire generation run.

#### Scenario: Transient LLM failure is retried
- **WHEN** an LLM call for a memory entry fails on the first attempt with a transient error (e.g., rate limit, timeout)
- **THEN** the shared base retries the call according to its retry policy

#### Scenario: Persistent LLM failure skips the entry
- **WHEN** an LLM call for a memory entry fails after all retries are exhausted
- **THEN** the generator skips that entry (leaving the old catalog entry if one exists), logs a warning, and continues processing remaining entries

#### Scenario: Partial failure does not abort the run
- **WHEN** 1 out of 5 memory entries fails LLM generation after retries
- **THEN** the catalog is written with 4 successfully generated entries (plus the old entry for the failed one if it existed), and the generator reports partial success

---

### Requirement: Atomic catalog write
The memory catalog generator MUST write the catalog file atomically so that a concurrent reader never sees a partial or corrupt file.

#### Scenario: Catalog is written atomically
- **WHEN** the generator finishes producing the new catalog
- **THEN** it writes to a temporary file first and renames it to `memory.json`, ensuring no intermediate state is visible to readers

#### Scenario: Crash during write does not corrupt existing catalog
- **WHEN** the generator process is interrupted during the catalog write phase
- **THEN** the previous valid `memory.json` remains intact (or no file exists if this was the first run)