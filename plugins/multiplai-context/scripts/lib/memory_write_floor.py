"""The code floor under every unattended memory write. It vetoes; it never grants.

Three checks, all deterministic, all model-free. They run **after** a verdict
has been reached — never before, and never as an input to it — and the only
thing they can do is refuse. Nothing a judge returns clears them; there is no
"the model was confident" path around this file, by construction: the caller
asks :func:`floor_check` about an item it has already decided to apply, and a
non-``None`` answer is final.

That ordering is the point. A gate consulted *before* the verdict is one the
verdict can be argued past — a model that knows what a check wants can phrase
its way through it. A gate applied *after*, on the concrete write, cannot be
argued with at all: it sees a filename and a change verb, not an argument.

## What is checked, and why each one

**Path containment.** The target filename comes out of a ``## Updates for
`x` `` heading written by a model and captured as ``[^`]+`` — arbitrary text.
``memory_dir / target`` will happily resolve ``../../CLAUDE.md`` to the
workspace file, and an ``.exists()`` check is no guard at all since the
interesting traversal targets are precisely the files that exist. This is not
hypothetical: PR #160 measured exactly that escape on a real proposal.

**Reserved filenames.** Dropping the old ``RECALL_FILES`` allowlist (settled
decision 3 — it was 18 hand-maintained names that went stale and was invisible
to any new memory file) leaves path containment as the only destination check.
But ``.multiplai/memory/CLAUDE.md`` *is* inside the memory dir and is the
memory system's own instruction file: containment alone would permit unattended
writes to it. ``kind: RULE`` never auto-applying covers rule-shaped *content*,
yet a ``FACT`` appended to an instruction file still edits an instruction file.

So two reserved basenames — ``CLAUDE.md`` and ``AGENTS.md`` — are refused in
every mode. **This is a flagged, provisional decision** (P4 §Floor): it is one
reserved pair with a stated reason, not a list to grow, and it deliberately
does not reintroduce the staleness problem that killed the allowlist. If it is
overruled, delete the check — do not soften it into a list.

The comparison is **case-insensitive**. On a case-insensitive filesystem
(macOS's default) ``claude.md`` and ``CLAUDE.md`` are the same file, so a
case-sensitive check would refuse the spelling a model is least likely to
write and permit the one it is most likely to.

**Append-only.** The change verb must be exactly ``add``. An ``update`` or a
``replace`` can destroy a line that was right, and nobody read this one.

**Parse integrity.** A change verb and a non-empty body must both be present.
A block with a verb but no quoted body parses "successfully" into empty text;
the applier is then handed a title and told to apply it, and composes a bullet
from the title — unreviewed, unsourced, and absent from the receipt too.

## Shape, not truth

Every check here judges an item's *form*. None of them can tell whether the
item is true, and that is deliberate: the semantic question belongs to the
judge (``lib/memory_judge.py``), and the whole architecture is that a judge
talked into ``verdict: apply`` still lands here.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Optional, Protocol

__all__ = [
    "ADDITIVE_CHANGES",
    "RESERVED_BASENAMES",
    "FLOOR_REASONS",
    "floor_check",
    "is_safe_target",
    "is_reserved_target",
]

_SAFE_TARGET_RE = re.compile(r"^[A-Za-z0-9._-]+\.md$")

# The only change verb that cannot destroy existing memory.
ADDITIVE_CHANGES = frozenset({"add"})

# See the module docstring. Two names, compared case-insensitively, with a
# stated reason. Not a list to grow.
RESERVED_BASENAMES = frozenset({"claude.md", "agents.md"})

# The reason codes this module can return, in the order it checks them. Kept
# here so the caller's label table and the floor cannot drift apart.
FLOOR_REASONS: tuple[str, ...] = (
    "unsafe-target",
    "reserved-filename",
    "unparsed",
    "not-additive",
)


class _WriteCandidate(Protocol):
    """The four fields the floor reads. Anything with them will do."""

    target: str
    change: str
    text: str


def is_safe_target(filename: str) -> bool:
    """Is *filename* a plain memory filename, safe to join onto ``memory_dir``?

    A basename ending in ``.md``, with no separators and no ``..``. Anything
    else is not a memory file, whatever it claims to be.
    """
    return (
        bool(_SAFE_TARGET_RE.match(filename))
        and ".." not in filename
        and filename == PurePosixPath(filename).name
    )


def is_reserved_target(filename: str) -> bool:
    """Is *filename* one of the reserved instruction filenames?

    Case-insensitive: on a case-insensitive filesystem ``claude.md`` and
    ``CLAUDE.md`` name the same file.
    """
    return PurePosixPath(filename).name.strip().lower() in RESERVED_BASENAMES


def floor_check(item: _WriteCandidate) -> Optional[str]:
    """The refusal reason for writing *item*, or ``None`` when it may be written.

    ``None`` is **permission to proceed with the caller's own verdict**, not a
    recommendation to apply. The floor never promotes anything; a caller that
    reads ``None`` as "apply this" has inverted the contract.
    """
    target = getattr(item, "target", "") or ""
    change = (getattr(item, "change", "") or "").strip().lower()
    text = getattr(item, "text", "") or ""

    if not is_safe_target(target):
        return "unsafe-target"
    if is_reserved_target(target):
        return "reserved-filename"
    if not change:
        return "unparsed"
    if change not in ADDITIVE_CHANGES:
        return "not-additive"
    if not text.strip():
        return "unparsed"
    return None
