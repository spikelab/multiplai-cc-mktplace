# Changelog

## 0.4.3 — 2026-07-06

### Changed
- **Bumped `multiplai-core` pin to `@v0.4.0`.** Picks up the library's security
  fix (the no-tools SDK client now also blocks Read/WebFetch/etc. under
  `bypassPermissions`, closing a prompt-injection exfiltration path) plus
  correctness fixes (malformed-timeout env var no longer crashes at import,
  atomic state writes, robust JSON extraction). All entry-point scripts and
  `requirements*.txt` updated.

## 0.4.2 — 2026-07-05

### Fixed
- **Every dispatcher run crashed the diary generator** — the 0.3.x
  `--only`-override feature made the dispatcher pass `force_enable` to every
  generator, but `DiaryGenerator.run()` still had the old signature
  (`TypeError: unexpected keyword argument`). Signature updated; a new
  contract test asserts every registered generator accepts the dispatcher's
  full run() contract, so future overrides can't regress this.

## 0.4.1 — 2026-07-05

### Fixed
- **refresh-catalogs skill doc contradicted the uv migration** — an Operational
  Note still told the session to invoke `generate_catalog.py` with bare
  `python` via the removed managed-venv self-routing, causing
  `ModuleNotFoundError: multiplai_core`. All skill docs now consistently
  mandate `uv run --no-project`. Also scrubbed stale `venv_dir`/`python
  dream.py` references from the health and dream skill docs.

## 0.4.0 — 2026-07-05

### Changed
- **uv + PEP 723 runtime.** The managed plugin venv (`venv_bootstrap`/`venv_guard`)
  is gone. Every entry-point script carries inline dependency metadata and runs
  via `uv run --no-project`. Shared modules (`paths`, `config`, `log_utils`,
  `model_client`) moved to the `multiplai-core` package, consumed as
  `git+https://github.com/spikelab/multiplai-core@v0.2`.

### Added
- **Installed-plugin skill discovery.** The skills catalog now also indexes
  skills shipped by installed Claude Code plugins (the themed Multiplai packs:
  pm, writing, research, dev, media) via `installed_plugins.json`, in addition
  to `skills_dir`. New `plugins_dir` option (empty = `$CLAUDE_CONFIG_DIR/plugins`).


### Changed
- **`refresh-catalogs --only <gen>` now honors its override contract.** An
  explicit `--only` filter has always been documented as running a generator
  regardless of config gating, but each generator's `run()` still re-checked
  its own `enable_*` flag and silently no-op'd — so `--only resources` did
  nothing when `enable_resources=false`. The dispatcher now threads a
  `force_enable` signal into the gated generators, so an explicitly-named
  generator runs even with its flag off (the `resources_dir` requirement is
  still enforced). This lets you keep a catalog fresh without turning on
  injection. Documented in the `refresh-catalogs` skill and README, with
  operational notes (managed-venv self-routing, `exit 1` = partial errors,
  don't `pkill -f generate_catalog`).
- **Resources catalog indexes Markdown only.** `ResourcesGenerator.discover_sources()`
  now allowlists `.md`/`.markdown` and skips dotfiles and binaries (PDFs,
  images, archives, scripts, raw `.txt`), so the routing surface is no longer
  diluted by placeholder "binary file" entries.

### Added
- **Action Items — a third dream disposition.** A learning that asks the
  toolchain to change its own code/config/file-structure ("split these files",
  "delete this orphan", "use install.sh in the Dockerfile") is no longer
  mis-filed as memory. The dream proposal now has a `## Action Items` section
  (`A{N}` numbering, What/Why/Source). On approval, `/multiplai-context:dream-remember`
  writes them to `PLANS/dream-actions-{date}.md` as unchecked tasks, so they
  survive the learnings cleanup and become durable work. A learning that
  carries a durable general *principle* alongside the change keeps **both** —
  the principle as memory and the change as an action item.
- **Bounded critic second pass over the dream proposal.** After the draft, a
  cheap second LLM pass (over the proposal only, not the raw backlog)
  surgically strips point-in-time residue (commit SHAs, `Decision (date):`
  framing, finished-task imperatives, one-off paths), demotes past-event
  records to Filtered Out, and reroutes mis-filed action items. Falls back to
  the raw draft on failure.
- **`filename:line` provenance.** Each proposal entry ends with a `**Source:**`
  line citing the learnings file and line number it was distilled from
  (pending learnings are now fed to the model with line-number prefixes so the
  citation is accurate, not guessed) — so a report is traceable on re-processing.

### Changed
- **Dream now distills generalized, reusable knowledge — not a session log.**
  The proposal prompt is built around an explicit DIARY-vs-MEMORY distinction:
  the diary already records what happened; memory holds guidance that changes a
  *future, different* task. Entries are generalized ("when X, do Y"), with the
  point-in-time scaffolding stripped. Report noise removed: no per-file
  learning counts, `seen Nx` notes, or trust labels (weak items get a
  `[warning low confidence]` marker instead).
- **`/multiplai-context:dream --auto` uses the same generalization pass as
  report mode.** Auto mode previously ran a thin per-file prompt with none of
  the above discipline. It now generates the same proposal (same prompt +
  critic), writes it to `.multiplai/dreams/` for audit, then mechanically
  applies each file's slice concurrently.
- **`router_model` option for the `llm` router** (default `claude-haiku-4-5`).
  The LLM router now forwards a model to the client; Haiku keeps the
  per-prompt classification cheap. The `UserPromptSubmit` hook timeout is
  raised to 15s (router timeout 12s) so an inline `llm` call can complete.
  See the README "Router latency" note — `llm` runs ~7-10s/prompt via the
  Agent SDK (CLI cold-start per call) and is best treated as a routing-
  quality experiment until routing moves off the blocking hook.
- **Re-recommendation cooldown.** After a file is injected, it is
  suppressed from re-injection for `recommend_cooldown_turns` turns
  (default `4`; `0` disables) — it's already in the conversation, so
  re-injecting wastes context. A turn counter and a `recently_injected`
  map persist in `data/session_state.json`; the `PreCompact` hook clears
  the map so post-compaction every file is eligible again. An
  all-suppressed turn logs `on cooldown, nothing injected` and is
  distinct from router abstention (no recency fallback). New
  `recommend_cooldown_turns` userConfig option.

### Changed
- **Activity-log `[context] inject` lines now group injected files by
  corpus** — `→ memory: … · skills: … · resources: …` instead of one
  flat comma-separated list, so you can tell which files came from which
  corpus. The JSONL mirror gains a `files_by_corpus` field.

### Fixed
- **Skills were routed but never injected.** The loader called
  `read_text()` on the skill *directory* instead of `<name>/SKILL.md`
  (the real Claude Code layout), so the skills count was always 0.
  Skills are now surfaced as lightweight recommendations (catalog
  summary + `/<name>` invocation hint) rather than full SKILL.md bodies.
- **`anti_domains` hard-excluded relevant entries across all corpora.**
  Anti phrases that reuse the entry's own positive vocabulary (e.g.
  "…unrelated to memory routing") nuked the entry on the very tokens
  that made it relevant. Anti tokens that are also the entry's
  `intent_domains` vocabulary are now dropped before the exclusion check.

## 0.3.0 — 2026-05-21

Diary layout aligned with learnings — one file per UTC day.

### Changed (breaking on-disk layout)
- **Diary now uses per-day files**, matching the learnings layout. Each
  ``YYYY-MM-DD.md`` file under your diary dir holds one ``# Diary``
  header plus one ``## Session: <id> — <ts> — <cwd>`` block per session
  that ran on that day. The previous ``YYYY-MM-DD/<sessionId>.md``
  per-session layout is gone.
- Why: easier to browse (`ls diary/` shows ~365 entries/year instead of
  thousands), consistent with learnings, append-only with `fcntl.flock`
  for concurrent SessionStart subprocesses.
- Idempotent on session_id: re-extracting the same session is a no-op.
- The diary catalog generator now iterates ``*.md`` files at the top of
  the diary directory; one catalog entry per day, same schema as before.
- Health check renamed ``diary.entry_count`` → ``diary.day_count`` to
  reflect what it actually measures.

### Migration
- No public users existed on the pre-0.3.0 layout, so no user-facing
  migration tool ships. The internal migration was a one-shot script
  applied to existing on-disk diaries during development and discarded.

## 0.2.1 — 2026-05-21

First public-marketplace-ready release. Focused on safety, transparency,
and onboarding rather than new features.

### Fixed
- **`UserPromptSubmit` hook can no longer crash your session.** The
  context-routing hook now wraps all work in a top-level guard: any
  unhandled error logs to `hook-errors.log` and emits a safe empty
  context. Previously a single failing file read could surface a
  traceback mid-prompt.

### Added
- One-time warning at session start when neither the Agent SDK nor an
  Anthropic API key is configured — so LLM-backed features (extraction,
  dreams, catalogs) silently no-op'ing is no longer a mystery.
- README: **How it works** (4-step lifecycle), **Where your data lives**
  (what gets written where + `.gitignore` snippet), **Quick start: the
  only options you probably need** (cuts the 18-option config wall down
  to 3-4 that matter).
- Platform support note in README — macOS / Linux / WSL on Windows;
  native Windows isn't supported.

### Changed
- `anthropic` dependency pinned to an exact version for reproducible
  installs.

## 0.2.0 — 2026-05-17

Internal release. Highlights for users:

- **Runtime state moved next to your memory.** Logs, catalogs, the
  plugin venv, and dream state now live at `<workspace>/.multiplai/data`
  instead of Claude Code's managed plugin dir. Side effect: routing
  catalogs are now actually loaded by default (they were silently
  unreachable before). New `data_dir` config option to override.
- **Tail-friendly logs.** Every log file rotates daily into
  `<name>-YYYY-MM-DD.log`, with `<name>.log` always pointing at today.
  Retention controlled by `MULTIPLAI_LOG_RETENTION_DAYS` (default 7).
- **`activity.log` curated stream.** One plain-language line per
  meaningful action — context injected, dream nudge, session boundary,
  diary write, learnings capture, catalog rebuild. Tail with
  `tail -f <data>/logs/activity.log`.
- Test suite hardened so it never inherits the host workspace's
  `CLAUDE_PLUGIN_*` / `WORKSPACE` env.

### Observability

- `log_utils.py` rewritten to the project logging standard: UTC ISO-8601
  lines with `[component] [session:xxxxxxxx] LEVEL:` shape, env-driven
  level (`MULTIPLAI_DEBUG=1` or `MULTIPLAI_LOG_LEVEL`), and a shared
  `hook-errors.log` for ERROR+ across all components.
- **Uniform date-rotation across every log.** `<name>.log` is always the
  *current* file; on the first write of a new UTC day it rotates to
  `<name>-YYYY-MM-DD.log` (date infix *before* the extension, per the
  standard). The stdlib `TimedRotatingFileHandler` was emitting the
  rejected `<name>.log.YYYY-MM-DD` form; legacy files in that shape are
  auto-migrated. Retention is `MULTIPLAI_LOG_RETENTION_DAYS` (default 7,
  `0` = keep forever), applied uniformly to every rotated file.
- New `log_event()` curated activity stream — one plain-language line per
  meaningful action in `activity.log` (current), mirrored to
  `activity.jsonl` for tooling, both rotating to `activity-YYYY-MM-DD.*`
  the same way. Previously these were always date-stamped with *today*,
  so there was never a stable current file to `tail -f`. Written
  regardless of log level; never raises into a hook.
- Lifecycle scripts instrumented: context inject/skip/fallback (with the
  exact files loaded), dream nudge, session start/end/pre-compact, diary
  write, learnings capture, catalog rebuild (+entry count and timing),
  deferred-extraction launch.
- README "Observability" section: live-watch command, debug toggle, log
  layout, retention.

### Fixed

- **`data_dir` is now workspace-anchored.** Previously `paths.py`
  resolved `data_dir` from `CLAUDE_PLUGIN_DATA`, so logs/catalogs/venv/
  dream-state landed in Claude Code's per-install managed dir —
  split away from `<workspace>/.multiplai/` where memory/diary/learnings
  live (and contradicting the in-code comment). Now: explicit `data_dir`
  option → `<workspace>/.multiplai/data` → `CLAUDE_PLUGIN_DATA` (only
  when no workspace) → `~/.multiplai/data`. New `data_dir` userConfig
  option. As a side effect this also resolves the router always falling
  back (the managed dir had no `catalogs/`).
- Test suite hardened: an autouse fixture scrubs ambient
  `CLAUDE_PLUGIN_*`/`WORKSPACE` so tests never inherit the host
  workspace.

## 0.1.0 — 2026-05-16

Initial public release of the **multiplai** context-manager plugin,
distributed via the `multiplai` Claude Code marketplace.

### Plugin

- `.claude-plugin/plugin.json` with `userConfig` (workspace/memory/diary/
  now/learnings dirs, sensitive `anthropic_api_key`, catalog & router
  options) and an explicit `hooks` declaration.
- Official Claude Code hooks schema (`hooks/hooks.json`): `SessionStart`
  (venv bootstrap + session init), `UserPromptSubmit` (context routing),
  `Stop` (lightweight checkpoint), `SessionEnd` and `PreCompact`
  (deferred-extraction markers).

### Core

- Path resolver with plugin-env → workspace → standalone cascade; all
  runtime state resolves through it (catalog generators included).
- Model client abstraction: Agent SDK (zero-config) with Anthropic
  API-key fallback; empty-content responses handled gracefully.
- First-run virtualenv bootstrap (`uv` preferred, `pip` fallback).
- Routed, per-prompt memory injection (`token_overlap` or `llm`
  strategy). `SessionStart` no longer dumps the full memory corpus.
- Diary-first learning extraction (brace-safe prompt construction);
  per-session diary, per-day learnings.
- Catalog generation (memory, diary, optional skills/resources) with
  content-hash incremental regeneration.
- Dream consolidation: report mode by default
  (`/multiplai-context:dream` → proposal in `.multiplai/dreams/`), opt-in
  `--auto` with memory-scoped git auto-commit.

### Skills

`/multiplai-context:setup`, `/multiplai-context:dream`, `/multiplai-context:dream-remember`,
`/multiplai-context:health`, `/multiplai-context:memory-health-audit`,
`/multiplai-context:refresh-catalogs`, `/multiplai-context:backfill`.

### Templates

Starter `me.md`, `technical-pref.md`, `preferences.md`.
