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

   - **Directory Validation**: For each Paths field (`memory_dir`, `diary_dir`, `data_dir`, `venv_dir`), report whether the directory exists on disk or is missing.

   - **Memory Files**: The script scans the **entire** memory dir (`memory_dir.glob("*.md")`), not a fixed list. Use `memory_summary` (`total`, `fresh`, `stale`, `required_missing`) for the headline (e.g. "21/27 fresh"). Then enumerate explicitly **only** the files that need attention — anything in `required_missing` and any entry with `"stale": true` (show size + age_days, oldest first). Do **not** dump a row for every healthy file; collapse those to the fresh count. `required_missing` lists only absent starter-template files (`me.md`, `technical-pref.md`, `preferences.md`); a missing non-starter file is not flagged as an error.

   - **Diary Status**: Number of diary entries found.

   - **Learnings**: Number of unprocessed learning lines.

   - **Dream**: Date of last dream consolidation, or "never" if none has occurred.

   - **Recommendations**: Actionable suggestions — recommend `/multiplai:setup` only when `required_missing` is non-empty, `/multiplai:dream` for stale memory (any file in the corpus, not just starter files) or unprocessed learnings. Pass the script's pre-built `recommendations` through; don't narrow them back to the starter trio.

3. **Handle fresh install gracefully:** If the plugin is not yet configured (no memory directory exists), report that the plugin needs first-time setup and recommend running `/multiplai:setup` rather than showing errors.

## Constraints
- The health check script uses the path resolver for all file locations — never hardcode paths.
- Works correctly with custom directories configured via userConfig.
- No direct SDK imports — client detection uses the model client module's `detect_client_type()` function.
