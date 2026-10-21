"""The epic split decision: one change, or several child changes with dependencies.

Copied from buildme's `prompts/epic_split.py`, then adapted: each child is
specified by its own pm-spec run, so its plan is the input of that run's
interview; and a ticket's existing child items are followed before any child of
the model's own."""

EPIC_SPLIT_PROMPT = """\
You decide whether a piece of product work is one change or several, and,
for several, write one plan per child change. Each child is then specified on
its own: its plan is all its interview starts from.

{notice}

{inputs}

## Instructions
One change is the default: work that one pull request can carry, reviewed in
one sitting, even if it touches several files. Answer several only when the
input itself describes separable deliverables (an epic, a PRD with distinct
features, a ticket with child items or a task list) that would each make a
reviewable pull request on their own.
{children_rule}
For several, give each child:
- `id`: unique, lowercase letters and digits joined by single hyphens
  (`billing-api`).
- `title`: one line.
- `depends_on`: the ids of the children that must be delivered before this one
  can start. Leave it empty when the child can start at once. No cycles.
- `ticket_ref`: the ref of the ticket child item this child delivers, exactly
  as listed, or empty for a child of your own.
- `plan`: the child's plan in Markdown, with these sections (short is fine,
  missing is not): `# Plan: <title>`, the line `> File: <id>.md · Date: {date}`,
  a one-sentence **Objective**, then `## Context`, `## Outcome`,
  `## Constraints / out of scope` and `## Done means` (numbered criteria, each
  checkable by an observation). State there everything the child needs from the
  inputs above.
{errors}
Return ONLY JSON matching this schema:
{{"single": true|false, "reason": "<one or two sentences>",
  "children": [{{"id": "...", "title": "...", "depends_on": ["..."],
                 "ticket_ref": "", "plan": "# Plan: ..."}}]}}
`children` is empty when `single` is true.
"""

CHILDREN_RULE = """
The ticket already lists these child items: {children}. When you answer
several, make one child per listed item first, in that order, each with its
`ticket_ref`; add a child of your own only for work none of them covers.
"""

EPIC_SPLIT_RETRY = """
## Your previous answer was rejected
Fix every one of these problems and answer again in full:
{errors}
"""
