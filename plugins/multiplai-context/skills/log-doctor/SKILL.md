---
name: log-doctor
description: Analyze the multiplai runtime logs to find failures, anomalies, and degradation across subsystems (context_manager, extract_learnings, backfill, dream, session lifecycle hooks, etc.), verify root causes against source code, and produce a fix-recommendation report. Can focus on a single subsystem. Triggers on "check the logs", "why is X failing", "log doctor", "analyze multiplai logs", "what's broken in multiplai", "log health report".
---

# log-doctor

Turn the multiplai log directory into an actionable fix report: scan → cluster →
verify against source → recommend.

## Step 1 — Run the scanner

```bash
uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/log_doctor.py" \
  [--subsystem <name>[,<name>]] \
  [--days N] [--errors-only] [--json] [--logs-dir DIR]
```

- If the user named a subsystem ("why is extract_learnings failing?"), pass
  `--subsystem`. Run `--list` first if unsure of the exact name — subsystem
  names are the log-file basenames (e.g. `context_manager`, `extract_learnings`,
  `backfill`, `dream`, `session_start`, `session_end`, `session_stop`,
  `synthesize_now`, `generate_catalog`, `venv_bootstrap`, `pre_compact`,
  `activity`, `hook-errors`).
- Default to the full directory when no focus was given.
- Use `--days 7` when the user asks about "recent" problems.
- The logs directory defaults to the plugin's `paths.logs_dir()`
  (`<workspace>/.multiplai/data/logs`); `--logs-dir` overrides it.

The digest gives you: per-subsystem entry/error counts, cross-cutting health
anomalies, and error/warning clusters (deduplicated by normalized signature,
with counts, first/last seen, and a traceback tail).

## Step 2 — Triage clusters

Pick the clusters that matter. Prioritize:

1. ERROR clusters with high counts or recent `last_seen`
2. WARNING clusters that indicate silent degradation (fallbacks, non-fatal
   failures that repeat every run)
3. Health anomalies (oversized append-only logs, unparseable formats, missing
   session ids)
4. INFO clusters that reveal wrong behavior (e.g. a mock client selected in a
   real run, duplicate emissions, router fallbacks on every prompt)

Ignore one-off transient errors (network blips) unless they repeat.

## Step 3 — Verify against source (MANDATORY before recommending)

A recommendation without a verified root cause is worthless. For each candidate:

1. The traceback tail names the failing file — usually a plugin script under
   `${CLAUDE_PLUGIN_ROOT}/scripts/`, a `multiplai_core` module, or a harness
   hook. Read the code at the failure site.
2. Confirm the mechanism: does the code actually do what the log implies?
   Quote the offending lines in the report.
3. Classify the fix target: **plugin script**, **multiplai-core library**,
   **harness/hooks**, **skill**, or **config/environment**.
4. If the cause cannot be confirmed from code, mark it "unverified hypothesis"
   — never present a guess as a diagnosis.

## Step 4 — Write the report

Write to `INBOX/log-doctor-<scope>-YYYY-MM-DD.md` in the workspace root
(never straight to PLANS/ or RESOURCES/). Structure:

```markdown
# Log Doctor Report — <scope> — <date>

## Summary
One paragraph: overall health, top issue.

## Findings
### F1 — <title> [severity: high|medium|low] [status: verified|hypothesis]
- **Subsystem:** …
- **Evidence:** log signature, count, first/last seen
- **Root cause:** quoted code + explanation (or "unverified")
- **Recommended fix:** concrete change, target file/repo
- **Effort:** trivial | small | medium

## Not worth fixing
Clusters triaged out, one line each with reason.
```

Reply in the console with a 3-line summary and the report path. Do not paste
the whole report into the console.

## Notes

- The scanner is read-only; it never modifies logs.
- `hook-errors.log` aggregates ERROR+ from all components — use it to catch
  errors from subsystems whose own logs rotated away, but attribute findings
  to the originating component, not to "hook-errors".
- Flag deviations from the logging standard (format drift, missing session
  ids, missing rotation) as findings too.
