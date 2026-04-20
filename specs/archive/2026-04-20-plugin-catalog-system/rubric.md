# Evaluation Rubric

## Code Architecture (weight: 2)

| Score | Criteria |
|-------|----------|
| 5 | Clean package structure under `scripts/generators/` with clear module boundaries. `GeneratorBase` template method enforces a consistent lifecycle without leaking implementation details. Each generator (Memory, Diary, Skills, Resources) is a single-responsibility subclass with no cross-generator coupling. Dispatcher depends only on the `GeneratorBase` interface. `context_manager.py` reads catalogs through a single `_read_catalog_or_scan()` pattern with no duplicated fallback logic. State management (atomic writes, schema versioning) is encapsulated in the base class. No circular imports across the plugin. |
| 3 | Package structure exists and generators inherit from the base class, but some responsibilities leak across boundaries — e.g., dispatcher contains generator-specific logic, or `context_manager.py` duplicates catalog-reading logic per context type instead of using a unified `_read_catalog_or_scan()`. Atomic write or state management may live outside the base class. Minor coupling between generators or between generators and the dispatcher. |
| 1 | No clear separation between generators — logic is monolithic or scattered. `GeneratorBase` is either missing or doesn't enforce a template method pattern, leaving each generator to reimplement the lifecycle. Circular dependencies between modules. `context_manager.py` rename is incomplete with lingering `context_router` references. State management is ad-hoc per generator. |

## Test Quality (weight: 1)

| Score | Criteria |
|-------|----------|
| 5 | Comprehensive unit tests for every generator covering: lifecycle execution, hash diffing (unchanged/new/deleted), state persistence round-trips, merge preservation (memory's `sections`, `bundle`, `co_retrieve_for`), diary lookback windowing, config validation edge cases (invalid enum, out-of-range numerics), dispatcher sequencing/filtering/error propagation, and `_read_catalog_or_scan()` scenarios (hit, miss, corrupt JSON, schema mismatch). Tests use meaningful assertions on behavior rather than implementation details. Integration tests verify `/dream` triggers regeneration and `/refresh-catalogs` flag combinations. Dry-run and force modes tested at both generator and dispatcher levels. |
| 3 | Tests exist for most generators and the dispatcher, covering happy paths and some edge cases. May be missing tests for merge preservation, pruning, corrupt catalog fallback, or some flag combinations. Assertions are present but occasionally test implementation details rather than behavior. Integration tests for skills may be partial. |
| 1 | Minimal or no tests. Key behaviors like hash diffing, state persistence, fail-open fallback, or config validation are untested. Tests that exist are shallow (e.g., only checking function existence) or brittle (mocking internals rather than verifying outcomes). No integration tests for `/dream` or `/refresh-catalogs`. |

## Spec Compliance (weight: 3)

| Score | Criteria |
|-------|----------|
| 5 | All nine tasks fully implemented and verified: (1) `context_router` → `context_manager` rename is complete with zero remaining references in code, hooks, imports, and tests. (2) `catalogs` config block in `plugin.json` with all fields, `CatalogConfig` dataclass with validation. (3) `GeneratorBase` implements full template-method lifecycle with atomic writes, dry-run, and force modes. (4) `MemoryGenerator` preserves hand-authored fields during merge. (5) `DiaryGenerator` respects lookback window and hashes sorted file contents. (6) Skills/Resources generators skip when disabled. (7) Dispatcher handles sequential execution, filtering, and error classification. (8) `_read_catalog_or_scan()` fail-open with once-per-session warnings and schema version checking. (9) `/dream` integration and `/refresh-catalogs` skill with all flags functional. All Design Decisions (1–10) satisfied. |
| 3 | Core generators and base class are implemented and functional. Rename is complete. Most Design Decisions are satisfied, but some specifics are missing — e.g., merge preservation is partial, diary lookback pruning not fully implemented, error classification in dispatcher is binary rather than critical/non-critical, or once-per-session warning deduplication is absent. Skills may exist but lack full flag support. |
| 1 | Multiple tasks incomplete or incorrectly implemented. Rename has residual `context_router` references. Generators don't follow the template-method pattern from the base class. Key Design Decisions unmet — e.g., no fail-open fallback, no atomic state writes, no config validation, or disabled generators still execute. `/dream` or `/refresh-catalogs` integration missing. |

## API Design (weight: 2)

| Score | Criteria |
|-------|----------|
| 5 | `GeneratorBase` presents a clean, well-documented template-method API with clear override points (`discover_sources`, `hash_source`, `build_prompt`, `parse_response`, `merge_entry`). `CatalogConfig` dataclass has sensible defaults and validates inputs at construction time. `generate_catalogs()` dispatcher has a clear function signature with `generators`, `force`, and `dry_run` parameters. `GenerationResult` and `GenerationState` dataclasses provide structured, inspectable return values. Catalog JSON schema is versioned. Internal APIs like `_read_catalog_or_scan()` have consistent signatures across context types. All public interfaces are typed. |
| 3 | Template-method API exists but override points may be unclear or inconsistently named. Config loading works but validation is incomplete or happens late. Dispatcher API is functional but may use loose parameter types (e.g., untyped dicts instead of dataclasses). Return values are structured but may omit useful fields. Schema versioning exists but version checking may be incomplete. |
| 1 | No clear API boundaries — generators use ad-hoc method signatures rather than a consistent template. Config is read as raw dicts without validation. Dispatcher uses positional arguments or global state. Return values are unstructured (plain strings or dicts). No schema versioning. Type hints missing or incorrect throughout. |

## Error Handling (weight: 1)

| Score | Criteria |
|-------|----------|
| 5 | Fail-open strategy fully implemented: missing or corrupt catalogs produce a once-per-session warning and fall back to live scanning without interrupting the user. Dispatcher classifies errors as critical (state file corruption → stop) vs. non-critical (single LLM failure → continue with remaining generators). Atomic state writes prevent partial/corrupt state files. `ModelClient` retries with appropriate backoff. Config validation raises clear errors with field names and expected ranges. Schema version mismatches are caught and handled gracefully. No silent failures — all error paths either recover or surface diagnostics. |
| 3 | Fail-open fallback exists but may not deduplicate warnings (logs every call instead of once-per-session). Dispatcher catches errors but treats all failures uniformly rather than classifying severity. Atomic writes are implemented but edge cases (disk full, permission errors) may not be handled. LLM retry exists but backoff may be missing. Some error paths may silently swallow exceptions. |
| 1 | Errors propagate uncaught, causing catalog failures to break context assembly. No fail-open fallback — missing catalog crashes the read path. State writes are non-atomic, risking corruption on interrupt. No retry logic for LLM calls. Dispatcher stops on first error regardless of severity. Config validation is absent, allowing invalid settings to cause downstream failures. |