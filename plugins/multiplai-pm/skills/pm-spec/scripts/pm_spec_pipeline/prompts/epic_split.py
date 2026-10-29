"""The components decision: one component, or several with dependencies, each with a plan.

Copied from buildme's `prompts/epic_split.py`, then adapted: it runs after the
proposal is approved, every component's plan is reviewed before its specs are
written, and a ticket's existing child items are followed before any
component of the model's own."""

EPIC_SPLIT_PROMPT = """\
You break an approved change into components and write one plan per
component. Each component is built and reviewed as its own pull request; its
specs are written later from its plan, the proposal and the use cases.

{notice}

{inputs}

## Instructions
One component is the default: work that one pull request can carry, reviewed
in one sitting, even if it touches several files. Answer several only when the
work holds separable deliverables that would each make a reviewable pull
request on their own.
{children_rule}{one_rule}
Give each component:
- `id`: unique, lowercase letters and digits joined by single hyphens
  (`token-retry`).
- `title`: one line.
- `depends_on`: the ids of the components that must be delivered before this
  one can start. Leave it empty when it can start at once. No cycles.
- One owner for each shared rule: when two components change or rely on the
  same behaviour of existing code, such as one function or one endpoint's
  access rule, one component owns it and states it. Every other component
  that changes or relies on it lists the owner in `depends_on` and cites it
  in its plan, rather than stating the rule again.
- `ticket_ref`: the ref of the ticket child item this component delivers,
  exactly as listed, or empty for a component of your own.
- `plan`: the component's plan in Markdown, with these sections (short is
  fine, missing is not): `# Plan: <title>`, the line
  `> File: <id>.md · Date: {date}`, a one-sentence **Objective**, then
  `## Context`, `## Outcome`, `## Constraints / out of scope` and
  `## Done means` (numbered criteria, each checkable by an observation).
{errors}
Return ONLY JSON matching this schema:
{{"reason": "<one or two sentences: why this many components>",
  "components": [{{"id": "...", "title": "...", "depends_on": ["..."],
                   "ticket_ref": "", "plan": "# Plan: ..."}}]}}
"""

CHILDREN_RULE = """
The ticket already lists these child items: {children}. Make one component per
listed item first, in that order, each with its `ticket_ref`; every listed
item must be the `ticket_ref` of exactly one component. Add a component of
your own only for work none of them covers.
"""

ONE_COMPONENT_RULE = """
Keep the work as exactly one component.
"""

EPIC_SPLIT_RETRY = """
## Your previous answer was rejected
Fix every one of these problems and answer again in full:
{errors}
"""
