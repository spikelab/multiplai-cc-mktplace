"""The two-axis learning taxonomy: **provenance** × **kind**.

One label cannot answer two questions. The extractor used to emit a single
``type:`` field whose values answered three different ones — ``OBSERVATION``
and ``PATTERN`` say *what sort of thing* a learning is, ``CORRECTION`` says
*where it came from*, and ``PREFERENCE`` says *whose authority backs it*. That
made ``CORRECTION`` and ``OBSERVATION`` mutually exclusive when they are
orthogonal: a correction *about a fact* and a correction *about how the agent
should behave* wore the same label and need opposite handling. The first is the
most trustworthy input in the system; the second must never be applied without
a human reading it.

So the axes are separate:

**Provenance — where the knowledge came from.** Sets confidence, and names the
only method by which the claim can be re-verified.

===============  =================================  ==========================
Value            Source                             Re-verifiable by
===============  =================================  ==========================
``RESEARCH``     External sources — docs, web       Re-reading the cited source
``EMPIRICAL``    Doing the work — it broke, it      Re-running the thing
                 was fixed, it now works
``CORRECTION``   The user said the agent was wrong  Only by asking the user
``DECLARATION``  The user stated it unprompted      Only by asking the user
``INFERENCE``    The model concluded it; nobody     Nothing — never verified
                 confirmed it
===============  =================================  ==========================

``DECLARATION`` is distinct from ``CORRECTION`` because no error preceded it: a
correction means the agent *had* a wrong model that needs overwriting; a
declaration is new information with nothing to overwrite. ``INFERENCE`` had no
representation at all before this taxonomy — model conclusions entered as
``trust: medium`` and, one dream cycle later, were indistinguishable from
verified fact.

**Kind — what sort of thing it is.** Sets blast radius.

=============  ===========================  ================================
Value          True or false?               Lifecycle
=============  ===========================  ================================
``FACT``       Yes                          Decays; re-verifiable per its
                                            provenance
``RULE``       No — normative               Never decays; revoked, not
                                            falsified
``DECISION``   No — in force or overturned  Superseded by a later decision
``INTENTION``  No — pending or fired        Expires (see ``prospective.md``)
=============  ===========================  ================================

``DECISION`` earns its own value because "we're going with X" is not a fact
that goes stale — it is a commitment that gets overturned, and a maintenance
pass treating it as a stale fact would propose deleting live architectural
decisions.

**Nothing in this repository yet acts on either axis.** This module defines and
carries the pair; the classifier that reads it is a later phase. The one thing
that must hold in the meantime is that the pair survives every hop from the
learnings file to the dream proposal — see ``normalize_legacy`` for how records
written before the taxonomy existed are read without inventing labels for them.

The corpus under ``.multiplai/memory/`` is deliberately **not** back-filled.
Guessing a provenance for a line written a year ago is fabricating exactly the
signal this taxonomy exists to make trustworthy.
"""

from __future__ import annotations

from typing import Mapping, Optional

# --- The closed value sets --------------------------------------------------
# Order is documentation, not policy: nothing here ranks confidence or blast
# radius. Ranking is a judgement about what may be applied unreviewed, and it
# belongs to whatever makes that decision — not to the vocabulary.

PROVENANCES: tuple[str, ...] = (
    "RESEARCH",
    "EMPIRICAL",
    "CORRECTION",
    "DECLARATION",
    "INFERENCE",
)

KINDS: tuple[str, ...] = ("FACT", "RULE", "DECISION", "INTENTION")

# What the extractor is told to answer when it genuinely cannot tell. Both
# defaults route toward human review rather than away from it: an unclear
# provenance is the weakest one, and an unclear kind is the one with the
# widest blast radius. The asymmetry is deliberate and is stated in the
# extraction prompt so the model understands what the answer costs.
UNCLEAR_PROVENANCE = "INFERENCE"
UNCLEAR_KIND = "RULE"

# The single-axis vocabulary this replaced. Kept because records on disk still
# use it and are never rewritten.
LEGACY_TYPES: tuple[str, ...] = (
    "OBSERVATION",
    "PREFERENCE",
    "CORRECTION",
    "PATTERN",
    "RULE-PROPOSAL",
    "INTENTION",
)

# Old ``type`` → (provenance, kind), for READING old records only.
#
# ``OBSERVATION``/``PATTERN`` → ``INFERENCE`` is the conservative reading and
# it is wrong for many records that were in fact empirical. That is the correct
# error direction: an ``INFERENCE`` goes to a human, so a mislabelled old
# record costs one line of reading, whereas a guessed ``EMPIRICAL`` costs a
# claim nobody ever checked.
#
# ``RULE-PROPOSAL`` maps to kind ``RULE`` with **no** provenance: the old
# vocabulary simply did not record where a proposed rule came from, and
# ``None`` says so honestly. A consumer that needs a value should treat a
# missing provenance as ``INFERENCE`` — but that substitution is the
# consumer's to make, not this table's.
LEGACY_TYPE_MAP: dict[str, tuple[Optional[str], Optional[str]]] = {
    "CORRECTION": ("CORRECTION", "FACT"),
    "PREFERENCE": ("DECLARATION", "FACT"),
    "RULE-PROPOSAL": (None, "RULE"),
    "INTENTION": ("DECLARATION", "INTENTION"),
    "OBSERVATION": ("INFERENCE", "FACT"),
    "PATTERN": ("INFERENCE", "FACT"),
}

# Rendered when one half of the pair is known and the other is not. A record
# that carries a kind but no provenance must not be printed as though it had
# one; "?" is the whole point.
UNKNOWN_MARKER = "?"


def normalize_provenance(value: Optional[str]) -> Optional[str]:
    """Return *value* as a canonical provenance, or ``None`` if it is not one.

    Case and surrounding whitespace are forgiven — those are transcription
    noise. An out-of-set word is **not**: it is rejected rather than coerced to
    the nearest neighbour, because a coerced label is a claim about origin that
    nobody made.
    """
    if not value:
        return None
    candidate = value.strip().upper()
    return candidate if candidate in PROVENANCES else None


def normalize_kind(value: Optional[str]) -> Optional[str]:
    """Return *value* as a canonical kind, or ``None`` if it is not one."""
    if not value:
        return None
    candidate = value.strip().upper()
    return candidate if candidate in KINDS else None


def normalize_legacy(record: Mapping) -> tuple[Optional[str], Optional[str]]:
    """Map a pre-taxonomy record's ``type`` to ``(provenance, kind)``.

    Pure: reads one key, touches nothing, and is never used to rewrite a file
    on disk. A record whose ``type`` is absent or unrecognised yields
    ``(None, None)`` — no label is better than an invented one.

    This deliberately does **not** look at the record's text. Inferring that a
    year-old ``OBSERVATION`` was "probably empirical" from how confidently it
    is worded is precisely the fabrication the taxonomy exists to prevent.
    """
    ltype = (record.get("type") or "").strip().upper()
    return LEGACY_TYPE_MAP.get(ltype, (None, None))


def pair(record: Mapping) -> tuple[Optional[str], Optional[str]]:
    """The ``(provenance, kind)`` of *record*, explicit fields first.

    A record written under the taxonomy carries the two fields directly. One
    written before it carries only ``type``, and falls back to
    :func:`normalize_legacy`. A record with an out-of-set value in an explicit
    field falls back for that half too — the explicit value is rejected, and
    what the legacy field can honestly say is used instead.
    """
    provenance = normalize_provenance(record.get("provenance"))
    kind = normalize_kind(record.get("kind"))
    if provenance is not None and kind is not None:
        return provenance, kind
    legacy_provenance, legacy_kind = normalize_legacy(record)
    return (
        provenance if provenance is not None else legacy_provenance,
        kind if kind is not None else legacy_kind,
    )


def has_taxonomy(record: Mapping) -> bool:
    """True when *record* carries at least one valid half of the pair itself.

    The renderer uses this to choose a format: a record that states its own
    provenance or kind is written in the new form, and one that has only the
    old ``type`` keeps the old form. Legacy records are therefore never
    reprinted wearing a label they do not have.
    """
    return (
        normalize_provenance(record.get("provenance")) is not None
        or normalize_kind(record.get("kind")) is not None
    )


def format_pair(provenance: Optional[str], kind: Optional[str]) -> str:
    """Render the pair as ``PROVENANCE/KIND``, with ``?`` for either unknown."""
    return f"{provenance or UNKNOWN_MARKER}/{kind or UNKNOWN_MARKER}"


def parse_pair(text: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Read a ``PROVENANCE/KIND`` string back into a validated pair.

    The inverse of :func:`format_pair`, and equally strict: an unrecognised
    half comes back as ``None`` rather than as the string that was written.
    Missing text, a missing slash, and a bare ``?`` all yield ``None`` for the
    half they concern.
    """
    if not text:
        return None, None
    left, sep, right = text.strip().partition("/")
    if not sep:
        return normalize_provenance(left), None
    return normalize_provenance(left), normalize_kind(right)
