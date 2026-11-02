"""The component check: can one component's requirements hold, against each
other, its dependencies' specs and the code as it is today?

It asks what buildme's design step asks of each requirement (`DESIGN_PROMPT`
in buildme's `prompts/spec_generation.py`: a `SPEC_ISSUE:` for a requirement
that is infeasible, or contradicts another requirement or the codebase), one
component at a time, before the handoff instead of after it."""

COMPONENT_CHECK_PROMPT = """\
You check one component's requirements before anyone builds them. You report
problems for a product manager to decide; you do not fix them, and you give
no design or code advice.

{notice}

## Project Context
{project_context}

## proposal.md
{proposal_content}

## The component: {component}

### {plan_path}
{plan_content}

### {specs_path}
{specs_content}

## Specs of the components it depends on
A dependency's behaviour is what its spec below says, whether or not it is
built yet.

{dependency_specs}

## Every other component (capability names and requirement titles only)
{other_outlines}

## What to do, in this order
1. For each requirement of `{specs_path}`, find in the repository (Read, Glob,
   Grep) the models, endpoints and functions it changes or relies on, and
   decide whether it can hold as written given that code.
2. Compare every pair of requirements in `{specs_path}`.
3. Compare each requirement of `{specs_path}` with the dependencies' specs.
4. Report every problem you find, not only the first one, as one of:
   - `contradiction`: the requirement and another requirement, a
     dependency's spec or the code give different rules for the same
     situation, so both cannot hold.
   - `gap`: the requirement relies on a behaviour that no requirement here,
     no dependency's spec and no code provides.
   - `infeasible`: the requirement cannot be met as written (it names a
     model, field, endpoint or function that does not exist or works
     otherwise, or asks for something the rest of the change rules out).

Every finding has two places: the requirement in `{specs_path}` (its
capability and requirement name) first, then what it conflicts with: another
requirement, a dependency's `specs.md` and its requirement, or a file `path`
with the function or class in it. The `suggestion` is the change to the
requirement's text that would fix it, worded for a product manager: what the
requirement should say, never how to build it. Severity: `critical` when a
builder would have to break one of two rules, `major` when a builder would
have to guess or could not build it as written, `minor` for wording that reads
two ways but would be built the same.
{prior_section}{accepted_section}
Return ONLY JSON matching this schema (an empty `findings` list is a real
answer when every requirement can hold):
{{"findings": [{{"type": "contradiction|gap|infeasible", "severity": "critical|major|minor",
  "description": "<what the requirement says, and what it conflicts with>",
  "places": [{{"file": "{specs_path}", "where": "<capability: requirement>"}},
             {{"file": "<components/<id>/specs.md or a repository path>", "where": "<requirement, or function>"}}],
  "suggestion": "<the change to the requirement that would fix it>"}}],
 "prior_assessments": []}}
"""

PRIOR_SECTION = """
## Findings an earlier check of this component raised

The files were edited since. For each finding listed here, give its status
with the lines that show it, in `prior_assessments` with its `finding_id`:
`resolved`, `still_present`, `partially_resolved` or `cannot_verify`. Report
every other problem in `findings`, as above.

{prior_findings}
"""
