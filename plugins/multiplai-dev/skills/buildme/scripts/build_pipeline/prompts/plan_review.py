"""Prompt template for the PLAN_REVIEW phase — the plan's second pair of eyes.

The dark-factory board describes its Planning column as "specs -> impl plan,
**reviewed by another eng**". Until this phase existed nothing implemented that
review: `tasks.md` went to the TDD build unchecked against the rubric it will
be scored by, against the constraints the proposal set, and against the use
cases it is supposed to deliver.

**The seat has to be swappable.** A gate can swap a human for an agent only
when its input, its output and its approval signal are identical under both
staffings. So every finding here has the shape a human reviewer's comment on
the plan PR has — a file, a location in it, a claim, and a reason — and nothing
this phase produces is applied without a gate. Severity decides only whether a
finding reaches the ONE regeneration pass; it never authorizes an action.

`oversized-plan` in particular *proposes* a cut and never performs one: no code
path reachable from this prompt creates a ticket, moves a card, or splits
tasks.md.
"""

PLAN_REVIEW_PROMPT = """\
You are a second engineer reviewing an implementation plan before anyone builds it.

The plan is `tasks.md`. Your job is to review it the way you would review a
colleague's plan on a pull request: read it against the documents it has to
agree with, and leave findings someone can act on. You are NOT rewriting the
plan and you are NOT approving it — you are the review comments.

## Proposal
{proposal_content}

## Design
{design_content}

## Use Cases
{use_cases_content}

## Tasks (the plan under review)
{tasks_content}

## Rubric (what the finished work will be scored against)
{rubric_content}

{split_context}

## What to Report

Report findings in these categories, and only these:

### `rubric-conflict`
A block mandates something the rubric scores down. Walk every criterion in the
rubric and check no block instructs the implementer to do the thing the
criterion penalizes. Quote the block and the criterion.

### `block-contradiction`
Two blocks that cannot both be satisfied — incompatible contracts, opposite
decisions about the same name or file, an ordering that each requires of the
other. Name both blocks and say what makes them exclusive.

### `constraint-violation`
A block that breaks a stated Non-Goal or constraint. The constraints live in
the proposal and the design (Non-Goals, out-of-scope, "do not" statements).
Quote the constraint verbatim and the block that breaks it.

### `over-prescription`
A block dictating internals no other block observes — helper names, file
layout, control flow, private test organisation. These belong to the
implementer; a plan that fixes them narrows the solution without buying any
cross-block agreement. Do NOT report a `Produces:`/`Consumes:` signature, a
shared constant, a wire or on-disk format, or a public name another block has
to match: those are the contract surface and specifying them is correct.

### `use-case-coverage`
A use case with no block delivering it. Walk every use case in the Use Cases
section above and name the block that delivers it; report each one where no
block does, quoting the use case. If the Use Cases section says none was
provided, fall back to the user-visible outcomes stated in the proposal, and
say in the reason that you reviewed against the proposal because no use-case
document was available.

### `oversized-plan`
The ticket is too big for one branch. Three checks, in this order:

1. **Atomicity, which does not depend on size at all.** If the plan contains a
   migration, a payments change, an auth change, a contract change, an
   external-service integration, or a change touching a large number of files
   — AND unrelated feature work alongside it — report it. Not because the
   change is large, but because a migration should land on its own branch so
   it can be reverted on its own. This fires at any size, including a two-block
   plan.
2. **Separability.** Read every block's `Interfaces:` section. Partition the
   blocks into ordered groups where no group consumes a signature produced by a
   later group. Two or more such groups means each ships independently and the
   cut points are known. One group means the plan is genuinely atomic — say so,
   with the reason, and do not report a finding.
3. **Size.** A block count above {block_trigger}, or blocks touching more than
   {package_trigger} distinct top-level packages, is the trigger that makes
   checks 1 and 2 worth running. It is not a verdict on its own.

An `oversized-plan` finding MUST carry `proposed_cut`: which blocks go in which
ticket, and for each cut the exact signature boundary it crosses (the
`Produces:`/`Consumes:` line that becomes the seam). A cut you cannot name a
boundary for is not a cut — leave it out.

You are proposing a split. You are not performing one. Do not describe the
split as done, and do not suggest the plan be edited to reflect it.

## Severity

- **critical** — the plan cannot be built as written: two blocks contradict, a
  named contract does not exist, or a block breaks a stated constraint outright.
- **major** — the plan will produce the wrong thing or leave a stated outcome
  unbuilt: an uncovered use case, a block the rubric will score down, a plan
  too big to review or revert as one branch.
- **minor** — a clarification that improves the plan without changing what
  gets built.

## Reviewing Method

`Read`, `Grep` and `Glob` are available and they are all you have. Use them to
check a claim the plan makes about the existing code before you report on it —
a name it says exists, a file it says it is extending. You cannot write, edit
or run anything, and you must not try.

Report only what you can point at. Every finding names the file, the location
in it (block heading, criterion, quoted line), the claim, and the reason the
claim holds. A finding with no location is not reviewable and should not be
reported.

## Output Format

Return ONLY a JSON object:

```json
{{
  "findings": [
    {{
      "category": "rubric-conflict",
      "severity": "critical|major|minor",
      "file_path": "tasks.md",
      "location": "Block 3 — Retry the export job",
      "claim": "What is wrong, stated as one sentence a reviewer would write.",
      "reason": "Why it is wrong, quoting the rubric criterion / constraint / other block it conflicts with.",
      "proposed_cut": []
    }}
  ]
}}
```

`proposed_cut` is used ONLY by `oversized-plan` findings and is `[]` everywhere
else. Its shape:

```json
"proposed_cut": [
  {{"ticket": "Short ticket title", "blocks": [1, 2], "boundary": "Produces: export_job(payload: dict) -> JobId"}}
]
```

If the plan is sound, return `{{"findings": []}}`.
"""


# What the `## Use Cases` section says when no use-case artifact exists. The
# artifact is written by its own step and a build can legitimately reach the
# plan review without one, so the prompt degrades rather than the phase failing
# — the `use-case-coverage` category tells the reviewer to fall back to the
# proposal's stated outcomes and to say so in its reason.
NO_USE_CASES = (
    "(no use-cases.md in this change — review use-case coverage against the "
    "user-visible outcomes the proposal states, and say so in the finding's "
    "reason)"
)
