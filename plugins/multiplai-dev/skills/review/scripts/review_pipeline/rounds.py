"""Earlier rounds of a review: kept under `<out>/<slug>/rounds/`, read by repeats and assess.

A PR is reviewed into the same `<out>/<slug>/` each time, so a new run would
write over the last round's `findings.json`. Before it does, `keep_round`
copies that round's files to `rounds/<old head_sha[:12]>/`, also when the new
run is on the same head (a rerun), so the repeat and assess stages have that
run to compare with. A second round on one head goes to `<sha12>-2/`, and so
on. Nothing there is ever deleted or overwritten.

A finding the repeats stage matched to a rejected one gets no entry of its
own in `decisions.json`; its `repeat` assessment records the earlier
decision. `load_rounds` reads that as the finding's own decision, so a later
round's copy of the reworded finding is matched by id as well.

The person's decisions live in `viewer/decisions.json` (written by
review-viewer), one entry per finding id, and persist across rounds. Because
they are keyed by id alone, a decision is applied to an earlier round's copy
of a finding only when it was made before a later round replaced that one
(its `ts` is not after the next round's `generated_at`). A decision made while
the current round was shown is never read back as an earlier one. A decision
or round with no readable time is applied, as before 0.27.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

ROUNDS_DIR = "rounds"
SHOWN_STATUSES = ("confirmed", "unverifiable")


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        if path.exists():
            log.warning("Unreadable %s: %s", path, e)
        return None
    return data if isinstance(data, dict) else None


def _round_dir(target_dir: Path, head: str, generated_at: str) -> Path:
    """`rounds/<sha12>/`, or `<sha12>-<n>/` when that holds another round of the same head.

    A directory whose `findings.json` has the same `generated_at`, or none
    readable (a copy cut short), is this round's: it is reused, never replaced.
    """
    n = 1
    while True:
        d = target_dir / ROUNDS_DIR / (head[:12] if n == 1 else f"{head[:12]}-{n}")
        if not d.exists():
            return d
        kept = _read_json(d / "findings.json") or {}
        if not kept.get("generated_at") or str(kept["generated_at"]) == generated_at:
            return d
        n += 1


def keep_round(target_dir: Path, slug: str, new_head_sha: str) -> Path | None:
    """Copy the last round's outputs to `rounds/<sha12>/` before a new run writes over them.

    The new run may be on the same head (a rerun): that round is kept too.
    Returns the round directory, or None when there was nothing to keep (no
    findings.json). `resume` does not call this. Files already in that round
    directory are left as they are, so a run that started, stopped and was
    started again keeps one copy.
    """
    findings = target_dir / "findings.json"
    data = _read_json(findings)
    if data is None:
        return None
    old_head = str((data.get("target") or {}).get("head_sha") or "")
    if not old_head:
        return None
    round_dir = _round_dir(target_dir, old_head, str(data.get("generated_at") or ""))
    round_dir.mkdir(parents=True, exist_ok=True)
    for name in ("findings.json", f"review-{slug}.md", "checks.json"):
        src, dest = target_dir / name, round_dir / name
        if src.is_file() and not dest.exists():
            shutil.copy2(src, dest)
    return round_dir


def load_decisions(target_dir: Path) -> dict[str, dict]:
    """`viewer/decisions.json`: {finding id: {decision, note, ts}}, or {}."""
    data = _read_json(target_dir / "viewer" / "decisions.json")
    return {k: v for k, v in (data or {}).items() if isinstance(v, dict)}


@dataclass
class EarlierFinding:
    """A finding from an earlier round, with the person's decision on it."""
    finding: dict  # the findings.json v1 row
    head_sha: str  # the round it came from
    decision: str  # "accept", "reject", "defer", or "" when none was recorded
    note: str

    @property
    def id(self) -> str:
        return self.finding["id"]


def _when(text) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def _made_by(decision: dict, until: datetime | None) -> dict:
    """*decision* when it was made no later than *until*, else {} (made in a later round)."""
    ts = _when(decision.get("ts") or "")
    if until is None or ts is None or ts <= until:
        return decision
    return {}


def _carried(f: dict) -> dict:
    """The rejection a `repeat` assessment records, as a decision: {} for any other finding."""
    a = f.get("assessment") or {}
    if a.get("label") == "repeat" and a.get("earlier_decision") == "reject":
        return {"decision": "reject", "note": a.get("earlier_note") or ""}
    return {}


def load_rounds(target_dir: Path, current_head: str = "") -> list[EarlierFinding]:
    """Every finding in `rounds/*/findings.json`, the latest round's copy of each id.

    Rounds are ordered by `generated_at`; a round on *current_head* counts
    (a rerun keeps one). Each copy gets the person's decision only when it was
    made before the next round was generated; for the last earlier round, the
    next round is the top-level `findings.json` when it is *current_head*'s
    and not itself a kept round (a finished review, as `assess-only` reads
    it), else no limit (a run that has not exported). A copy with no decision
    of its own takes the one its `repeat` assessment carried over.
    """
    rounds = []
    for path in sorted((target_dir / ROUNDS_DIR).glob("*/findings.json")):
        data = _read_json(path)
        if data is None:
            continue
        head = str((data.get("target") or {}).get("head_sha") or "")
        rounds.append((str(data.get("generated_at") or ""), head, data.get("findings") or []))
    rounds.sort(key=lambda r: r[0])
    current = _read_json(target_dir / "findings.json") or {}
    current_at = None
    if (current_head and str((current.get("target") or {}).get("head_sha") or "") == current_head
            and str(current.get("generated_at") or "") not in {r[0] for r in rounds}):
        current_at = _when(current.get("generated_at") or "")
    ends = [_when(r[0]) for r in rounds[1:]] + [current_at]
    decisions = load_decisions(target_dir)
    by_id: dict[str, EarlierFinding] = {}
    for (_, head, findings), until in zip(rounds, ends):
        for f in findings:
            if not isinstance(f, dict) or not f.get("id"):
                continue
            d = _made_by(decisions.get(f["id"]) or {}, until) or _carried(f)
            by_id[f["id"]] = EarlierFinding(finding=f, head_sha=head, decision=str(d.get("decision") or ""),
                                            note=str(d.get("note") or ""))
    return list(by_id.values())


def rejected(earlier: list[EarlierFinding]) -> list[EarlierFinding]:
    """Earlier findings the person rejected."""
    return [e for e in earlier if e.decision == "reject"]


def accepted_or_open(earlier: list[EarlierFinding]) -> list[EarlierFinding]:
    """Earlier shown findings the person accepted or has not decided (deferred counts as not decided)."""
    return [e for e in earlier
            if e.decision != "reject" and e.finding.get("status") in SHOWN_STATUSES]
