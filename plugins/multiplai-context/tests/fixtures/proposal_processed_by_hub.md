# Processed Learnings — 2026-07-31

**Sources:** 2 files, ~6 entries

---

## Updates for `git-policy.md` (2 learnings)



---

## Updates for `technical-pref.md` (1 learning)


---

## Action Items



---

## Filtered Out

- Trivial rename — one-off (Source: 2026-07-30.md:70)

---

## Processed

_Items decided via the GUI or `/dream-remember`, moved here so they are no longer pending. Kept for history; delete the `**Processed:**` line and move a block back up to restore it._

### 2. Never skip a pre-commit hook
**Processed:** rejected · 2026-07-31T09:00:00Z
**Section:** Branching
**Change:** update
> A red hook is information, not an obstacle.

**Source:** 2026-07-30.md:41

### 1. Worktrees are the default for non-trivial work
**Processed:** applied → git-policy.md · 2026-07-31T09:00:00Z
**Section:** Branching
**Change:** add
> Any multi-file change goes on a branch in a worktree.

**Source:** 2026-07-30.md:12

### 1. Prefer uv over pip
**Processed:** edited → technical-pref.md · 2026-07-31T09:00:00Z
**Section:** Python
**Change:** add
> Reach for `uv` first.

**Source:** 2026-07-31.md:8

### A2. Garbage-collect consolidated learnings
**Processed:** rejected · 2026-07-31T09:00:00Z
- What: a deterministic GC subcommand
- Why: the directory grows without bound
- Source: 2026-07-31.md:60

### A1. Batch the mark-processed calls
**Processed:** applied · 2026-07-31T09:00:00Z
- What: accept a JSON array of decisions
- Why: 70 cold starts per review
- Source: 2026-07-31.md:52
