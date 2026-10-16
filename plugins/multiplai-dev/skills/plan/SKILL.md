---
name: plan
description: Author self-contained, executable implementation plans that carry their own completion contract — verifiable "Done means" criteria, explicit constraints/out-of-scope, stop-and-ask gates, and a table of what is on disk and what resume does at every point a step can stop. The resulting plan file can be handed to a fresh session ("implement the plan"), fed to a goal runner, or used as buildme input, with no conversation context needed. Triggers on "write a plan", "make a plan", "implementation plan", "plan this out", "draft a plan for", or explicit /plan invocation.
---

# Plan

Produce an implementation plan that defines **done**, not just **how**. A plan
that only describes steps leaves the executing agent to decide when to stop —
that is where scope drift and premature "done!" come from. Every plan this
skill emits carries four things most plans lack:

1. **Done means** — numbered, verifiable completion criteria.
2. **Constraints / out of scope** — hard rules, exclusions, and stop-and-ask
   gates that survive context compaction.
3. **Self-containedness** — a fresh session with zero conversation context can
   execute the plan from the file alone.
4. **Stop and resume states** — a table with one row per step and stop
   point, saying what is on disk and what resume does, so nobody invents
   resume behaviour while writing or fixing the code.

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
- List every step of the delivered work that writes state — files, git
  refs or commits, a state or checkpoint file, a lock, a ledger line, a
  pushed branch or PR — and every point where it can stop: a kill or
  crash, Ctrl-C, a timeout, a failed command, a budget or round cap. If
  resume code already exists, read it, so the table says what resume does
  today.

**Stop and ask when recon shows the scope is not homogeneous.** If the
requested work turns out to span mechanisms with different risk and effort
profiles — a config change and a protocol rewrite, a no-tools call path and a
tool-using agent harness — put the split to the user as explicit choices before
you write scope into the plan. Do not pick which piece is in scope on their
behalf, and do not silently plan all of it. The user decides what the plan
covers; recon only tells them what the pieces are. Pieces that can only
ship together are one scope: plan them, and note the risk split under the
work items instead of asking.

### 3. Write the plan file

Plans go to files, never to the console.

- **Routing:** follow the workspace's routing rules if a workspace `CLAUDE.md`
  defines them (e.g. an inbox/landing directory). Otherwise use the project's
  `plans/` directory, creating it if needed.
- **Filename:** `plan-<slug>-YYYY-MM-DD.md`. State the plan's own *filename*
  (not its full path) in its header so the executor can name it unambiguously —
  plans routed to a landing directory (e.g. INBOX) get promoted elsewhere, and
  an embedded full path would go stale on the move.

### 4. Self-test before delivering

Re-read the plan as if you were a fresh session with no conversation history:

- No "as discussed above" / "the approach we agreed on" — inline it.
- All paths are absolute or repo-relative from a stated root.
- Every referenced artifact either exists (verified in step 2) or is created
  by a numbered work item.
- Each "Done means" criterion is checkable by a command or observation.
- Every step that writes state has a row in "Stop and resume states" for
  each point where it can stop, plus each of the four perturbation rows
  that applies to it.
- Done means names the test that stops at each row and lists the rows it
  covers, or says in one line why the plan needs none.

Fix what fails, then reply with the file path and a 2–3 sentence summary —
not the plan body.

## Template (mandatory sections)

```markdown
# Plan: <title>

> File: <this-plan-filename.md> · Date: <YYYY-MM-DD>

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

## Stop and resume states

One row per step × stop point. A step is a step of the delivered code
that writes state. Number the rows so tests and fixers can name them.

| # | Step | Stops at | On disk | Resume does |
|---|---|---|---|---|
| S1 | <step> | <after X, before Y> | <files, refs, commits, saved fields> | <what it keeps, what it redoes, what it refuses and the message it prints> |

Add these rows for every step they apply to:
- the user edited files between the stop and the resume;
- origin or the base branch moved;
- the same step stopped twice in a row;
- the process that stopped is still alive when resume starts.

If the delivered code writes no state, say so in one sentence and give
the rows for this plan's own work items as an executor runs them
(branch, commits, push, PR).

## Constraints / out of scope

- Hard rules the executor must not violate (repos not to touch, branches
  not to push, APIs not to call).
- Explicit exclusions: things a reasonable agent might assume are in
  scope but are not — name them.
- Stop-and-ask gates: "if X fails / is missing, stop and ask" for any
  step that depends on access, credentials, or decisions the executor
  may not have. Never let the agent improvise past a gate.
- Implementers and fixers add no resume behaviour the Stop and resume
  states table does not ask for: no extra refusal, fast-forward, retry,
  saved field or ref name. Saved state the table does not list yet is
  added only as the next bullet says.
- A change that saves new state (a field, a ref name, a state file, a
  lock, a ledger line) first adds a row for it to the table in this plan
  file, and a test case for that row.
- If a row's "Resume does" cannot be built as written, stop and ask.

## Done means

Numbered criteria. Every criterion is verifiable by a specific command
or observation — see quality bar below.

When the Stop and resume states table has rows for the delivered code,
one criterion names a test that, for each row, stops the step at that
point (or applies that row's perturbation), resumes, and compares the
result with a run that never stopped: same files, commits, refs and
saved state, except where the row's "Resume does" says otherwise. The
test names the rows it covers (S1, S2, …).

## Verification

How to prove each criterion: the commands to run, the outputs to expect,
and what the final report should contain (e.g. a pass/fail table with
evidence). If something can only be verified by the user (visual check,
production access), say so explicitly instead of claiming it.
```

Sections may be short, but none may be omitted. An empty
"Constraints / out of scope" is a statement ("no constraints beyond
workspace defaults"), not a missing section.
A plan whose delivered code writes no state still has "Stop and resume
states": one sentence saying so, and the rows for its own work items.

## Quality bar for "Done means"

Each criterion must be checkable without judgment calls:

| ❌ Vague | ✅ Verifiable |
|---|---|
| Auth works | `pytest tests/auth -x` passes and `curl -s localhost:8000/login` returns 200 |
| Docs updated | `README.md` has a "Configuration" section listing all 4 env vars from `.env.example` |
| Doesn't break existing flows | `make test` passes; `docker compose config` shows `front` outside the default profile |
| Report delivered | Report written to `<exact path>` ending with a pass/fail table and the user's merge commands |
| Resume works | `pytest tests/test_resume.py -v` shows one passing case per table row, S1–S12, each compared with a run that never stopped |

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
