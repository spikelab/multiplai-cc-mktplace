"""Regex counts of how the specs state outcomes, written to the progress file.

Each consistency check that reads the specs appends one line to
`.pm-spec/progress.md` (`SPEC_COUNTS`) counting, over every specs file:

- refusals with no status: a THEN (or AND) line that refuses, rejects or
  denies something and names no HTTP status (a 4xx or 5xx number) and quotes
  no message;
- "as before" THENs with no baseline: a THEN line that says a behaviour
  stays as before, unchanged or as today, and does not say how a baseline is
  captured;
- "as today" statements with no named function: a line about today's
  behaviour with no `name(` or `.py` in backticks;
- commit pins: a hex string of 7 to 40 characters with a letter and a digit.

The counts are advisory: nothing blocks on them. They show whether the specs
prompt's outcome rules are followed, run after run.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import layout

_REFUSAL = re.compile(r"\b(refus|reject|den(y|ies|ied)|forbid)", re.IGNORECASE)
_STATUS = re.compile(r"\b[45]\d\d\b")
_QUOTED = re.compile(r'"[^"]+"|“[^”]+”')
_THEN = re.compile(r"^\s*-\s*\*\*(THEN|AND)\*\*", re.IGNORECASE)
_AS_BEFORE = re.compile(
    r"\b(as before|as it does today|as today|unchanged|same as before)\b",
    re.IGNORECASE,
)
_BASELINE = re.compile(
    r"\b(baseline|captured|recorded before|snapshot)\b", re.IGNORECASE
)
_TODAY = re.compile(r"\b(as today|today's|today)\b", re.IGNORECASE)
_NAMED = re.compile(r"`[^`]*(\w\(|\.py\b)[^`]*`")
_PIN = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")

LABELS = {
    "refusal_no_status": "refusals with no status",
    "as_before_no_baseline": '"as before" THENs with no baseline',
    "today_no_function": '"as today" statements with no named function',
    "commit_pins": "commit pins",
}


def count_text(text: str) -> dict[str, int]:
    """The four counts over one specs file's text."""
    counts = dict.fromkeys(LABELS, 0)
    for line in text.splitlines():
        then = bool(_THEN.match(line))
        if (
            then
            and _REFUSAL.search(line)
            and not _STATUS.search(line)
            and not _QUOTED.search(line)
        ):
            counts["refusal_no_status"] += 1
        if then and _AS_BEFORE.search(line) and not _BASELINE.search(line):
            counts["as_before_no_baseline"] += 1
        if _TODAY.search(line) and not _NAMED.search(line):
            counts["today_no_function"] += 1
        counts["commit_pins"] += len(_PIN.findall(line))
    return counts


def count_specs(change_dir: Path) -> dict[str, int]:
    """The four counts summed over every specs file of the change."""
    total = dict.fromkeys(LABELS, 0)
    for unit in layout.units(change_dir):
        for key, n in count_text((Path(change_dir) / unit).read_text()).items():
            total[key] += n
    return total


def counts_line(counts: dict[str, int]) -> str:
    """One line: `label: n; ...` in a fixed order."""
    return "; ".join(f"{LABELS[k]}: {counts[k]}" for k in LABELS)
