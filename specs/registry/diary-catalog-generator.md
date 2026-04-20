## ADDED Requirements

### Requirement: Diary catalog file structure
The diary catalog generator MUST produce a JSON catalog file at `$CLAUDE_PLUGIN_DATA/catalogs/diary.json` conforming to a versioned schema. Each entry in the catalog represents one day's diary file and includes: date, session summaries, projects mentioned, topics covered, and word count.

#### Scenario: Generate catalog from diary entries
- **WHEN** the diary catalog generator runs and diary day-files exist under the diary directory
- **THEN** a `diary.json` file is written to `$CLAUDE_PLUGIN_DATA/catalogs/` containing a top-level `schema_version` field, a `generated_at` ISO-8601 timestamp, and an `entries` array where each element has `date` (YYYY-MM-DD), `sessions` (array of session summaries), `projects` (array of strings), `topics` (array of strings), and `word_count` (integer)

#### Scenario: Empty diary directory
- **WHEN** the diary catalog generator runs and no diary day-files exist
- **THEN** a `diary.json` file is written with an empty `entries` array, a valid `schema_version`, and a `generated_at` timestamp

### Requirement: Per-day LLM summarization
The generator MUST send each diary day-file's content to the LLM (via `model_client`) to produce structured session, project, and topic summaries. The LLM call uses the model and reasoning effort specified in plugin config.

#### Scenario: Single day-file summarization
- **WHEN** a diary day-file for `2026-04-15` contains entries spanning two coding sessions on project "acme-api" covering topics "auth refactor" and "rate limiting"
- **THEN** the resulting catalog entry for `2026-04-15` includes at least two session summaries, `"acme-api"` in `projects`, and both `"auth refactor"` and `"rate limiting"` in `topics`

#### Scenario: LLM call uses configured model and effort
- **WHEN** `plugin.json` userConfig specifies `catalog_model` as `claude-sonnet-4-6` and `catalog_reasoning_effort` as `medium`
- **THEN** the `model_client` call for diary summarization uses model `claude-sonnet-4-6` with reasoning effort `medium`

#### Scenario: LLM call uses default model when not configured
- **WHEN** `plugin.json` userConfig does not specify `catalog_model` or `catalog_reasoning_effort`
- **THEN** the `model_client` call defaults to `claude-sonnet-4-6` with reasoning effort `medium`

### Requirement: Configurable lookback window
The generator MUST respect a configurable diary lookback window that limits how many days back it processes. Day-files older than the window are excluded from generation.

#### Scenario: Lookback window limits processing
- **WHEN** the diary lookback window is configured to `30` days and diary day-files exist for 90 days
- **THEN** only the most recent 30 days of diary files are processed and included in the catalog `entries` array

#### Scenario: Default lookback window
- **WHEN** no diary lookback window is configured in `plugin.json`
- **THEN** a default lookback window is applied (not unlimited) so the generator does not process unbounded history

#### Scenario: All files within window
- **WHEN** the lookback window is `60` days and only 10 days of diary files exist
- **THEN** all 10 days are processed and included in the catalog

### Requirement: Word count accuracy
Each catalog entry MUST include an accurate `word_count` field computed directly from the source diary day-file content, not from the LLM summary.

#### Scenario: Word count matches source content
- **WHEN** a diary day-file for `2026-04-10` contains exactly 350 words of raw text
- **THEN** the catalog entry for `2026-04-10` has `word_count` set to `350`

#### Scenario: Word count is zero for empty day-file
- **WHEN** a diary day-file exists but contains no text content (only whitespace or is empty)
- **THEN** the catalog entry for that date has `word_count` set to `0`

### Requirement: Content-hash-based skip logic
The generator MUST compute a content hash of each diary day-file and compare it against the hash stored in `.generation-state.json`. Unchanged files are skipped to avoid redundant LLM calls.

#### Scenario: Skip unchanged day-file
- **WHEN** the generator runs and a diary day-file for `2026-04-12` has the same content hash as recorded in `.generation-state.json`
- **THEN** no LLM call is made for `2026-04-12` and the existing catalog entry for that date is preserved unchanged

#### Scenario: Regenerate changed day-file
- **WHEN** a diary day-file for `2026-04-12` has been modified since the last generation (content hash differs from `.generation-state.json`)
- **THEN** the LLM is called to re-summarize that day-file and the catalog entry and stored hash are both updated

#### Scenario: Generate new day-file with no prior state
- **WHEN** a diary day-file for `2026-04-18` exists but has no entry in `.generation-state.json`
- **THEN** the LLM is called to summarize it, a new catalog entry is added, and the content hash is recorded in `.generation-state.json`

### Requirement: Deletion pruning
When a diary day-file has been deleted but its entry still exists in the catalog and state file, the generator MUST remove the orphaned entry from both the catalog and `.generation-state.json`.

#### Scenario: Prune deleted day-file from catalog
- **WHEN** the generator runs and `.generation-state.json` contains an entry for `2026-04-05` but no diary day-file exists for that date
- **THEN** the catalog entry for `2026-04-05` is removed from `diary.json` and the hash entry is removed from `.generation-state.json`

#### Scenario: No pruning when file still exists
- **WHEN** the generator runs and both a diary day-file and a state entry exist for `2026-04-05`
- **THEN** the catalog entry for `2026-04-05` is retained (either preserved or regenerated based on hash comparison)

### Requirement: State file persistence
The generator MUST read from and write to `$CLAUDE_PLUGIN_DATA/catalogs/.generation-state.json`, storing per-day-file content hashes under a `diary` namespace to avoid collision with other generators' state.

#### Scenario: State file created on first run
- **WHEN** the generator runs for the first time and no `.generation-state.json` exists
- **THEN** a `.generation-state.json` file is created with a `diary` key containing content hashes for all processed day-files

#### Scenario: State file updated incrementally
- **WHEN** the generator runs and `.generation-state.json` already exists with entries under `diary` and other namespaces (e.g., `memory`)
- **THEN** only the `diary` namespace is modified; other namespaces remain untouched

#### Scenario: Corrupt state file handling
- **WHEN** `.generation-state.json` exists but contains invalid JSON
- **THEN** the generator treats all day-files as needing regeneration and overwrites the `diary` namespace with fresh hashes (without destroying other namespace data if recoverable, otherwise creating a fresh state file)

### Requirement: LLM failure handling
If the LLM call fails for a specific day-file, the generator MUST skip that entry without aborting the entire catalog generation. The failed entry's previous catalog data (if any) is preserved.

#### Scenario: LLM call fails for one day, others succeed
- **WHEN** the generator processes 5 day-files and the LLM call fails (network error, timeout, or API error) for `2026-04-14` but succeeds for the other 4
- **THEN** the catalog contains updated entries for the 4 successful days, the entry for `2026-04-14` retains its previous catalog data (or is absent if no prior entry existed), and no content hash update is recorded for `2026-04-14` in `.generation-state.json`

#### Scenario: All LLM calls fail
- **WHEN** the generator runs and every LLM call fails
- **THEN** the generator completes without error, the catalog retains all prior entries unchanged, and no hashes are updated in `.generation-state.json`

### Requirement: Schema versioning
The diary catalog MUST include a `schema_version` field. When the schema changes in a future release, the generator MUST be able to detect an outdated version and trigger full regeneration.

#### Scenario: Schema version mismatch triggers full regeneration
- **WHEN** the generator runs and `diary.json` exists with `schema_version` `1` but the current generator expects version `2`
- **THEN** all day-files within the lookback window are re-summarized regardless of content hash matches, and the resulting catalog is written with `schema_version` `2`

#### Scenario: Schema version match uses incremental mode
- **WHEN** the generator runs and `diary.json` exists with a `schema_version` matching the current expected version
- **THEN** the generator uses content-hash-based incremental processing (skip unchanged, regenerate changed)

### Requirement: Base class integration
The diary catalog generator MUST extend the shared catalog generation base class from `scripts/generators/`, using its LLM client access, retry logic, content hashing utilities, and state file I/O rather than implementing these independently.

#### Scenario: Generator uses base class hashing
- **WHEN** the diary catalog generator computes a content hash for a day-file
- **THEN** it delegates to the base class content hashing method, producing the same hash as any other generator would for identical content

#### Scenario: Generator uses base class state I/O
- **WHEN** the diary catalog generator reads or writes `.generation-state.json`
- **THEN** it uses the base class state file I/O methods, ensuring consistent file format and locking behavior across all generators

### Requirement: Diary day-file discovery
The generator MUST discover diary day-files by scanning the configured diary directory for files matching the expected naming convention (date-based filenames). Files that do not match the expected pattern are ignored.

#### Scenario: Standard day-files discovered
- **WHEN** the diary directory contains files `2026-04-15.md`, `2026-04-16.md`, and `2026-04-17.md`
- **THEN** all three files are recognized as diary day-files and eligible for processing

#### Scenario: Non-diary files ignored
- **WHEN** the diary directory contains `2026-04-15.md`, `README.md`, and `notes.txt`
- **THEN** only `2026-04-15.md` is processed; `README.md` and `notes.txt` are ignored

#### Scenario: Malformed date filenames ignored
- **WHEN** the diary directory contains `not-a-date.md` or `2026-13-45.md`
- **THEN** those files are not processed and no catalog entries are created for them