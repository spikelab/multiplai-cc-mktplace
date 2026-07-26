"""Prompt templates for the unknowns/edge-case explainer gate (B1).

`EXPLAINER_PROMPT` runs once per dependency that is new to the project and
produces that dependency's section of `unknowns.md`. `UNKNOWNS_REGEN_PROMPT`
is the single regeneration pass the gate triggers when a section is missing or
one of its required lists came back empty.

Both are positive recipes: they state the sections to write and the observable
condition for each (a bullet per case, each assumption falsifiable). The
enforcement is `gates.unknowns_gate`, not prompt severity.
"""

from __future__ import annotations

EXPLAINER_PROMPT = """\
You are writing the explainer for one dependency a change is about to take on,
so the team knows its real behavior before any code depends on it.

## Dependency
`{dep_name}`

## Where it was named in the specs
{mentioned_in}

## Why we believe it is new to this project
{evidence}

## Project Context
{project_context}

## How this change intends to use it
{usage_context}

## Your job

Research this dependency (docs, changelogs, issue trackers, and the project's
own code where relevant) and write ONE markdown section for `unknowns.md`.

Use exactly this structure, with the dependency name as the level-2 heading:

## {dep_name}

### What it is
One paragraph: what it is, who maintains it, what job it does in this change.

### The contract we rely on
The exact API surface this change calls — function/endpoint names, inputs,
outputs, and what the documentation promises. Quote signatures verbatim.

### Edge cases & failure modes
One bullet per case, each naming the OBSERVED behavior rather than a
possibility. Cover at least these five inputs/conditions:
- empty or degenerate input
- malformed or unexpected-format input
- oversized input (limits, truncation, timeouts)
- concurrent or repeated use (thread/process safety, rate limits, state)
- offline / unavailable / unauthenticated

The bar is the Whisper example: "given pure silence, Whisper does not return an
empty string — it hallucinates common caption text such as 'thanks for
watching'". Name that class of surprise wherever it exists here. When the
documented behavior is genuinely unknown, write the bullet as
"Unknown: <question> — <how to check>".

### Assumptions we are making
One bullet per assumption, each phrased so it could be proven wrong by an
observation ("X returns UTF-8 for every supported locale", not "X handles
encodings well").

### How we would find out cheaply
The smallest concrete experiment that would test the assumptions above —
a command, a five-line script, or a single API call, with the result that
would falsify each assumption.

## Output
Output ONLY that markdown section, starting with `## {dep_name}`. No preamble,
no closing summary.
"""

UNKNOWNS_REGEN_PROMPT = """\
You are completing an `unknowns.md` document. A structural gate found sections
that are missing or incomplete; this is the one pass to fix them.

## Dependencies that each need a complete section
{dependency_list}

## Gate findings (what is missing)
{audit_findings}

## Current unknowns.md
{current_unknowns}

## Project Context
{project_context}

## Instructions
{instruction}

## Output Format
Output the COMPLETE corrected `unknowns.md`, keeping every section that is
already complete byte-for-byte and filling in only what the findings name.

Each dependency gets a level-2 heading (`## <name>`) followed by these five
level-3 sections in order:

{template}

Every `### Edge cases & failure modes` list has at least one bullet naming
observed behavior on empty, malformed, oversized, concurrent, and
offline/unavailable input. Every `### Assumptions we are making` list has at
least one bullet phrased as a falsifiable claim. Where the behavior is genuinely
undocumented, write "Unknown: <question> — <how to check>" rather than dropping
the bullet.

Output ONLY markdown.
"""
