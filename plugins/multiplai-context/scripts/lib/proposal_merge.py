"""Deterministic merge of N chunk drafts into one reviewable proposal.

Chunked drafting buys a backlog that finishes, and pays for it in fragments: a
dozen documents each with its own ``### 1.`` and its own ``## Filtered Out``.
This module puts them back together — pure code, no model call, because the
alternative (asking a model to merge) reintroduces exactly the byte-size
problem chunking exists to solve, on a document that is *already* the expensive
output.

**The invariant is that nothing is lost.** A dropped entry here is a learning
that was consumed from the backlog, marked processed in the ledger, and never
shown to anyone — silent and unrecoverable. So the merge only ever renumbers
the ``### N.`` heading line; every other line of an entry, in particular its
``**Source:**`` provenance, is copied through verbatim.

Output ordering is fully determined by the inputs (target files sorted, draft
order preserved within a file), so re-running the merge on the same drafts
produces the same bytes.

:func:`split_by_file` is the parser ``dream.py::_split_proposal_by_file``
performs, lifted here so the producer and the consumer of a proposal cannot
drift apart. It matches that function's semantics exactly, including returning
the ``## Updates for`` header line as part of each section body.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

# `## Updates for `file`` — the group heading. Length 16 is the offset dream.py
# uses to look past the opening backtick; keep the two in step.
_UPDATES_PREFIX = "## Updates for `"
_UPDATES_FOR_RE = re.compile(r"^## Updates for `(?P<file>[^`]+)`")
_ACTIONS_RE = re.compile(r"^## Action Items\b")
_FILTERED_RE = re.compile(r"^## Filtered Out\b")
_ENTRY_RE = re.compile(r"^### (?P<num>\d+)\.(?P<rest>.*)$")
_ACTION_ENTRY_RE = re.compile(r"^### A(?P<num>\d+)\.(?P<rest>.*)$")
_RULE_RE = re.compile(r"^-{3,}\s*$")

SECTION_SEPARATOR = "\n\n---\n\n"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def split_by_file(proposal: str) -> dict[str, str]:
    """``{target_file: section_body}`` for each ``## Updates for `file`` section.

    Byte-compatible with ``dream.py::_split_proposal_by_file``: the body keeps
    its own heading line, any other H2 (``## Filtered Out``, ``## Action
    Items``, ``## Processed``) terminates the current section, preamble is
    dropped, and a repeated heading for the same file keeps the last occurrence.
    """
    sections: dict[str, str] = {}
    current_file: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if current_file is not None:
            sections[current_file] = "\n".join(buf).strip()

    for line in proposal.splitlines():
        if line.startswith(_UPDATES_PREFIX) and "`" in line[len(_UPDATES_PREFIX):]:
            flush()
            current_file = line.split("`")[1]
            buf = [line]
        elif line.startswith("## "):
            flush()
            current_file = None
            buf = []
        elif current_file is not None:
            buf.append(line)
    flush()
    return sections


def _trim(lines: list[str]) -> list[str]:
    """Drop the blank lines and ``---`` rules that separate one block from the next.

    Both ends: the leading blank belongs to the heading above, the trailing rule
    to the section below. Trimming them here is what lets the renderer own
    spacing, so a merged document has the same shape however its inputs were
    spaced.
    """
    start, end = 0, len(lines)
    while end > start and (not lines[end - 1].strip() or _RULE_RE.match(lines[end - 1])):
        end -= 1
    while start < end and (not lines[start].strip() or _RULE_RE.match(lines[start])):
        start += 1
    return lines[start:end]


def _sections(proposal: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Split into ``(preamble_lines, [(heading, body_lines), …])``.

    ``body_lines`` excludes the heading and the trailing rule/blank run.
    """
    preamble: list[str] = []
    out: list[tuple[str, list[str]]] = []
    heading: str | None = None
    buf: list[str] = []
    for line in proposal.splitlines():
        if line.startswith("## "):
            if heading is None:
                preamble = _trim(buf)
            else:
                out.append((heading, _trim(buf)))
            heading, buf = line, []
        else:
            buf.append(line)
    if heading is None:
        preamble = _trim(buf)
    else:
        out.append((heading, _trim(buf)))
    return preamble, out


def _entries(body: list[str], pattern: re.Pattern) -> tuple[list[str], list[list[str]]]:
    """Split a section body into ``(intro_lines, [entry_block, …])``.

    An entry runs from its ``### `` heading to the next one (or the end),
    trailing blanks and rules trimmed. Anything before the first heading is the
    section's intro prose.
    """
    starts = [i for i, line in enumerate(body) if pattern.match(line)]
    if not starts:
        return _trim(body), []
    intro = _trim(body[:starts[0]])
    bounds = starts + [len(body)]
    blocks = [
        _trim(body[a:b]) for a, b in zip(bounds, bounds[1:])
    ]
    return intro, [b for b in blocks if b]


def _renumber(block: list[str], number: int, prefix: str = "") -> list[str]:
    """Rewrite only the ``### N.`` heading; every other line is copied verbatim."""
    pattern = _ACTION_ENTRY_RE if prefix else _ENTRY_RE
    m = pattern.match(block[0])
    rest = m.group("rest") if m else ""
    return [f"### {prefix}{number}.{rest}", *block[1:]]


def source_lines(text: str) -> list[str]:
    """Every ``**Source:**`` provenance line in *text*, in order.

    Exposed for the no-loss assertions in the tests and for a caller that wants
    to check a merge against its inputs before recording anything in the ledger.
    """
    return [line for line in text.splitlines() if line.lstrip().startswith("**Source:**")]


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def merge_drafts(drafts: Sequence[str]) -> str:
    """Concatenate chunk drafts into one proposal, renumbered and deduplicated by section.

    - Preamble: the first draft's, unchanged (it carries the title and the
      ``Sources:`` line dream's readers expect).
    - Updates: grouped by target file, files sorted, drafts in input order
      within a file, entries renumbered ``1..N`` **across the whole proposal** —
      one running counter, not one per file. A reviewer says "skip 14" and means
      one entry; per-file numbering made them say the filename too. The
      ``(update, target, index)`` reference in ``lib/dream_processed.py`` and the
      GUI hub both still resolve, because a globally-unique index keeps the pair
      unique — it just no longer restarts at 1 under each heading.
    - Action items: merged into one section, renumbered ``A1..Ak``.
    - Filtered Out: bodies concatenated in draft order. Deliberately **not**
      deduplicated — a repeated line costs a reader a second, a wrongly-dropped
      one costs them the only record that a learning was discarded.
    - Any other H2 a draft emitted is preserved verbatim at the end rather than
      dropped, on the same "never lose content" principle.
    """
    non_empty = [d for d in drafts if d and d.strip()]
    if not non_empty:
        return ""

    preamble: list[str] = []
    per_file: dict[str, list[list[str]]] = {}
    file_intro: dict[str, list[str]] = {}
    actions: list[list[str]] = []
    actions_intro: list[str] = []
    filtered: list[str] = []
    extras: list[tuple[str, list[str]]] = []

    for draft in non_empty:
        head, sections = _sections(draft)
        if not preamble and head:
            preamble = head
        for heading, body in sections:
            m = _UPDATES_FOR_RE.match(heading)
            if m:
                name = m.group("file")
                intro, blocks = _entries(body, _ENTRY_RE)
                if intro and name not in file_intro:
                    file_intro[name] = intro
                per_file.setdefault(name, []).extend(blocks)
            elif _ACTIONS_RE.match(heading):
                intro, blocks = _entries(body, _ACTION_ENTRY_RE)
                if intro and not actions_intro:
                    actions_intro = intro
                actions.extend(blocks)
            elif _FILTERED_RE.match(heading):
                filtered.extend(_trim(body))
            else:
                extras.append((heading, body))

    parts: list[str] = []
    # One counter for the whole document. It must not reset per file: the
    # number is how a reviewer names an entry out loud, and a number that only
    # identifies an entry once you also say the filename is not an identifier.
    entry_number = 0
    for name in sorted(per_file):
        block_lines = [f"## Updates for `{name}`"]
        if file_intro.get(name):
            block_lines += ["", *file_intro[name]]
        for entry in per_file[name]:
            entry_number += 1
            block_lines += ["", *_renumber(entry, entry_number)]
        parts.append("\n".join(block_lines))

    if actions:
        block_lines = [f"## Action Items ({len(actions)} items)"]
        if actions_intro:
            block_lines += ["", *actions_intro]
        for i, entry in enumerate(actions, start=1):
            block_lines += ["", *_renumber(entry, i, prefix="A")]
        parts.append("\n".join(block_lines))

    filtered = _trim(filtered)
    if filtered:
        n = sum(1 for line in filtered if line.strip().startswith("- "))
        parts.append("\n".join([f"## Filtered Out ({n} items)", "", *filtered]))

    for heading, body in extras:
        parts.append("\n".join([heading, *(["", *body] if body else [])]))

    document = SECTION_SEPARATOR.join(parts)
    if preamble:
        document = "\n".join(preamble) + SECTION_SEPARATOR + document
    return document.rstrip("\n") + "\n"


def count_entries(proposal: str) -> int:
    """Total ``### N.`` update entries plus ``### A{N}.`` action items.

    The merge's own tripwire: if this drops between the inputs and the output,
    the run has lost a learning and must stop rather than record the ledger keys.
    """
    total = 0
    _, sections = _sections(proposal)
    for heading, body in sections:
        if _UPDATES_FOR_RE.match(heading):
            total += len(_entries(body, _ENTRY_RE)[1])
        elif _ACTIONS_RE.match(heading):
            total += len(_entries(body, _ACTION_ENTRY_RE)[1])
    return total


def count_entries_in(drafts: Iterable[str]) -> int:
    """:func:`count_entries` summed over drafts."""
    return sum(count_entries(d) for d in drafts)
