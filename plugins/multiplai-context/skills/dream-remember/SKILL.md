---
name: dream-remember
description: Review and apply pending memory updates from the learnings backlog. Checks .multiplai/dreams/ for a pre-generated Dream proposal; if none exists, generates one. Then presents updates grouped by target file, waits for approval, applies edits, and cleans up processed learnings files.
model: opus
effort: medium
---

# Multiplai: Process Learnings

Human-in-the-loop workflow for applying accumulated session learnings to memory files.

Dream (nightly or on demand via `/multiplai-context:dream`) generates a proposal file in
`.multiplai/dreams/`. This skill loads that proposal, walks through it with the user, and
applies approved changes.

### The proposal file is the decision record

The proposal `.md` is itself the record of what has been reviewed. When an item is
decided — applied, edited, or rejected — its block is **moved into a `## Processed`
section at the end of the file** (via `dream.py --mark-processed`, never by hand). Items
under `## Processed` are **no longer pending**: this skill (and any future client) skips
them, so a proposal can be reviewed in more than one sitting or tool with no
double-applies. That one heading is the entire cross-tool contract — there is no sidecar
file. A proposal is archived (Step 6) only once **nothing is left pending**.

---

## Step 1: Locate the Proposal

Check `.multiplai/dreams/` for a file matching `processed-learnings-*.md` — top level
only, skip the `applied/` and `rejected/` subdirectories (those hold already-reviewed
proposals; never recurse into them). Take the **most recently modified** (newest
mtime — not lexical name order; a same-day re-run writes a `-2`, `-3`, … suffixed file
that is newer but sorts *before* the base name).

- **Found:** load it, report its date and summary line to the user, and **record its
  exact path — Step 6 archives that exact file; never re-discover it later** (a newer
  proposal from another session may appear mid-review). Proceed to Step 1b.
- **Not found:** tell the user "No pre-generated proposal found — generating one now" and run:
  ```
  uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py"
  ```
  This can run for many minutes, past the Bash tool's 600s max timeout. You **MUST**
  invoke it via the Bash tool with **`run_in_background: true`** (no `&`, no `nohup`).
  The harness re-invokes you automatically when the process exits. Confirm success by
  the sentinel line **`Proposal written to <path>`**; then load the newly written file
  from the dreams directory and proceed to Step 1b.

  **NEVER** wait via a process-liveness loop (`until ! ps -p "$PID" …`): here PID 1 is
  the `claude` process, not an init reaper, so a finished script becomes an unreaped
  `<defunct>` zombie that matches `ps -p PID` forever and the loop never terminates.
  Detect completion by the sentinel line / output file only.

Determine `CLAUDE_PLUGIN_ROOT` from the environment variable `$CLAUDE_PLUGIN_ROOT`.
The learnings directory is `.multiplai/learnings/` relative to the workspace root
(or the path returned by `paths.learnings_dir` — same thing).

---

## Step 1b: Triage — apply the uncontroversial half first

**Always run this before presenting anything.** A proposal is not too long to
review; it is too long to review *item by item*. At ~190 items the walk costs a
whole context window and gets abandoned partway, which is how the backlog grew
to 380 bullets instead of shrinking.

```bash
uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" \
  --triage --proposal <exact-proposal-path>
```

It classifies every pending item **deterministically** (no model judgement) and
applies only the ones with no decision in them: an additive entry, to a
non-behavioural memory file, that no gate flagged. Everything else stays
pending for you to present in Step 3 — rule proposals, anything landing in
`CLAUDE.md`, anything the routing gate flagged, anything the drafter marked low
confidence, and anything that rewrites rather than appends.

Auto-applied items are marked processed in the proposal and written to a
**receipt** under `.multiplai/dreams/applied/<date>-auto-apply-receipt.md`,
naming each one's target, section, text and source. Memory is under git, so the
pair "receipt + `git diff`" is what makes applying-without-reading reversible.

Report to the user, in this order:

1. the auto-applied count and the receipt path — **tell them to skim it**;
2. any `NOT APPLIED` lines (a file whose applier result was unsafe — those items
   are still pending and will appear in Step 3);
3. then move to Step 3 with what remains.

Use `--dry-run` to see the partition without writing anything. If triage exits
non-zero, or the script is unavailable, fall back to reviewing every item —
never skip items because triage failed.

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

Read the proposal file in full. **Ignore everything under a `## Processed` section** —
those items were already decided (here or in the GUI) and are kept only for history.
Present only the still-pending items (those under `## Updates for …` and `## Action
Items`). If a `## Processed` section exists, tell the user: "N item(s) already processed
(via the GUI or an earlier run); showing the remaining M."

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

### Applying edits — one pass per target file

The proposal already groups updates under ``## Updates for `file` ``, so **the target
file is the unit of work, not the item**. Working item by item re-reads the same memory
file once per update bound for it and spends a fresh `uv run` cold start on every
decision; a 70-item review across 14 target files exhausted its context window that way
and had to hand off mid-review after five files.

For each target file that has at least one **decided** item (approved or rejected):

1. Read the target memory file **once** — it may have changed since the proposal was
   generated. Don't re-read it between edits.
2. Apply that file's approved edits with the Edit tool, in item order, at the insertion
   point each item names (its `**Section:**`).
3. Update the file's "Last Updated" date **once**, after the last edit, if present.
4. Record **every** decision for that file in **one** call — approved *and* rejected.
   `--decisions -` reads a JSON array from stdin, so a large review hits no argv limit
   and there are no shell quoting rules to get wrong:

   ```bash
   uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" --mark-processed \
     --proposal <exact-proposal-path> --decisions - <<'JSON'
   [
     {"kind":"update","file":"technical-pref.md","index":3,"status":"applied","target":"technical-pref.md"},
     {"kind":"update","file":"technical-pref.md","index":4,"status":"edited","target":"technical-pref.md"},
     {"kind":"update","file":"technical-pref.md","index":7,"status":"rejected"}
   ]
   JSON
   ```

   - `file` — the ``## Updates for `file` `` group the item is listed under.
   - `index` — its `### N.` number **within that group**.
   - `status` — what actually happened: `applied`, `edited` (the user changed the text
     before approving), or `rejected`.
   - `target` — the memory file the text actually went to. Usually the same as `file`;
     it differs only when you rerouted the item. Omit it for rejects.

   The command prints `marked N processed, M unchanged` and rewrites the proposal
   atomically, so a failure leaves it exactly as it was rather than half-decided.

Record per file rather than once at the very end: if the session is interrupted,
everything already applied is already recorded and the review resumes at the next file
instead of being re-decided from scratch.

**A reject is a decision.** Record rejects too — including every item on a `none`
review — because Step 6 can only archive once nothing is left pending. A reject writes
nothing to memory. **Items the user neither approved nor rejected stay pending**: leave
them out of the JSON entirely and they remain for a later run or the GUI.

**Batching is mechanics, never consent.** `[RULE-PROPOSAL]` items are still presented
and answered one at a time (above). Only the recording is batched.

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

Then **record every decided action item** (approved *and* rejected) in **one** call, same
JSON shape as Step 4 with `"kind":"action"` and no `file`:

```bash
uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" --mark-processed \
  --proposal <exact-proposal-path> --decisions - <<'JSON'
[
  {"kind":"action","index":1,"status":"applied"},
  {"kind":"action","index":3,"status":"rejected"}
]
JSON
```

`index` is the `### A{N}.` number. Undecided action items stay out of the array.

---

## Step 5: Collect Consolidated Learnings

Always run the collector, then report what it removed:

```bash
uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" --gc-learnings
```

It is pure code — no model call, no lock — and it makes the keep/delete call **per file,
in code**, so you never have to. A learnings file is removed only when **both** hold:

- every `## Session Learnings` record in it has already been consolidated (dream's ledger
  has its hash), **and**
- no proposal citing it is still pending in `.multiplai/dreams/` — i.e. every proposal it
  fed has moved to `applied/`, `rejected/`, or `superseded/`.

Everything else is kept, with the reason printed beside it. Read that output and pass it
on to the user; don't second-guess it.

This is why the old "delete the sources, but only if the whole proposal is now decided"
judgement is gone. Getting it wrong deleted the evidence behind a review that was still
running, and there was no way back. Now: items you deliberately left pending keep their
source files automatically, so their `**Source:** file:line` citations still resolve for
whoever finishes the review (here or in the GUI); and a file appended to since the last
dream run — today's, usually — is kept for the same reason, because its newest records
are not consolidated yet. Git history preserves whatever does get collected.

**You do not delete learnings files yourself, and you never bulk-clear the dreams
directory.** Never glob-delete `.multiplai/dreams/processed-learnings-*.md`: a batch or
recovery run can leave another session's proposal mid-review there, those files are not
yours to touch, and `dream.py` already writes non-colliding `-2`/`-3` suffixes so nothing
needs clearing. (Step 6's `--archive` moving the ONE proposal file this session reviewed
into `dreams/applied/` or `dreams/rejected/` is fine and expected — dream.py moves the
specific path you give it; the ban is on globbing files other sessions may own.) If you
must stop a running `dream.py`/catalog job, kill its specific python PID — **never
`pkill -f <script>`**, which also matches the calling shell and kills your own session.

---

## Step 6: Record the Consolidation (stamp dream state, archive proposal)

**Always run this after the review — including when the user chose `none`.**
It writes `last_run` to `dream_state.yaml` so the SessionStart dream gate stops
nudging — the report-only `/dream` and this skill otherwise never record that a
consolidation happened, leaving the gate permanently "due" — and it archives the
reviewed proposal so `dreams/` holds only pending proposals (without this, reviewed
and pending proposals are indistinguishable and pile up).

```bash
uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" --stamp \
  --files-updated <M> --learnings-processed <N> \
  --archive <exact-proposal-path-recorded-in-Step-1>
```

Where `<M>` = number of memory files actually edited and `<N>` = number of updates
applied. When the user chose `none`, use `--files-updated 0 --learnings-processed 0`
and add **`--archive-as rejected`** — a fully rejected proposal is reviewed-and-done and
must not linger looking pending; it lands in `dreams/rejected/` instead of
`dreams/applied/`. (Its learnings are collected by Step 5 on this run or the next one,
once the archive move makes the proposal no longer pending.)

Pass the exact path recorded in Step 1 — never re-discover the file here (a newer
pending proposal from another session may have arrived mid-review; see the Step 5
warning). dream.py performs the move itself: collision-safe (a same-name file already
archived gets a `-2`/`-3` suffix, never overwritten) and with a plain rename, so it
works whether or not the workspace git tracks `.multiplai/`.

**`--archive` only succeeds when nothing is left pending.** Because every decided item was
moved to `## Processed` in Step 4/4b, a fully-reviewed proposal archives cleanly. If you
deliberately left items pending, `--archive` **refuses** (exits non-zero: "still has
pending items … left pending, not archived") — that is expected. In that case still run
`--stamp` (without `--archive`) so the dream gate stops nudging, tell the user which
item(s) remain pending (to finish in the GUI or a later `/dream-remember`), and do **not**
report the proposal as archived. Split the command when leaving items pending:

```bash
uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" --stamp \
  --files-updated <M> --learnings-processed <N>   # no --archive: items still pending
```

---

## Step 7: Regenerate Memory Catalog

After memory files have been updated, run:

```bash
uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "${CLAUDE_PLUGIN_ROOT}/scripts/generate_catalog.py"
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
✓ Collected N learnings files (M kept — reason)
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
- Never silently drop learnings — filtered-out items are still collected by Step 5 (they're in the proposal's "Filtered Out" section so the user saw them).
- Do not ask for confirmation on items the user didn't mention in their approval range.
