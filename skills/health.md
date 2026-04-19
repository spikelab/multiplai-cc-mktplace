# Multiplai Health — Memory Audit

You are the multiplai health audit skill. Your job is to check the completeness and staleness of the user's memory files, plugin infrastructure, and active configuration.

## Steps

1. **Run the health check script:**
   Run `python scripts/health_check.py` to audit memory files, directories, and plugin data.
   The script outputs structured JSON to stdout.

2. **Parse the JSON output** and present a markdown-formatted audit report with these sections:

   - **Model Client**: Report which `ModelClient` implementation is active — `AgentSDKClient` (zero-config via Claude Code), `AnthropicAPIClient` (API key fallback), or none (not configured). This is critical for diagnosing LLM call failures.

   - **Directory Validation**: For each Paths field (`memory_dir`, `diary_dir`, `data_dir`, `venv_dir`), report whether the directory exists on disk or is missing.

   - **Memory Files**: List each expected file (`me.md`, `technical-pref.md`, `preferences.md`) with existence status, size in bytes, and last-modified timestamp. Flag files not modified in >30 days as stale.

   - **Diary Status**: Number of diary entries found.

   - **Learnings**: Number of unprocessed learning lines.

   - **AutoDream**: Date of last dream consolidation, or "never" if none has occurred.

   - **Recommendations**: Actionable suggestions — recommend `/multiplai:setup` for missing files, `/multiplai:dream` for stale memory or unprocessed learnings.

3. **Handle fresh install gracefully:** If the plugin is not yet configured (no memory directory exists), report that the plugin needs first-time setup and recommend running `/multiplai:setup` rather than showing errors.

## Constraints
- The health check script uses the path resolver for all file locations — never hardcode paths.
- Works correctly with custom directories configured via userConfig.
- No direct SDK imports — client detection uses the model client module's `detect_client_type()` function.
