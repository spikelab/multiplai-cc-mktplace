"""Repair ``**Source:**`` citations that name the wrong learnings file.

Dream's proposals cite provenance as ``<learnings-file>:<line>``, and a reviewer
follows those citations to check an entry before applying it. A citation that
points at the wrong file sends them somewhere the content is not, and it fails
silently: the filename is a real file and the line number is a real number.

**The failure this repairs.** Each record is rendered under a ``### File: <name>``
header with its true 1-indexed line numbers, but every record also opens with its
own ``## Session Learnings — <timestamp>`` line. When a session ran past midnight
its record lands in the *next* day's file, and the model sometimes takes the date
from the timestamp instead of the header it was given. Measured on a 283 KB
fixture: 68 of 231 records were exposed to that mismatch and 9 of 461 citations
(2.0%) went wrong — every one of them the same swap, with a correct line number
under a filename one day early.

That the line number survives is what makes a deterministic repair possible: a
line number is only meaningful against the file it was read from, so
``2026-07-28.md:174`` naming content that lives at ``2026-07-29.md:174`` is not a
coincidence to be guessed at — it is a filename substitution with the evidence
still attached.

**Conservative by construction.** A citation is only rewritten when it is both
provably broken (the line does not exist in the file it names, or that file does
not exist) and unambiguously resolvable (exactly one record in exactly one other
file covers that line and carries the cited date in its own timestamp). Anything
else is reported and left alone: a wrong repair is worse than a wrong citation,
because it looks right.

This never invents a citation and never edits anything but the filename in one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

# Both provenance forms the proposal template emits: the block form on entries
# (`**Source:** f.md:12` / `f.md:12-14`) and the inline form used in the
# "Filtered Out" list (`(Source: f.md:12)`). Kept as one pattern so a repair
# cannot fix one and miss the other for the same record.
_CITATION_RE = re.compile(
    r"(?P<prefix>\*\*Source:\*\*\s*|\(Source:\s*)"
    r"(?P<file>[\w.\-]+\.md)"
    r":(?P<lo>\d+)(?:-(?P<hi>\d+))?"
)

# The record's own header, whose date is what the model substitutes for the
# filename. Only the date part matters — the time is what makes the two differ.
_STAMP_RE = re.compile(r"^##\s+Session Learnings\s*[—-]\s*(\d{4}-\d{2}-\d{2})", re.M)

# A learnings filename is its date. Anything else (a renamed or hand-made file)
# simply never matches a stamp, so it is reported rather than repaired.
_FILE_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


# `_resolve` reports soundness through its reason string; comparing against a
# literal in two places is how the range check came to test only one end.
_IN_RANGE = "in range"


@dataclass(frozen=True)
class Repair:
    """One citation rewritten, or one left alone with the reason why."""

    cited_file: str
    # None when the finding is about the file as a whole rather than one
    # citation in it — an unreadable file, where no citation was checked.
    line: int | None
    resolved_file: str | None  # None when it could not be resolved
    reason: str

    @property
    def repaired(self) -> bool:
        return self.resolved_file is not None

    @property
    def where(self) -> str:
        return f"{self.cited_file}:{self.line}" if self.line else self.cited_file


def _stamp_date(block_text: str) -> str | None:
    match = _STAMP_RE.search(block_text)
    return match.group(1) if match else None


def _resolve(
    cited_file: str, line: int, blocks: Sequence, line_counts: dict[str, int]
) -> tuple[str | None, str]:
    """Find the file a broken ``cited_file:line`` citation really refers to.

    Returns ``(resolved_file, reason)``; ``resolved_file`` is None when the
    citation is not broken, or is broken but not unambiguously resolvable.
    """
    known = cited_file in line_counts
    if known and 1 <= line <= line_counts[cited_file]:
        return None, _IN_RANGE

    cited_date_match = _FILE_DATE_RE.match(cited_file)
    if not cited_date_match:
        return None, f"cited file `{cited_file}` is not a dated learnings file"
    cited_date = cited_date_match.group(1)

    # Candidates: records in some OTHER file that cover this line and whose own
    # timestamp carries the date the model used as a filename.
    candidates = {
        b.file
        for b in blocks
        if b.file != cited_file
        and b.start_line <= line <= b.end_line
        and _stamp_date(b.text) == cited_date
    }

    if len(candidates) == 1:
        return candidates.pop(), "timestamp date matched one file"
    if not candidates:
        return None, (
            f"no record at line {line} of any other file carries date {cited_date}"
        )
    return None, (
        f"ambiguous — line {line} matches date {cited_date} in "
        + ", ".join(f"`{c}`" for c in sorted(candidates))
    )


def cited_files(proposal: str) -> set[str]:
    """Every learnings filename a proposal cites, in either provenance form.

    Exposed because retention has to answer "is this file still spoken for?"
    from the proposal text itself. The ledger cannot answer it: it records the
    proposal a block was *first* consolidated into, and a fold-forward moves
    the items to a successor without re-pointing those entries. The citations
    move with the items, so they stay true when the ledger has gone stale.

    Deliberately reuses `_CITATION_RE` rather than re-deriving it — a retention
    check that recognised fewer forms than the repairer would silently protect
    less than it appears to.
    """
    return {m.group("file") for m in _CITATION_RE.finditer(proposal)}


def repair_citations(
    proposal: str,
    blocks: Iterable,
    learnings: dict[str, str],
    unreadable: Iterable[str] = (),
) -> tuple[str, list[Repair]]:
    """Rewrite provably-wrong, unambiguously-resolvable citation filenames.

    *blocks* are the parsed records of the backlog (``learnings_ledger.Block``),
    *learnings* maps filename to its full text. Returns the proposal and every
    citation that did not verify — repaired or not — so the caller can report
    both rather than repairing silently.

    *unreadable* names files that exist but could not be read. They must be
    passed, not merely omitted from *learnings*: absent from *learnings* a file
    is indistinguishable from one that does not exist, and every citation to it
    then looks *provably* broken — the line "does not exist" in a file that was
    never opened. That produced a confirmed false repair in review: a valid
    ``2026-07-28.md:10`` was rewritten to ``2026-07-29.md:10`` on a transient
    read failure, because 07-29 held a past-midnight record stamped 07-28
    covering line 10 — exactly the record shape this module exists to fix. It
    was then listed under "Citation Repairs" as a verified correction, which is
    the one outcome the module must never produce. Citations naming an
    unreadable file are therefore left alone, and the file is reported once so
    the reviewer knows a check was skipped rather than passed.
    """
    blocks = list(blocks)
    unreadable = frozenset(unreadable)
    line_counts = {
        name: len(text.splitlines())
        for name, text in learnings.items()
        if name not in unreadable
    }
    findings: list[Repair] = []
    skipped: set[str] = set()

    def substitute(match: re.Match) -> str:
        lo = int(match.group("lo"))
        hi = int(match.group("hi") or lo)
        cited = match.group("file")

        if cited in unreadable:
            skipped.add(cited)
            return match.group(0)

        # Check the whole cited range: a citation is only sound if both ends
        # resolve, and repairing on the strength of one end could move a range
        # that legitimately spans nothing.
        resolved_lo, reason = _resolve(cited, lo, blocks, line_counts)
        if resolved_lo is None:
            if reason != _IN_RANGE:
                findings.append(Repair(cited, lo, None, reason))
            elif hi != lo:
                # `lo` verified, so nothing here is repairable — but a range is
                # only sound if its tail exists too. Report rather than repair:
                # a citation half of which is right is not evidence of which
                # file the other half meant.
                _, hi_reason = _resolve(cited, hi, blocks, line_counts)
                if hi_reason != _IN_RANGE:
                    findings.append(
                        Repair(
                            cited,
                            lo,
                            None,
                            f"line {lo} verifies but the range ends at {hi}, "
                            f"which does not — {hi_reason}",
                        )
                    )
            return match.group(0)

        resolved_hi, hi_reason = _resolve(cited, hi, blocks, line_counts)
        if hi != lo and resolved_hi != resolved_lo:
            findings.append(
                Repair(cited, lo, None, f"range ends disagree ({hi_reason})")
            )
            return match.group(0)

        findings.append(Repair(cited, lo, resolved_lo, reason))
        span = f"{lo}-{hi}" if match.group("hi") else str(lo)
        return f"{match.group('prefix')}{resolved_lo}:{span}"

    repaired = _CITATION_RE.sub(substitute, proposal)

    # One finding per unreadable file that the proposal actually cites — not
    # one per citation. The reviewer needs to know a check was skipped, and
    # repeating it per citation would bury the citations that genuinely failed.
    findings += [
        Repair(
            name,
            None,
            None,
            "file could not be read, so citations naming it were not checked "
            "(they are left exactly as written)",
        )
        for name in sorted(skipped)
    ]

    return repaired, findings


def render_findings(findings: Sequence[Repair]) -> str:
    """The ``## Citation Repairs`` section, or '' when every citation verified.

    Unresolvable citations are listed too. A reviewer who cannot tell a repaired
    citation from a broken one has to re-check all of them, which is the state
    this module exists to get out of.
    """
    if not findings:
        return ""

    fixed = [f for f in findings if f.repaired]
    broken = [f for f in findings if not f.repaired]
    out = ["## Citation Repairs", ""]

    if fixed:
        out.append(
            f"{len(fixed)} **Source:** citation(s) named a file the cited line does "
            "not exist in, and were corrected to the file whose record carries that "
            "line and that date:"
        )
        out.append("")
        out += [
            f"- `{f.cited_file}:{f.line}` → `{f.resolved_file}:{f.line}`"
            for f in fixed
        ]
        out.append("")

    if broken:
        out.append(
            f"{len(broken)} item(s) could not be verified and were left "
            "unchanged — check these by hand before relying on them. An entry "
            "naming a file rather than a line is a file that could not be "
            "read, so none of its citations were checked:"
        )
        out.append("")
        out += [f"- `{f.where}` — {f.reason}" for f in broken]
        out.append("")

    return "\n".join(out).rstrip() + "\n"
