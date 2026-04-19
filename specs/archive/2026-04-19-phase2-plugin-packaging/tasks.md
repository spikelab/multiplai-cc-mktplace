## 1. Repository Scaffold & Plugin Manifests

Create the `multiplai-plugin/` directory structure, `plugin.json`, `marketplace.json`, `hooks.json`, `requirements.txt`, and empty placeholder files for all scripts and skills. This block produces a skeleton that passes `claude --plugin-dir ./multiplai-plugin` validation (loads without errors, no hooks execute yet). Acceptance: plugin loads cleanly, directory tree matches D1 layout exactly, manifests contain correct JSON per D5/D6.

Satisfies: D1 (repository layout), D5 (hooks.json structure), D6 (skill definitions in plugin.json)

- [ ] Create top-level `multiplai-plugin/` with `plugin.json`, `marketplace.json`, `hooks.json`, `LICENSE`, `README.md`, `CHANGELOG.md`, `requirements.txt`
- [ ] Create `scripts/lib/` package with `__init__.py` and empty module files (`paths.py`, `model_client.py`, `log_utils.py`, `config.py`)
- [ ] Create stub entry-point scripts in `scripts/` (all hook scripts from D1)
- [ ] Create `skills/` directory with stub `.md` files for setup, dream, health
- [ ] Create `templates/` directory with stub `.md` files (me.md, technical-pref.md, preferences.md)
- [ ] Validate plugin loads via `claude --plugin-dir` without errors

---

## 2. Path Resolver Module

Implement `scripts/lib/paths.py` with the `Paths` frozen dataclass and `Paths.resolve()` class method per D2. The module reads plugin environment variables (`CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA`, `CLAUDE_PLUGIN_OPTION_*`) with fallbacks to `~/.multiplai/` paths for standalone development. Acceptance: `from lib.paths import paths` works from any script in `scripts/`, both plugin-mode and standalone-mode paths resolve correctly, frozen dataclass prevents mutation.

Satisfies: D2 (path resolution — environment variable cascade with hardcoded fallbacks), R4 (dual-mode testing surface)

- [ ] Implement `Paths` dataclass with all seven path fields (plugin_root, data_dir, memory_dir, diary_dir, venv_dir, catalogs_dir, templates_dir)
- [ ] Implement `Paths.resolve()` with env-var-first, fallback-second cascade per D2 table
- [ ] Export module-level `paths = Paths.resolve()` singleton
- [ ] Add unit tests exercising both plugin-mode (env vars set) and standalone-mode (env vars absent) per R4 mitigation
- [ ] Ensure `sys.path` setup allows `from lib.paths import paths` from any `scripts/*.py`

---

## 3. Model Client Abstraction

Implement `scripts/lib/model_client.py` with the `ModelClient` Protocol, `AgentSDKClient`, `AnthropicAPIClient`, and `create_client()` factory per D3. The Agent SDK path is preferred; API key fallback activates when `claude_agent_sdk` is not importable. Both clients expose an async `query()` method with `prompt`, `system`, `model`, and `max_tokens` parameters. Acceptance: `create_client()` returns the correct client based on runtime environment, both implementations handle basic query/response cycles.

Satisfies: D3 (model client abstraction), R1 (Agent SDK import availability)

- [ ] Define `ModelClient` Protocol with async `query()` signature
- [ ] Implement `AgentSDKClient` wrapping `claude_agent_sdk.query()`
- [ ] Implement `AnthropicAPIClient` wrapping `anthropic.AsyncAnthropic`
- [ ] Implement `create_client()` factory with try/except import detection
- [ ] Add logging of which client was selected (supports R1 mitigation)
- [ ] Add unit tests with mocked SDK/API backends

---

## 4. Venv Bootstrap Hook

Implement `scripts/venv_bootstrap.py` as the first `SessionStart` hook per D4. The script creates a venv at `$data_dir/venv/` with `--system-site-packages`, installs `requirements.txt`, and writes a `.bootstrap-complete` marker containing the requirements hash. Subsequent runs are no-ops (< 50ms). Also implement the re-exec preamble pattern that all other scripts will use to ensure they run inside the venv. Acceptance: first run creates venv and installs deps, second run is a no-op, changing `requirements.txt` triggers re-bootstrap.

Satisfies: D4 (venv bootstrap), R3 (fallback if `after` unsupported), R7 (requirements hash invalidation)

- [ ] Implement venv creation with `--system-site-packages` flag
- [ ] Implement `pip install -r requirements.txt` within the created venv
- [ ] Implement `.bootstrap-complete` marker file with SHA-256 hash of `requirements.txt`
- [ ] Implement idempotency check: skip if marker exists and hash matches
- [ ] Create reusable re-exec preamble snippet for other scripts (D4 pattern)
- [ ] Test: fresh bootstrap, no-op on second run, re-bootstrap on hash change

---

## 5. Session Lifecycle Hooks

Port `session_start.py`, `session_stop.py`, and `session_end.py` from `claude-code-multiplai` using the D8 porting strategy: replace hardcoded paths with `lib.paths`, replace SDK calls with `lib.model_client`, strip git/auto-commit logic. Session start loads user context and logs client status. Session stop triggers learning extraction. Session end finalizes the captain's log entry. Acceptance: all three hooks execute without errors under `--plugin-dir`, re-exec into venv, and produce correct log/memory file outputs.

Satisfies: D8 (porting strategy), D5 (hook wiring), G5 (feature parity for session lifecycle)

- [ ] Port `session_start.py`: load memory files, inject context, log client selection
- [ ] Port `session_stop.py`: trigger learning extraction on session stop
- [ ] Port `session_end.py`: finalize captain's log, write diary entry
- [ ] Apply all three D8 transformations (paths, SDK, stripping) to each script
- [ ] Add re-exec venv preamble to each script
- [ ] Verify `hooks.json` timeout values are adequate for each hook

---

## 6. Context Router Hook

Port `scripts/context_router.py` for the `UserPromptSubmit` event per D8. The router reads memory file metadata, ranks relevance to the current prompt, and injects top-candidate content into context — all within the 5-second timeout (R2). Catalog caching in `$data_dir/catalogs/` avoids re-reading all files on every prompt. Omit resource/skill catalog routing branches per NG5. Acceptance: router injects relevant context, respects timeout, catalog refreshes asynchronously.

Satisfies: D8 (porting strategy), G5 (feature parity for context routing), NG5 (simpler router), R2 (timeout pressure)

- [ ] Port context router with D8 transformations applied
- [ ] Implement metadata-first ranking (size, mtime) before content reads per R2 mitigation
- [ ] Implement catalog caching in `$data_dir/catalogs/`
- [ ] Strip resource/skill catalog routing branches (NG5)
- [ ] Verify execution completes within 5-second timeout on representative memory sets
- [ ] Add re-exec venv preamble

---

## 7. Learning Extraction & AutoDream

Port `scripts/extract_learnings.py` and `scripts/autodream.py` per D8. Learning extraction analyzes session transcripts and appends insights to memory files. AutoDream performs periodic memory consolidation and synthesis via concurrent LLM calls through the model client. Also port `scripts/synthesize_now.py` for on-demand synthesis and `scripts/pre_compact.py` for the `PreCompact` hook. Acceptance: learnings append to correct memory files, dream synthesis produces coherent consolidated output, pre-compact captures context before compaction.

Satisfies: D8 (porting strategy), G5 (feature parity for learning extraction and AutoDream)

- [ ] Port `extract_learnings.py` with path/SDK/stripping transformations
- [ ] Port `autodream.py` with concurrent LLM calls via async model client
- [ ] Port `synthesize_now.py` for manual dream trigger
- [ ] Port `pre_compact.py` for PreCompact hook event
- [ ] Verify memory file writes go to `paths.memory_dir` and `paths.diary_dir`
- [ ] Add re-exec venv preamble to all four scripts

---

## 8. Memory Templates & Onboarding Skill

Create the `templates/*.md` starter files per D7 with structured markdown headings and instructional comments. Write the `skills/setup.md` skill prompt that interviews the user, populates memory files from templates (copy-if-absent), and writes personalized content based on interview responses. Acceptance: `/multiplai:setup` creates memory files from templates, never overwrites existing files, interview produces functional memory content from cold start.

Satisfies: D7 (memory templates), D6 (skill definitions), G4 (onboarding flow), R6 (memory file format)

- [ ] Write `templates/me.md` with identity/background structure
- [ ] Write `templates/technical-pref.md` with coding preferences structure
- [ ] Write `templates/preferences.md` with workflow preferences structure
- [ ] Write `skills/setup.md` prompt: interview flow, copy-if-absent logic, content population
- [ ] Verify setup skill invokes `scripts/` for file I/O operations
- [ ] Test cold-start onboarding produces functional system

---

## 9. Dream & Health Skills

Write `skills/dream.md` for manual AutoDream triggering and `skills/health.md` for memory/system auditing per D6. The dream skill invokes `scripts/synthesize_now.py` via Bash tool. The health skill checks client status (R1 mitigation), validates memory file structure, reports path resolution status, and verifies venv integrity. Acceptance: `/multiplai:dream` triggers synthesis, `/multiplai:health` reports system status including client type and path resolution mode.

Satisfies: D6 (skill definitions), R1 (health check reports client status)

- [ ] Write `skills/dream.md` prompt: trigger synthesis, report results
- [ ] Write `skills/health.md` prompt: check client, paths, venv, memory structure
- [ ] Ensure health skill reports which `ModelClient` implementation is active
- [ ] Ensure health skill validates all `Paths` fields resolve to existing directories
- [ ] Test both skills execute correctly under `--plugin-dir`

---

## 10. Integration Wiring & Validation

Wire all components together: finalize `plugin.json` with correct skill references, verify `hooks.json` ordering (especially `after` field for venv bootstrap per R3), ensure all scripts share consistent `lib` imports, and run end-to-end validation. Test the full session lifecycle: plugin load → venv bootstrap → session start → context routing on prompt → session stop → session end. Verify feature parity with `claude-code-multiplai` per G5. Acceptance: full lifecycle executes without errors, all hooks fire in correct order, all skills are accessible, `generate_catalog.py` produces valid catalog output.

Satisfies: G1 (plugin validation), G2 (no hardcoded paths), G5 (feature parity), R3 (`after` field verification)

- [ ] Finalize `plugin.json` with all skill and option declarations
- [ ] Verify `hooks.json` `after` field support; implement inline-bootstrap fallback if unsupported (R3)
- [ ] Port `scripts/generate_catalog.py` for catalog generation
- [ ] Run `claude --plugin-dir ./multiplai-plugin` validation — confirm clean load
- [ ] End-to-end test: full session lifecycle (start → prompt → stop → end)
- [ ] Grep audit: zero remaining hardcoded paths (`~/.claude/`, `/home/spike/`, absolute paths) per G2
- [ ] Verify all `requirements.txt` dependencies are minimal per G6 (only `anthropic`, `pyyaml`)