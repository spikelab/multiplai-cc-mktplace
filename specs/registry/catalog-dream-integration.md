## ADDED Requirements

### Requirement: Dream lifecycle triggers catalog regeneration
When the `/dream` skill executes its reflection cycle, it must invoke catalog regeneration as a post-processing step so that catalogs stay fresh without requiring manual intervention.

#### Scenario: Catalogs regenerate after a normal dream cycle
- **WHEN** `/dream` completes its reflection and diary-writing phase successfully
- **THEN** the catalog dispatcher (`generate_catalog.py`) is invoked, running all enabled generators with state-aware skipping

#### Scenario: Dream completes even if catalog generation fails
- **WHEN** `/dream` completes its reflection phase but catalog generation encounters an error (e.g., LLM call failure, file write error)
- **THEN** the dream cycle still completes successfully, the error is logged, and no dream output is lost

### Requirement: Autodream call chain includes catalog regeneration
The `dream.py` script, which triggers dream cycles automatically, must also trigger catalog regeneration as part of its execution flow.

#### Scenario: Autodream triggers catalog generation
- **WHEN** `dream.py` invokes a dream cycle and the dream phase completes
- **THEN** catalog regeneration is triggered via the catalog dispatcher with the same behavior as a manual `/dream` invocation

#### Scenario: Autodream respects catalog configuration
- **WHEN** `dream.py` triggers catalog regeneration and optional catalogs (skills, resources) are disabled in `plugin.json`
- **THEN** only mandatory/enabled catalog generators run; disabled generators are skipped

### Requirement: Catalog regeneration runs after diary write
Catalog generation must occur after the dream's diary entry has been written to disk, so the diary catalog generator can index the new entry.

#### Scenario: New diary entry is included in catalog
- **WHEN** `/dream` writes a new diary entry for today and then triggers catalog regeneration
- **THEN** the diary catalog generator processes the newly written entry, and the diary catalog reflects today's session data

#### Scenario: Ordering guarantee on diary write before catalog generation
- **WHEN** `/dream` is executing its lifecycle
- **THEN** the diary file write completes and is flushed to disk before the catalog dispatcher is invoked

### Requirement: Dream skill markdown references catalog regeneration
The `skills/dream.md` file must document that catalog regeneration is part of the dream lifecycle so the LLM executing the skill knows to perform this step.

#### Scenario: dream.md contains catalog regeneration hook
- **WHEN** the `skills/dream.md` file is read
- **THEN** it contains an explicit step or instruction to invoke catalog regeneration (via `generate_catalog.py` or equivalent) after diary writing

### Requirement: Catalog regeneration uses state-aware skipping during dream
During dream-triggered regeneration, unchanged sources should be skipped to keep dream execution fast.

#### Scenario: Unchanged catalogs are skipped
- **WHEN** `/dream` triggers catalog regeneration and no source files have changed since the last generation (content hashes match in `.generation-state.json`)
- **THEN** the catalog dispatcher skips regeneration for those catalogs and completes without making LLM calls for unchanged sources

#### Scenario: Only changed catalogs are regenerated
- **WHEN** `/dream` writes a new diary entry (changing diary source) but memory files are unchanged
- **THEN** the diary catalog generator runs and updates the diary catalog, while the memory catalog generator is skipped due to matching content hashes

### Requirement: Dream-triggered regeneration does not block on optional catalogs
If optional catalog generators (skills, resources) are enabled but slow, they should not prevent the dream cycle from completing in a reasonable time.

#### Scenario: Dream completes with slow optional generator
- **WHEN** `/dream` triggers catalog regeneration and the skills catalog generator takes longer than expected (e.g., many skill files to process)
- **THEN** the dream cycle still completes; catalog generation runs to completion but does not cause the dream to hang indefinitely

### Requirement: Catalog regeneration inherits configured model settings
When triggered from dream, catalog generation must use the model and reasoning effort specified in `plugin.json` config, not hardcoded defaults.

#### Scenario: Custom model config is used during dream-triggered generation
- **WHEN** `plugin.json` specifies a custom `catalog_model` and `catalog_reasoning_effort` and `/dream` triggers catalog regeneration
- **THEN** the catalog dispatcher passes the configured model and effort level to all generators' LLM calls via `model_client`

#### Scenario: Default model is used when no config is set
- **WHEN** `plugin.json` does not specify `catalog_model` or `catalog_reasoning_effort` and `/dream` triggers catalog regeneration
- **THEN** the catalog dispatcher uses `claude-sonnet-4-6` at medium reasoning effort as defaults

### Requirement: Deleted sources are pruned during dream-triggered regeneration
When dream triggers catalog regeneration, the dispatcher must prune catalog entries whose source files no longer exist.

#### Scenario: Removed diary day file is pruned from catalog
- **WHEN** a diary day file that was previously cataloged has been deleted, and `/dream` triggers catalog regeneration
- **THEN** the diary catalog no longer contains an entry for that deleted day, and the corresponding hash is removed from `.generation-state.json`