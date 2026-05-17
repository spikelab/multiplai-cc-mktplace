# Changelog

## 0.2.0 — 2026-05-17

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
  (`/multiplai:dream` → proposal in `.multiplai/dreams/`), opt-in
  `--auto` with memory-scoped git auto-commit.

### Skills

`/multiplai:setup`, `/multiplai:dream`, `/multiplai:dream-remember`,
`/multiplai:health`, `/multiplai:memory-health-audit`,
`/multiplai:refresh-catalogs`, `/multiplai:backfill`.

### Templates

Starter `me.md`, `technical-pref.md`, `preferences.md`.
