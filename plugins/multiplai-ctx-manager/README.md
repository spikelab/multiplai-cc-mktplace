# multiplai — context manager

A Claude Code plugin for **context routing, continuous learning, session
awareness, and memory management**. It injects only the memory relevant
to each prompt, captures learnings from your sessions, keeps a per-session
diary, and consolidates what it learns back into your memory files.

This is the first plugin in the [`multiplai`](../../README.md) marketplace.

## Installation

From the marketplace (recommended):

```
/plugin marketplace add spikelab/multiplai-cc-mktplace
/plugin install multiplai@multiplai
```

For local development, point Claude Code at the plugin directory:

```
claude --plugin-dir ./plugins/multiplai-ctx-manager
```

On first run a `SessionStart` hook bootstraps an isolated virtualenv for
the plugin's Python dependencies (`uv` if available, else `python -m venv`
+ `pip`). No manual install step.

## Configuration

All options are set via the plugin's `userConfig` (Claude Code prompts for
them at enable time; values are exposed to hooks as
`CLAUDE_PLUGIN_OPTION_*`).

### Directory layout

`workspace_dir` is the anchor. When set, memory/diary/now/learnings
default to `<workspace_dir>/.multiplai/{memory,diary,now,learnings}`. If
unset, everything falls back to `~/.multiplai/`. Individual overrides win
over the anchor:

| Option | Default | Purpose |
|--------|---------|---------|
| `workspace_dir` | `""` | Anchor for all state dirs (see above) |
| `memory_dir` | `<workspace>/.multiplai/memory` | Memory files (`me.md`, `technical-pref.md`, `preferences.md`, …) |
| `diary_dir` | `<workspace>/.multiplai/diary` | Per-session diary entries (`YYYY-MM-DD/<session>.md`) |
| `now_dir` | `<workspace>/.multiplai/now` | Per-project status summaries |
| `learnings_dir` | `<workspace>/.multiplai/learnings` | Per-day captured learnings |

### Model & catalog

| Option | Default | Purpose |
|--------|---------|---------|
| `anthropic_api_key` | _(unset, sensitive)_ | API key fallback when the Agent SDK is unavailable. Marked sensitive — stored in the system keychain, never logged. |
| `catalog_model` | `claude-sonnet-4-6` | Model for LLM catalog generation |
| `catalog_model_diary` | _(inherits)_ | Optional model override for the diary catalog |
| `catalog_reasoning_effort` | `medium` | Reasoning effort for catalog generation |
| `catalog_ttl_hours` | `168` | Hours a generated catalog stays valid |
| `diary_catalog_days` | `7` | Days of diary history the diary catalog covers |
| `memory_router` | `token_overlap` | Context selection strategy: `token_overlap` (offline, fast) or `llm` (one Sonnet call per prompt) |
| `enable_skills` / `skills_dir` | `false` / `~/.claude/skills` | Optionally catalog skills for routing |
| `enable_resources` / `resources_dir` | `false` / `""` | Optionally catalog a research/reference corpus |

## Skills

All commands are namespaced under `/multiplai:`.

| Command | What it does |
|---------|--------------|
| `/multiplai:setup` | Onboarding interviewer — populates memory files from starter templates. |
| `/multiplai:dream` | Generate a consolidation **proposal** from the pending learnings backlog into `.multiplai/dreams/`. Does not modify memory. |
| `/multiplai:dream-remember` | Review the proposal (generating one if needed), approve/reject per target file, apply approved edits, clean up processed learnings. |
| `/multiplai:health` | Infrastructure audit — completeness/staleness of memory files, plugin dirs, active model client. |
| `/multiplai:memory-health-audit` | Deeper audit — cross-correlates retrieval logs, diary, learnings, and memory structure. |
| `/multiplai:refresh-catalogs` | Regenerate catalog indexes. Supports `--force`, `--dry-run`, `--only`. |
| `/multiplai:backfill` | Reconstruct learnings/diary/now summaries from existing Claude Code transcripts. Default window 7 days; `--days N`, `--since DATE`, `--all`. |

## Where your memory lives

By default `memory_dir` is under `.multiplai/` with no version control.
Over time memory accumulates and a single bad write can erase state that
took months to build.

**Recommended: point `memory_dir` at a git repository.**
`/multiplai:setup` detects whether your chosen `memory_dir` is inside a
git repo and offers to `git init` it. Once tracked, `/multiplai:dream`
(in `--auto` mode) commits memory changes after each consolidation so you
always have a recoverable history. Auto-commit is scoped to memory
markdown files, so it won't sweep unrelated work when memory lives inside
a larger repo. If `memory_dir` isn't a git repo, auto-commit is skipped
with a log warning and everything else keeps working.

## Architecture

### Lifecycle hooks (`hooks/hooks.json`, official Claude Code schema)

| Event | Script | Role |
|-------|--------|------|
| `SessionStart` | `venv_bootstrap.py`, `session_start.py` | Bootstrap venv; init session state; drain deferred extractions; emit the dream-due nudge. **Does not** dump memory into context. |
| `UserPromptSubmit` | `context_manager.py` | Route the prompt against catalogs and inject only the relevant memory. |
| `Stop` | `session_stop.py` | Lightweight checkpoint (extraction is deferred, not run here). |
| `SessionEnd` | `session_end.py` | Write a deferred-extraction marker for the next session to process. |
| `PreCompact` | `pre_compact.py` | Enqueue a deferred-extraction marker so pre-compaction learnings survive. |

Heavy LLM extraction never runs inside a kill-within-seconds hook: it is
deferred via a marker queue and processed by `extract_learnings.py` from
the next `SessionStart`.

### Key libraries

- **`scripts/lib/paths.py`** — single source of truth for path
  resolution (plugin env → workspace fallback → `~/.multiplai`). All
  runtime state resolves through here.
- **`scripts/lib/model_client.py`** — LLM abstraction: Agent SDK
  (zero-config) with an Anthropic API-key fallback.
- **`scripts/venv_bootstrap.py`** — first-run virtualenv setup.

### Learning lifecycle

1. **Capture** — `SessionEnd`/`PreCompact` enqueue markers; the next
   `SessionStart` runs `extract_learnings.py`, writing diary entries and
   per-day learnings.
2. **Propose** — `/multiplai:dream` reads learnings + diary and writes a
   review proposal to `.multiplai/dreams/`.
3. **Apply** — `/multiplai:dream-remember` walks the proposal with you
   and applies approved edits to memory files.

## Observability

The plugin is not a black box — every meaningful action is logged. All
runtime state (logs, catalogs, venv, dream state) lives with the
workspace, beside memory/diary/learnings:

```
<workspace>/.multiplai/data/logs/
```

`<workspace>` is the configured workspace dir (`workspace_dir` option or
`$WORKSPACE`). The plugin deliberately does **not** scatter logs into
Claude Code's per-install `CLAUDE_PLUGIN_DATA` dir — runtime state stays
with the workspace it describes. Fallbacks: an explicit `data_dir`
option overrides everything; with no workspace configured it uses
`CLAUDE_PLUGIN_DATA` (managed) or finally `~/.multiplai/data`.

### The activity log — what to watch

`activity-YYYY-MM-DD.log` is the human-in-the-loop view: one
plain-language line per meaningful action — context injected (and the
exact files), nudges fired, diary written, learnings captured, catalogs
rebuilt, session start/end.

```
14:51:03 [context]   injected 10 memory · 0 skills · 0 resources → preferences.md, technical-pref.md, multiplai.md, …
14:51:03 [nudge]     dream gate open (>24h, pending learnings) — surfaced to user
14:51:18 [diary]     wrote diary entry (1 unit(s)) to <session>.md
14:51:18 [learnings] captured 2 learning(s) + 0 correction(s) to backlog
14:52:01 [catalog]   rebuilt 3 catalog(s) (14 entries, 0 pruned) in 312ms
```

The message is the line, verbatim — no `key=value` tail. Structured
fields (full file list, byte counts, timings) live in the `.jsonl`
mirror.

Watch it live from a **second terminal** (it stays out of Claude's
context entirely):

```bash
tail -f <workspace>/.multiplai/data/logs/activity-$(date +%F).log
```

`activity-YYYY-MM-DD.jsonl` mirrors the same events as one JSON object
per line, for tooling and the health audit.

### Debug mode — see every script

Logging level is environment-driven. Launch Claude with:

```bash
MULTIPLAI_DEBUG=1 claude          # everything at DEBUG, all scripts
MULTIPLAI_LOG_LEVEL=WARNING claude # quieter
```

`MULTIPLAI_DEBUG=1` makes every hook and script (context routing, diary,
learnings, catalog rebuilds, venv bootstrap) emit DEBUG detail to its
per-component log **and** stderr — visible under `claude --debug`.

### Log layout & retention

- `<component>.log` — per-component, date-rotated, **7-day retention**.
- `hook-errors.log` — every ERROR+ across all components, append-only.
- `activity-YYYY-MM-DD.{log,jsonl}` — curated activity stream,
  14-day retention.

Every line follows the project logging standard:
`[<UTC ISO-8601>Z] [<component>] [session:<8-char id>] LEVEL: message`.

## Development

```
cd plugins/multiplai-ctx-manager
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Tests live in `tests/` and are dev-only — never loaded by the plugin
runtime.

## License

MIT — see [LICENSE](../../LICENSE).
