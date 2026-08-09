#!/usr/bin/env python3
"""Conflict-triggered supersede edits (the Mem0 pattern).

Generic consolidation treats a correction like any other learning: it goes into
the pile, the model reads the pile, and *maybe* it notices that a memory line
now says the opposite of what the user just confirmed. Nothing guarantees it —
and a stale line that contradicts a verified correction is the single most
expensive kind of memory error, because it actively teaches the wrong thing
until someone spots it.

This runs before consolidation and is deterministic on purpose. For each
CORRECTION (or otherwise `trust: verified`) learning, it looks in that
learning's routed target file for the line the correction contradicts, and — if
it finds one confidently — emits a targeted supersede edit that the proposal
puts *first*, under its own heading, so review sees conflicts before it sees
new-information entries.

**Precision over recall, deliberately.** A missed conflict costs one cycle: the
generic pass may still catch it, and the next correction will re-raise it. A
*wrong* match proposes superseding an unrelated true fact, and the reviewer is
reading a plausible-looking diff. So the matcher demands strong overlap and
emits nothing when it is unsure. `MIN_OVERLAP` is the knob; lowering it trades
away the property that makes this safe to put at the top of the proposal.

**What this can and cannot tell you.** Text overlap finds *the existing line
this learning is about*. It cannot distinguish "contradicts it" from "restates
it" — and on the live corpus both real hits were restatements, not
contradictions. That is still the useful signal, because both want the same
handling: update the existing line in place instead of appending a near
duplicate beside it, which is how memory files accumulate three phrasings of
one fact. The section says so plainly rather than claiming a contradiction
detector it isn't; deciding which of the two it is stays with the reviewer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from lib.taxonomy import normalize_kind, normalize_provenance

# A learnings line as written by extraction._format_learning_entry, in either
# of the two forms it emits:
#   - **[CORRECTION/FACT]** <desc> → Target: <file> — <action>       (taxonomy)
#   - **[trust: verified]** CORRECTION <desc> → Target: <file> — …   (legacy)
#
# Both are matched because both are on disk simultaneously and will be for as
# long as the pending backlog lives. A parser that knew only the old form would
# not fail loudly — it would quietly stop seeing every new learning, which is
# the worst available outcome for a detector whose whole job is noticing.
# `[A-Z?-]+`, not `[A-Z?]+`: a hyphen in either half (`RULE-PROPOSAL/FACT`)
# matched neither arm, so the line was dropped from the scan entirely rather
# than parsed and judged. `learnings_ledger._LEARNING_MARKER_RE` already
# accepted hyphens, and two parsers disagreeing about what a learning *is* is
# how a detector goes quiet without failing.
_MARKER = (
    r"(?:\[(?P<provenance>[A-Z?][A-Z?-]*)/(?P<kind>[A-Z?][A-Z?-]*)\]\*\*\s+"
    r"|\[trust:\s*(?P<trust>\w+)\]\*\*\s+(?P<type>[A-Z-]+)\s+)"
)
LEARNING_LINE_RE = re.compile(
    r"^-\s+\*\*" + _MARKER +
    r"(?P<description>.*?)"
    r"(?:\s+→\s+Target:\s*(?P<target>[\w.-]+)(?:\s+—\s+(?P<action>.*))?)?$")

# Words that carry no discriminating signal. Overlap computed on these produces
# confident matches between sentences that share nothing but grammar.
STOPWORDS = frozenset("""
a an the and or but if then than that this these those is are was were be been
being do does did done have has had having will would shall should can could
may might must to of in on at by for with from as it its into about over under
not no nor so such own same too very just when where which who whom what why
how all any both each few more most other some only own s t don now use used
using via per within without across
""".split())

# Jaccard overlap on content words. Calibrated by hand against real learnings
# and memory lines: 0.35 admits genuine restatements ("Opus is the default
# model" vs "the default model is Opus 4.5") while rejecting same-topic lines
# that assert different things.
MIN_OVERLAP = 0.35

# Below this many content words a line is too short for overlap to mean
# anything — two three-word lines sharing two words score 0.66 by accident.
MIN_CONTENT_WORDS = 4

# Structure, not facts.
_SKIP_LINE_RE = re.compile(r"^\s*(?:#{1,6}\s|```|\||-{3,}\s*$|\*\*Last Updated)")

#: Provenances that count as "the world was observed to be otherwise", and so
#: may put an existing memory line up for supersession. The two-axis equivalent
#: of the legacy `CORRECTION or trust: verified` rule — see
#: :attr:`Learning.is_conflict_candidate` for why ``EMPIRICAL`` belongs here and
#: ``RESEARCH`` does not.
CONFLICT_PROVENANCES = frozenset({"CORRECTION", "EMPIRICAL"})


@dataclass(frozen=True)
class Learning:
    trust: str
    type: str
    description: str
    target: str
    action: str
    provenance: str = ""
    kind: str = ""

    @property
    def is_conflict_candidate(self) -> bool:
        """CORRECTIONs always; other provenances only when directly observed.

        A weak inference that happens to touch an existing line is not evidence
        the line is wrong. Superseding on that basis would let a guess overwrite
        a confirmed fact.

        Under the legacy vocabulary the rule was `CORRECTION` **or**
        `trust: verified`. The two-axis mapping of "verified" is
        :data:`CONFLICT_PROVENANCES` — ``CORRECTION`` and ``EMPIRICAL`` — not
        ``CORRECTION`` alone. Narrowing to ``CORRECTION`` looks like the safe
        direction and is not: the extractor returns ``EMPIRICAL`` for roughly
        five records in six, so `## Conflict Resolutions` would have gone from
        firing on most verified learnings to firing on corrections only. This
        detector exists to catch a stale memory line contradicted by a
        *confirmed* fact, and ``EMPIRICAL`` — something observed in this
        session — is the strongest evidence class the new vocabulary has.
        ``RESEARCH``, ``DECLARATION`` and ``INFERENCE`` stay out: read
        somewhere, asserted, or reasoned to, none of them observed here.
        """
        if self.provenance:
            return self.provenance in CONFLICT_PROVENANCES
        return self.type == "CORRECTION" or self.trust == "verified"

    @property
    def basis(self) -> str:
        """How this learning describes itself, for the reviewer's benefit."""
        if self.provenance or self.kind:
            return f"{self.provenance or '?'}/{self.kind or '?'}"
        return f"{self.type}, trust: {self.trust}"


@dataclass(frozen=True)
class ConflictEdit:
    target: str
    lineno: int
    old_line: str
    learning: Learning
    overlap: float

    def render(self, today: date) -> str:
        stamp = today.isoformat()
        return (
            f"### `{self.target}` line {self.lineno}\n\n"
            f"- **Superseded** (was): {self.old_line.strip()}\n"
            f"- **Now**: {self.learning.description}\n"
            f"- **Edit**: {self.learning.action or 'replace the superseded line'}\n"
            f"- **Basis**: {self.learning.basis}; "
            f"match confidence {self.overlap:.2f}\n"
            f"- **If keeping both**: mark the old line "
            f"`(superseded {stamp})` rather than deleting it.\n"
        )


def _content_words(text: str) -> set[str]:
    """Lowercased alphanumeric words, minus stopwords and markdown noise."""
    text = re.sub(r"`[^`]*`", " ", text)          # code spans: often identical boilerplate
    text = re.sub(r"\*+|_+|→|—", " ", text)
    words = re.findall(r"[a-z0-9][a-z0-9.\-/]*", text.lower())
    return {w.strip(".-/") for w in words if w not in STOPWORDS and len(w) > 2}


def overlap(a: str, b: str) -> float:
    """Jaccard overlap of content words. 0.0 when either side is too thin."""
    wa, wb = _content_words(a), _content_words(b)
    if len(wa) < MIN_CONTENT_WORDS or len(wb) < MIN_CONTENT_WORDS:
        return 0.0
    union = wa | wb
    return len(wa & wb) / len(union) if union else 0.0


def parse_learnings(text: str) -> list[Learning]:
    """Parse learning lines out of a learnings-file body."""
    out: list[Learning] = []
    for line in text.splitlines():
        match = LEARNING_LINE_RE.match(line.strip())
        if not match:
            continue
        out.append(Learning(
            trust=match.group("trust") or "",
            type=match.group("type") or "",
            description=(match.group("description") or "").strip(),
            target=(match.group("target") or "").strip(),
            action=(match.group("action") or "").strip(),
            provenance=normalize_provenance(match.group("provenance")) or "",
            kind=normalize_kind(match.group("kind")) or "",
        ))
    return out


def find_contradicted_line(
    learning: Learning, memory_text: str
) -> tuple[int, str, float] | None:
    """Best-matching line in the target file, or None if nothing is confident.

    Returns the single best candidate rather than all above threshold: a
    correction supersedes one fact. Offering the reviewer three "maybe this
    one" options is how a targeted edit turns back into generic consolidation.
    """
    best: tuple[int, str, float] | None = None
    for lineno, line in enumerate(memory_text.splitlines(), 1):
        if _SKIP_LINE_RE.match(line) or not line.strip():
            continue
        score = overlap(learning.description, line)
        if score >= MIN_OVERLAP and (best is None or score > best[2]):
            best = (lineno, line, score)
    return best


def detect_conflicts(
    learnings: list[Learning], memory_contents: dict[str, str]
) -> list[ConflictEdit]:
    """Pair conflict-candidate learnings with the memory lines they contradict."""
    edits: list[ConflictEdit] = []
    for learning in learnings:
        if not learning.is_conflict_candidate or not learning.target:
            continue
        memory_text = memory_contents.get(learning.target)
        if memory_text is None:
            continue  # routed to a file that doesn't exist — generic pass handles it
        found = find_contradicted_line(learning, memory_text)
        if found is None:
            continue
        lineno, old_line, score = found
        edits.append(ConflictEdit(
            target=learning.target, lineno=lineno, old_line=old_line,
            learning=learning, overlap=score))
    # Highest-confidence first: the reviewer's attention is the scarce resource.
    return sorted(edits, key=lambda e: (-e.overlap, e.target, e.lineno))


def render_section(edits: list[ConflictEdit], today: date | None = None) -> str:
    """The ``## Conflict Resolutions`` block, or empty string when there are none.

    Empty rather than a "none found" heading: an empty section in every
    proposal trains the reader to scroll past the place conflicts appear.
    """
    if not edits:
        return ""
    today = today or date.today()
    body = "\n".join(e.render(today) for e in edits)
    return (
        "## Conflict Resolutions\n\n"
        "_Each of these learnings is about a line that already exists in memory. "
        "Review them first — they change facts already recorded, rather than "
        "adding new ones._\n\n"
        "_Matching is deterministic text overlap, which finds the line but "
        "cannot tell you whether the learning **contradicts** it (supersede the "
        "old line) or merely **restates** it (update in place, don't append a "
        "second phrasing). Read both and decide; confirm the matched line is "
        "really the one meant._\n\n"
        f"{body}"
    )


def conflict_section_for(
    learnings_text: str, memory_contents: dict[str, str],
    today: date | None = None,
) -> str:
    """Convenience entry point: learnings text + memory files → proposal section."""
    return render_section(
        detect_conflicts(parse_learnings(learnings_text), memory_contents), today)
