---
name: dream-remember
description: Review and apply pending memory updates from the learnings backlog. Checks .multiplai/dreams/ for a pre-generated Dream proposal; if none exists, generates one. Then presents updates grouped by target file, waits for approval, applies edits, and cleans up processed learnings files.
model: opus
effort: high
---

# Multiplai: Process Learnings

Human-in-the-loop workflow for applying accumulated session learnings to memory files.

Dream (nightly or on demand via `/multiplai-context:dream`) generates a proposal file in
`.multiplai/dreams/`. This skill loads that proposal, walks through it with the user, and
applies approved changes.

---

## Step 1: Locate the Proposal

Check `.multiplai/dreams/` for a file matching `processed-learnings-*.md`, taking the **most
recently modified** (newest mtime — not lexical name order; a same-day re-run writes a
`-2`, `-3`, … suffixed file that is newer but sorts *before* the base name).

- **Found:** load it, report its date and summary line to the user, proceed to Step 3.
- **Not found:** tell the user "No pre-generated proposal found — generating one now" and run:
  ```
  uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py"
  ```
  This can run for many minutes, past the Bash tool's 600s max timeout. You **MUST**
  invoke it via the Bash tool with **`run_in_background: true`** (no `&`, no `nohup`).
  The harness re-invokes you automatically when the process exits. Confirm success by
  the sentinel line **`Proposal written to <path>`**; then load the newly written file
  from the dreams directory and proceed to Step 3.

  **NEVER** wait via a process-liveness loop (`until ! ps -p "$PID" …`): here PID 1 is
  the `claude` process, not an init reaper, so a finished script becomes an unreaped
  `<defunct>` zombie that matches `ps -p PID` forever and the loop never terminates.
  Detect completion by the sentinel line / output file only.

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
- A one-line summary: e.g. "17 proposed updates across 5 files, plus 3 action items, from 8 learnings files"
- If the proposal has a `## Action Items` section, mention the count — these are NOT memory;
  approved ones get written to `PLANS/dream-actions-{date}.md` (handled in Step 4b).
- **"Review the file and tell me: `all` / `none` / numbers like `1,3,5` or `1-12,16-20` / `A1,A3` for action items / or `modify`"**

Do NOT dump the full proposal into chat. Tell the user where the file is so they can open it.

Memory updates are numbered `N`; action items are numbered `A{N}`. The user can approve each
set independently (e.g. `all` for memory, `A1,A2` for action items).

---

## Step 4: Apply Approved Updates

Parse the user's response:

- **`all`** → apply every numbered update
- **`none`** → skip all, go to Step 5 cleanup
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

Source: {learnings_file}:{line-number(s)}

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

## Step 4b: Write Approved Action Items to PLANS/

If the proposal has a `## Action Items` section, handle the user's approved `A{N}` items
(`all`, an explicit `A1,A3` list, or `none`). Action items are NOT memory — they are work
the toolchain should do, and must survive the Step 5 learnings cleanup, so they go to a
tracked file.

For each approved action item, append to `PLANS/dream-actions-{YYYY-MM-DD}.md` (create it if
absent, today's date). Each entry as an unchecked task:

```
## Dream action items — {date}

- [ ] {short imperative title}
  - What: {concrete change}
  - Why: {problem it fixes}
  - Source: {learnings_file}:{line-number(s)}
```

If the file already exists for today, append new items under the same heading (don't
duplicate the heading). Report the path and count to the user. Skip this step if there are no
action items or the user approved `none`.

---

## Step 5: Clean Up Processed Learnings

After all approved updates are applied:

1. Delete ALL `.md` files in `.multiplai/learnings/` that were listed as sources in the proposal.
   - Today's file exception: delete anyway — the Stop hook recreates it if needed.
   - Rejected items get deleted too — reviewed-and-rejected is done.
2. Git history preserves originals for forensic review.

**Never bulk-clear the dreams directory.** Cleanup targets `.multiplai/learnings/`
only — never glob-delete `.multiplai/dreams/processed-learnings-*.md`. A batch or
recovery run can leave another session's proposal mid-review there; those files are
not yours to remove, and `dream.py` already writes non-colliding `-2`/`-3` suffixes so
nothing needs clearing. (Moving the ONE proposal file you just applied into
`dreams/applied/` — Step 6 — is fine and expected; the ban is on globbing files other
sessions may own.) If you must stop a running `dream.py`/catalog job, kill its
specific python PID — **never `pkill -f <script>`**, which also matches the calling
shell and kills your own session.

---

## Step 6: Record the Consolidation (stamp dream state, archive proposal)

**Always run this after applying updates** (even if the user approved only a subset).
It writes `last_run` to `dream_state.yaml` so the SessionStart dream gate stops
nudging — the report-only `/dream` and this skill otherwise never record that a
consolidation happened, leaving the gate permanently "due".

```bash
uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" --stamp \
  --files-updated <M> --learnings-processed <N>
```

Where `<M>` = number of memory files actually edited and `<N>` = number of updates
applied. Skip only when the user chose `none` (nothing was applied).

**Then archive the applied proposal** so `dreams/` holds only pending proposals —
without this, applied and pending proposals are indistinguishable and pile up:

```bash
mkdir -p .multiplai/dreams/applied
mv <the-proposal-file-you-applied> .multiplai/dreams/applied/
```

Move ONLY the specific proposal file this session just applied (never a glob — see
the Step 5 warning). If the workspace git tracks `.multiplai/`, use `git mv` instead.
Archive even on a partial apply (some items approved); skip only on `none`.

---

## Step 7: Regenerate Memory Catalog

After memory files have been updated, run:

```bash
uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/generate_catalog.py"
```

Skip this step if no memory files were actually modified.

---

## Step 8: Summary

Print a brief summary:

```
✓ Applied N updates across M files
  - technical-pref.md: N updates
  - preferences.md: N updates
✓ Wrote N action items to PLANS/dream-actions-{date}.md
✓ Deleted N learnings files
✓ Archived proposal to .multiplai/dreams/applied/
⊘ Skipped N updates (items #X, #Y — not approved)
```

Omit the action-items line if there were none.

---

## Guidelines

- Be aggressive about deduplication. The same lesson appearing 4× should become ONE entry.
- Respect trust levels — don't apply untrusted single-occurrence items unless the user explicitly approves.
- Match the existing style of each memory file exactly.
- Never silently drop learnings — filtered-out items still get deleted (they're in the proposal's "Filtered Out" section so the user saw them).
- Do not ask for confirmation on items the user didn't mention in their approval range.
