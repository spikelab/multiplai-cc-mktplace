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
finding reaches the rewrite; it never authorizes an action.

This is buildme's one planning check: it replaced the design audit, the tasks
audit and the plan review, which each read the same documents for overlapping
problems. Every checklist item appears here once.

`oversized-plan` in particular *proposes* a cut and never performs one: no code
path reachable from this prompt creates a ticket, moves a card, or splits
tasks.md.
"""

# The most findings one planning check returns. The prompt asks for it
# (`FINDINGS_RULE`), and `plan_review_steps.run_plan_review` keeps the most
# severe this many.
MAX_PLAN_FINDINGS = 10

FINDINGS_RULE = f"""\
## Which Findings to Report

- Report the findings that would make the build wrong or leave a stated
  behaviour unbuilt, most important first, at most {MAX_PLAN_FINDINGS}.
- Report an edge case only for an input or failure that a spec scenario or the
  plan names.
- Leave out anything that is minor unless no critical or major finding exists."""

PLAN_CHECK_PROMPT = """\
You are a second engineer reviewing an implementation plan before anyone builds it.

The plan is `design.md` and `tasks.md`. Your job is to review it the way you
would review a colleague's plan on a pull request: read it against the
documents it has to agree with and against the code it changes, and leave
findings someone can act on. You are NOT rewriting the plan and you are NOT
approving it — you are the review comments.

## Proposal
{proposal_content}

{plan_section}

## Specs
{specs_content}

## Design
{design_content}

## Use Cases
{use_cases_content}

## Tasks (the plan under review)
{tasks_content}

## Rubric (what the finished work will be scored against)
{rubric_content}

## Change Type
{change_type}

{split_context}

## What to Report

Report findings in these categories, and only these. The shape the build
needs is vertical slices: each block is one thin end-to-end behaviour,
exercisable by a test or a command the moment the block completes.

### `coverage`
A spec scenario, or a use case, with no block that delivers it. Walk every
WHEN/THEN scenario in the Specs section and every use case in the Use Cases
section, and name the block that delivers each; report each one no block
delivers, quoting it. If the Use Cases section says none was provided, check
the user-visible outcomes stated in the proposal instead, and say so in the
reason.

### `no-new-behaviour`
A block that adds nothing the code lacks before it is built: it only confirms,
verifies or pins what an earlier block delivers, adds tests and no code, or
says an earlier block satisfies it. The TDD build runs each block's tests
before implementing it and fails the build when they already pass. Name the
earlier block its criteria belong in. This is always **critical**.

### `interface-mismatch`
A contract that does not line up. For every `Consumes:` line in a block's
Interfaces, find the earlier block whose `Produces:` line it names, and report:
- a Consumes with no matching earlier Produces
- a Consumes whose signature differs from the Produces it references
- two blocks producing the same name with different signatures
- a contract in the design that no block's Interfaces match

### `horizontal-decomposition`
Blocks sliced by layer instead of by behaviour:
- blocks scoped by architectural layer (e.g. "database schema", "data models",
  "API endpoints", "services", "frontend UI") rather than by a user-visible or
  test-visible behaviour
- a final "wiring", "integration", or "glue" block — wiring must happen inside
  each slice, not be deferred to the end
- a block that completes without anything runnable or testable end-to-end
- a block whose deliverable can only be exercised after a LATER block lands

What NOT to report here:

- Dependency ordering between slices — a DAG of slices is fine; layering is the
  anti-pattern, not ordering
- A first walking-skeleton slice (one trivial behavior through all layers) — that
  IS a vertical slice
- Setup or scaffolding checkbox items *inside* a behavior-scoped block

### `placeholder`
Deferred specification on the **contract surface** — the parts of a block
another block or a test has to agree with:
- a `Consumes:` or `Produces:` signature left unnamed or unspecified, or given
  as prose where a signature belongs
- a shared constant, wire format, on-disk format, or public name that another
  block reads, left unstated
- "TBD", "TODO", or "similar to block N" on anything that crosses a block
  boundary
- an instruction that requires the implementer to guess a name, signature, or
  literal value that a later block has to match exactly

**What NOT to Flag — block-internal choices.** These belong to the implementer,
and a block that leaves them open is correctly scoped, not under-specified:

- helper or private function names, and how the work is factored inside the
  block
- which files the block creates and how its code is laid out across them
- control flow — loops, early returns, error handling that stays inside the
  block
- open guidance such as "handle the empty case sensibly" or "extract a helper if
  it gets long"
- test names, fixture names, or how the block organises its own tests

If the missing detail is invisible from outside the block, it is not a
placeholder.

### `design-coherence`
A claim the design or a block makes about the existing code that the
repository contradicts: a name, file, endpoint, table or behaviour it says
exists and does not, or exists in another shape. Check each such claim with
`Grep` or `Read` before you report it, and quote what the repository holds.
Also report a design decision that contradicts a spec requirement.

### `over-engineering`
An abstraction the build will produce — an interface, a base class, a
plugin/registry/factory, a configuration knob, a new layer, a generalized
parameter — that no requirement or spec scenario needs. Quote it and state
the simpler shape (a direct call, a literal, one concrete implementation).

### `testability`
A block with no observable a test could assert on at all — no return value,
written file, logged line, emitted event or exit code. Say what to expose so it
becomes assertable.

### `edge-case`
An input or failure that a spec scenario or the plan names and that no block
covers, with the scenario that should exist. Type-specific checks for this
change ({change_type}): a migration needs a rollback and data-integrity
scenarios; a refactor needs behavioural-equivalence scenarios and no new
functionality; infra needs failure-mode scenarios.

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

## Fixes That Need a Requirements Change

The proposal and the specs are fixed inputs here: the rewrite this check drives
changes only design.md and tasks.md. When a finding can only be fixed by
changing a requirement (it is infeasible, or contradicts another requirement or
the codebase, or a needed behaviour is missing from the specs), set
`needs_requirements_change` to true and name the requirement in `requirement`
as `{spec_ref}: <requirement name>`; the build then stops and sends it to the
requirements' author. Every other finding has false.

## Severity

- **critical** — the plan cannot be built as written: two blocks contradict, a
  named contract does not exist, a block breaks a stated constraint outright,
  or a block adds no behaviour (its tests pass before it is built).
- **major** — the plan will produce the wrong thing or leave a stated outcome
  unbuilt: an uncovered spec scenario or use case, a block the rubric will
  score down, a layered breakdown, an unneeded abstraction that will be built,
  a block that cannot be verified, a plan too big to review or revert as one
  branch.
- **minor** — a clarification that improves the plan without changing what
  gets built.

{findings_rule}

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
      "category": "coverage",
      "severity": "critical|major|minor",
      "file_path": "tasks.md",
      "location": "Block 3 — Retry the export job",
      "claim": "What is wrong, stated as one sentence a reviewer would write.",
      "reason": "Why it is wrong, quoting the scenario / rubric criterion / constraint / code it conflicts with.",
      "suggestion": "How to fix it, concrete enough to apply.",
      "needs_requirements_change": false,
      "requirement": "",
      "proposed_cut": []
    }}
  ]
}}
```

`file_path` is `design.md` or `tasks.md`: the document the fix belongs in.
`proposed_cut` is used ONLY by `oversized-plan` findings and is `[]` everywhere
else. Its shape:

```json
"proposed_cut": [
  {{"ticket": "Short ticket title", "blocks": [1, 2], "boundary": "Produces: export_job(payload: dict) -> JobId"}}
]
```

If the plan is sound, return `{{"findings": []}}`.
"""
# `FINDINGS_RULE` has no braces, so the prompt still formats with `.format`.
PLAN_CHECK_PROMPT = PLAN_CHECK_PROMPT.replace("{findings_rule}", FINDINGS_RULE)


# What the `## Use Cases` section says when no use-case artifact exists. The
# artifact is written by its own step and a build can legitimately reach the
# plan review without one, so the prompt degrades rather than the phase failing
# — the `coverage` category tells the reviewer to fall back to the
# proposal's stated outcomes and to say so in its reason.
NO_USE_CASES = (
    "(no use-cases.md in this change — review coverage against the "
    "user-visible outcomes the proposal states, and say so in the finding's "
    "reason)"
)


# The plan, in an audit or review prompt. A component's proposal and use cases
# are its change's, which covers every component, so a prompt that judged a
# component against them alone reported its siblings' capabilities as missing.
# The plan is what narrows them to the one being built; for a component the
# prompt says so (`plan_section`).
PLAN_SECTION = """\
## Plan — what this builds, what is out of scope, and what done means
{plan_content}"""

COMPONENT_SCOPE = """\
## Scope — one component of the change
The proposal and the use cases describe the whole change. This build is one
component of it: the plan below says what this component builds and what it
leaves to other components. Judge these documents against the plan. A
capability, goal or use case the plan does not give this component is built by
another component, and is not a gap here."""


def plan_section(plan_content: str, *, component: bool) -> str:
    """The `## Plan` section of an audit or review prompt, with the
    `COMPONENT_SCOPE` note above it when *component*."""
    section = PLAN_SECTION.format(plan_content=plan_content)
    return f"{COMPONENT_SCOPE}\n\n{section}" if component else section


# Appended to the plan check when it rechecks a rewrite (`prior_findings_section`), and only then: a first audit's
# prompt is unchanged. The status wording is the per-block reviewer's
# (`prompts/review.py`), adapted to plan documents.
PRIOR_FINDINGS_SECTION = """

## Findings the rewrite was asked to fix

These documents were rewritten to fix the findings below.

{prior_findings}

For each finding listed here, give its status with the lines of the documents
that show it. The status is `resolved`, `still_present`, `partially_resolved`
or `cannot_verify`, and it goes in `prior_assessments` with the finding's
`finding_id`. Report every other problem in `findings`, as above.

Return one JSON object holding both lists. Where the format above asks for a
bare array, that array becomes the value of `findings`:

```json
{{
  "findings": [],
  "prior_assessments": [
    {{
      "finding_id": "PR-F1",
      "status": "resolved | still_present | partially_resolved | cannot_verify",
      "evidence": "The lines of the documents that show the status"
    }}
  ]
}}
```
"""


def prior_findings_section(prior: list[dict] | None) -> str:
    """The recheck section for these listed findings; empty for none."""
    if not prior:
        return ""
    lines = "\n".join(
        f"- {f.get('finding_id', '?')} [{f.get('severity', '?')}] ({f.get('category', '?')}) {f.get('text', '')}"
        for f in prior
    )
    return PRIOR_FINDINGS_SECTION.format(prior_findings=lines)


CHANGED_LINES_SECTION = """

## What the rewrite changed

The rewrite edited the documents in place. This is its diff:

```diff
{diff}
```

Give a status for every finding listed above. Report a new finding only when
it is in these changed lines or caused by them. A new critical finding
anywhere in the documents is still reported.
"""


def changed_lines_section(diff: str) -> str:
    """The recheck section for a rewrite's diff; empty for none."""
    return CHANGED_LINES_SECTION.format(diff=diff.rstrip()) if diff.strip() else ""
