---
name: plan
description: Author self-contained, executable implementation plans that carry their own completion contract — verifiable "Done means" criteria, explicit constraints/out-of-scope, and stop-and-ask gates. The resulting plan file can be handed to a fresh session ("implement the plan"), fed to a goal runner, or used as buildme input, with no conversation context needed. Triggers on "write a plan", "make a plan", "implementation plan", "plan this out", "draft a plan for", or explicit /plan invocation.
---

# Plan

Produce an implementation plan that defines **done**, not just **how**. A plan
that only describes steps leaves the executing agent to decide when to stop —
that is where scope drift and premature "done!" come from. Every plan this
skill emits carries three things most plans lack:

1. **Done means** — numbered, verifiable completion criteria.
2. **Constraints / out of scope** — hard rules, exclusions, and stop-and-ask
   gates that survive context compaction.
3. **Self-containedness** — a fresh session with zero conversation context can
   execute the plan from the file alone.

## Process

### 1. Scope check

If the request is underspecified — no target repo, unclear outcome, unknown
consumers of the result — ask 2–3 focused questions before writing anything.
Do not fill gaps with assumptions. If the `interviewer` skill
(multiplai-research) is installed and the gaps are large, offer it.

### 2. Recon before writing

Ground the plan in reality, not memory:

- Read the code and files the plan will touch.
- Verify **every** path, filename, branch, command, and tool you reference
  actually exists — or mark it explicitly as *to-be-created by this plan*.
  A plan that references `PLAN-2-hub.md` when the file is `PLAN-2-app.md`
  sends the executor into a wall.
- Check repo state (`git status`, current branch) for anything the plan must
  work around (uncommitted WIP, missing remotes, credential scope).

### 3. Write the plan file

Plans go to files, never to the console.

- **Routing:** follow the workspace's routing rules if a workspace `CLAUDE.md`
  defines them (e.g. an inbox/landing directory). Otherwise use the project's
  `plans/` directory, creating it if needed.
- **Filename:** `plan-<slug>-YYYY-MM-DD.md`. State the plan's own path in its
  header so the executor can name it unambiguously.

### 4. Self-test before delivering

Re-read the plan as if you were a fresh session with no conversation history:

- No "as discussed above" / "the approach we agreed on" — inline it.
- All paths are absolute or repo-relative from a stated root.
- Every referenced artifact either exists (verified in step 2) or is created
  by a numbered work item.
- Each "Done means" criterion is checkable by a command or observation.

Fix what fails, then reply with the file path and a 2–3 sentence summary —
not the plan body.

## Template (mandatory sections)

```markdown
# Plan: <title>

> File: <path/to/this/file.md> · Date: <YYYY-MM-DD>

**Objective** — one sentence: what exists when this plan is complete.

## Context

Why this work exists. Exact paths/links to source research, prior plans,
or decisions the executor may need. What was already tried, if relevant.

## Outcome

The observable end state, in prose. What a user or session can *do*
afterwards that they cannot do now.

## Work items

Ordered, numbered, with file-level specifics. Each item names its
deliverable (file, branch, PR, passing test) so progress is checkable.
Note parallelizable items explicitly.

## Constraints / out of scope

- Hard rules the executor must not violate (repos not to touch, branches
  not to push, APIs not to call).
- Explicit exclusions: things a reasonable agent might assume are in
  scope but are not — name them.
- Stop-and-ask gates: "if X fails / is missing, stop and ask" for any
  step that depends on access, credentials, or decisions the executor
  may not have. Never let the agent improvise past a gate.

## Done means

Numbered criteria. Every criterion is verifiable by a specific command
or observation — see quality bar below.

## Verification

How to prove each criterion: the commands to run, the outputs to expect,
and what the final report should contain (e.g. a pass/fail table with
evidence). If something can only be verified by the user (visual check,
production access), say so explicitly instead of claiming it.
```

Sections may be short, but none may be omitted. An empty
"Constraints / out of scope" is a statement ("no constraints beyond
workspace defaults"), not a missing section.

## Quality bar for "Done means"

Each criterion must be checkable without judgment calls:

| ❌ Vague | ✅ Verifiable |
|---|---|
| Auth works | `pytest tests/auth -x` passes and `curl -s localhost:8000/login` returns 200 |
| Docs updated | `README.md` has a "Configuration" section listing all 4 env vars from `.env.example` |
| Doesn't break existing flows | `make test` passes; `docker compose config` shows `front` outside the default profile |
| Report delivered | Report written to `<exact path>` ending with a pass/fail table and the user's merge commands |

If a criterion cannot be made verifiable, either sharpen the work item
until it can, or move it to Verification as an explicitly user-checked
item.

## Composing downstream

The plan file is the single artifact — do not maintain a parallel "goal"
document that restates it (two artifacts pointing at each other drift).
It is directly consumable by:

- **"implement the plan"** — a fresh session executing it end-to-end,
  reporting against Done means.
- **A goal/autonomous runner** — Outcome + Done means + Constraints are
  the goal contract; Work items are the decomposition.
- **buildme** (multiplai-dev) — Context + Outcome seed the proposal stage.
