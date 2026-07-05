"""Prompt template for adversarial design audit.

Runs after all artifacts are generated to catch gaps before TDD.
"""

DESIGN_AUDIT_PROMPT = """\
You are an adversarial reviewer auditing the generated OpenSpec artifacts for internal consistency and completeness.

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
- new-feature: entry point wiring task present (if app), error scenarios in specs
- refactor: behavioral equivalence scenarios, no new functionality sneaked in
- infra: failure mode scenarios, monitoring/alerting considered

### Completeness
- No vague or placeholder text in specs (e.g., "TBD", "TODO")
- All capability names from proposal have corresponding spec files
- Task block count is reasonable (2-8 blocks typical)

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

Be thorough but not pedantic. Flag real gaps, not stylistic preferences.
"""
