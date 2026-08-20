"""Prompt template for adversarial design audit.

Runs after all artifacts are generated to catch gaps before TDD.

Its findings are not report-only: critical and major gaps drive exactly one
regeneration pass of design.md and tasks.md
(``spec_generator.run_design_audit_stage``). That is why the checklist covers
plan *quality* — over-engineering, granularity, testability, edge cases — and
why the severity scale below is calibrated: severity decides what gets rewritten.
"""

DESIGN_AUDIT_PROMPT = """\
You are an adversarial reviewer auditing the generated OpenSpec artifacts for internal consistency, completeness, and plan quality.

## Proposal
{proposal_content}

## Specs
{specs_content}

## Design
{design_content}

## Tasks
{tasks_content}

## Change Type
{change_type}

## Audit Checklist

Cross-reference these artifacts and report ANY gaps:

### Spec-Task Alignment
- Every spec scenario has a task block that satisfies it
- Every task block references at least one spec
- No orphan tasks (tasks not linked to any spec)
- No orphan specs (spec scenarios not covered by any task)

### Design Coherence
- Design decisions are consistent with spec requirements
- Integration contracts in design match the module boundaries implied by tasks
- No spec requirement contradicts a design decision

### Type-Specific Checks ({change_type})
- migration: rollback plan exists, data integrity scenarios covered
- new-feature: entry point wiring covered inside the slices that need it (if app), error scenarios in specs
- refactor: behavioral equivalence scenarios, no new functionality sneaked in
- infra: failure mode scenarios, monitoring/alerting considered

### Completeness
- No vague or placeholder text in specs (e.g., "TBD", "TODO")
- All capability names from proposal have corresponding spec files
- Task block count is reasonable (2-8 blocks typical)

### Plan Quality — Right-Sizing (category "over-engineering")
For every abstraction the design introduces — an interface, a base class, a
plugin/registry/factory, a configuration knob, a new layer, a generalized
parameter — name the requirement or spec scenario that needs it. Report each
one where no requirement needs it, quoting the abstraction and stating what
the simpler shape would be (a direct call, a literal, one concrete
implementation). The same applies to scope the proposal did not ask for:
report design decisions or task blocks that build capability nothing in the
specs requires.

### Plan Quality — Task Granularity (category "granularity")
Report blocks that are mis-sized for review and verification:
- a block whose deliverable spans several unrelated behaviors (say which
  behaviors, and where the split goes)
- a block so small it cannot be verified on its own (say which neighbor it
  belongs with)
- a block whose checkbox items do not add up to the behavior its title claims

### Plan Quality — Testability (category "testability")
For every design decision and every task block, name the observable a test
could assert on — a return value, a written file, a logged line, an emitted
event, an exit code. Report each decision or block where no such observable
exists, and say what to expose (a return value instead of an internal
mutation, a seam instead of a hidden global) so it becomes assertable.

### Plan Quality — Edge Cases (category "edge-case")
Walk the inputs and the failure surface the design implies and report the ones
no spec scenario and no task block covers. At minimum consider: empty or
absent input, input at the size/format boundary, each external dependency
failing or timing out, partial completion followed by a retry or resume,
concurrent or repeated invocation, and the permission/credential being missing.
Report each uncovered case with the scenario that should exist.

## Severity

Severity decides what gets rewritten, so calibrate it:

- **critical** — the change cannot be built correctly from these documents as
  written: a required behavior has no home, two documents contradict, or a
  named contract does not exist.
- **major** — the documents will produce the wrong shape or leave a real
  behavior unbuilt: an unneeded abstraction that will be built, an uncovered
  failure mode, a block that cannot be verified, a spec scenario with no block.
- **minor** — a clarification that improves the documents without changing
  what gets built.

## Output Format
Return a JSON array of gap objects:

```json
[
  {{
    "category": "spec-task-alignment",
    "severity": "critical|major|minor",
    "description": "Specific description of the gap",
    "suggestion": "How to fix it"
  }}
]
```

If no gaps found, return an empty array: `[]`

Report what you can point at in these documents — quote the line, the block, or
the scenario each finding comes from, and make the suggestion concrete enough
to apply.
"""


TASKS_AUDIT_PROMPT = """\
You are an adversarial reviewer auditing a generated task breakdown for horizontal
(layer-by-layer) decomposition. The required shape is vertical slices: each block is
one thin end-to-end behavior, exercisable via a test or command the moment the block
completes, cutting through all the layers that behavior needs.

## Design
{design_content}

## Specs
{specs_content}

## Tasks
{tasks_content}

## What to Flag

- Layer-per-block decomposition: blocks scoped by architectural layer (e.g.
  "database schema", "data models", "API endpoints", "services", "frontend UI")
  rather than by a user-visible or test-visible behavior
- A final "wiring", "integration", or "glue" block — wiring must happen inside
  each slice, not be deferred to the end
- Blocks that complete without anything runnable or testable end-to-end
- A block whose deliverable can only be exercised after a LATER block lands

## Spec-Coverage Traceability

Walk every WHEN/THEN scenario in the specs and name the block that implements
it. Report each scenario with no implementing block as a finding
(category "spec-coverage") listing the scenario verbatim.

## Cross-Block Signature Consistency

For every `Consumes:` line in a block's Interfaces, find the earlier block
whose `Produces:` line it names. Report (category "interface-mismatch"):
- a Consumes with no matching earlier Produces
- a Consumes whose signature differs from the Produces it references
- two blocks producing the same name with different signatures

## Placeholders

Flag deferred specification only on the **contract surface** — the parts of a
block another block or a test has to agree with (category "placeholder"):

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

## What NOT to Flag

- Dependency ordering between slices — a DAG of slices is fine; layering is the
  anti-pattern, not ordering
- A first walking-skeleton slice (one trivial behavior through all layers) — that
  IS a vertical slice
- Setup or scaffolding checkbox items *inside* a behavior-scoped block

## Output Format
Return a JSON array of finding objects:

```json
[
  {{
    "category": "horizontal-decomposition",
    "severity": "critical|major|minor",
    "description": "Which blocks are layered and why that shape is horizontal",
    "suggestion": "How to re-slice them vertically"
  }}
]
```

If the breakdown is properly sliced into vertical slices, return an empty array: `[]`

Flag shape problems only — not naming style, granularity taste, or block count.
"""
