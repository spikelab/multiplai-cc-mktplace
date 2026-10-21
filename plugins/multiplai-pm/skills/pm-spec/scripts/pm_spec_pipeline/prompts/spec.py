"""Prompts, templates and instructions for the proposal, use cases and requirements.

Copied from buildme's `prompts/spec_generation.py`, `change_manager.py`
(`TEMPLATES`, `INSTRUCTIONS`) and `spec_generator.USE_CASES_PROMPT`, then
adapted: the proposal opens with a `# <title>` line (the epic's title in
`tickets.json`) and, when a PRD or ticket children were given, closes with a
`## Sources` section that names, per capability, the PRD section or ticket
it comes from.
"""

PROPOSAL_TEMPLATE = """\
# <!-- a short title for the change -->

## Why

<!-- The problem, and why now. -->

## What Changes

<!-- What will change. Then, explicitly:
     Goals — what this change aims to achieve.
     Non-Goals — what is deliberately out of scope. -->

## Capabilities

### New Capabilities
<!-- kebab-case names only, one per line, no descriptions — use-cases.md
     already carries the intent. Each creates requirements/<name>.md -->
- `<capability-name>`

### Modified Capabilities
<!-- Existing capabilities whose REQUIREMENTS are changing. Leave empty if none. -->

## Impact

<!-- Affected code, APIs, dependencies, systems. Facts about the codebase, not
     restatements of What Changes. Wrap every dependency name in backticks so
     the dependency scan can see it. -->
"""

SOURCES_TEMPLATE = """
## Sources

<!-- One line per capability, naming where it comes from:
     - `<capability-name>`: {source_forms} -->
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

REQUIREMENTS_TEMPLATE = """\
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
    "explicitly); Capabilities (kebab-case names only, no descriptions, each "
    "becoming requirements/<name>.md); Impact (facts about the codebase — "
    "affected code, APIs, dependencies, systems — not a restatement of What "
    "Changes, with every dependency name in backticks). "
    "Personas and use cases live in use-cases.md, not here. "
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

REQUIREMENTS_INSTRUCTION = (
    "Create one requirements file per capability listed in the proposal, "
    "at requirements/<capability>.md. "
    "Use WHEN/THEN format for scenarios. Each requirement MUST have at least one scenario. "
    "Scenarios must be testable — you know when they pass."
)

PRD_RULE = """\
The PRD below is the primary source. Every capability comes from a section of
it: in `## Sources`, write one line per capability, `- \\`<capability>\\`: PRD
"<the exact text of the PRD heading it comes from>"`. Name only headings that
are in the PRD."""

TICKET_CHILDREN_RULE = """\
The ticket lists these child items: {children}. A capability that delivers one
of them names it in `## Sources`: `- \\`<capability>\\`: ticket <ref>`, with the ref
exactly as listed. A capability can name a PRD heading and a ticket on one line,
separated by `; `."""

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
- Capability names MUST be kebab-case (e.g., `user-auth`, `data-export`)
- Each capability listed under "New Capabilities" will become a requirements/<name>.md file
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

REQUIREMENTS_PROMPT = """\
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

REGISTRY_HEADING = "## Current behaviour (specs/registry)"
REGISTRY_REQUIREMENTS_RULE = (
    "These files are what the code does today. A requirement this change alters goes under "
    "`## MODIFIED Requirements` with the same `### Requirement:` name as here; a requirement it "
    "removes goes under `## REMOVED Requirements` with that name; only new requirements go under "
    "`## ADDED Requirements`."
)
REGISTRY_PROPOSAL_RULE = (
    "These capabilities are what the code does today, one file each in specs/registry/. A "
    "capability this change alters goes under `### Modified Capabilities` with exactly the name "
    "listed here, so its requirements are written against that file; a new capability goes under "
    "`### New Capabilities` with a name not listed here."
)

REWRITE_PROPOSAL_PROMPT = """\
You are revising the proposal of a spec-driven change to fix the findings
listed below. Keep everything the findings do not touch as it is.

{notice}

## Project Context
{project_context}

## Interview
{interview}
{prd_section}
## Current proposal
{proposal_content}

## Current requirements
{requirements_content}

## Findings to fix
{findings}

## Output Format
Keep the proposal's structure: a `# <title>` line, `## Why`, `## What Changes`,
`## Capabilities` (kebab-case names in backticks, one per line), `## Impact`{sources_note}.

Output ONLY the whole revised proposal as markdown. No commentary.
"""

REWRITE_REQUIREMENTS_PROMPT = """\
You are revising one capability's requirements file in a spec-driven change to
fix the findings listed below. Keep every requirement the findings do not touch
as it is, word for word.

## Project Context
{project_context}

## Proposal
{proposal_content}

## Use cases
{use_cases_content}

## Every requirements file of the change
{requirements_content}

## The file to revise: requirements/{capability_name}.md
{current_content}

## Findings to fix
{findings}

## Instructions
Fix each finding in this file where the fix belongs here. A behaviour two
capabilities both require belongs to the capability the proposal names for it:
keep it there and take it out of the other. Two requirements that contradict
each other are resolved by stating the one rule the proposal and the use cases
support. A gap is closed with a requirement and a WHEN/THEN scenario. When none
of the findings needs a change in this file, return it unchanged.

## Output Format
The same format as the current file: `## ADDED Requirements` (or MODIFIED /
REMOVED), `### Requirement: <name>`, and under each requirement at least one
`#### Scenario: <name>` with a `- **WHEN** ...` and a `- **THEN** ...` line.

Output ONLY the whole file as markdown. No commentary.
"""
