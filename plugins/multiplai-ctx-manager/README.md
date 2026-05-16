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
| `/multiplai:dream` | Generate a consolidation **proposal** from the pending learnings backlog into `.multiplai/inbox/`. Does not modify memory. |
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
   review proposal to `.multiplai/inbox/`.
3. **Apply** — `/multiplai:dream-remember` walks the proposal with you
   and applies approved edits to memory files.

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
