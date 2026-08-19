"""Prompt templates for rubric generation.

The rubric is tailored to the change type (frontend, backend, fullstack, infra).
"""

# The Code Architecture fallback baseline below (the twelve smells, plus the
# two binding rules that govern them) is adapted from `mattpocock/skills` ->
# `skills/engineering/code-review/SKILL.md`, itself citing Fowler,
# *Refactoring*, ch. 3 ("Bad Smells in Code").
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
- **Test Quality** (weight: 1) — behavior verification, edge cases, meaningful
  assertions. Score down for the testing anti-patterns: assertions that only
  interrogate mocks (`.called`, `assert_called_*`) with no observable-outcome
  assertion; mock setup outweighing assertions; mocks that don't honor the
  real collaborator's contract; production methods added only for tests;
  fixed sleeps instead of condition polling in integration tests.
- **Spec Compliance** (weight: 3) — WHEN/THEN scenarios implemented and tested

### Code Architecture baseline (fallback only)

When the project documents no standards of its own — no CONTEXT.md, no
CONVENTIONS.md, no architecture or style guide, no ADRs covering structure —
score Code Architecture against this fixed baseline of twelve code smells, each
given as what it is followed by how to fix it:

- **Mysterious Name** — a name that does not say what the thing is or does →
  rename it to the concept it actually carries.
- **Duplicated Code** — the same structure appearing in more than one place →
  extract it into one function and call it from both; pull it up when the
  copies sit in sibling classes.
- **Feature Envy** — a function that reaches into another module's data more
  than its own → move the function next to the data it works on.
- **Data Clumps** — the same two or three values travelling together through
  signature after signature → extract them into one object and pass that.
- **Primitive Obsession** — domain concepts carried as bare strings, ints, or
  dicts → replace the primitive with a type that names the concept, and a type
  code with an enum or subclasses.
- **Repeated Switches** — the same switch or if-chain on the same type code in
  several places → replace the conditional with polymorphism or a single
  dispatch table.
- **Shotgun Surgery** — one behavioural change forcing small edits across many
  modules → move the scattered pieces together so the change lands in one
  place.
- **Divergent Change** — one module edited for several unrelated reasons →
  split it along its axes of change into separate modules.
- **Speculative Generality** — hooks, parameters, or abstract bases serving a
  single caller and a future that never arrived → inline it, collapse the
  hierarchy, drop the unused parameter.
- **Message Chains** — a caller navigating far through the object graph, such
  as a.b().c().d() → hide the delegate, or move the calling code closer to the
  data.
- **Middle Man** — a class that delegates nearly everything it is asked to
  another object → remove the middle man and let callers talk to the real
  object.
- **Refused Bequest** — a subclass that ignores or rejects most of what it
  inherits → replace inheritance with delegation, or push members down to the
  subclass that wants them.

Two rules bind this baseline:

1. **The repo overrides.** A documented repo standard always wins. Where the
   project's own documented standard endorses something this baseline would
   flag, suppress the smell and say nothing about it.
2. **Always a judgment call.** Every item above is a labelled heuristic —
   write "possible Feature Envy", never a hard violation — and none of it is
   pass/fail on its own. Skip anything the project's tooling (formatter,
   linter, type checker) already enforces.

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
