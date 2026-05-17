---
name: backfill
description: Reconstruct learnings, diary entries, and now/ summaries from existing Claude Code session transcripts. Scans $CLAUDE_CONFIG_DIR/projects/**/*.jsonl, distills transcripts, runs diary-first extraction, and writes to the same files as the live pipeline. Default window: last 7 days; extends via --days N, --since DATE, or --all.
---

# /multiplai:backfill

Reconstruct diary entries and learnings from existing Claude Code session transcripts.

## When to use

- You've just installed multiplai-plugin and want to backfill from your session history
- Sessions ran before the plugin was installed or before the diary pipeline was wired
- You want to rebuild `now/` and `diary.json` catalog with historical depth

## Steps

1. **Scope the run** — decide the window:
   - Default: last 7 days (`--days 7`)
   - Extend: `--days 30`, `--since 2026-04-01`, or `--all` (entire history)
   - Scope to project: `--projects multiplai-plugin,dolcebot`

2. **Dry-run first** to see what will be processed:

   ```
   python "${CLAUDE_PLUGIN_ROOT}/scripts/backfill.py" --dry-run [--days N] [--projects slug,...]
   ```

   Review: session count, estimated tokens, and the **privacy notice** — backfill
   reads all local Claude Code transcripts. Use `--projects` to limit scope.

3. **Confirm with the user** — surface the dry-run output and ask:
   > "This will process N sessions (~X estimated tokens). Proceed?"

4. **Run the backfill**:

   ```
   python "${CLAUDE_PLUGIN_ROOT}/scripts/backfill.py" [--days N] [--since DATE] [--all]
                                                      [--projects slug,...] [--concurrency 3]
   ```

   Flags:
   - `--concurrency 3` — parallel LLM calls (default: 3; reduce if rate-limited)
   - `--no-catalogs` — skip `diary.json` regeneration
   - `--no-now` — skip `now/` regeneration

5. **After the run**, consolidate the captured learnings into memory files:

   ```
   /multiplai:dream
   /multiplai:dream-remember
   ```

## Notes

- **Idempotent**: sessions already in `learnings/` + `diary/` are skipped
- **Cost**: one LLM call per chunk (~32k token budget per chunk); dry-run shows estimate
- **Diary lookback**: `diary_catalog_days` config limits how far back `diary.json` reaches;
  deep-history backfill won't all appear in the catalog unless lookback is raised
- **Out of scope**: the live `extract_learnings.py` pipeline (runs per-session on Stop hook)
  is not replaced — backfill only fills gaps for past sessions
