"""Prompts, templates and instructions for the proposal, the use cases and each component's specs.

Copied from buildme's `prompts/spec_generation.py`, `change_manager.py`
(`TEMPLATES`, `INSTRUCTIONS`) and `spec_generator.USE_CASES_PROMPT`, then
adapted: the proposal opens with a `# <title>` line (the epic's title in
`tickets.json`), names no capability (only a component's `specs.md` does),
and, when a PRD was given, closes with a `## Sources` section naming the PRD
sections it comes from. A component's `specs.md` holds one
`# Capability: <name>` section per capability, each in the delta format.
"""

PROPOSAL_TEMPLATE = """\
# <!-- a short title for the change -->

## Why

<!-- The problem, and why now. -->

## What Changes

<!-- What will change. Then, explicitly:
     Goals — what this change aims to achieve.
     Non-Goals — what is deliberately out of scope. -->

## Impact

<!-- Affected code, APIs, dependencies, systems. Facts about the codebase, not
     restatements of What Changes. Wrap every dependency name in backticks so
     the dependency scan can see it. -->
"""

SOURCES_TEMPLATE = """
## Sources

<!-- One line per PRD section this change comes from:
     - PRD "<the exact text of the PRD heading>" -->
"""

USE_CASES_TEMPLATE = """\
# Use cases — who this change is for

## Personas

<!-- One block per persona. Real people in a real situation, not job titles. -->

### <persona-name>
- **Who they are:** <role, and the situation they are in>
- **The job they are doing:** <the job they are trying to get done>
- **The constraint they are under:** <time, access, skill, budget, policy>

## Use cases

<!-- One per line, in this exact form:
     <persona> wants <goal> so that <observable outcome>.
     The outcome must be observable from OUTSIDE the system — something the
     persona can see, read, or measure without reading the code. -->
- <persona> wants <goal> so that <observable outcome>
"""

SPECS_TEMPLATE = """\
# Capability: <capability-name>

## ADDED Requirements

### Requirement: <!-- requirement name -->
<!-- requirement text -->

#### Scenario: <!-- scenario name -->
- **WHEN** <!-- condition -->
- **THEN** <!-- expected outcome -->
"""

PROPOSAL_INSTRUCTION = (
    "Create the proposal document that establishes WHY this change is needed. "
    "Start with a `# <title>` line. Sections: Why; What Changes (which states Goals — what "
    "this change aims to achieve — and Non-Goals — what is deliberately out of scope — "
    "explicitly); Impact (facts about the codebase — "
    "affected code, APIs, dependencies, systems — not a restatement of What "
    "Changes, with every dependency name in backticks). "
    "Personas and use cases live in use-cases.md, and capabilities and requirements in "
    "each component's specs.md, not here. "
    "Keep concise (1-2 pages). Focus on the 'why' not the 'how'."
)

USE_CASES_INSTRUCTION = (
    "Create the use cases document that establishes WHO this change is for. "
    "Personas: one block per persona, naming who they are, the job they are "
    "doing, and the constraint they are under — real people in a real "
    "situation, not job titles. "
    "Use cases: one per line, in the form "
    "'<persona> wants <goal> so that <observable outcome>'. "
    "Every use case names a persona defined above it, and every outcome is "
    "observable from OUTSIDE the system — something that persona can see, "
    "read, or measure without reading the code. An outcome phrased as an "
    "internal implementation detail is not a use case; rewrite it as what "
    "the persona observes."
)

SPECS_INSTRUCTION = (
    "Create the specs.md of ONE component: what it must do, as requirements grouped by "
    "capability. Start each capability with a `# Capability: <kebab-case-name>` line, then "
    "its `## ADDED Requirements`. `## MODIFIED` and `## REMOVED Requirements` are allowed "
    "only in a capability that has a file in specs/registry/ (see below). "
    "Use WHEN/THEN format for scenarios. Each requirement MUST have at least one scenario. "
    "Scenarios must be testable — you know when they pass. Specify only this component's "
    "work, as its plan describes it; the other components are specified separately."
)

PRD_RULE = """\
The PRD below is the primary source. In `## Sources`, write one line per PRD
section this change comes from, `- PRD "<the exact text of the PRD heading>"`.
Name only headings that are in the PRD."""

PROPOSAL_PROMPT = """\
You are generating a proposal document for a spec-driven change.

{notice}

## Project Context
{project_context}

## Interview
{interview}
{prd_section}
## Instructions
{instruction}
{source_rules}
## Output Format
Generate the proposal as markdown following this template structure:

{template}

Rules:
- Keep concise (1-2 pages)
- Name no capabilities and write no requirements: those come later, per component
- Focus on WHY, not HOW — the build that follows decides the design
- Be specific about what changes — vague proposals create vague specs

Output ONLY the markdown content. No commentary.
"""

PROPOSAL_RETRY = """

## Your previous proposal was rejected
Fix every one of these problems and write the whole proposal again:
{errors}
"""

USE_CASES_PROMPT = """\
You are generating the use cases document for a spec-driven change.

{notice}

## Project Context
{project_context}

## Interview
{interview}

## Proposal
{proposal_content}

## Instructions
{instruction}

## Output Format
Generate the document as markdown following this template structure:

{template}

Rules:
- Every persona block names who they are, the job they are doing, and the
  constraint they are under
- Every use case reads exactly: <persona> wants <goal> so that <observable outcome>
- Every use case names a persona defined in the Personas section
- Every outcome is observable from OUTSIDE the system — something the persona
  can see, read, or measure without reading the code. "The cache is warmed" is
  not an outcome; "the page loads before they finish reading the heading" is
- No use case invents a persona, a goal, or an outcome the proposal and the
  interview do not support

Output ONLY the markdown content. No commentary.
"""

SPECS_PROMPT = """\
You are writing the specs of one component of a spec-driven change.

{notice}

## Project Context
{project_context}

## Proposal (the whole change)
{proposal_content}

## Use cases (the whole change)
{use_cases_content}

## The component: {component}
{plan_content}

## Other components of this change (specified separately)
{other_components}

## Prototype
{prototype_files}

## Instructions
{instruction}

## Output Format
Generate specs.md as markdown following this template structure, with one
`# Capability:` section per capability:

{template}

Rules:
- Capability names MUST be kebab-case (e.g., `user-auth`, `data-export`)
- Every requirement MUST have at least one WHEN/THEN scenario
- Scenarios must be testable — unambiguous pass/fail
- Requirements should be atomic — one behavior per requirement
- Include edge cases and error scenarios, not just happy path
- The prototype shows what things look like; the behaviour is yours to state here

Output ONLY the markdown content. No commentary.
"""

REGISTRY_HEADING = "## Current behaviour (specs/registry)"
REGISTRY_REQUIREMENTS_RULE = (
    "These files are what the code does today. A requirement this change alters goes under "
    "`## MODIFIED Requirements` with the same `### Requirement:` name as here; a requirement it "
    "removes goes under `## REMOVED Requirements` with that name; only new requirements go under "
    "`## ADDED Requirements`."
)
REGISTRY_ONLY_RULE = (
    "Write `## MODIFIED Requirements` and `## REMOVED Requirements` only in a capability "
    "listed here: {names}. Every other capability is new: all its requirements go under "
    "`## ADDED Requirements`."
)
REGISTRY_EMPTY_RULE = (
    "specs/registry has no capability files, so no current behaviour is recorded for this "
    "change to modify or remove. Write only `## ADDED Requirements`, in every capability."
)
REGISTRY_NAMES_RULE = (
    "Each capability below is what the code does today, one file each in specs/registry/. A "
    "capability whose behaviour exists today takes exactly the name listed here, so its "
    "requirements are written against that file; a new capability takes a name not listed here."
)

REWRITE_SPECS_PROMPT = """\
You are revising one specs file of a spec-driven change to fix the findings
listed below. Keep every requirement the findings do not touch
as it is, word for word.

{notice}

## Project Context
{project_context}

## Proposal
{proposal_content}

## Use cases
{use_cases_content}

## Every specs file of the change
{specs_content}

## The file to revise: {unit}
{current_content}

## Findings to fix
{findings}

## Instructions
Fix each finding in this file where the fix belongs here. A behaviour two
places both require belongs to the component whose plan names it: keep it
there and take it out of the other. Two requirements that contradict
each other are resolved by stating the one rule the proposal and the use cases
support. A gap is closed with a requirement and a WHEN/THEN scenario. When none
of the findings needs a change in this file, return it unchanged.

## Output Format
The same format as the current file{format_note}: `## ADDED Requirements` (or
MODIFIED / REMOVED), `### Requirement: <name>`, and under each requirement at
least one `#### Scenario: <name>` with a `- **WHEN** ...` and a `- **THEN** ...` line.

Output ONLY the whole file as markdown. No commentary.
"""
