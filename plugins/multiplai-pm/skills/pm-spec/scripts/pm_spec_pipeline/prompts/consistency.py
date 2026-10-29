"""The consistency check: do the proposal, the use cases, the plans and the specs agree?

The prior-findings section is restated from buildme's
`prompts/design_audit.py` (`PRIOR_FINDINGS_SECTION`), adapted to spec files."""

CONSISTENCY_PROMPT = """\
You check that the specs of one change agree with each other, before anyone
builds from them. You report problems; you do not fix them.

{notice}

## Project Context
{project_context}

## proposal.md
{proposal_content}

## use-cases.md
{use_cases_content}

## Component plans
{plans_content}

## Specs
{specs_content}

## What to report
Read every document above together. You may read the repository (Read, Glob,
Grep) to understand names it uses, but judge only these documents, with one
exception: for each statement about what the code does today that names a
file and a function, read that function, and report a `contradiction` when
the code does something else, citing the statement and the function as its
two places. Report each problem of these three types:

- `contradiction`: two places require behaviour that cannot both hold — two
  requirements, or a requirement and the proposal, a plan or a use case, give
  different rules for the same situation.
- `gap`: something the proposal, a plan or a use case promises that no
  requirement specifies, or a requirement that relies on a behaviour no
  requirement states.
- `duplicate`: the same behaviour required in two capabilities or two components.

Every finding cites both places, each as the file, by the path its heading
above gives (`proposal.md`, `use-cases.md`, a `plan.md` or a `specs.md`), and
where in it (a capability, a requirement or scenario name, a section, or a
use case). Severity:
`critical` when a builder would have to implement one of two incompatible
rules, `major` when a builder would have to guess, `minor` for wording that
reads two ways but would be built the same.
{prior_section}{accepted_section}
Return ONLY JSON matching this schema (an empty `findings` list is a real
answer when the documents agree):
{{"findings": [{{"type": "contradiction|gap|duplicate", "severity": "critical|major|minor",
  "description": "<what disagrees>",
  "places": [{{"file": "components/<id>/specs.md", "where": "<capability: requirement>"}},
             {{"file": "...", "where": "..."}}],
  "suggestion": "<the change that would make them agree>"}}],
 "prior_assessments": []}}
"""

PRIOR_FINDINGS_SECTION = """
## Findings the {by} was asked to fix

These documents were {done} to fix the findings below.

{prior_findings}

For each finding listed here, give its status with the lines of the documents
that show it. The status is `resolved`, `still_present`, `partially_resolved`
or `cannot_verify`, and it goes in `prior_assessments` with the finding's
`finding_id`. Report every other problem in `findings`, as above:

{{"finding_id": "CC-F1", "status": "resolved | still_present | partially_resolved | cannot_verify",
  "evidence": "The lines of the documents that show the status"}}
"""


ACCEPTED_SECTION = """
## Findings a person accepted

A person read each finding below and accepted the documents as they are. Do
not report any of them again, nor the same problem in other words.

{accepted_findings}
"""


def _lines(findings: list[dict]) -> str:
    return "\n".join(
        f"- {f.get('finding_id', '?')} [{f.get('severity', '?')}] ({f.get('category', '?')}) {f.get('text', '')}"
        for f in findings
    )


def prior_findings_section(prior: list[dict] | None, *, edited: bool = False) -> str:
    """The recheck section for these listed findings; empty for none. *edited*:
    a person changed the files (after the specs review), not the rewrite."""
    if not prior:
        return ""
    by, done = ("edit", "edited") if edited else ("rewrite", "rewritten")
    return PRIOR_FINDINGS_SECTION.format(by=by, done=done, prior_findings=_lines(prior))


def accepted_section(accepted: list[dict] | None) -> str:
    """The findings a person accepted (`resume --accept`); empty for none."""
    if not accepted:
        return ""
    return ACCEPTED_SECTION.format(accepted_findings=_lines(accepted))
