# Changelog

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
  (`/multiplai:dream` → proposal in `.multiplai/dreams/`), opt-in
  `--auto` with memory-scoped git auto-commit.

### Skills

`/multiplai:setup`, `/multiplai:dream`, `/multiplai:dream-remember`,
`/multiplai:health`, `/multiplai:memory-health-audit`,
`/multiplai:refresh-catalogs`, `/multiplai:backfill`.

### Templates

Starter `me.md`, `technical-pref.md`, `preferences.md`.
