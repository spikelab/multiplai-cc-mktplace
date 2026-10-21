"""The UI or output-format prototype, copied from buildme's `prompts/prototype.py`
and adapted: it runs before the requirements are final, on the proposal and
the requirements (there is no design yet), and its notes feed a rewrite of
those two."""

PROTOTYPE_PROMPT = """\
You are a prototyping agent. Your job is to produce the cheapest artifact that
proves the *shape* of this change — what it looks like, what it outputs —
before its requirements are final.

## Change: {change_name}

## Proposal
{proposal_content}

## Requirements
{requirements_content}

## Where to write
Write every file inside this directory and nowhere else:

    {prototype_dir}

Do not create, edit, or delete anything outside it — no repo source files, no
config, no tests. The pipeline checks the paths you report against this
directory and fails the stage when a file lands outside.

## What to build
Pick the ONE form that proves the most about this change for the least work:

- **A user interface** → a single self-contained `mockup.html` file: inline CSS,
  inline JS if it truly needs interaction, hardcoded sample data, no framework,
  no build step, no external requests. It must open straight from disk.
- **An output format** (report, export, CLI output, document) → a
  `sample-output.<ext>` file containing realistic example output, filled with
  plausible values rather than placeholders.
- **A command-line or API exchange** → a hand-written `transcript.md` showing
  the exact commands/requests and the exact responses, as they would look.

Hardcode everything. Fake data is the point — this artifact exists to be looked
at and argued with, not to run. Keep it to one file plus NOTES.md where you can.

## Then write {prototype_dir}/NOTES.md
End NOTES.md with these REQUIRED slots, each on its own line:

```
PROVES: <what the artifact settles about the shape — layout, fields, wording,
  the exact output format, the exact command sequence>
DISPROVES: <what building it showed was wrong or unworkable in the proposal or
  the requirements, or "none">
OPEN_QUESTIONS: <what a person still has to decide after looking at it, or "none">
STATUS: <DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED>
```

`DISPROVES:` and `OPEN_QUESTIONS:` are the valuable slots: what you write there
is fed straight back into the proposal and the requirements. Writing "none"
when the prototype genuinely surfaced nothing is a real answer. Report
`NEEDS_CONTEXT` or `BLOCKED` if the proposal and the requirements do not say
enough to draw the shape at all — say what is missing in that case.
"""
