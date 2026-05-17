---
name: dream-remember
description: Review and apply pending memory updates from the learnings backlog. Checks .multiplai/dreams/ for a pre-generated Dream proposal; if none exists, generates one. Then presents updates grouped by target file, waits for approval, applies edits, and cleans up processed learnings files.
model: opus
effort: high
---

# Multiplai: Process Learnings

Human-in-the-loop workflow for applying accumulated session learnings to memory files.

Dream (nightly or on demand via `/multiplai:dream`) generates a proposal file in
`.multiplai/dreams/`. This skill loads that proposal, walks through it with the user, and
applies approved changes.

---

## Step 1: Locate the Proposal

Check `.multiplai/dreams/` for a file matching `processed-learnings-*.md`, most recent first.

- **Found:** load it, report its date and summary line to the user, proceed to Step 3.
- **Not found:** tell the user "No pre-generated proposal found — generating one now" and run:
  ```
  python "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py"
  ```
  Wait for it to complete, then load the newly written file from the dreams directory and proceed to Step 3.

Determine `CLAUDE_PLUGIN_ROOT` from the environment variable `$CLAUDE_PLUGIN_ROOT`.
The learnings directory is `.multiplai/learnings/` relative to the workspace root
(or the path returned by `paths.learnings_dir` — same thing).

---

## Step 2: Scan the Backlog (only if generating fresh without dream.py)

If `dream.py` is unavailable and you need to generate manually:

1. Read all `.md` files in `.multiplai/learnings/` (skip `archived/` subdirectory).
2. Split on `---` separators to extract individual session blocks.
3. Parse each block: trust level, type, description, target file, suggested change.
4. Group by target file, deduplicate, resolve contradictions (most recent high-trust wins).
5. Draft updates following the format in Step 3.

---

## Step 3: Present the Proposal

Read the proposal file in full. Then tell the user:

- The source file path and date
- A one-line summary: e.g. "17 proposed updates across 5 files from 8 learnings files"
- **"Review the file and tell me: `all` / `none` / numbers like `1,3,5` or `1-12,16-20` / or `modify`"**

Do NOT dump the full proposal into chat. Tell the user where the file is so they can open it.

---

## Step 4: Apply Approved Updates

Parse the user's response:

- **`all`** → apply every numbered update
- **`none`** → skip all, go to Step 6 cleanup
- **Number ranges** (e.g. `1-12, 16-20, 34`) → apply only those items; silently skip unlisted ones — do NOT ask for confirmation on skipped items
- **`modify N`** → ask the user what change they want for item N, then apply modified version

### RULE-PROPOSAL handling

Items marked `**[RULE-PROPOSAL]**` (changes to CLAUDE.md behavioral rules) MUST be
presented individually, one at a time, even when the user said "all". For each one:

```
RULE-PROPOSAL #N: {short title}
Target: CLAUDE.md — {section}
Proposed rule:
> {exact text}

Source: {learnings file}
Trust: {level}

Apply this rule? (yes / no / modify)
```

Wait for explicit answer before moving to the next RULE-PROPOSAL. After all
RULE-PROPOSAL items are resolved, apply remaining standard items as approved.

### Applying edits

For each approved update:

1. Read the target memory file fresh (it may have changed since proposal was generated).
2. Find the right insertion point (section header mentioned in the proposal).
3. Apply with the Edit tool — one edit at a time per file, in order.
4. Update "Last Updated" date if present.
5. Confirm each edit was applied.

---

## Step 5: Clean Up Processed Learnings

After all approved updates are applied:

1. Delete ALL `.md` files in `.multiplai/learnings/` that were listed as sources in the proposal.
   - Today's file exception: delete anyway — the Stop hook recreates it if needed.
   - Rejected items get deleted too — reviewed-and-rejected is done.
2. Git history preserves originals for forensic review.

---

## Step 6: Regenerate Memory Catalog

After memory files have been updated, run:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/generate_catalog.py"
```

Skip this step if no memory files were actually modified.

---

## Step 7: Summary

Print a brief summary:

```
✓ Applied N updates across M files
  - technical-pref.md: N updates
  - preferences.md: N updates
✓ Deleted N learnings files
⊘ Skipped N updates (items #X, #Y — not approved)
```

---

## Guidelines

- Be aggressive about deduplication. The same lesson appearing 4× should become ONE entry.
- Respect trust levels — don't apply untrusted single-occurrence items unless the user explicitly approves.
- Match the existing style of each memory file exactly.
- Never silently drop learnings — filtered-out items still get deleted (they're in the proposal's "Filtered Out" section so the user saw them).
- Do not ask for confirmation on items the user didn't mention in their approval range.
