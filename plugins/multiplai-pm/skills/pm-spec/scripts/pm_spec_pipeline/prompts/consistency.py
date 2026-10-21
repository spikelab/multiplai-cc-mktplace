"""The consistency check: do the proposal, the use cases and the requirements agree?

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

## Requirements
{requirements_content}

## What to report
Read every document above together. You may read the repository (Read, Glob,
Grep) to understand names it uses, but judge only these documents. Report
each problem of these three types:

- `contradiction`: two places require behaviour that cannot both hold — two
  requirements, or a requirement and the proposal or a use case, give
  different rules for the same situation.
- `gap`: something the proposal or a use case promises that no requirement
  specifies, or a requirement that relies on a behaviour no requirement states.
- `duplicate`: the same behaviour required in two capabilities.

Every finding cites both places, each as the file (`proposal.md`,
`use-cases.md` or `requirements/<capability>.md`) and where in it (a
requirement or scenario name, a section, or a use case). Severity:
`critical` when a builder would have to implement one of two incompatible
rules, `major` when a builder would have to guess, `minor` for wording that
reads two ways but would be built the same.
{prior_section}
Return ONLY JSON matching this schema (an empty `findings` list is a real
answer when the documents agree):
{{"findings": [{{"type": "contradiction|gap|duplicate", "severity": "critical|major|minor",
  "description": "<what disagrees>",
  "places": [{{"file": "requirements/<capability>.md", "where": "<requirement or section>"}},
             {{"file": "...", "where": "..."}}],
  "suggestion": "<the change that would make them agree>"}}],
 "prior_assessments": []}}
"""

PRIOR_FINDINGS_SECTION = """
## Findings the rewrite was asked to fix

These documents were rewritten to fix the findings below.

{prior_findings}

For each finding listed here, give its status with the lines of the documents
that show it. The status is `resolved`, `still_present`, `partially_resolved`
or `cannot_verify`, and it goes in `prior_assessments` with the finding's
`finding_id`. Report every other problem in `findings`, as above:

{{"finding_id": "CC-F1", "status": "resolved | still_present | partially_resolved | cannot_verify",
  "evidence": "The lines of the documents that show the status"}}
"""


def prior_findings_section(prior: list[dict] | None) -> str:
    """The recheck section for these listed findings; empty for none."""
    if not prior:
        return ""
    lines = "\n".join(
        f"- {f.get('finding_id', '?')} [{f.get('severity', '?')}] ({f.get('category', '?')}) {f.get('text', '')}"
        for f in prior)
    return PRIOR_FINDINGS_SECTION.format(prior_findings=lines)
