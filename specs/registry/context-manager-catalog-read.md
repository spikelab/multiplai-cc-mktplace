## ADDED Requirements

### Requirement: Catalog-first read path for memory context
The context manager MUST attempt to read the memory catalog file before falling back to scanning raw memory files. When a valid memory catalog exists, the context manager uses its contents to assemble memory context without reading individual memory source files.

#### Scenario: Memory catalog exists and is valid
- **WHEN** the context manager is asked to assemble memory context and `$CLAUDE_PLUGIN_DATA/catalogs/memory.json` exists with a valid schema version
- **THEN** the context manager reads context from the catalog file and does NOT scan or parse individual memory source files

#### Scenario: Memory catalog is used for routing decisions
- **WHEN** the context manager reads a valid memory catalog containing section metadata, bundle info, and co_retrieve_for fields
- **THEN** the context manager uses those catalog fields to make routing and selection decisions without loading the underlying memory files

### Requirement: Catalog-first read path for diary context
The context manager MUST attempt to read the diary catalog file before falling back to scanning raw diary day files. When a valid diary catalog exists, the context manager uses per-day summaries (sessions, projects, topics, word count) to select relevant days without reading raw files.

#### Scenario: Diary catalog exists and is valid
- **WHEN** the context manager is asked to assemble diary context and `$CLAUDE_PLUGIN_DATA/catalogs/diary.json` exists with a valid schema version
- **THEN** the context manager reads per-day summaries from the catalog and selects relevant diary days based on catalog metadata instead of scanning individual day files

#### Scenario: Diary catalog enables selective day loading
- **WHEN** the diary catalog contains entries for 30 days and only 3 days match the current routing criteria (project, topic)
- **THEN** the context manager only reads the raw files for those 3 matching days, not all 30

### Requirement: Catalog-first read path for skills context
The context manager MUST attempt to read the skills catalog when the `enable_skills` config flag is set, using catalog contents for skill routing instead of scanning skill files directly.

#### Scenario: Skills catalog exists and skills are enabled
- **WHEN** the context manager is asked to assemble skills context, `enable_skills` is true in config, and `$CLAUDE_PLUGIN_DATA/catalogs/skills.json` exists with a valid schema version
- **THEN** the context manager reads skill metadata from the catalog file for routing decisions

#### Scenario: Skills catalog does not exist but skills are enabled
- **WHEN** `enable_skills` is true and the skills catalog file does not exist
- **THEN** the context manager falls back to live scanning of skill files (fail-open behavior)

### Requirement: Catalog-first read path for resources context
The context manager MUST attempt to read the resources catalog when both `enable_resources` and `resources_dir` config values are set, using catalog contents for resource routing.

#### Scenario: Resources catalog exists and resources are enabled
- **WHEN** the context manager is asked to assemble resources context, `enable_resources` is true, `resources_dir` is configured, and `$CLAUDE_PLUGIN_DATA/catalogs/resources.json` exists with a valid schema version
- **THEN** the context manager reads resource metadata from the catalog file for routing decisions

### Requirement: Fail-open fallback when mandatory catalog is missing
When a mandatory catalog (memory or diary) is missing entirely, the context manager MUST fall back to live file scanning — the current behavior — rather than raising an error or returning empty context.

#### Scenario: Memory catalog file does not exist
- **WHEN** the context manager is asked to assemble memory context and `$CLAUDE_PLUGIN_DATA/catalogs/memory.json` does not exist
- **THEN** the context manager falls back to scanning and parsing individual memory source files, producing the same output as the pre-catalog behavior

#### Scenario: Diary catalog file does not exist
- **WHEN** the context manager is asked to assemble diary context and `$CLAUDE_PLUGIN_DATA/catalogs/diary.json` does not exist
- **THEN** the context manager falls back to scanning and parsing individual diary day files, producing the same output as the pre-catalog behavior

### Requirement: Fail-open fallback when mandatory catalog is corrupt
When a mandatory catalog file exists but contains invalid JSON or fails schema validation, the context manager MUST log a warning and fall back to live file scanning rather than crashing.

#### Scenario: Memory catalog contains invalid JSON
- **WHEN** the context manager reads `memory.json` and the file contains malformed JSON (e.g., truncated, syntax error)
- **THEN** the context manager logs a warning message indicating the catalog is corrupt, falls back to live file scanning, and does NOT raise an unhandled exception

#### Scenario: Memory catalog has wrong schema version
- **WHEN** the context manager reads `memory.json` and the `schema_version` field does not match the expected version
- **THEN** the context manager logs a warning about schema mismatch and falls back to live file scanning

#### Scenario: Diary catalog contains invalid JSON
- **WHEN** the context manager reads `diary.json` and the file contains malformed JSON
- **THEN** the context manager logs a warning, falls back to live diary file scanning, and does NOT raise an unhandled exception

### Requirement: Fail-open fallback when mandatory catalog is empty
When a mandatory catalog file exists but contains an empty entries list, the context manager MUST treat it as a valid-but-empty catalog, not as a corrupt file requiring fallback.

#### Scenario: Memory catalog exists with zero entries
- **WHEN** the context manager reads `memory.json` and it has a valid schema version but an empty entries array
- **THEN** the context manager treats this as "no memory context available" and does NOT fall back to live scanning (the catalog is authoritative)

### Requirement: Optional catalogs do not trigger fallback when missing
When an optional catalog (skills, resources) is missing, the context manager MUST silently skip that context source — no fallback scanning, no warning — since those catalogs are gated behind config flags.

#### Scenario: Skills catalog missing with skills enabled
- **WHEN** `enable_skills` is true and the skills catalog file does not exist
- **THEN** the context manager falls back to live scanning of skill files without logging an error (warning level at most)

#### Scenario: Resources catalog missing with resources disabled
- **WHEN** `enable_resources` is false and the resources catalog file does not exist
- **THEN** the context manager does not attempt to read the resources catalog or scan resource files at all

### Requirement: Catalog read does not block on stale data
The context manager MUST use whatever catalog data is available at read time, regardless of catalog age or TTL. TTL enforcement is the responsibility of the generation side, not the read side.

#### Scenario: Catalog older than configured TTL
- **WHEN** the context manager reads a catalog file whose `generated_at` timestamp is older than the configured `catalog_ttl`
- **THEN** the context manager still uses the catalog data for context assembly (TTL is advisory for regeneration scheduling, not a read-side gate)

### Requirement: Catalog read path is transparent to callers
The context manager's public interface for assembling context MUST remain unchanged. Callers should not need to know whether context was assembled from catalogs or from live scanning.

#### Scenario: Same return shape regardless of source
- **WHEN** the context manager assembles memory context from the catalog path
- **THEN** the returned context has the same structure and format as when assembled via live file scanning

#### Scenario: Same return shape for diary context
- **WHEN** the context manager assembles diary context from the catalog path
- **THEN** the returned context has the same structure and format as when assembled via live diary file scanning

### Requirement: Catalog read errors are isolated per catalog type
A failure reading one catalog MUST NOT prevent the context manager from reading other catalogs or assembling other context types.

#### Scenario: Corrupt diary catalog does not block memory catalog
- **WHEN** the diary catalog contains invalid JSON but the memory catalog is valid
- **THEN** the context manager falls back to live scanning for diary context, successfully reads the memory catalog for memory context, and returns both context types

#### Scenario: Missing memory catalog does not block diary catalog
- **WHEN** the memory catalog file does not exist but the diary catalog is valid
- **THEN** the context manager falls back to live scanning for memory context, successfully reads the diary catalog for diary context, and returns both context types