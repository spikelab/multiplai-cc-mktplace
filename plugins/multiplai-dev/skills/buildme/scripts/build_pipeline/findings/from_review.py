"""`findings --from-review <review_dir>`: the fixer reads the review's own
findings (plan-factory-merged-2026-10-29.md, items 16-17).

An epic's review posts as a GitHub App, which `findings.authors` does not
allow, so the fixer acted on none of its comments. With `--from-review`, the
blockers are read from `<review_dir>/findings.json`, chosen as `--auto` posts
them (`decisions.chosen_ids`, with `--severities` and `--code-only`). A
fetched comment whose review marker names one of those ids is acted on
whatever its author, and is acted on again in a later round while the review
still lists it. Every other comment is still allowed by `findings.authors`
and `findings.allow_bots` alone, and every reply still goes under the
comment.

The round saves the review's head and the ids (`FindingsRound.review_head`,
`expected`) when it starts, and one decision per id as the step that settles
it finishes (`decide`): `fixed` with its commit, `not a defect` with the
reason, `spec change` with the `spec-issues.md` heading, or `still open` with
why. A saved decision is never replaced. An id with none when the fixer ends
is printed as `FINDINGS:INGESTION_ERROR:<ids>` (`undecided`).
"""

from __future__ import annotations

import json
from pathlib import Path

from ..state import FindingsRound
from .decisions import chosen_ids
from .models import Verdict

FIXED = "fixed"
NOT_DEFECT = "not a defect"
SPEC_CHANGE = "spec change"
STILL_OPEN = "still open"

INGESTION_ERROR = "FINDINGS:INGESTION_ERROR"
REVIEW_CHANGED = "FINDINGS:REVIEW_CHANGED"


def blockers(
    review_dir: str | Path,
    severities: tuple[str, ...] | None = None,
    code_only: bool = False,
) -> tuple[str, list[str]]:
    """(the reviewed head, the ids `--auto` posts) from
    `<review_dir>/findings.json`. Raises `FileNotFoundError` when the file is
    missing and `ValueError` when it cannot be read as the review's findings."""
    source = Path(review_dir) / "findings.json"
    if not source.is_file():
        raise FileNotFoundError(f"no findings.json at {source}")
    try:
        doc = json.loads(source.read_text(encoding="utf-8"))
        head = str((doc.get("target") or {}).get("head_sha") or "")
        ids = chosen_ids(doc, severities, code_only)
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        KeyError,
        TypeError,
        AttributeError,
    ) as e:
        raise ValueError(f"cannot read {source}: {type(e).__name__}: {e}") from e
    return head, ids


def _decision(rnd: FindingsRound, fid: str) -> dict[str, str] | None:
    """The decision on finding *fid* that the round's finished steps settle;
    None while none does, or when no triaged comment of the round names it."""
    from .publish import records_of

    record = next(
        (
            r
            for r in records_of(rnd)
            if r.comment.finding_id == fid and r.triage is not None
        ),
        None,
    )
    if record is None or "triage" not in rnd.finished:
        return None
    t, key = record.triage, record.comment.key
    if t.verdict == Verdict.NOT_DEFECT:
        return {"decision": NOT_DEFECT, "reason": t.reason}
    if t.verdict == Verdict.QUESTION:
        return {"decision": STILL_OPEN, "reason": f"a question: {t.reason}"}
    if t.verdict == Verdict.SPEC_CHANGE or t.spec_conflict:
        if "spec" not in rnd.finished:
            return None
        issue = next((s for s in rnd.spec_issues if s.finding_key == key), None)
        if issue is None:
            return {
                "decision": STILL_OPEN,
                "reason": "no requirements issue was recorded",
            }
        return {"decision": SPEC_CHANGE, "heading": issue.heading}
    if "code" not in rnd.finished:
        return None
    fix = rnd.fixes.get(key) or {}
    if fix.get("impl"):
        return {"decision": FIXED, "commit": fix["impl"]}
    return {
        "decision": STILL_OPEN,
        "reason": fix.get("failed") or "the fix did not produce a commit",
    }


def decide(rnd: FindingsRound) -> bool:
    """Save a decision for each expected id that has none and that the round's
    finished steps settle. True when one was added."""
    added = False
    for fid in rnd.expected:
        if fid in rnd.decisions:
            continue
        found = _decision(rnd, fid)
        if found is not None:
            rnd.decisions[fid] = found
            added = True
    return added


def undecided(rnd: FindingsRound) -> list[str]:
    """The round's expected ids with no saved decision, in the review's order."""
    return [fid for fid in rnd.expected if fid not in rnd.decisions]


def ingestion_error(ids: list[str]) -> str:
    return f"{INGESTION_ERROR}:{','.join(ids)}"
