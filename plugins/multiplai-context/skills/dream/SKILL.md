---
name: dream
description: "Generate a processed-learnings proposal from the pending backlog and write it to .multiplai/dreams/ for review. Does NOT apply changes — run /multiplai-context:dream-remember to review and apply."
---

# Multiplai Dream — Generate Learnings Proposal

Runs the Dream analysis pipeline: reads all pending learnings from `.multiplai/learnings/`,
calls the LLM to deduplicate and draft a structured change proposal, and writes it to
`.multiplai/dreams/processed-learnings-YYYY-MM-DD.md`.

**No memory files are modified.** The proposal is for review only.
Run `/multiplai-context:dream-remember` to load the proposal and apply approved changes.

---

## Steps

1. **Check for pending learnings:**
   ```
   uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" --check
   ```
   If the output says no pending learnings, inform the user and exit.

2. **Generate the proposal** — this can run for many minutes (often >10 min on a
   large backlog), past the Bash tool's 600s max timeout. You **MUST** invoke it
   via the Bash tool with `run_in_background: true`:
   ```
   uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py"
   ```
   (No flags — default is report mode.)

   - Use the Bash tool's **`run_in_background: true`** option (no `&`, no `nohup`).
     The harness **re-invokes you automatically when the process exits** — no polling.
   - On re-invocation, confirm success by the sentinel line
     **`Proposal written to <path>`** (the file under `.multiplai/dreams/` will also
     exist). Only then proceed to step 3.
   - **NEVER** detect completion with `nohup … & ; until ! ps -p "$PID" …`. In this
     environment PID 1 is the `claude` process, not an init reaper: a finished script
     becomes an **unreaped `<defunct>` zombie that matches `ps -p PID` forever**, so
     the loop never terminates and the user has to kill it by hand. Detect completion
     by the **sentinel line / output file only** — never by process liveness.

3. **Report results:**
   - Path to the proposal file in `.multiplai/dreams/`
   - Number of source files and approximate learnings count
   - Remind: run `/multiplai-context:dream-remember` to review and apply

---

## Autonomous mode (--auto)

If the user explicitly asks to apply changes without review:
```
uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" --auto
```

This rewrites memory files directly and commits. Use only when the user explicitly
requests fully autonomous operation — the default is always human-in-the-loop.

---

## Catalog Regeneration

After `--auto` mode completes, regenerate catalogs:
```
uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/generate_catalog.py"
```
(Skipped for report-only mode — catalogs are regenerated after the user applies
changes via `/multiplai-context:dream-remember`.)

---

## Constraints
- Never invoke `--auto` unless the user explicitly requests autonomous operation.
- The default (`python dream.py`) is always report mode — safe to run anytime.
- The dream script uses the path resolver for all file locations — never hardcode paths.
- All LLM calls go through the model client abstraction — never import the SDK directly.
- If catalog generation fails or errors occur, the dream cycle still completes successfully. Catalog failures are logged but do not block or prevent the dream from finishing.
- If there is nothing to consolidate (empty learnings), inform the user and exit — do not run consolidation on an empty backlog.
