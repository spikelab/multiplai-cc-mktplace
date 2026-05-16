---
name: dream
description: "Generate a processed-learnings proposal from the pending backlog and write it to .multiplai/inbox/ for review. Does NOT apply changes — run /multiplai:process-learnings to review and apply."
---

# Multiplai Dream — Generate Learnings Proposal

Runs the Dream analysis pipeline: reads all pending learnings from `.multiplai/learnings/`,
calls the LLM to deduplicate and draft a structured change proposal, and writes it to
`.multiplai/inbox/processed-learnings-YYYY-MM-DD.md`.

**No memory files are modified.** The proposal is for review only.
Run `/multiplai:process-learnings` to load the proposal and apply approved changes.

---

## Steps

1. **Check for pending learnings:**
   ```
   python "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" --check
   ```
   If the output says no pending learnings, inform Spike and exit.

2. **Generate the proposal:**
   ```
   python "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py"
   ```
   (No flags — default is report mode.)

3. **Report results:**
   - Path to the proposal file in `.multiplai/inbox/`
   - Number of source files and approximate learnings count
   - Remind: run `/multiplai:process-learnings` to review and apply

---

## Autonomous mode (--auto)

If Spike explicitly asks to apply changes without review:
```
python "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" --auto
```

This rewrites memory files directly and commits. Use only when Spike explicitly
requests fully autonomous operation — the default is always human-in-the-loop.

---

## Catalog Regeneration

After `--auto` mode completes, regenerate catalogs:
```
python "${CLAUDE_PLUGIN_ROOT}/scripts/generate_catalog.py"
```
(Skipped for report-only mode — catalogs are regenerated after the user applies
changes via `/multiplai:process-learnings`.)

---

## Constraints
- Never invoke `--auto` unless Spike explicitly requests autonomous operation.
- The default (`python dream.py`) is always report mode — safe to run anytime.
- The dream script uses the path resolver for all file locations — never hardcode paths.
- All LLM calls go through the model client abstraction — never import the SDK directly.
- If catalog generation fails or errors occur, the dream cycle still completes successfully. Catalog failures are logged but do not block or prevent the dream from finishing.
- If there is nothing to consolidate (empty learnings), inform the user and exit — do not run consolidation on an empty backlog.
