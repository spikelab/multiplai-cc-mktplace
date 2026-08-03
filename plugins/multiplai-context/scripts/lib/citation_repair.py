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


@dataclass(frozen=True)
class Repair:
    """One citation rewritten, or one left alone with the reason why."""

    cited_file: str
    line: int
    resolved_file: str | None  # None when it could not be resolved
    reason: str

    @property
    def repaired(self) -> bool:
        return self.resolved_file is not None


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
        return None, "in range"

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


def repair_citations(
    proposal: str, blocks: Iterable, learnings: dict[str, str]
) -> tuple[str, list[Repair]]:
    """Rewrite provably-wrong, unambiguously-resolvable citation filenames.

    *blocks* are the parsed records of the backlog (``learnings_ledger.Block``),
    *learnings* maps filename to its full text. Returns the proposal and every
    citation that did not verify — repaired or not — so the caller can report
    both rather than repairing silently.
    """
    blocks = list(blocks)
    line_counts = {name: len(text.splitlines()) for name, text in learnings.items()}
    findings: list[Repair] = []

    def substitute(match: re.Match) -> str:
        lo = int(match.group("lo"))
        hi = int(match.group("hi") or lo)
        cited = match.group("file")

        # Check the whole cited range: a citation is only sound if both ends
        # resolve, and repairing on the strength of one end could move a range
        # that legitimately spans nothing.
        resolved_lo, reason = _resolve(cited, lo, blocks, line_counts)
        if resolved_lo is None:
            if reason != "in range":
                findings.append(Repair(cited, lo, None, reason))
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

    return _CITATION_RE.sub(substitute, proposal), findings


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
            f"{len(broken)} citation(s) could not be verified and were left "
            "unchanged — check these by hand before relying on them:"
        )
        out.append("")
        out += [f"- `{f.cited_file}:{f.line}` — {f.reason}" for f in broken]
        out.append("")

    return "\n".join(out).rstrip() + "\n"
