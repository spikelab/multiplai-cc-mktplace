#!/usr/bin/env python3
"""Staleness lint for memory files.

Memory files carry a single file-level ``**Last Updated:**`` stamp. That tells
you when the file was touched, not whether any particular fact in it is still
true — and the facts that go stale are never the whole file. A price, a version
number, an employer, a "current best X": each rots on its own schedule while
the file around it stays accurate, and the file stamp keeps saying "fresh".

The convention this enforces is one suffix on the fact itself:

    (as of 2026-07)                       — when the fact was true
    (as of 2026-07, review by 2026-10)    — and when to re-check it

Three checks:

  expired    a ``review by`` date that has passed
  unmarked   a volatile-class fact with no ``as of`` at all
  duplicate-h2  the same H2 section name in more than one memory file

The third is not about staleness. It guards the corpus-wide uniqueness rule
stated in the memory ``CLAUDE.md`` — *"H2 section names must be unique
corpus-wide. Duplicate top-level section names across memory files break
deterministic routing."* That rule was unenforced while it only mattered for
humans skimming. It stopped being cosmetic once catalog generation started
emitting ``section_anchors``: the router picks ``file.md#Section``, so two
files both offering ``## Overview`` make the pick ambiguous, and whichever
file the router names is the one whose ``Overview`` gets loaded — including
the wrong one.

Deliberately warn-only and non-rewriting. A linter that edits memory would be
applying unreviewed changes to the one artifact the whole pipeline keeps behind
human review, and the volatile-class patterns below are heuristics — they will
have false positives, and the cost of a false positive must stay "one noisy
line in a report", never "a fact silently rewritten".

Exit codes:
    0 — no findings
    1 — at least one finding
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# `(as of YYYY-MM[-DD][, review by YYYY-MM[-DD]])`. Day is optional on both:
# most memory facts are month-granular and forcing a day invents precision.
AS_OF_RE = re.compile(
    r"\(as of (?P<as_of>\d{4}-\d{2}(?:-\d{2})?)"
    r"(?:\s*,\s*review by (?P<review_by>\d{4}-\d{2}(?:-\d{2})?))?\)",
    re.IGNORECASE)

# Volatile-fact detection is a CONJUNCTION, not a disjunction, and this is the
# whole design.
#
# The first draft fired on any line containing a version number, a currency
# amount, or the word "current". Run against the real memory tree it produced
# 195 findings, of which essentially all were wrong: "Swift 6.3 rejects
# covariant Self" is a permanent technical fact that happens to name a version;
# "<200 files" is a threshold, not a price; "currently" appears in ordinary
# prose on every other line.
#
# What actually rots is a fact that makes a claim about *now* AND names a
# *specific changeable value*. "The current Opus model is 4.5" rots. "Swift 6.3
# rejects covariant Self" does not. So a line must match both halves.

# Half one: the sentence claims to describe the present moment.
CURRENCY_RE = re.compile(
    r"\b(?:current(?:ly)?|at present|as of now|right now|these days|nowadays|"
    r"latest|newest|most recent|state of the art|"
    r"best (?:available|option|choice|model|tool)|"
    r"works? at|employed (?:at|by)|now (?:costs?|uses?|runs?|lives?))\b",
    re.IGNORECASE)

# Half two: a specific value that can be superseded.
CONCRETE_CLASSES: list[tuple[str, re.Pattern, str]] = [
    ("price",
     re.compile(r"(?:[$€£]\s?\d[\d,.]*(?:[KkMm]\b)?"
                r"|\b\d[\d,.]*\s?(?:USD|EUR|GBP)\b)"),
     "prices change without announcement"),
    ("version",
     re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b(?!\s*%)"),
     "version numbers are superseded silently"),
    ("employer",
     re.compile(r"\b(?:works? at|employed (?:at|by))\s+[A-Z]", re.IGNORECASE),
     "employment changes and the old fact reads as current"),
]

# Deadlines are the exception to the conjunction rule: a deadline is volatile by
# definition, and it carries its own date, so no currency language is needed.
# It must name an actual date to fire — "don't automate end-to-end on day one"
# mentions no deadline, it just uses the word "day".
DEADLINE_RE = re.compile(
    r"\b(?:deadline|due (?:by|on)|expires? on|renewal|ends? on|valid until)\b"
    r"[^.\n]{0,40}?\b(?:\d{4}-\d{2}(?:-\d{2})?|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}?,?\s*\d{4})",
    re.IGNORECASE)

# Lines that are structure, not facts.
_SKIP_LINE_RE = re.compile(r"^\s*(?:#{1,6}\s|```|\||-{3,}\s*$|\*\*Last Updated)")

_FENCE_RE = re.compile(r"^\s*```")

# Same shape as ``lib/section_loader._H2_RE``, kept local so this file stays a
# runnable standalone script (it is invoked as ``python lib/memory_lint.py``,
# where a sibling ``lib.*`` import does not resolve). Divergence here costs a
# missing or spurious *warning* — the generator path, where a mismatched name
# would silently degrade routing forever, imports the canonical parser instead.
_H2_RE = re.compile(r"^##\s+(.+?)\s*$")

# How old an `(as of ...)` with no `review by` gets before it is reported. Twelve
# months is chosen to be uncontroversial: it is long enough that nobody is
# nagged about a fact they stamped this year, and short enough that a stamp from
# a previous era stops passing as fresh.
AS_OF_STALE_DAYS = 365


@dataclass(frozen=True)
class Finding:
    path: Path
    lineno: int
    kind: str          # "expired" | "unmarked" | "undated" | "duplicate-h2"
    fact_class: str
    line: str
    detail: str

    def render(self, root: Path | None = None) -> str:
        where = self.path
        if root:
            try:
                where = self.path.relative_to(root)
            except ValueError:
                pass
        excerpt = self.line.strip()
        if len(excerpt) > 100:
            excerpt = excerpt[:97] + "..."
        return f"{where}:{self.lineno}: {self.kind} [{self.fact_class}] {self.detail}\n    {excerpt}"


def _parse_stamp(value: str) -> date:
    """`YYYY-MM` means the *end* of that month, not the 1st.

    A fact marked `review by 2026-10` is not overdue on 2026-10-01 — the whole
    month is the review window. Treating it as the 1st would make every
    month-granular annotation fire up to 31 days early, which is exactly the
    kind of early noise that gets a lint switched off.
    """
    parts = value.split("-")
    if len(parts) == 3:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    year, month = int(parts[0]), int(parts[1])
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


# A fact the author has already marked as past. Without this, `**US cost base
# (historical, no longer current):** ~$4,500/month` fires — on the word
# "current" inside the phrase disclaiming it. Asking someone to date-stamp a
# fact they have explicitly labelled stale is the lint arguing with a reader
# who already did the right thing.
HISTORICAL_RE = re.compile(
    r"\b(?:historical|no longer current|formerly|previously|used to be|"
    r"deprecated|superseded|obsolete|was:|until \d{4})\b", re.IGNORECASE)


def classify(line: str) -> list[tuple[str, str]]:
    """Which volatile classes this line belongs to, if any.

    A dated deadline qualifies on its own. Everything else needs both a claim
    about the present and a concrete value — see the note above CURRENCY_RE for
    why the disjunctive version of this was unusable.
    """
    if HISTORICAL_RE.search(line):
        return []
    if DEADLINE_RE.search(line):
        return [("deadline", "a passed deadline is worse than no deadline")]
    if not CURRENCY_RE.search(line):
        return []
    return [(name, why) for name, pattern, why in CONCRETE_CLASSES
            if pattern.search(line)]


def lint_text(text: str, path: Path, today: date) -> list[Finding]:
    findings: list[Finding] = []
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        # Code blocks are full of version numbers and prices that are examples,
        # not claims about the world.
        if in_fence or _SKIP_LINE_RE.match(line) or not line.strip():
            continue

        match = AS_OF_RE.search(line)
        if match:
            review_by = match.group("review_by")
            if review_by:
                if _parse_stamp(review_by) < today:
                    findings.append(Finding(
                        path=path, lineno=lineno, kind="expired",
                        fact_class="annotated", line=line,
                        detail=f"review by {review_by} has passed — re-verify or re-stamp"))
                continue
            # `(as of ...)` with NO `review by`: the author dated the fact but
            # never said when it stops being trustworthy, so nothing can ever
            # expire it. `(as of 2019-01)` would stay permanently clean — and
            # the premise of this whole linter is that facts rot on their own
            # schedule. Reported as its OWN kind, not folded into `expired`:
            # nothing has passed, so calling it expired would misdescribe it
            # and blur a real deadline miss with a missing annotation.
            as_of = _parse_stamp(match.group("as_of"))
            age_days = (today - as_of).days
            if age_days > AS_OF_STALE_DAYS:
                findings.append(Finding(
                    path=path, lineno=lineno, kind="undated",
                    fact_class="annotated", line=line,
                    detail=f"'as of {match.group('as_of')}' is {age_days // 30} "
                           f"months old with no 'review by' — nothing can expire "
                           f"it; add 'review by YYYY-MM'"))
            continue  # annotated: the author already made a claim about freshness

        for name, why in classify(line):
            findings.append(Finding(
                path=path, lineno=lineno, kind="unmarked",
                fact_class=name, line=line,
                detail=f"{why}; add '(as of YYYY-MM[, review by YYYY-MM])'"))
            break  # one finding per line — the first class is enough to act on
    return findings


def _h2_positions(text: str) -> list[tuple[str, int]]:
    """``(header text, 1-based line number)`` for each H2, code fences skipped.

    Uses the same ``## `` shape ``section_loader`` matches on, so what this
    reports as a duplicate is exactly what would be ambiguous to resolve.
    Fenced blocks are excluded: a ``## `` inside a shell snippet is a
    comment, not a section, and would produce findings nobody can act on.
    """
    out: list[tuple[str, int]] = []
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _H2_RE.match(line)
        if m:
            header = m.group(1).strip()
            if header:
                out.append((header, lineno))
    return out


def find_duplicate_h2(texts: dict[Path, str]) -> list[Finding]:
    """Report H2 names that appear in more than one file.

    Warn-only, like the rest of this module: it names the collision and
    the other files holding it, and renames nothing. Which of two
    colliding sections should be retitled is a judgement about what the
    memory *means*, and this module's standing rule is that it never
    edits the corpus.

    A name repeated *within* one file is not reported here — that is one
    addressable section as far as ``extract_section`` is concerned (it
    returns the first match), not a cross-file routing ambiguity.
    """
    by_name: dict[str, list[tuple[Path, int, str]]] = {}
    for path in sorted(texts):
        seen_in_file: set[str] = set()
        for header, lineno in _h2_positions(texts[path]):
            key = header.lower()
            if key in seen_in_file:
                continue
            seen_in_file.add(key)
            by_name.setdefault(key, []).append((path, lineno, header))

    findings: list[Finding] = []
    for key, occurrences in sorted(by_name.items()):
        if len(occurrences) < 2:
            continue
        names = [p.name for p, _, _ in occurrences]
        for path, lineno, header in occurrences:
            others = [n for n in names if n != path.name] or names
            findings.append(Finding(
                path=path, lineno=lineno, kind="duplicate-h2",
                fact_class="section", line=f"## {header}",
                detail=f"'{header}' is also an H2 in {', '.join(sorted(set(others)))}"
                       f" — a '#{header}' section pick is ambiguous; retitle one"))
    return findings


def lint_dir(memory_dir: Path, today: date | None = None) -> list[Finding]:
    today = today or date.today()
    findings: list[Finding] = []
    texts: dict[Path, str] = {}
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == "CLAUDE.md":
            continue  # the index, not a fact store
        text = path.read_text(encoding="utf-8")
        texts[path] = text
        findings.extend(lint_text(text, path, today))
    findings.extend(find_duplicate_h2(texts))
    return findings


def summarize(findings: list[Finding], root: Path | None = None) -> str:
    if not findings:
        return "memory_lint: clean"
    expired = [f for f in findings if f.kind == "expired"]
    unmarked = [f for f in findings if f.kind == "unmarked"]
    undated = [f for f in findings if f.kind == "undated"]
    duplicate = [f for f in findings if f.kind == "duplicate-h2"]
    lines: list[str] = []
    if expired:
        lines.append(f"## Expired ({len(expired)})")
        lines.extend(f.render(root) for f in expired)
        lines.append("")
    if undated:
        lines.append(f"## Stamped but never expiring ({len(undated)})")
        lines.extend(f.render(root) for f in undated)
        lines.append("")
    if unmarked:
        lines.append(f"## Missing validity annotation ({len(unmarked)})")
        lines.extend(f.render(root) for f in unmarked)
        lines.append("")
    if duplicate:
        lines.append(f"## Duplicate H2 section names ({len(duplicate)})")
        lines.extend(f.render(root) for f in duplicate)
        lines.append("")
    lines.append(f"memory_lint: {len(expired)} expired, {len(undated)} undated, "
                 f"{len(unmarked)} unmarked, {len(duplicate)} duplicate-h2")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Lint memory files for stale and unannotated volatile facts.")
    ap.add_argument("memory_dir", nargs="?", type=Path, default=None,
                    help="memory directory (default: the configured one)")
    ap.add_argument("--expired-only", action="store_true",
                    help="report only passed 'review by' dates, not missing annotations")
    ap.add_argument("--today", default=None,
                    help="YYYY-MM-DD to evaluate against (testing)")
    args = ap.parse_args(argv)

    memory_dir = args.memory_dir
    if memory_dir is None:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from multiplai_core.paths import get_paths  # type: ignore
            memory_dir = get_paths().memory_dir()
        except Exception:
            print("memory_lint: no memory directory given and none configured",
                  file=sys.stderr)
            return 1

    if not memory_dir.is_dir():
        print(f"memory_lint: not a directory: {memory_dir}", file=sys.stderr)
        return 1

    today = (date.fromisoformat(args.today) if args.today else date.today())
    findings = lint_dir(memory_dir, today)
    if args.expired_only:
        findings = [f for f in findings if f.kind == "expired"]

    print(summarize(findings, root=memory_dir))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
