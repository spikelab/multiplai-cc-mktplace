# plan — why this skill exists

`/plan` writes implementation plans that carry their own **completion
contract**. It exists because of a workflow observation: plans and "goal"
documents were being maintained as two separate artifacts, and the split
caused real failures.

## The problem it solves

A typical plan describes **how** — the steps, the files, the approach. It
usually does not define **done**. When such a plan is handed to an agent
("implement the plan", a goal runner, an autonomous session), the agent's
stopping condition is whatever it judges complete. That is the root of two
recurring failure modes:

- **Scope drift** — the agent wanders into work the author considered
  obviously out of scope, because nothing said so in a place that survives
  context compaction.
- **Premature "done!"** — the agent declares success on its own judgment,
  because no criterion was verifiable by a command or observation.

The workaround was to write a second document per piece of work: a *goal*
doc wrapping the plan with an Outcome, verifiable success criteria,
constraints, and stop-and-ask gates. That layer worked — goal-driven runs
recovered scope after interrupts and decomposed cleanly into parallel
worktrees — but it duplicated the plan. Two artifacts pointing at each
other drift: in practice, goal runs failed on simple filename mismatches
between the goal doc and the plan files it referenced (`PLAN-2-hub.md` vs
`PLAN-2-app.md`).

## The resolution

The value of the goal layer was never the extra document — it was three
properties the plan itself was missing:

1. **A verifiable "Done means"** — numbered criteria, each checkable by a
   specific command or observation ("`pytest tests/auth` passes", not
   "auth works").
2. **Explicit constraints / out of scope** — hard rules, exclusions, and
   "if X fails, stop and ask" gates, written down so they survive
   compaction and session restarts.
3. **Self-containedness** — a fresh session with zero conversation history
   can execute the plan from the file alone: no "as discussed", verified
   paths, stated filename.

This skill folds those properties into the plan itself, so **one artifact**
serves every consumer: a human reviewer, a fresh session told to
"implement the plan", a goal/autonomous runner (Outcome + Done means +
Constraints *are* the goal contract), or buildme (Context + Outcome seed
the proposal stage).

## When a separate goal doc is still the right call

When the goal genuinely **aggregates several plans** — multi-repo
initiatives, parallel plan tracks sharing a frozen contract, work spanning
multiple sessions with independent worktrees. There, the goal document
adds real information (cross-plan sequencing, shared success criteria)
rather than restating one plan. For a single plan, it's ceremony; skip it.

## Usage

Trigger with `/plan` or naturally ("write a plan for…", "make an
implementation plan…"). The skill will push back if the request is
underspecified, recon the affected repos before writing, emit the plan to
a file (workspace routing rules decide where), and self-test it for
fresh-session executability. See [SKILL.md](SKILL.md) for the template and
the quality bar.
