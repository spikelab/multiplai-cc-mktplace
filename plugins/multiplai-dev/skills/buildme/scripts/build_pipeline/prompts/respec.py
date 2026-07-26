"""Prompt template for the end-of-build respec proposal."""

RESPEC_PROMPT = """\
You are closing the loop between a spec and the code that was just built from
it. The build is finished. Along the way, each agent recorded what did not match
the spec or design. Your job is to turn those notes into a concrete proposed
delta to the requirements — a document a human (or the next change) can read and
apply deliberately.

You are writing a **proposal**. Nothing you write is applied automatically, and
no spec file is edited by this step. Say plainly what you would change and why.

## Change: {change_name}

## Implementation notes recorded during the build
{notes}

## Current requirements
{requirements}

## Current design
{design}

## How to decide what to propose

- A note marked `contradicts` means the block could only be built by doing
  something the spec does not say, or says otherwise. That is the strongest
  signal: propose the MODIFIED requirement whose scenarios the code now
  disagrees with, quoting the note that motivated it.
- A note marked `clarify` means the spec was silent or ambiguous and an agent
  had to choose. Propose an ADDED or MODIFIED requirement that writes the
  choice down as a WHEN/THEN scenario, so the next build does not have to
  guess.
- Propose REMOVED only for a requirement the build showed to be unbuildable or
  superseded — and say which note showed it.
- When a note describes a passing surprise that changes nothing about the
  contract (a tool quirk, a local workaround), leave it out of the delta and
  mention it under Notes considered and not proposed.
- Propose only what the notes support. An empty section is a valid, honest
  answer.

## Output format

Output markdown only, in exactly this shape (keep all three section headings,
even when a section is empty):

```
## ADDED Requirements

### Requirement: <name>
The system SHALL <behavior>.

#### Scenario: <name>
- **WHEN** <trigger>
- **THEN** <observable outcome>

_Motivated by:_ <the note, quoted, with block number and role>

## MODIFIED Requirements

### Requirement: <exact existing requirement name>
<the full replacement text for that requirement, scenarios included>

_Motivated by:_ <the note, quoted, with block number and role>

## REMOVED Requirements

### Requirement: <exact existing requirement name>

_Motivated by:_ <the note, quoted, with block number and role>

## Notes considered and not proposed
- <note> — <why it does not change the spec>
```

Requirement names under MODIFIED and REMOVED must match the existing
requirement names in the current requirements above, character for character —
that is how the delta is matched when someone applies it.
"""
