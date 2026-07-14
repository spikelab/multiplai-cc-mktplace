"""Prompt templates for OpenSpec artifact generation.

Each template uses {placeholders} for context injection by the spec generator.
"""

PROPOSAL_PROMPT = """\
You are generating a proposal document for an spec-driven change.

## Project Context
{project_context}

## Interview Summary
{interview_summary}

## Research Findings
{research}

## Instructions
{instruction}

## Output Format
Generate the proposal as markdown following this template structure:

{template}

Rules:
- Keep concise (1-2 pages)
- Capability names MUST be kebab-case (e.g., `user-auth`, `data-export`)
- Each capability listed under "New Capabilities" will become a requirements/<name>.md file
- Focus on WHY, not HOW — the design doc covers implementation
- Be specific about what changes — vague proposals create vague specs

Output ONLY the markdown content. No commentary.
"""

SPEC_PROMPT = """\
You are generating a requirements file for a single capability in a spec-driven change.

## Project Context
{project_context}

## Proposal
{proposal_content}

## Capability to Specify
{capability_name}

## Instructions
{instruction}

## Output Format
Generate the spec as markdown following this template structure:

{template}

Rules:
- Every requirement MUST have at least one WHEN/THEN scenario
- Scenarios must be testable — unambiguous pass/fail
- Use ADDED Requirements section header for new specs
- Requirements should be atomic — one behavior per requirement
- Include edge cases and error scenarios, not just happy path
- Reference the proposal for context but add detail it lacks

Output ONLY the markdown content. No commentary.
"""

DESIGN_PROMPT = """\
You are generating a design document for an spec-driven change.

## Project Context
{project_context}

## Proposal
{proposal_content}

## Specs
{specs_content}

## Existing Codebase Analysis
{codebase_analysis}

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
- Flag any spec requirements that seem infeasible or contradictory

Output ONLY the markdown content. No commentary.
"""

TASKS_PROMPT = """\
You are generating a task breakdown for an spec-driven change.

## Project Context
{project_context}

## Proposal
{proposal_content}

## Specs
{specs_content}

## Design
{design_content}

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
- Block descriptions: 2-4 sentences covering what it delivers, key behaviors,
  acceptance criteria, and how to exercise the slice end-to-end once it's done
- "Satisfies:" line MUST reference specific spec files or scenarios
- Order blocks by dependency — a DAG of slices is fine (a later slice may build on
  an earlier one); layering is the anti-pattern, not ordering. The first slice can
  be a walking skeleton: one trivial behavior through all layers.
- If {granularity} is "blocks": use coarse blocks (## 1. Block Name), 2-4 sentences each
- If {granularity} is "checkboxes": add checkbox items under each block (- [ ] task)
- If Shape Audit Findings are present, this is a regeneration pass: fix every
  finding by re-slicing the flagged blocks vertically

Output ONLY the markdown content. No commentary.
"""
