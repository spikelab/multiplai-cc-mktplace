---
name: log-doctor
description: Analyze the multiplai runtime logs to find failures, anomalies, and degradation across subsystems (context_manager, extract_learnings, backfill, dream, session lifecycle hooks, etc.), verify root causes against source code, and produce a fix-recommendation report. Can focus on a single subsystem, and can actively exercise a functionality (probe mode) to verify its expected logs appear. Triggers on "check the logs", "why is X failing", "log doctor", "analyze multiplai logs", "what's broken in multiplai", "log health report", "verify X logs correctly", "exercise X and check the logs".
---

# log-doctor

Two modes:

- **Passive scan** (default) — turn the log directory into an actionable fix
  report: scan → cluster → verify against source → recommend.
- **Probe** — actively exercise a functionality (start a session, run
  deep-research, regenerate catalogs, …) and assert its expected log entries
  appeared. Use when the user asks to "verify X logs correctly" or "exercise
  X and check the logs".

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

Write to `{workspace}/INBOX/log-doctor-<scope>-YYYY-MM-DD.md`, where `{workspace}` is
the path in `$CLAUDE_CONFIG_DIR/.workspace` (with `CLAUDE_CONFIG_DIR` defaulting to the
standard Claude config dir) — NOT the session cwd, and never straight to PLANS/ or
RESOURCES/. If no workspace is configured (vanilla install), write the report to the
current directory instead and tell the user where it landed — do not invent an
`INBOX/`. Structure:

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

## Injection forensics — "why did it inject that?"

When the user questions a context injection (wrong files, irrelevant memory,
"why did X get injected"), use `--injections`. It reconstructs each routing
decision by joining `context_manager` logs (candidate scores, cap/floor,
cooldown suppressions) with `activity.jsonl` inject events (session id, final
files, bytes) on timestamp.

```bash
uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/log_doctor.py" --injections \
  [--file life.md] [--trace 5] [--days N] [--json]
```

- Aggregate table: per file — times picked by the router, times actually
  injected, times cooldown-suppressed, avg/max score. High `injected` with low
  `avg_score` = a file that rides in near the floor; high `suppressed` = a
  file the router wants constantly.
- `--trace N` shows the last N full decisions: every candidate with its score,
  what cooldown removed, what was finally injected.

**Interpreting a "makes no sense" injection — check in this order:**

1. **Cooldown survivor:** the top scorers were suppressed (recently injected),
   so near-floor files filled the slots. Visible in the trace: injected files
   have low scores while suppressed ones scored high. This is a router-design
   issue, not a scoring bug.
2. **Low floor:** the file scored just above `floor_excluded` — weak token
   overlap admitted it. Recurring low-score injections of the same file
   suggest raising the floor or improving that file's routing keywords.
3. **Prompt attribution:** prompts are NOT logged, so to see what the tokens
   matched against, take the session id + timestamp from the trace and find
   the user message in the session transcript
   (`$CLAUDE_CONFIG_DIR/projects/<project>/<session>.jsonl`).

Diagnosability gaps worth reporting as findings when they bite: the router
logs no prompt text (even truncated) and logs `ROUTING_SCORES` only for the
memory corpus — skills/resources injections leave no score trail.

## Probe mode — exercise a functionality, verify its logs

Three steps: baseline → trigger → check.

```bash
# 1. Baseline: snapshot current log sizes
uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/log_doctor.py" --probe-start

# 2. Trigger the functionality (see below)

# 3. Check: only entries appended since the baseline are evaluated
uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/log_doctor.py" \
  --probe-check --scenario <name>        # exit 0 = passed, 1 = failed
```

Run `--scenarios` to list the built-in scenarios; each includes its trigger
instruction. Available: `session-start`, `session-end`, `session-stop`,
`routing`, `extract-learnings`, `generate-catalog`, `synthesize-now`,
`backfill`, `dream`, `deep-research`.

**How to trigger, by scenario:**

- `session-start` / `session-end` / `session-stop` / `routing` /
  `extract-learnings`: run a one-shot nested session from the workspace root —
  `claude -p "say hi"` — which fires the whole lifecycle (SessionStart,
  UserPromptSubmit routing, Stop, SessionEnd). Check each scenario afterwards.
- `generate-catalog`: run the refresh-catalogs skill or
  `uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/generate_catalog.py" --dry-run`
  (dry-run: no writes, no LLM calls).
- `backfill` / `dream` / `synthesize-now`: invoke the corresponding skill
  (keep it small, e.g. backfill `--days 1`).
- `deep-research`: invoke the deep-research skill with a trivial one-source
  question.

For functionality without a built-in scenario, pass ad-hoc expectations
(repeatable): `--expect 'SUBSYSTEM:LEVEL:REGEX'` (LEVEL may be `*`; subsystem
matches the log-file basename or the `[component]` field, so components that
only appear in `hook-errors.log` are still attributable).

**Interpreting results:**

- A failed expectation means the functionality ran but didn't log what the
  logging standard / its own contract says it should — that's a finding
  (missing logging is a bug too), unless the trigger itself failed. Verify the
  trigger actually ran before blaming the logging.
- Any ERROR/CRITICAL from the involved subsystems during the probe fails it
  (override with `--allow-errors` when the error is a known, separate issue).
- Probe failures feed the same report format as passive findings — evidence
  is the probe verdict instead of a cluster.
- The baseline lives at `<logs>/state/log-doctor-probe.json`; re-running
  `--probe-start` overwrites it. Probing is still read-only with respect to
  the logs themselves.

## Notes

- The scanner is read-only; it never modifies logs.
- `hook-errors.log` aggregates ERROR+ from all components — use it to catch
  errors from subsystems whose own logs rotated away, but attribute findings
  to the originating component, not to "hook-errors".
- Flag deviations from the logging standard (format drift, missing session
  ids, missing rotation) as findings too.
