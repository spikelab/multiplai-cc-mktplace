"""The EPIC_SPLIT decision: one change, or several children with dependencies."""

EPIC_SPLIT_PROMPT = """\
You decide whether a piece of software work is one change or several, and,
for several, write one implementation plan per child change.

{notice}

{inputs}

## Instructions
One change is the default: work that one pull request can carry, reviewed in
one sitting, even if it touches several files. Answer several only when the
input itself describes separable deliverables (an epic, a PRD with distinct
features, a ticket with sub-issues or a task list) that would each make a
reviewable pull request on their own.

For several, give each child:
- `id`: unique, lowercase letters and digits joined by single hyphens
  (`billing-api`).
- `title`: one line.
- `depends_on`: the ids of the children that must be merged before this one
  can start. Leave it empty when the child can start at once. No cycles.
- `repo`: leave it out unless the child belongs in a different repository,
  then give the path of that repository's local checkout as the inputs name
  it, absolute or relative to this repository's root (`../billing`). The
  child builds there.
- `plan`: a complete implementation plan in Markdown, in the
  `/multiplai-dev:plan` format, with every one of these sections (short is
  fine, missing is not): `# Plan: <title>`, the line
  `> File: <id>.md · Date: {date}`, a one-sentence **Objective**, then
  `## Context`, `## Outcome`, `## Work items` (numbered, with file-level
  specifics), `## Stop and resume states` (a table with columns
  `# | Step | Stops at | On disk | Resume does`, one row per step that writes
  state and each point it can stop at, or one sentence saying the change
  writes no state), `## Constraints / out of scope`, `## Done means`
  (numbered criteria, each checkable by a command or an observation) and
  `## Verification` (how to check each criterion). The plan is all the
  child's build sees, so state there anything it needs from the inputs above.
{errors}
Return ONLY JSON matching this schema:
{{"single": true|false, "reason": "<one or two sentences>",
  "children": [{{"id": "...", "title": "...", "depends_on": ["..."],
                 "repo": "path to a local checkout, or omitted", "plan": "# Plan: ..."}}]}}
`children` is empty when `single` is true.
"""

EPIC_SPLIT_RETRY = """
## Your previous answer was rejected
Fix every one of these problems and answer again in full:
{errors}
"""
