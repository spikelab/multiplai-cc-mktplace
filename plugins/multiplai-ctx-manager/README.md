# multiplai — context manager

A Claude Code plugin for **context routing, continuous learning, session
awareness, and memory management**. It injects only the memory relevant
to each prompt, captures learnings from your sessions, keeps a per-session
diary, and consolidates what it learns back into your memory files.

This is the first plugin in the [`multiplai`](../../README.md) marketplace.

**Platforms:** built and tested on macOS and Linux. WSL on Windows is
expected to work but not actively tested — please open an issue if it
doesn't. Native Windows (without WSL) isn't supported.

## How it works

1. **Setup** — `/multiplai:setup` walks you through populating a small set
   of memory files (who you are, how you work, technical preferences).
2. **Per prompt** — a `UserPromptSubmit` hook routes your prompt against
   indexed catalogs of memory (and optionally skills/resources) and
   injects only the relevant pieces. No memory dump. Files injected on
   recent turns are skipped — they're already in the conversation (see
   [Re-recommendation cooldown](#re-recommendation-cooldown)).
3. **Per session** — diary entries and a learnings backlog are captured
   in the background; nothing blocks your session.
4. **Consolidation** — `/multiplai:dream-remember` reviews the backlog
   and proposes edits back into your memory files. You approve before
   anything is written.

```
  setup → memory files
              ↓
  every prompt: context_manager picks relevant memory/skills/resources
              ↓
  Stop / SessionEnd: diary written, learnings queued
              ↓
  /multiplai:dream-remember: backlog reviewed → memory updated
```

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

### Quick start: the only options you probably need

Most users only touch these:

| Option | Why |
|--------|-----|
| `workspace_dir` | Anchor for all state. Set it once; everything else defaults under it. |
| `anthropic_api_key` | Only if you're not running inside Claude Code's Agent SDK (set as a fallback). |
| `memory_router` | Leave `token_overlap` (fast, offline) unless you want LLM-based routing (`llm` = one Sonnet call per prompt). |

Optional, for power users:

- `enable_skills` + `skills_dir` — index a skills corpus for routing.
- `enable_resources` + `resources_dir` — index a research/notes corpus.

Everything else (catalog model, TTL, reasoning effort, diary catalog
window, individual `*_dir` overrides) has sensible defaults — leave
alone unless you're tuning.

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
| `memory_router` | `token_overlap` | Context selection strategy: `token_overlap` (offline, fast) or `llm` (one model call per prompt). See [Router latency](#router-latency) before choosing `llm`. |
| `router_model` | `claude-haiku-4-5` | Model for the `llm` router. Haiku by default — routing is cheap classification, so the smallest/fastest model keeps per-prompt latency down. Ignored under `token_overlap`. |
| `recommend_cooldown_turns` | `4` | After a file is injected, suppress re-injecting it for this many turns (it's already in the conversation). `0` disables. See [Re-recommendation cooldown](#re-recommendation-cooldown). |
| `enable_skills` / `skills_dir` | `false` / `~/.claude/skills` | Optionally catalog skills for routing |
| `enable_resources` / `resources_dir` | `false` / `""` | Optionally catalog a research/reference corpus |

#### Router latency

The `llm` router runs one model call **inside the blocking
`UserPromptSubmit` hook**, before Claude sees your prompt. Via the Agent
SDK this measured **~7–10s/prompt** (Haiku, memory+skills) — the cost is
the SDK spawning the `claude` CLI subprocess per call, not the model. The
hook timeout is therefore raised to 15s (router timeout 12s) when `llm`
is active. That is a real per-prompt latency cost; `token_overlap` (the
default) is instant.

`llm` is currently best treated as a **routing-quality experiment**, not
a steady-state config. The durable fix is to move routing out of the
blocking hook — an always-running external routing agent / local service
that holds a warm model connection (no per-call cold-start), or a
direct-API path (needs an API key with credits, which bypasses the SDK
subprocess). Until then, prefer `token_overlap` for daily use.

## Skills

All commands are namespaced under `/multiplai:`.

| Command | What it does |
|---------|--------------|
| `/multiplai:setup` | Onboarding interviewer — populates memory files from starter templates. |
| `/multiplai:dream` | Generate a consolidation **proposal** from the pending learnings backlog into `.multiplai/dreams/`. Does not modify memory. |
| `/multiplai:dream-remember` | Review the proposal (generating one if needed), approve/reject per target file, apply approved edits, clean up processed learnings. |
| `/multiplai:health` | **Is it broken?** Mechanical infrastructure check (deterministic script): active model client, directories present, memory-file freshness by mtime, diary/learnings/dream counts. Fast, cheap, run anytime. |
| `/multiplai:memory-health-audit` | **Is it good?** Analytical effectiveness audit — cross-correlates retrieval logs, diary, learnings, and memory structure to find what's useful, what's wasted, and what to restructure. Slower; run ~monthly. |
| `/multiplai:refresh-catalogs` | Regenerate catalog indexes. Supports `--force`, `--dry-run`, `--only`. |
| `/multiplai:backfill` | Reconstruct learnings/diary/now summaries from existing Claude Code transcripts. Default window 7 days; `--days N`, `--since DATE`, `--all`. |

## Where your data lives

Everything stays on your machine under `<workspace>/.multiplai/`
(or `~/.multiplai/` if `workspace_dir` is unset):

| Subdir | What's in it |
|--------|--------------|
| `memory/` | Your memory files. You edit these directly. |
| `diary/YYYY-MM-DD/` | One file per session — a narrative of what happened. |
| `learnings/` | Extracted insights pending consolidation. |
| `now/` | Per-project current-state summaries. |
| `data/` | Runtime state — catalogs, logs, plugin venv. Disposable; recreated as needed. |

Delete any of these any time; the plugin recreates what it needs. If
`.multiplai/` lives inside a git repo and you don't want diary/learnings
tracked, add to `.gitignore`:

```gitignore
.multiplai/diary/
.multiplai/learnings/
.multiplai/data/
```

Memory files are the one thing worth tracking — see the next section.

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
| `PreCompact` | `pre_compact.py` | Enqueue a deferred-extraction marker so pre-compaction learnings survive; clear the re-recommendation cooldown map (injected context is summarized away). |

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

1. **Capture** — when you exit a session (or it pre-compacts), the
   `SessionEnd`/`PreCompact` hook writes a tiny *marker* JSON to
   `data/pending_extractions/`. The hook itself does no LLM work — those
   hooks get killed within seconds by Claude Code, so any multi-second
   call would be unreliable.
2. **Extract (deferred, async)** — the next `SessionStart` reads the
   pending markers and spawns `extract_learnings.py` as a *detached
   background subprocess* (`subprocess.Popen(..., start_new_session=True)`).
   `SessionStart` returns immediately so your first prompt isn't blocked.
   The subprocess does the LLM call to produce the diary entry + per-day
   learnings, writes them, and removes its marker.
3. **Propose** — `/multiplai:dream` reads learnings + diary and writes a
   review proposal to `.multiplai/dreams/`.
4. **Apply** — `/multiplai:dream-remember` walks the proposal with you
   and applies approved edits to memory files.

> **Heads-up on timing.** Because extraction runs in the background, it
> may still be in flight when you ask your first question (or run
> `/multiplai:health`). A typical transcript takes 10-30 seconds depending
> on length and model latency. If you started a session and the latest
> diary entry isn't there yet, wait ~30 seconds and check again — the
> subprocess is still working. The plugin will *never* block your prompt
> on extraction; it always catches up asynchronously.
>
> If a marker stays in `data/pending_extractions/` across multiple
> sessions, the next `SessionStart` retries it (up to 3 attempts); a
> permanently-failing transcript is moved to `data/failed_extractions/`
> for inspection.

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

`activity.log` is the human-in-the-loop view: one plain-language line
per meaningful action — context injected (and the exact files), nudges
fired, diary written, learnings captured, catalogs rebuilt, session
start/end. It's the *current* file (no date); the previous day's stream
rotates to `activity-YYYY-MM-DD.log` on the first write of a new day.

```
14:51:03Z [a1b2c3d4] [context]   injected 4 memory · 0 skills · 0 resources · scores 31.5→9.8 (4/12 kept) → memory: finances.md, life.md, preferences.md, taxes-italy.md
14:51:03Z [a1b2c3d4] [nudge]     dream gate open (>24h, pending learnings) — surfaced to user
14:51:18Z [a1b2c3d4] [diary]     wrote diary entry (1 unit(s)) to <session>.md
14:51:18Z [a1b2c3d4] [learnings] captured 2 learning(s) + 0 correction(s) to backlog
14:52:01Z [e5f6a7b8] [catalog]   rebuilt 3 catalog(s) (14 entries, 0 pruned) in 312ms
```

Each line is `HH:MM:SS**Z** [**session**] [component] message`:

- The **`Z`** marks the time as **UTC** — it is *not* your local
  clock. If you're at UTC+2, `14:51Z` happened at 16:51 your time.
- The **8-char session id** in brackets makes a line self-traceable:
  `grep a1b2c3d4 activity.log` replays everything one session did,
  and the same id maps to the transcript at
  `$CLAUDE_CONFIG_DIR/projects/**/a1b2c3d4-*.jsonl` (which has the
  actual prompts).
- The message is verbatim — no `key=value` tail. Structured fields
  (full file list, byte counts, timings) live in the `.jsonl` mirror.

### Reading a `[context]` routing line

This is the most important line to understand — it tells you *whether
routing is actually working*, not just that it ran. Anatomy:

```
injected 4 memory · 0 skills · 0 resources · scores 31.5→9.8 (4/12 kept) → memory: finances.md, life.md, …
         └── how many files from each corpus made it in           └── files grouped by corpus
                                          └── routing-quality hint (token_overlap only)
```

The file list after `→` is grouped and labelled by corpus —
`memory: … · skills: … · resources: …` — so you can always tell which
corpus each injected file came from (only corpora that contributed are
shown; files are alphabetical within each).

**The score hint** (`scores TOP→FLOOR (KEPT/CANDIDATES kept)`) is the
signal:

- `scores 31.5→9.8` — the highest-scoring file scored 31.5, the
  lowest one that was *actually injected* scored 9.8. A wide gap
  (top ≫ floor) means routing found a clear winner; a flat range
  (e.g. `7.2→6.8`) means everything scored about the same — weak,
  low-confidence routing where the cut is near-arbitrary.
- `(4/12 kept)` — **12 files** had some keyword overlap (candidates);
  only **4** cleared the relevance cutoff and were injected. The
  other 8 were dropped as too weak. Big drop = good filtering;
  `(12/12 kept)` = the filter did nothing.
- `CAP-HIT` — appears as `scores 22.4→6.5 CAP-HIT (10/18 kept)`. It
  means the relevance cutoff would have kept *more* than the 10-file
  ceiling, so the #10/#11 boundary is arbitrary. Frequent `CAP-HIT`
  on low top-scores = the prompt is matching too much weakly; routing
  is noisy, not precise.

**Abstention** — routing deciding *nothing* is relevant is correct
behaviour, not a failure. You'll see one of:

- `· continuation — nothing injected` — the prompt was a bare
  go-ahead (`yes`, `go on`, `do it`); the context is already in the
  conversation, so nothing is added.
- `· no match (best 1.4 < floor) — nothing injected` — files were
  considered but even the best one scored below the relevance floor.
- A `[context] skip` line such as
  `router abstained — best memory score 1.4 below relevance floor
  (3 cand), nothing injected` — same thing when *no* corpus produced
  anything, so there was nothing to inject at all.
- `all 4 matched file(s) injected within the last 4 turns — on
  cooldown, nothing injected` — routing *did* find relevant files, but
  they were all injected recently and are still in the conversation.
  See [Re-recommendation cooldown](#re-recommendation-cooldown).

**Fallback** — `[context] router matched nothing — fell back to
recency-ranked memory → …` means routing failed (catalog/disk drift
or a router error, **not** a clean abstention) and the most-recently-
edited memory files were injected as a safety net. Occasional is fine;
frequent fallback means the catalog is stale — run
`/multiplai:refresh-catalogs`.

Notes: the score hint only appears under the `token_overlap` router
(the default) — the `llm` router doesn't expose scores. Healthy
`token_overlap` looks like: a clear `TOP→FLOOR` gap, `KEPT` well
below `CANDIDATES`, and `CAP-HIT` rare. Persistent flat ranges,
`CAP-HIT` everywhere, or constant fallback are the symptoms to act on
(start with `/multiplai:health`, which summarises these same numbers).

### Re-recommendation cooldown

Routing runs fresh on every prompt, so without a guard a multi-turn
exchange on one topic would re-inject the *same* files turn after turn —
content that's already sitting in the conversation. The cooldown
suppresses that waste.

**How it works.** Each prompt advances a turn counter, and every file
that gets injected is stamped with the current turn in a small
`recently_injected` map (kept in `data/session_state.json`). On the next
prompt, any pick that was injected within the last
`recommend_cooldown_turns` turns is dropped before it's loaded — it's
already in context. A file becomes eligible again once the window passes;
if it's re-injected, its stamp refreshes. Aged entries are pruned so the
map stays small.

```
turn 1  "audit the routing quality"   → injects 7 memory · 1 skill · 10 resources
turn 2  "and the false-negative rate" → on cooldown, nothing injected
turn 3  "what about precision"         → on cooldown, nothing injected
turn 4  (window passed)                → re-injects the relevant set
```

(That trace is with `recommend_cooldown_turns = 2`; the default is `4`.)

**Compaction resets it.** When Claude Code compacts the conversation,
the injected content is summarized away — so the `PreCompact` hook clears
`recently_injected`, making every file eligible again. This is what keeps
a longer cooldown safe: it can never starve the model of context that
compaction has already discarded.

**Tuning.** `recommend_cooldown_turns` (default `4`) is the window. Raise
it if you find the same files re-appearing too often within a focused
session; lower it (or set `0` to disable) if you want routing to re-inject
more eagerly. Suppression is distinct from abstention — an all-suppressed
turn logs `on cooldown, nothing injected` and does **not** trigger the
recency fallback.

Watch it live from a **second terminal** (it stays out of Claude's
context entirely):

```bash
tail -f <workspace>/.multiplai/data/logs/activity.log
```

`activity.jsonl` mirrors the same events as one JSON object per line,
for tooling and the health audit (rotated the same way).

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

- `<component>.log` — current per-component file; rotates to
  `<component>-YYYY-MM-DD.log` on UTC day change.
- `activity.{log,jsonl}` — current curated activity stream; rotates to
  `activity-YYYY-MM-DD.{log,jsonl}` the same way.
- `hook-errors.log` — every ERROR+ across all components, append-only.
- Retention: `MULTIPLAI_LOG_RETENTION_DAYS` (default **7**, `0` = keep
  forever) — applies uniformly to every rotated `*-DATE.{log,jsonl}`.
  The rejected `<name>.log.DATE` form is auto-migrated to the standard
  `<name>-DATE.log`.

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
