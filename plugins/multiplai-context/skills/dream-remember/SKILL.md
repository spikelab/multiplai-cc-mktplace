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

### Shared decision record (`## Processed`) — read this before Step 3

The proposal `.md` is **itself the decision record**, shared with the multiplai-gui hub
GUI. When an item is decided (applied, edited, or rejected) its block is **moved into a
`## Processed` section** at the end of the file; the parser ignores that section, so a
moved item is no longer pending. That is the entire cross-tool contract — **no sidecar,
no key scheme** — so one proposal can be reviewed partly in the GUI and finished here (or
vice-versa) with no double-applies and nothing re-presented. This skill drives it through
`dream.py`: `--pending-view` to see what is still pending, `--mark-processed` to record
each decision, and `--archive` (which now *refuses* to archive while anything is still
pending). The canonical spec of this contract lives in `scripts/lib/dream_processed.py` and,
across the repo boundary, in `multiplai-gui`'s `hub/src/multiplai_hub/dreams.py`. Never
hand-write a `## Processed` block — always go through `dream.py`.

---

## Step 1: Locate the Proposal

Check `.multiplai/dreams/` for a file matching `processed-learnings-*.md` — top level
only, skip the `applied/` and `rejected/` subdirectories (those hold already-reviewed
proposals; never recurse into them). Take the **most recently modified** (newest
mtime — not lexical name order; a same-day re-run writes a `-2`, `-3`, … suffixed file
that is newer but sorts *before* the base name).

- **Found:** load it, report its date and summary line to the user, and **record its
  exact path — Step 6 archives that exact file; never re-discover it later** (a newer
  proposal from another session may appear mid-review). Proceed to Step 3.
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

**First, find out what is still pending.** Some items may already have been decided in
the multiplai-gui GUI (moved to the proposal's `## Processed` section). Run:

```bash
uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" \
  --pending-view --proposal <exact-proposal-path-recorded-in-Step-1>
```

It prints `PENDING: <count>` followed by one ref per still-open item
(`update:<file>#<N>` / `action:A<N>`). **Present ONLY the pending items** — never
re-present anything under `## Processed`. Compare the pending count to the proposal's
total item count (from reading the file): if some are already decided, tell the user
plainly, e.g. *"2 items were already processed via the GUI — showing the remaining 4."*
If `PENDING: 0`, everything is already decided: skip to Step 6 (archive) and tell the
user there was nothing left to review.

Read the proposal file in full (so you have each pending item's content and section).
Then tell the user:

- The source file path and date
- A one-line summary: e.g. "17 proposed updates across 5 files, plus 3 action items, from 8 learnings files"
- If the proposal has a `## Action Items` section, mention the count — these are NOT memory;
  approved ones get written to `PLANS/dream-actions-{date}.md` (handled in Step 4b).
- **Check the `## Routing Warnings` section** (appended by dream.py's deterministic
  validation gate). If it says `(none)`, say "routing validation clean". If it lists
  warnings, surface them to the user NOW, next to the affected item numbers — each
  warning names its item as `` `file` #N (title) ``. If the section is missing
  entirely, say so: the gate didn't run, so misroutes/duplicates were not checked.
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

### Routing Warnings gate (before applying anything)

Never silently apply an item that appears in `## Routing Warnings`. For each flagged
item the user approved:

- **"section … does not exist in target but does in `X`"** → propose applying to `X`
  instead (section names are unique across memory files — the section's owner file is
  the right home). Ask, don't reroute silently.
- **"new section collides with an existing section in `X`"** → ask the user to rename
  the section or reroute to `X`; applying as-is would break the unique-section invariant.
- **"proposed text already present in … `file:line`"** → read that location; if it's the
  same insight, skip the item (or merge into the existing entry) and tell the user.
  Apply as new text only if the user confirms it's an intentional update.

Unflagged items proceed normally.

### Applying edits

For each approved update:

1. Read the target memory file fresh (it may have changed since proposal was generated).
2. Find the right insertion point (section header mentioned in the proposal).
3. Apply with the Edit tool — one edit at a time per file, in order.
4. Update "Last Updated" date if present.
5. Confirm each edit was applied.
6. **Record the decision to the shared `## Processed` record** — immediately after the
   edit lands (so a crash can only ever leave a just-written item still pending, never
   double-applied):
   ```bash
   uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" \
     --mark-processed --proposal <path> \
     --ref update:<file>#<N> --status applied --target <memory-file>
   ```
   Use `--status edited` (still with `--target`) when you applied a user-modified
   version (`modify N`). The `<file>` in `--ref` is the **raw target string from the
   `## Updates for \`<file>\`` heading** (not a resolved path); `<N>` is the item's
   `### N.` number.

**Also record every reject.** For each item the user did NOT approve (an explicit
reject, or an item skipped on a partial/`none` review), move it to `## Processed` too so
the proposal becomes fully decided and archivable — a recorded reject writes nothing to
memory:
```bash
uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" \
  --mark-processed --proposal <path> --ref update:<file>#<N> --status rejected
```
(No `--target` on a reject.) Do the same for action items in Step 4b using
`--ref action:A<N>`. When you are done, `--pending-view` should print `PENDING: 0` — that
is what lets Step 6 archive.

---

## Step 4b: Write Approved Action Items to PLANS/

If the proposal has a `## Action Items` section, handle the user's approved `A{N}` items
(`all`, an explicit `A1,A3` list, or `none`). Action items are NOT memory — they are work
the toolchain should do, and must survive the Step 5 learnings cleanup, so they go to a
tracked file.

**Where `PLANS/` lives:** resolve it against the workspace root — the path in
`$CLAUDE_CONFIG_DIR/.workspace` (with `CLAUDE_CONFIG_DIR` defaulting to the standard
Claude config dir) — NOT the session cwd. If no workspace is configured (vanilla
install), ask the user where to put action items before writing anything; suggest
`~/.multiplai/PLANS/`. Never create a bare `PLANS/` directory wherever the session
happens to be.

For each approved action item, append to `{workspace}/PLANS/dream-actions-{YYYY-MM-DD}.md`
(create it if absent, today's date). Each entry as an unchecked task:

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

**Record each action-item decision to `## Processed`** (same contract as Step 4): after
writing an approved item to PLANS, mark it `applied`; mark every un-approved action item
`rejected`. This is what makes the proposal fully decided:
```bash
uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" \
  --mark-processed --proposal <path> --ref action:A<N> --status {applied|rejected}
```

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
nothing needs clearing. (Step 6's `--archive` flag moving the ONE proposal file this
session reviewed into `dreams/applied/` or `dreams/rejected/` is fine and expected —
dream.py moves the specific path you give it; the ban is on globbing files other
sessions may own.) If you must stop a running `dream.py`/catalog job, kill its
specific python PID — **never `pkill -f <script>`**, which also matches the calling
shell and kills your own session.

---

## Step 6: Record the Consolidation (stamp dream state, archive proposal)

**Always run this after the review — including when the user chose `none`.**
It writes `last_run` to `dream_state.yaml` so the SessionStart dream gate stops
nudging — the report-only `/dream` and this skill otherwise never record that a
consolidation happened, leaving the gate permanently "due" — and it archives the
reviewed proposal so `dreams/` holds only pending proposals (without this, reviewed
and pending proposals are indistinguishable and pile up).

```bash
uv run --no-project "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" --stamp \
  --files-updated <M> --learnings-processed <N> \
  --archive <exact-proposal-path-recorded-in-Step-1>
```

Where `<M>` = number of memory files actually edited and `<N>` = number of updates
applied. When the user chose `none`, use `--files-updated 0 --learnings-processed 0`
and add **`--archive-as rejected`** — a fully rejected proposal is reviewed-and-done
(its learnings are already deleted by Step 5) and must not linger looking pending; it
lands in `dreams/rejected/` instead of `dreams/applied/`.

Pass the exact path recorded in Step 1 — never re-discover the file here (a newer
pending proposal from another session may have arrived mid-review; see the Step 5
warning). dream.py performs the move itself: collision-safe (a same-name file already
archived gets a `-2`/`-3` suffix, never overwritten) and with a plain rename, so it
works whether or not the workspace git tracks `.multiplai/`.

**`--archive` refuses to archive a partially-decided proposal.** If any item is still
pending (not moved to `## Processed`), the command exits non-zero, lists the pending
refs, and does **not** move the file or touch `dream_state`. This is the backstop for
the GUI/CLI split review — the CLI must never archive a proposal the GUI still has live
items in. So if you followed Step 4/4b correctly (recording a decision for **every**
item, approvals *and* rejects), archiving succeeds; if it refuses, you missed some — run
`--pending-view` to see which, decide/record them, then re-run this step. Do NOT force it
or delete the file by hand; leave the proposal pending and tell the user which items
remain.

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

Omit the action-items line if there were none. On a `none` review the archive line
reads `✓ Archived rejected proposal to .multiplai/dreams/rejected/`; if archiving
failed or was somehow not performed, say so instead of printing the ✓ line.

---

## Guidelines

- Be aggressive about deduplication. The same lesson appearing 4× should become ONE entry.
- Respect trust levels — don't apply untrusted single-occurrence items unless the user explicitly approves.
- Match the existing style of each memory file exactly.
- Never silently drop learnings — filtered-out items still get deleted (they're in the proposal's "Filtered Out" section so the user saw them).
- Do not ask for confirmation on items the user didn't mention in their approval range.
