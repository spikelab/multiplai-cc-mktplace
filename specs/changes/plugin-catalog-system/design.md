## Context

The multiplai plugin manages context assembly for Claude Code sessions — pulling in memories, skills, resources, and diary entries to build relevant context windows. Today, catalog generation (the process of summarizing raw source files into lightweight JSON indexes) lives outside the plugin in `claude-code-multiplai/dotfiles/hooks/` as standalone Python scripts. These scripts depend on plugin internals (paths, config format, diary structure) but aren't versioned, tested, or deployed with the plugin itself.

Three context sources — memory, skills, and resources — already have catalogs that `context_router.py` reads for fast routing. Diary entries, the fastest-growing source, have none. Every diary query requires scanning and parsing individual day files under `$CLAUDE_PLUGIN_DATA/diary/`, which scales linearly with history.

The module responsible for context assembly is named `context_router.py`, but its responsibilities extend well beyond routing: it manages fallback logic, catalog reads, context window budgeting, and assembly. The name misleads contributors about where to add new context behavior.

This design brings catalog generation into the plugin as first-class infrastructure, adds the missing diary catalog, renames the context manager honestly, and wires generation into the existing `/dream` reflection cycle so catalogs stay fresh without manual intervention.

## Goals / Non-Goals

**Goals:**

- **Self-contained catalog generation**: All catalog generators live inside the plugin under `scripts/generators/`, versioned and testable alongside the code that reads them.
- **Diary catalog**: Per-day diary summaries (sessions, projects, topics, word count) enabling O(1) day selection during context assembly instead of O(n) file scanning.
- **State-aware regeneration**: Content-hash-based change detection so unchanged sources are skipped on re-run, and deleted sources are pruned from catalogs automatically.
- **Fail-open reliability**: Missing or corrupt catalogs degrade to live file scanning (current behavior) rather than breaking context assembly.
- **Honest naming**: `context_router.py` → `context_manager.py` with all references updated.
- **Automated freshness**: Catalog regeneration runs as part of `/dream`, keeping catalogs current within the existing reflection cycle.
- **Manual override**: A `/refresh-catalogs` skill for force-regeneration and dry-run inspection.
- **Configurable surface**: Plugin-level config for optional catalogs, model selection, reasoning effort, diary window size, and catalog TTL.

**Non-Goals:**

- **Deleting external dotfiles scripts**: Those live in a separate repo (`claude-code-multiplai/dotfiles/`). This work supersedes them but does not modify or remove them.
- **Real-time catalog updates**: Catalogs are batch-regenerated during `/dream` or manual refresh, not updated on every file write. Near-real-time freshness is out of scope.
- **Catalog format migration tooling**: If the catalog schema changes in a future version, migration is a separate concern. This design versions schemas but does not build a migrator.
- **Multi-model orchestration**: All generators use a single configured model. Per-generator model overrides are not in scope.
- **Context assembly algorithm changes**: How `context_manager` selects and budgets context is unchanged. This work only adds catalog-first read paths with fallback.

## Decisions

### 1. Generator architecture: shared base class vs. independent scripts

**Decision**: Implement a `GeneratorBase` class in `scripts/generators/base.py` that all four generators inherit from.

**Alternatives considered**:
- **Independent scripts** (current dotfiles approach): Each generator is self-contained with its own LLM call logic, hashing, and state management. Simpler to understand individually but leads to duplicated retry logic, inconsistent error handling, and divergent state file formats.
- **Functional composition** (shared utility functions, no class hierarchy): Generators import shared functions but remain standalone. Avoids inheritance complexity but makes it harder to enforce consistent behavior (e.g., every generator must hash before calling LLM, must prune deleted entries).

**Why chosen**: The generators share a non-trivial lifecycle — hash sources, compare against state, call LLM for changed entries, merge results, prune deletions, write state. A base class encodes this lifecycle as a template method, ensuring new generators get correct behavior by default. The inheritance depth is exactly one level, so the complexity cost is minimal.

**Interface contract — `GeneratorBase`**:
```python
class GeneratorBase:
    def __init__(self, config: CatalogConfig, model_client: ModelClient)
    
    # Template method — orchestrates the full lifecycle
    def run(self, force: bool = False, dry_run: bool = False) -> GenerationResult
    
    # Subclass hooks
    def discover_sources(self) -> dict[str, SourceEntry]      # Find raw files to catalog
    def hash_source(self, path: Path) -> str                   # Content hash for change detection
    def build_prompt(self, source: SourceEntry) -> str          # LLM prompt for one entry
    def parse_response(self, raw: str) -> CatalogEntry          # Parse LLM output into structured entry
    def merge_entry(self, existing: CatalogEntry | None, new: CatalogEntry) -> CatalogEntry  # Preserve hand-authored fields
    
    # Provided by base
    def _load_state(self) -> GenerationState
    def _save_state(self, state: GenerationState) -> None
    def _read_catalog(self) -> dict
    def _write_catalog(self, catalog: dict) -> None
    def _call_llm(self, prompt: str) -> str                    # Via model_client with retry
```

**`GenerationResult` contract**:
```python
@dataclass
class GenerationResult:
    generator: str           # e.g., "memory", "diary"
    total_sources: int
    skipped: int             # Unchanged hash
    generated: int           # LLM called
    pruned: int              # Deleted from catalog
    errors: list[str]
    dry_run: bool
```

### 2. Content hashing strategy: per-file vs. per-entry

**Decision**: Hash at the granularity of the source unit each generator operates on — per-file for memory/skills/resources, per-day-directory for diary.

**Alternatives considered**:
- **Per-file universally**: Hash every individual file. Simple and uniform, but diary entries span multiple files per day (sessions, reflections, notes), and a change to any one file in a day should trigger re-summarization of that day's catalog entry.
- **Whole-catalog hash**: Single hash of all sources; any change regenerates everything. Too coarse — a single memory edit would trigger LLM calls for all skills and resources too.

**Why chosen**: The natural unit of change matches the natural unit of cataloging. Memory has one file → one catalog. Skills have one file per skill → one entry per skill. Diary has one directory per day → one entry per day. Hashing at this granularity minimizes unnecessary LLM calls while ensuring completeness.

**Implementation**: `hash_source()` in each generator subclass defines the boundary. For diary, it computes `sha256(sorted(file_contents for file in day_dir))`. State file tracks `{source_key: hash}` mappings.

### 3. State tracking: sidecar file vs. embedded in catalog

**Decision**: Separate `.generation-state.json` file in `$CLAUDE_PLUGIN_DATA/catalogs/`.

**Alternatives considered**:
- **Embedded in catalog JSON**: Each catalog entry carries its own `_source_hash` and `_generated_at` metadata. Keeps everything in one file but pollutes the catalog schema that `context_manager` reads, and makes it harder to reset generation state without destroying catalog content.
- **Per-generator state files**: Each generator writes its own `memory.state.json`, `diary.state.json`, etc. Clean separation but more files to manage and no unified view of generation status.

**Why chosen**: A single state file provides a unified view for the dispatcher and `/refresh-catalogs` dry-run mode, while keeping catalog JSON clean for consumption by `context_manager`. The state file is an implementation detail of the generation system, not a contract with downstream consumers.

**State file schema**:
```json
{
  "schema_version": 1,
  "generators": {
    "memory": {
      "last_run": "2026-04-19T10:30:00Z",
      "source_hashes": { "memory.md": "abc123..." },
      "entry_count": 1
    },
    "diary": {
      "last_run": "2026-04-19T10:30:00Z",
      "source_hashes": { "2026-04-19": "def456...", "2026-04-18": "ghi789..." },
      "entry_count": 2
    }
  }
}
```

### 4. Diary catalog structure: flat list vs. per-day entries

**Decision**: Per-day entries keyed by date string, each containing session summaries, project references, topic tags, and word count.

**Alternatives considered**:
- **Flat list of sessions**: Each diary session gets its own catalog entry regardless of day. More granular but produces a much larger catalog (potentially hundreds of entries) that's slower to scan during context assembly.
- **Rolling window summary**: A single summary of the last N days, re-generated fully each time. Simplest to consume but wastes LLM calls — changing one day regenerates the entire summary.

**Why chosen**: Per-day entries align with how diary data is stored (one directory per day) and how `context_manager` selects context (by recency and relevance). The hash-per-day strategy means only days with new entries trigger regeneration. The configurable `diary_lookback_days` (default: 30) bounds catalog size.

**Diary catalog entry schema**:
```json
{
  "date": "2026-04-19",
  "sessions": [
    { "id": "session-abc", "project": "multiplai", "summary": "..." }
  ],
  "topics": ["catalog-generation", "context-routing"],
  "projects": ["multiplai", "dotfiles"],
  "word_count": 2340,
  "generated_at": "2026-04-19T22:00:00Z"
}
```

### 5. Memory catalog: preserving hand-authored fields

**Decision**: The memory catalog generator uses a `merge_entry()` override that preserves `sections`, `bundle`, and `co_retrieve_for` fields from existing catalog entries when regenerating.

**Alternatives considered**:
- **Full regeneration every time**: LLM re-derives all fields including hand-authored ones. Risks losing manual curation and produces inconsistent results across runs.
- **Separate hand-authored sidecar**: Keep manual overrides in a separate file that gets merged at read time. Adds file management complexity and a new concept for users to learn.

**Why chosen**: The merge approach matches how the external dotfiles script works today, preserving backward compatibility. Hand-authored fields are identified by convention (they exist in the current catalog but are not in the LLM output schema), and the merge is a simple dict update where existing values take precedence for protected keys.

### 6. Dispatcher design: sequential vs. parallel generation

**Decision**: Sequential generation with early termination on critical failure, continuing past non-critical errors.

**Alternatives considered**:
- **Parallel generation** (asyncio or threading): Run all generators concurrently. Faster wall-clock time but complicates error handling, makes logs harder to follow, and risks rate-limiting on the LLM API since all generators share one model endpoint.
- **Fully independent** (each generator invoked separately): No dispatcher; `/dream` calls each generator individually. Duplicates orchestration logic and makes dry-run across all generators impossible.

**Why chosen**: Catalog generation runs during `/dream` (already a slow, reflective operation) or manual `/refresh-catalogs`. Wall-clock optimization is not a priority. Sequential execution produces clear, ordered logs and avoids LLM rate-limit contention. The dispatcher can be made parallel later without changing generator interfaces.

**Dispatcher interface**:
```python
def generate_catalogs(
    config: CatalogConfig,
    generators: list[str] | None = None,  # None = all enabled
    force: bool = False,
    dry_run: bool = False
) -> list[GenerationResult]
```

### 7. `/dream` integration: inline vs. post-hook

**Decision**: Add catalog regeneration as a post-step in the `/dream` skill, called after reflection completes.

**Alternatives considered**:
- **Separate hook**: Register a new hook that fires after `/dream`. Decouples dream from catalogs but adds hook management complexity and a failure mode where the hook isn't registered.
- **Cron-based independent schedule**: Catalogs regenerate on their own timer, independent of `/dream`. Maximum decoupling but means catalogs can be stale for arbitrary periods and adds a new scheduling concern.

**Why chosen**: `/dream` already represents "do maintenance work" in the plugin's mental model. Catalog generation is maintenance work. Piggybacking on `/dream` means catalogs refresh at the natural cadence without introducing new scheduling infrastructure. The `dream.md` skill file gets a `<!-- catalog-regen -->` section that calls the dispatcher.

### 8. Fail-open strategy: silent fallback vs. logged warning

**Decision**: When a mandatory catalog (memory, diary) is missing or fails JSON parsing, `context_manager` logs a warning and falls back to live file scanning. Optional catalogs (skills, resources) are simply skipped.

**Alternatives considered**:
- **Hard failure**: Raise an error if a mandatory catalog is missing. Ensures catalogs are always present but breaks context assembly for new installations or after data corruption.
- **Silent fallback**: Fall back without logging. Users would never know their catalogs are broken.

**Why chosen**: Fail-open with warnings balances reliability (context assembly never breaks) with observability (users can diagnose why context might be slower or less relevant). The warning is emitted once per session, not per call, to avoid log spam.

**Fallback contract in `context_manager.py`**:
```python
def _read_catalog_or_scan(self, catalog_type: str) -> list[ContextEntry]:
    """
    Try catalog first. On any failure (missing file, parse error, 
    schema mismatch), log warning and fall back to live scanning.
    """
```

### 9. Rename strategy: atomic rename vs. gradual migration

**Decision**: Atomic rename of `context_router.py` → `context_manager.py` in a single commit, updating all references in `hooks.json`, imports, and tests simultaneously.

**Alternatives considered**:
- **Gradual migration**: Create `context_manager.py` as a wrapper that imports from `context_router.py`, then migrate callers over multiple commits, then delete the old file. Safer for large teams but overkill for a single-contributor plugin with a small reference surface.
- **Alias module**: Keep both filenames, with one importing the other. Avoids breaking external references but creates permanent confusion about which is canonical.

**Why chosen**: The reference surface is small (one `hooks.json` entry, a handful of internal imports, and tests). An atomic rename is cleaner and avoids the confusion of maintaining two names. A simple grep for `context_router` after the rename confirms completeness.

### 10. Config schema: flat keys vs. nested object

**Decision**: Nested `catalogs` object in `plugin.json` `userConfig` with per-concern grouping.

**Config schema**:
```json
{
  "catalogs": {
    "model": { "type": "string", "default": "claude-sonnet-4-6" },
    "reasoning_effort": { "type": "string", "enum": ["low", "medium", "high"], "default": "medium" },
    "ttl_hours": { "type": "number", "default": 24 },
    "diary_lookback_days": { "type": "number", "default": 30 },
    "enable_skills": { "type": "boolean", "default": false },
    "enable_resources": { "type": "boolean", "default": false },
    "resources_dir": { "type": "string", "default": "" }
  }
}
```

**Why**: Nesting under `catalogs` keeps the config namespace clean as the plugin grows. Memory and diary catalogs are always enabled (mandatory); skills and resources are opt-in via boolean gates.

## Risks / Trade-offs

### LLM cost on first run

**Risk**: First-time generation with no state file will call the LLM for every source file across all enabled generators. For a user with 30 days of diary entries and 20 skills, this could be 50+ LLM calls in a single `/dream` invocation.

**Mitigation**: Default diary lookback is 30 days (configurable down). Skills and resources catalogs are opt-in (disabled by default). The dispatcher logs progress so users see what's happening. Medium reasoning effort keeps per-call cost low.

### Catalog staleness between `/dream` runs

**Trade-off**: Catalogs only refresh during `/dream` or manual `/refresh-catalogs`. If a user writes extensively in a session without dreaming, the diary catalog won't reflect today's entries until the next dream cycle.

**Acceptance**: This is acceptable because (a) `context_manager` falls back to live scanning when catalog data is absent for a given day, (b) the most recent day's entries are small enough that scanning is fast, and (c) adding real-time catalog updates would require file watchers or hook-on-write infrastructure that's out of scope.

### Hand-authored field preservation is fragile

**Risk**: The memory catalog generator preserves `sections`, `bundle`, and `co_retrieve_for` by key name. If a user adds a new hand-authored field that the generator doesn't know about, it will be overwritten on next generation.

**Mitigation**: Document the protected field list. Consider a `_manual` namespace convention in a future iteration where any field prefixed with `_manual_` is automatically preserved.

### Rename breaks external references

**Risk**: Any tooling outside the plugin that references `context_router.py` by path (e.g., external hooks, user scripts) will break after the rename.

**Mitigation**: The only known external caller is the dotfiles hooks repo, which is being superseded by this work anyway. The rename is called out in the changelog. A `grep -r context_router` across the plugin after rename confirms no internal references remain.

### State file corruption

**Risk**: If the process is killed mid-write, `.generation-state.json` could be corrupted, causing all sources to appear "new" on the next run and triggering full regeneration.

**Mitigation**: Write state files atomically (write to `.tmp`, then `os.replace()`). Full regeneration is expensive but not incorrect — it's the same as a first run. No data is lost; only LLM budget is wasted.

### Schema versioning without migration

**Trade-off**: Catalog and state files include a `schema_version` field, but there's no automated migration path. A version bump means regenerating from scratch.

**Acceptance**: This is acceptable for the current scale. Catalogs are derived data — regeneration is always safe, just costly. If schema changes become frequent, a migration framework can be added later.

### Generator base class coupling

**Trade-off**: All generators inherit from `GeneratorBase`, which means changes to the base lifecycle affect all generators. A bug in `_call_llm` or `_save_state` breaks everything.

**Acceptance**: The alternative (duplicated logic in each generator) is worse for a system with four generators that share 80% of their lifecycle. The base class is small (~150 lines) and well-tested. The template method pattern makes the lifecycle explicit and auditable.