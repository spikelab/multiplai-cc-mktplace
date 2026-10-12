"""Earlier rounds of a review: kept under `<out>/<slug>/rounds/`, read by repeats and assess.

A PR is reviewed into the same `<out>/<slug>/` each time, so a new run would
write over the last round's files. Before it does, `keep_round` copies every
file and directory of that round (`findings.json`, the review, `checks.json`,
`progress.log`, `review-state.json`, `diff.patch`, `commits.txt`, ... but not
`rounds/`, `viewer/` or the snapshots git can rebuild: `tree/`, `base-tree/`,
`assess-tree/`) to `rounds/<NN>-<old head_sha[:12]>/`, also when the new run is
on the same head (a rerun), so the repeat and assess stages have that run to
compare with. `<NN>` is the two-digit round number, one more than the
directories already in `rounds/`, so a second run on one head gets the next
number. The copy is made under a hidden name and renamed when complete.
Nothing there is ever deleted or overwritten. Rounds kept before 0.33 are
named `<sha12>/` and `<sha12>-2/`; they are still read, ordered by
`generated_at`.

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
import re
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


# Not copied into a kept round: the rounds themselves, the viewer's state (it
# persists across rounds where it is), and head/base snapshots git rebuilds.
NOT_KEPT = frozenset({ROUNDS_DIR, "viewer", "tree", "base-tree", "assess-tree"})
PARTIAL_PREFIX = ".partial-"


def _round_dirs(target_dir: Path) -> list[Path]:
    """Every kept round's directory, partial copies left out."""
    root = target_dir / ROUNDS_DIR
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith("."))


def _round_key(data: dict) -> tuple[str, str]:
    """What tells two rounds apart: `generated_at` (whole seconds) and the run's start (milliseconds)."""
    return str(data.get("generated_at") or ""), str((data.get("run") or {}).get("started_at") or "")


def round_number(target_dir: Path) -> int:
    """The number the next kept round gets, which is also the current run's: one more than the rounds kept."""
    dirs = _round_dirs(target_dir)
    n = len(dirs) + 1
    while any(d.name.startswith(f"{n:02d}-") for d in dirs):
        n += 1
    return n


def _kept_already(target_dir: Path, data: dict) -> Path | None:
    """The round directory that already holds the round in *data*, if one does."""
    key = _round_key(data)
    for d in _round_dirs(target_dir):
        kept = _read_json(d / "findings.json")
        if kept is not None and _round_key(kept) == key:
            return d
    return None


def _name_head(name: str) -> str:
    """The 12-character head a round directory's name gives, or ""."""
    m = re.fullmatch(r"(?:\d{2,}-)?([0-9a-f]{12})(?:-\d+)?", name)
    return m.group(1) if m else ""


def round_name(target_dir: Path, findings_file: dict) -> str:
    """The round id `post` puts in its PR markers: the round's head sha cut to 12, `-k` for its k-th round.

    Read-only. The id does not change when a later run keeps the round under
    `rounds/<NN>-<sha12>/`: the first round of a head is `<sha12>`, a later
    one on the same head `<sha12>-2`, `-3`, ... (the v1 marker format), counted
    in `generated_at` order over the kept rounds of that head.
    """
    head = str((findings_file.get("target") or {}).get("head_sha") or "")
    key = _round_key(findings_file)
    keys = []
    for d in _round_dirs(target_dir):
        kept = _read_json(d / "findings.json")
        if kept is None:
            continue
        kept_head = str((kept.get("target") or {}).get("head_sha") or "")
        # A round whose findings.json names no head: its directory name does (`<sha12>`, `<sha12>-2`, `<NN>-<sha12>`).
        same = kept_head == head if kept_head else _name_head(d.name) == head[:12]
        if same:
            keys.append(_round_key(kept))
    keys.sort()
    k = keys.index(key) + 1 if key in keys else len(keys) + 1
    return head[:12] if k == 1 else f"{head[:12]}-{k}"


def keep_round(target_dir: Path, slug: str, new_head_sha: str) -> Path | None:
    """Copy the last round in full to `rounds/<NN>-<sha12>/` before a new run writes over it.

    The new run may be on the same head (a rerun): that round is kept too.
    Returns the round directory, or None when there was nothing to keep (no
    findings.json). `resume` does not call this. A round already kept (same
    `generated_at` and run start) is not copied again, so a run that started,
    stopped and was started again keeps one copy.
    """
    data = _read_json(target_dir / "findings.json")
    if data is None:
        return None
    old_head = str((data.get("target") or {}).get("head_sha") or "")
    if not old_head:
        return None
    kept = _kept_already(target_dir, data)
    if kept is not None:
        return kept
    name = f"{round_number(target_dir):02d}-{old_head[:12]}"
    root = target_dir / ROUNDS_DIR
    root.mkdir(parents=True, exist_ok=True)
    partial = root / f"{PARTIAL_PREFIX}{name}"
    shutil.rmtree(partial, ignore_errors=True)  # a copy an earlier run did not finish

    def skip(directory: str, names: list[str]) -> set[str]:
        return {n for n in names if n in NOT_KEPT} if Path(directory) == target_dir else set()

    shutil.copytree(target_dir, partial, symlinks=True, ignore=skip)
    round_dir = root / name
    partial.rename(round_dir)
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
    for path in (d / "findings.json" for d in _round_dirs(target_dir)):
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
