"""Prompt templates for rubric generation.

The rubric is tailored to the change type (frontend, backend, fullstack, infra).
"""

RUBRIC_PROMPT = """\
You are generating an evaluation rubric for code review of an OpenSpec change.

## Change Type
{change_type}

## Spec Summaries
{spec_summaries}

## Task Overview
{tasks_summary}

## Instructions
Generate a rubric.md with scoring dimensions tailored to this change.

Every rubric MUST include these core dimensions:
- **Code Architecture** (weight: 2) — module boundaries, coupling, patterns
- **Test Quality** (weight: 1) — behavior verification, edge cases, meaningful assertions
- **Spec Compliance** (weight: 3) — WHEN/THEN scenarios implemented and tested

Additionally, add 1-2 dimensions specific to the change type:
- frontend: Design Fidelity (weight: 2), Accessibility (weight: 1)
- backend: API Design (weight: 2), Error Handling (weight: 1)
- fullstack: Integration Coherence (weight: 2)
- infra: Operational Readiness (weight: 2), Security Posture (weight: 1)

## Output Format
Each dimension should have a table:

## Dimension Name (weight: N)
| Score | Criteria |
|-------|----------|
| 5 | Excellent criteria description |
| 3 | Acceptable criteria description |
| 1 | Failing criteria description |

Output ONLY the markdown content starting with `# Evaluation Rubric`. No commentary.
"""
