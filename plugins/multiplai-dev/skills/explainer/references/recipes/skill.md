# Recipe: a skill, a pipeline, or several used together

A Claude Code skill, a CLI tool, or a chain of them, e.g. one skill writing the specs another builds from
and a third reviews. The page says what goes in, what
comes out, and how each one hands on to the next.

## Gather

1. Each skill's `SKILL.md`: its steps, what it reads, what it writes, what it asks the user, where it
   stops for review.
2. Its scripts' entry points (`__main__.py`, the CLI's subcommands) and its config file, for the
   flags and settings the page names. Cite them.
3. **For several skills: the hand-offs.** For each pair, what file, record, branch or command one
   leaves for the next, and what the next one checks before it starts. The hand-offs are usually the
   most useful part of the page.
4. Where a human decides: each review stop, approval, or question.

For a deep look at one skill's code, run codebase-walkthrough on its scripts directory (`codebase.md`).

## Shape

- **Answer**: what goes in, what comes out, and who decides along the way.
- **Picture**: one box per skill or stage; each arrow labelled with what it hands on (`text.sub` on
  the arrow: "specs.md", "ready record", "PR").
- **Worked example**: one request followed through all of it: one feature, one ticket, one change.
  Show the actual file or record at each hand-off in the `.example` box.
- **Each card's where line**: the slash command or script, and where its output lands.
- **Deeper sections**: "Where you decide" (each review stop, as `.rows`); "What it costs" (`.tiles`,
  from measured runs only); "When it stops early and how to resume".
- **Status words**: agent / code / you.
