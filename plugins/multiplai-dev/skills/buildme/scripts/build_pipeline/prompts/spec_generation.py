"""Prompt templates for buildme's planning artifacts: design and tasks.

Each template uses {placeholders} for context injection by the spec generator.
The proposal, use cases and requirements they read are pm-spec's; buildme
never writes them. A problem the design finds in them goes back to pm-spec as
a `SPEC_ISSUE:` line (`SPEC_ISSUE_RULE`).
"""

# The design step's one way to say a requirement cannot be built as written.
# `spec_generator.design_spec_issues` reads these lines; a design that has any
# is not published, and the issues go to spec-issues.md for `pm-spec revise`.
SPEC_ISSUE_PREFIX = "SPEC_ISSUE:"

DESIGN_PROMPT = """\
You are generating a design document for an spec-driven change.

## Project Context
{project_context}

## Proposal
{proposal_content}

## Plan — what this builds, what is out of scope, and what done means
{plan_content}

## Specs
{specs_content}

## Existing Codebase Analysis
{codebase_analysis}

## Research Findings
{research}

## Reference Docs — the conventions this project builds to
{reference_docs}

## Findings To Address
{audit_findings}

## Instructions
{instruction}

## Output Format
Generate the design as markdown following this template structure:

{template}

Rules:
- Decisions section: state the decision, list alternatives considered, explain why chosen
- Reference specific spec scenarios when explaining how they'll be implemented
- Integration contracts: define interfaces between components
- Be explicit about what's new vs what's modified
- The proposal, use cases and specs are fixed inputs: you cannot change them,
  and the design must not tell their author what to reword. When a requirement
  is infeasible, or contradicts another requirement or the codebase, end your
  output with one line per problem, in exactly this form and nothing after:
  `SPEC_ISSUE: {spec_ref}: <requirement name>: <what is
  infeasible or contradictory, and the change to the requirement that would
  fix it>`. Those lines go back to the requirements' author; write them only
  for a problem a design cannot solve
- The Reference Docs above are this project's prescribed practices. Where one
  states a convention that applies (package manager, project layout, error
  handling, test framework, naming), the design follows it and names the doc in
  the Decisions section. Where a decision departs from a reference doc, say
  which doc and why — an unexplained departure is a defect
- The Existing Codebase Analysis is the repo as it actually is. Extend the
  modules, patterns and integration points it names rather than introducing a
  parallel structure beside them
- REQUIRED section `## Global Constraints`: the project-wide rules every
  implementation block must honor — version floors, naming conventions,
  library choices, exact string literals/IDs/paths. One rule per line, each
  stating its verbatim value (write the exact literal; a paraphrase forces
  implementers to guess). When the change genuinely has none, write "None."
  under the heading.

Output ONLY the markdown content. No commentary.
"""

TASKS_PROMPT = """\
You are generating a task breakdown for an spec-driven change.

## Project Context
{project_context}

## Proposal
{proposal_content}

## Plan — what this builds, what is out of scope, and what done means
{plan_content}

## Use cases
{use_cases_content}

## Specs
{specs_content}

## Design
{design_content}

## Unknowns — dependencies new to this project, and their edge cases
{unknowns_content}

## Reference Docs — the conventions this project builds to
{reference_docs}

## Task Granularity
{granularity}

## Shape Audit Findings
{audit_findings}

## Instructions
{instruction}

## Output Format
Generate tasks as markdown following this template structure:

{template}

Rules:
- Each block MUST be a vertical slice: one thin end-to-end behavior that can be
  exercised via a test or command the moment the block completes, cutting through
  ALL the layers that behavior needs (schema, logic, API, UI, wiring — whatever it touches)
- Layer-per-block decomposition is FORBIDDEN: never emit a schema block, then an
  API block, then a UI block. Blocks scoped by architectural layer leave nothing
  runnable until the very end. Slice by behavior, not by layer.
- Wiring happens inside each slice; a final integration block is a smell. Every
  block leaves the system integrated and demonstrable.
- Every block MUST add behaviour the code does not have before the block is
  built: its tests must fail until its own code is written. The build's TDD
  RED gate runs each block's tests before implementing it, and a block whose
  tests already pass fails the whole build.
- Put criteria that confirm, verify, pin or re-test what an earlier block
  delivers (edge cases, conventions, "no change to X"), and their tests, in
  the block that builds that behaviour.
- Block descriptions: 2-4 sentences covering what it delivers, key behaviors,
  acceptance criteria, and how to exercise the slice end-to-end once it's done
- "Satisfies:" line MUST reference specific spec files or scenarios
- Each block MUST carry an `Interfaces:` section (directly above its Satisfies
  line) declaring its cross-block contract:
  - `- Produces: <exact signature>` — one line per function/class/endpoint this
    block creates that later blocks call, with the exact signature (names,
    parameters, types, return)
  - `- Consumes: Block <N>: <exact signature>` — one line per earlier-block
    interface this block calls, naming the source block and repeating the
    signature exactly as that block's Produces line states it
  A block with no cross-block interfaces writes `- Produces: (none)`.
- Every block is fully specified in place: exact names, signatures, and
  literal values. Where "TBD", "TODO", "add appropriate error handling", or
  "similar to block N" would appear, write the concrete content instead —
  repeat the signature or code verbatim rather than pointing at another block.
- Order blocks by dependency — a DAG of slices is fine (a later slice may build on
  an earlier one); layering is the anti-pattern, not ordering. The first slice can
  be a walking skeleton: one trivial behavior through all layers.
- If {granularity} is "blocks": use coarse blocks (## 1. Block Name), 2-4 sentences each
- If {granularity} is "checkboxes": add checkbox items under each block (- [ ] task)
- If Shape Audit Findings are present, this is a regeneration pass: fix every
  finding by re-slicing the flagged blocks vertically
- Where the Reference Docs prescribe concrete tooling (test runner, package
  manager, directory layout), tasks name that exact tooling and command rather
  than a generic equivalent
- Every edge case listed under Unknowns belongs to the block that first touches
  that dependency: name it in that block's acceptance criteria so the test
  writer turns it into a test (e.g. "silence input produces an empty
  transcript, not hallucinated caption text")

Output ONLY the markdown content. No commentary.
"""


# PLAN_EDIT_PROMPT's `{plan_rule}` for a component that reads its change's
# approved plan (`change_manager.reads_change_plan`); "" for every other build.
CHANGE_PLAN_EDIT_RULE = """\
- implementation-plan.md is the change's plan, which an engineer approved
  for every component. Edit only tasks.md. A finding that needs the plan
  changed gets no edit; put its id in `not_fixed`.
"""

PLAN_EDIT_PROMPT = """\
You are fixing findings in a change's plan: its implementation-plan.md and tasks.md. Fix
them by editing the documents in place. Do not rewrite them.

## Findings to fix
{findings}

## implementation-plan.md
{design_content}

## tasks.md
{tasks_content}

## Proposal
{proposal_content}

## Plan — what this builds, what is out of scope, and what done means
{plan_section}

## Use cases
{use_cases_content}

## Specs
{specs_content}

## Instructions
- Return the smallest set of exact replacements that fixes the findings.
  Change nothing a finding does not need changed.
- Each edit names its file (`implementation-plan.md` or `tasks.md`), an `old_str` copied
  exactly from that file, and the `new_str` that replaces it. Copy enough of
  the surrounding text that `old_str` occurs exactly once in the file.
- Edits apply in order. A later edit's `old_str` is matched against the file
  as the earlier edits leave it.
- Before you write about existing code (a name, an endpoint, a table), check
  it with Read, Grep or Glob. You cannot change any file yourself.
- Keep tasks.md's block format: each block keeps its `Interfaces:` and
  `Satisfies:` lines, and every block adds behaviour its tests can show.
- When you cannot fix a finding by editing these documents, make no edit for
  it and put its finding id in `not_fixed`.
{plan_rule}
Return one JSON object:

```json
{{
  "edits": [
    {{"file": "tasks.md", "old_str": "exact text", "new_str": "replacement"}}
  ],
  "not_fixed": []
}}
```
"""
