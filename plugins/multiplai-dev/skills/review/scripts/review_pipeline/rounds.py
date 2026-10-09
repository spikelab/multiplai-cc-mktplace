"""Earlier rounds of a review: kept under `<out>/<slug>/rounds/`, read by repeats and assess.

A PR is reviewed into the same `<out>/<slug>/` each time, so a new run would
write over the last round's `findings.json`. Before it does, `keep_round`
copies that round's files to `rounds/<old head_sha[:12]>/`. Nothing there is
ever deleted or overwritten.

The person's decisions live in `viewer/decisions.json` (written by
review-viewer), one entry per finding id, and persist across rounds.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
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


def keep_round(target_dir: Path, slug: str, new_head_sha: str) -> Path | None:
    """Copy the last round's outputs to `rounds/<sha12>/` when the new run is for another head.

    Returns the round directory, or None when there was nothing to keep (no
    findings.json, or the same head: a resume or a repeat run). Files already
    in that round directory are left as they are.
    """
    findings = target_dir / "findings.json"
    data = _read_json(findings)
    if data is None:
        return None
    old_head = str((data.get("target") or {}).get("head_sha") or "")
    if not old_head or old_head == new_head_sha:
        return None
    round_dir = target_dir / ROUNDS_DIR / old_head[:12]
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


def load_rounds(target_dir: Path, current_head: str = "") -> list[EarlierFinding]:
    """Every finding in `rounds/*/findings.json`, the latest round's copy of each id.

    Rounds are ordered by `generated_at`. A round for *current_head* is skipped.
    """
    rounds = []
    for path in sorted((target_dir / ROUNDS_DIR).glob("*/findings.json")):
        data = _read_json(path)
        if data is None:
            continue
        head = str((data.get("target") or {}).get("head_sha") or "")
        if current_head and head == current_head:
            continue
        rounds.append((str(data.get("generated_at") or ""), head, data.get("findings") or []))
    rounds.sort(key=lambda r: r[0])
    decisions = load_decisions(target_dir)
    by_id: dict[str, EarlierFinding] = {}
    for _, head, findings in rounds:
        for f in findings:
            if not isinstance(f, dict) or not f.get("id"):
                continue
            d = decisions.get(f["id"]) or {}
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
