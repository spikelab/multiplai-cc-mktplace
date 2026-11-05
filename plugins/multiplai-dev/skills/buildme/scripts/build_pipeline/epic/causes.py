"""Why a child stopped short of merged: one cause from a fixed list.

The coordinator writes a child's `reason` as text at about forty places
(`coordinator.py`, `fix.py`). `cause_of` sorts that text into the fixed
`CAUSES` below by the words each place starts its text with, so runs of
different versions can be counted by cause in the run summary and the shared
run row (`multiplai_core.runs`). `PATTERNS` lists, for each cause, the
words it matches and the module that writes them;
`tests/test_epic_causes.py` checks that every pattern still appears in that
module's source, so a reworded message fails a test instead of falling into
`other` unseen.

A dependant held back by another child (`dependency`) is counted apart from
the causes of the child that held it.
"""

from __future__ import annotations

from .. import intake, readiness

# (cause, module whose source writes the words, the words, how they match:
# "start" for text that begins with them, "in" for text holding them), in
# match order: the first match wins, so the fixer's narrower causes come
# before `fixer-failed`, whose text holds theirs.
PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    ("spec-issues", "readiness", f"{readiness.MARKER_ISSUES}:", "start"),
    ("spec-not-ready", "readiness", f"{readiness.MARKER_NOT_READY}:", "start"),
    ("plan-waiting", "intake", f"{intake.MARKER_PLAN_WAITING}:", "start"),
    ("split-required", "intake", f"{intake.MARKER_SPLIT_REQUIRED}:", "start"),
    (
        "plan-blocked",
        "epic.coordinator",
        "its planning check is blocked at this plan revision",
        "in",
    ),
    ("review-cap", "epic.coordinator", "review round ", "start"),
    (
        "fixer-undecided",
        "epic.coordinator",
        "the fixer left review findings with no decision",
        "start",
    ),
    ("fixer-left-blockers", "epic.coordinator", "the fixer ended (", "start"),
    (
        "baseline-tests-failing",
        "findings.loop",
        "test suite fails before any fix",
        "in",
    ),
    ("final-verification-failed", "findings.loop", "Final verification failed", "in"),
    ("fixer-failed", "epic.fix", "the fixer failed at ", "start"),
    ("merge-tests-failed", "epic.coordinator", "merge tests failed: ", "start"),
    ("merge-conflict", "epic.coordinator", "conflicts in ", "in"),
    ("merge-unknown", "epic.coordinator", "cannot merge: ", "start"),
    ("no-pr", "epic.coordinator", "no PR opened", "start"),
    ("pr-closed", "epic.coordinator", "PR closed without a merge", "start"),
    ("pr-unreadable", "epic.coordinator", "cannot read its PR's state", "start"),
    ("dependency", "epic.coordinator", "dependency ", "start"),
    ("dependency", "epic.coordinator", "waiting on ", "start"),
    ("timeout", "epic.coordinator", "timed out after", "in"),
    ("exit-code", "epic.coordinator", "exit ", "start"),
)
# Matched anywhere in the text, before PATTERNS: a usage limit can end any step.
USAGE_LIMIT_WORDS = ("usage limit", "rate limit", "rate_limit")

CAUSES = tuple(dict.fromkeys(["usage-limit", *(p[0] for p in PATTERNS), "other"]))

# Statuses a child reaches without stopping short.
NO_CAUSE = frozenset({"pending", "running", "built", "reviewing", "merged"})


def cause_of(status: str, reason: str) -> str | None:
    """The cause of a child at *status* with *reason*: None for a child not
    stopped short (`NO_CAUSE`), else one of `CAUSES`."""
    if status in NO_CAUSE:
        return None
    text = reason or ""
    if any(w in text.lower() for w in USAGE_LIMIT_WORDS):
        return "usage-limit"
    for cause, _, words, how in PATTERNS:
        if text.startswith(words) if how == "start" else words in text:
            return cause
    return "other"
