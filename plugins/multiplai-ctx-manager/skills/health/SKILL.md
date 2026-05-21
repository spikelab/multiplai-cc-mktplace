---
name: health
description: "Memory audit — checks completeness and staleness of memory files, plugin infrastructure, and active configuration"
---

# Multiplai Health — Memory Audit

You are the multiplai health audit skill. Your job is to check the completeness and staleness of the user's memory files, plugin infrastructure, and active configuration.

## Steps

1. **Run the health check script:**
   Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/health_check.py"` to audit memory files, directories, and plugin data.
   The script outputs structured JSON to stdout.

2. **Parse the JSON output** and present a markdown-formatted audit report with these sections:

   - **Model Client**: Report which `ModelClient` implementation is active — `AgentSDKClient` (zero-config via Claude Code), `AnthropicAPIClient` (API key fallback), or none (not configured). This is critical for diagnosing LLM call failures.

   - **Routing Quality** (the single most critical signal — lead with it): parse the `routing` block.
     - **Strategy**: report `configured_strategy` vs `effective_strategy`. The default is `token_overlap` (instant, runs synchronously every prompt). `degraded_to_fallback` is only true if someone *explicitly* opted into `llm` without a model client — if so, state that the explicit `llm` choice fell back to `token_overlap`.
     - **Live aggregates** (`routing.live`, from recent real prompts): report `samples`, `cap_saturation_pct`, `mean_picked`, and the `mean_top_score`→`mean_floor_score` spread. Interpret honestly: high saturation (>50%) with a flat spread (top ≈ floor) = low-signal routing (the cutoff is arbitrary). Healthy `token_overlap` shows saturation well under 20% and top noticeably above floor. Few samples (<10) → say it's not yet meaningful.
     - **Last eval** (`routing.last_eval`): report `strategy`, `total_cases`, `recall_pct`, `precision_pct`, `none_accuracy_pct`, `cap_saturation_pct`, and `age_days`. **Mandatory caveats — state these, do not present the numbers bare:**
       - `token_overlap` `none_accuracy_pct ≈ 0` is **expected and by design**, not a regression — lexical overlap structurally cannot abstain (proven: NONE and true-positive score distributions fully overlap). Abstention needs the semantic `llm` router, which is **deferred/opt-in**: ~17s/prompt via the Agent SDK, unusable as a blocking pre-prompt hook pending an async / API-key design.
       - The eval scores whatever `strategy` it last ran with (normally `token_overlap`, the default). If `last_eval.strategy` differs from `effective_strategy`, the numbers don't describe what's running — say so.
       - If `last_eval` is null or `age_days` > 30, recommend re-running `python "${CLAUDE_PLUGIN_ROOT}/scripts/eval_router.py"` (zero LLM cost under `token_overlap`).

   - **Directory Validation**: For each Paths field (`memory_dir`, `diary_dir`, `data_dir`, `venv_dir`), report whether the directory exists on disk or is missing.

   - **Memory Files**: The script scans the **entire** memory dir (`memory_dir.glob("*.md")`), not a fixed list. Use `memory_summary` (`total`, `fresh`, `stale`, `required_missing`) for the headline (e.g. "21/27 fresh"). Then enumerate explicitly **only** the files that need attention — anything in `required_missing` and any entry with `"stale": true` (show size + age_days, oldest first). Do **not** dump a row for every healthy file; collapse those to the fresh count. `required_missing` lists only absent starter-template files (`me.md`, `technical-pref.md`, `preferences.md`); a missing non-starter file is not flagged as an error.

   - **Diary Status**: Number of per-day diary files (`diary.day_count`, one file per UTC day, each with one or more `## Session:` blocks inside — v0.3.0 layout, aligned with learnings). If `extractions.in_flight > 0`, append a one-line caveat: *"N extraction(s) in flight — this count may grow shortly without further action"*. Counts are a filesystem snapshot, not a settled state.

   - **Extractions**: Read the `extractions` block. If `in_flight == 0` and `failed == 0`, omit this section entirely (no news is good news). Otherwise report `pending`, `processing`, `failed`, and `oldest_processing_age_s` (in-flight age). Briefly explain: deferred extraction runs as a detached subprocess from `SessionStart`, so a freshly-started session can transiently report 0 new diary entries while the subprocess works (typical: 10-30s). If `failed > 0`, recommend the user inspect `<data_dir>/failed_extractions/` and surface the marker filenames if useful.

   - **Learnings**: Number of unprocessed learning lines. Same caveat as Diary if extractions are in flight.

   - **Dream**: Date of last dream consolidation, or "never" if none has occurred.

   - **Recommendations**: Actionable suggestions — recommend `/multiplai:setup` only when `required_missing` is non-empty, `/multiplai:dream` for stale memory (any file in the corpus, not just starter files) or unprocessed learnings. Pass the script's pre-built `recommendations` through; don't narrow them back to the starter trio.

3. **Handle fresh install gracefully:** If the plugin is not yet configured (no memory directory exists), report that the plugin needs first-time setup and recommend running `/multiplai:setup` rather than showing errors.

## Constraints
- The health check script uses the path resolver for all file locations — never hardcode paths.
- Works correctly with custom directories configured via userConfig.
- No direct SDK imports — client detection uses the model client module's `detect_client_type()` function.
