# Evaluation Rubric

## Code Architecture (weight: 2)

| Score | Criteria |
|-------|----------|
| 5 | Clean module boundaries: `lib/` package cleanly separates concerns (paths, model client, config, logging). No circular imports. Frozen dataclass enforces immutability. Protocol-based model client enables substitution without modification. Re-exec venv preamble is a single reusable snippet, not copy-pasted. All scripts import from `lib.*` with consistent `sys.path` setup. Plugin manifests (`plugin.json`, `hooks.json`) are the sole source of truth for wiring — no implicit dependencies between components. |
| 3 | Module boundaries exist but have minor leaks: e.g., some scripts directly read env vars instead of going through `Paths`, or model client factory has hardcoded fallback logic mixed with selection logic. Re-exec preamble is mostly consistent but has minor variations across scripts. `lib/` package works but has one or two unnecessary cross-imports. Plugin manifests are correct but contain redundant or inconsistent entries. |
| 1 | No clear separation: path resolution logic duplicated across scripts, model client instantiation scattered rather than centralized in factory. Scripts contain hardcoded paths violating G2. Circular imports between `lib/` modules. Re-exec preamble inconsistently applied or missing from hooks. Plugin manifests reference nonexistent files or have structural errors. |

## Test Quality (weight: 1)

| Score | Criteria |
|-------|----------|
| 5 | Unit tests cover both plugin-mode and standalone-mode path resolution (R4). Model client tests mock both `claude_agent_sdk` and `anthropic` backends, verifying factory selection logic and query behavior. Venv bootstrap tests verify fresh creation, no-op idempotency (< 50ms), and re-bootstrap on hash change. Tests use meaningful assertions on actual behavior (file contents, return values, side effects) rather than just "no exception." Edge cases covered: missing env vars, unimportable SDK, corrupt marker files, empty memory directories. |
| 3 | Tests exist for core modules (paths, model client, venv bootstrap) but miss some modes or edge cases. E.g., only plugin-mode paths tested, or venv hash-change re-bootstrap not verified. Assertions are present but occasionally test implementation details rather than behavior. Mocking is functional but brittle (patching internal names rather than interfaces). |
| 1 | Tests are absent, trivial (assert True), or only test the happy path for one configuration. No mocking of SDK/API backends. No verification of idempotency or dual-mode path resolution. Venv bootstrap tested only by manual inspection. No edge case coverage. |

## Spec Compliance (weight: 3)

| Score | Criteria |
|-------|----------|
| 5 | All ten task blocks fully implemented: directory tree matches D1 layout exactly; `Paths` dataclass implements the full D2 env-var cascade with all seven fields; model client follows D3 Protocol with both implementations and factory; venv bootstrap satisfies D4 including marker hash and re-exec pattern; all session lifecycle hooks (D5/D8) ported with path/SDK/stripping transformations; context router respects R2 timeout with metadata-first ranking and catalog caching; learning extraction and AutoDream ported with async model client; templates follow D7 structure; all three skills (setup, dream, health) match D6 definitions; integration wiring passes `claude --plugin-dir` validation with zero hardcoded paths (G2) and minimal dependencies (G6). `after` field handling includes R3 fallback. |
| 3 | Core infrastructure complete (tasks 1–4) and most hooks ported (tasks 5–7), but with gaps: e.g., context router missing catalog caching, or health skill doesn't report model client type (R1), or templates lack instructional comments (D7). Plugin loads without errors but one or two hooks have incorrect timeout values or missing `after` ordering. Skills exist but setup interview flow is incomplete or dream skill doesn't invoke `synthesize_now.py` via the correct mechanism. Minor G2 violations (1–2 hardcoded paths remain). |
| 1 | Scaffold exists but major components missing or non-functional: `Paths` doesn't implement env-var cascade, model client lacks one implementation or factory, venv bootstrap doesn't handle idempotency, hooks not ported with D8 transformations (still contain hardcoded paths or direct SDK calls). Plugin fails `--plugin-dir` validation. Multiple spec references (D2–D8, R1–R7, G1–G6) unaddressed. Skills are empty stubs. |

## API Design (weight: 2)

| Score | Criteria |
|-------|----------|
| 5 | `ModelClient` Protocol is minimal and correct: async `query()` with `prompt`, `system`, `model`, `max_tokens` parameters — no leaky abstractions. `Paths.resolve()` classmethod returns a frozen instance, preventing mutation. `create_client()` factory has a clean selection interface with logging. All public interfaces are typed. Hook scripts expose no internal state — they communicate exclusively through file I/O and exit codes as expected by the plugin system. Plugin option declarations in `plugin.json` map cleanly to `CLAUDE_PLUGIN_OPTION_*` env vars consumed by `Paths.resolve()`. |
| 3 | Interfaces are functional but have rough edges: e.g., `ModelClient` query method has extra parameters beyond the spec, or `Paths` exposes a mutable field. Factory returns correct client but selection criteria are ambiguous. Hook scripts mostly communicate through proper channels but one or two have side effects or implicit dependencies. Plugin options exist but naming is inconsistent with env var expectations. |
| 1 | No clear API contracts: model client implementations have divergent signatures, `Paths` is a plain dict or mutable object, factory function has unclear failure modes. Hook scripts depend on global state or import-time side effects. Plugin manifest declarations don't align with what scripts actually read. Type annotations absent or incorrect. |

## Error Handling (weight: 1)

| Score | Criteria |
|-------|----------|
| 5 | Model client factory gracefully handles missing `claude_agent_sdk` with logged fallback to API client (R1). Venv bootstrap handles permission errors, missing `pip`, and corrupt marker files without crashing. Path resolution provides clear error messages when neither env vars nor fallback directories exist. Context router respects 5-second timeout (R2) with graceful degradation — returns partial results rather than failing. All hooks have top-level exception handlers that log errors and exit with appropriate codes. `requirements.txt` hash comparison handles file-not-found. Re-exec preamble handles missing venv by triggering re-bootstrap. |
| 3 | Primary error paths handled: SDK import failure triggers fallback, venv bootstrap skips on existing marker. But some secondary failures unhandled: e.g., corrupt memory files crash learning extraction, context router doesn't enforce timeout, or path resolution silently returns None for missing directories. Some hooks lack top-level exception handling. |
| 1 | Errors propagate as unhandled exceptions: missing SDK crashes rather than falling back, venv bootstrap fails without cleanup on partial creation, path resolution raises on missing env vars with no fallback. No timeout enforcement on context router. Hook failures produce unhelpful tracebacks with no logging context. No idempotency guards on file operations. |