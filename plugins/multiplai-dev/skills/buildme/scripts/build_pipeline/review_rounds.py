"""Records of the per-block review loop, in the change directory.

``reviews/block-<NN>-round-<k>.json`` holds one review round of one block:
the scores and the gate's verdict, the findings the judge accepted and the
ones it rejected (with its reason), the brief sent to the fix agent, and,
once that agent returns, its report, its commit and its ``SEARCHED:`` list.
Round numbers continue across resumes: a new round takes the next free
number and never replaces an earlier file.

``unfixed-findings.md`` lists the accepted findings a block that passed kept
unfixed: the fix on pass was not kept, or not attempted. PUBLISH hands it to
the pr skill as a PR-body section (``orchestrator.unfixed_findings_section``).

Both files are committed with the block's commits, like the change
directory's other history files. Nothing here raises: a record that cannot
be written is logged, and the build goes on.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import BlockInfo, PriorAssessment, ReviewFinding, ReviewResult, class_line

log = logging.getLogger(__name__)

REVIEWS_DIR = "reviews"
UNFIXED_FINDINGS_FILE = "unfixed-findings.md"
UNFIXED_HEADING = "## Unfixed review findings"

# The severities the fix on pass works on. `Note` is left out on purpose.
FIX_ON_PASS_SEVERITIES = frozenset({"critical", "major", "minor"})

_ROUND_RE = re.compile(r"^block-(\d+)-round-(\d+)\.json$")


def reviews_dir(change_dir: Path) -> Path:
    return Path(change_dir) / REVIEWS_DIR


def round_numbers(change_dir: Path, block_number: int) -> list[int]:
    """The round numbers already on disk for *block_number*, ascending."""
    directory = reviews_dir(change_dir)
    if not directory.is_dir():
        return []
    rounds = []
    for path in directory.iterdir():
        m = _ROUND_RE.match(path.name)
        if m and int(m.group(1)) == block_number:
            rounds.append(int(m.group(2)))
    return sorted(rounds)


def fixable_on_pass(findings: list[ReviewFinding]) -> list[ReviewFinding]:
    """The accepted findings the fix on pass works on: every severity but Note."""
    return [f for f in findings if f.severity.strip().lower() in FIX_ON_PASS_SEVERITIES]


def _judge_reason(finding: ReviewFinding) -> str:
    """The judge's reason, which `_adjudicate_review_findings` appends to a
    rejected finding's evidence as ``REJECTED: <reason>``."""
    _, sep, reason = finding.evidence.rpartition("REJECTED:")
    return reason.strip() if sep else ""


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def finding_id(block_number: int, round_number: int, index: int, prefix: str = "F") -> str:
    """The id of the *index*-th (from 0) finding of a round: accepted findings
    are ``F``, rejected ones ``X``. ``B01-R2-F1`` is block 1's first accepted
    finding in round 2."""
    return f"B{block_number:02d}-R{round_number}-{prefix}{index + 1}"


def _with_ids(findings: list[ReviewFinding], block_number: int,
              round_number: int) -> list[ReviewFinding]:
    """*findings* with an id on each that has none, by its place in the list."""
    return [f if f.finding_id else f.model_copy(
                update={"finding_id": finding_id(block_number, round_number, i)})
            for i, f in enumerate(findings)]


def record_round(
    change_dir: Path,
    block: BlockInfo,
    *,
    head_sha: str | None,
    review: ReviewResult,
    gate_passed: bool,
    gate_reason: str,
    weighted_average: float,
    kind: str = "review",
    carried_open: list[ReviewFinding] | None = None,
    new_open: list[int] | None = None,
    outside: list[int] | None = None,
    prior_assessments: list[PriorAssessment] | None = None,
) -> Path | None:
    """Write the next round's record for *block* and return its path.

    The file is created exclusively, so an existing round is never replaced:
    if the next number is taken (another writer, or a file this process did
    not count), the one after it is used. Returns None when nothing could be
    written.

    Every accepted finding without a ``finding_id`` gets one for the round
    number the file takes. ``open_findings`` is *carried_open* (findings an
    earlier round left open, with their ids) followed by the accepted
    findings at the indices *new_open*; with *new_open* None, as in round 1,
    it is every accepted finding. ``outside_fix_scope`` holds the accepted
    findings at the indices *outside*."""
    directory = reviews_dir(change_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("Could not create %s: %s — review round not recorded", directory, e)
        return None
    taken = round_numbers(change_dir, block.number)
    k = (taken[-1] if taken else 0) + 1
    record = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "block": {"number": block.number, "name": block.name},
        "round": k,
        "head_sha": head_sha,
        "scores": [s.model_dump(mode="json") for s in review.scores],
        "weighted_average": weighted_average,
        "gate": {"passed": gate_passed, "reason": gate_reason},
        "missing": list(review.missing),
        "extra": list(review.extra),
        "misunderstood": list(review.misunderstood),
        "accepted": None,  # filled in for the round number the file takes
        "rejected": [
            {**f.model_dump(mode="json"), "judge_reason": _judge_reason(f)}
            for f in review.rejected_findings
        ],
        "open_findings": None,
        "prior_assessments": [a.model_dump(mode="json") for a in prior_assessments or []],
        "outside_fix_scope": None,
        "fix_brief": None,
        "fix": None,
    }

    def numbered(k: int) -> dict:
        accepted = _with_ids(review.findings, block.number, k)
        opened = list(carried_open or []) + (
            accepted if new_open is None else [accepted[i] for i in new_open])
        return {
            **record, "round": k,
            "accepted": [f.model_dump(mode="json") for f in accepted],
            "open_findings": [f.model_dump(mode="json") for f in opened],
            "outside_fix_scope": [accepted[i].model_dump(mode="json") for i in outside or []],
        }

    for _ in range(100):
        path = directory / f"block-{block.number:02d}-round-{k}.json"
        try:
            with path.open("x") as fh:
                fh.write(json.dumps(numbered(k), indent=2) + "\n")
            return path
        except FileExistsError:
            k += 1
        except OSError as e:
            log.warning("Could not write %s: %s — review round not recorded", path, e)
            return None
    log.warning("No free round number for block %d in %s", block.number, directory)
    return None


def _review_records(change_dir: Path, block_number: int) -> list[dict]:
    """The block's records with ``kind == "review"``, by round number. A
    record that cannot be read is logged and left out."""
    records = []
    for k in round_numbers(change_dir, block_number):
        path = reviews_dir(change_dir) / f"block-{block_number:02d}-round-{k}.json"
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            log.warning("Could not read review round record %s: %s", path, e)
            continue
        if isinstance(record, dict) and record.get("kind") == "review":
            records.append({**record, "round": record.get("round", k)})
    return records


def _findings(entries, block_number: int, round_number: int) -> list[ReviewFinding]:
    """The findings in a record's list, ids given by the rule where missing."""
    out = []
    for entry in entries or []:
        try:
            out.append(ReviewFinding.model_validate(entry))
        except (TypeError, ValueError) as e:
            log.warning("Skipping an unreadable finding in round %s of block %d: %s",
                        round_number, block_number, e)
    return _with_ids(out, block_number, round_number)


def open_state(
    change_dir: Path, block_number: int,
) -> tuple[list[ReviewFinding], list[dict], dict | None]:
    """What the block's earlier review rounds left: ``(open, rejected, last)``.

    *last* is the latest record with ``kind == "review"`` (by round number),
    or None before the block's first review. *open* is its
    ``open_findings``; a record written before that field existed gives its
    ``accepted`` instead, with ids by `finding_id`'s rule. *rejected* is
    every finding any of the block's review records rejected, each with its
    ``judge_reason`` and an id (``X`` for rejected)."""
    records = _review_records(change_dir, block_number)
    if not records:
        return [], [], None
    rejected: list[dict] = []
    seen: set[str] = set()
    for record in records:
        for i, entry in enumerate(record.get("rejected") or []):
            if not isinstance(entry, dict):
                continue
            fid = entry.get("finding_id") or finding_id(block_number, record["round"], i, "X")
            if fid not in seen:
                seen.add(fid)
                rejected.append({**entry, "finding_id": fid})
    last = records[-1]
    entries = last["open_findings"] if "open_findings" in last else last.get("accepted")
    return _findings(entries, block_number, last["round"]), rejected, last


@dataclass
class RoundMemory:
    """What the adjudicator is given from round 2 of a block on: the open
    findings, every earlier-rejected one (with its judge's reason), and the
    scope diff since the previous review (None when git could not give it,
    which leaves every finding in scope)."""

    open: list[ReviewFinding]
    rejected: list[dict]
    scope_diff: str | None


def complete_assessments(
    assessments: list[PriorAssessment], open_: list[ReviewFinding],
) -> list[PriorAssessment]:
    """One assessment per open finding, in the open list's order. An open
    finding the review gave no status is ``cannot_verify``, never resolved;
    a status for an id that is not open is left out."""
    given = {a.finding_id: a for a in assessments}
    return [given.get(f.finding_id) or PriorAssessment(finding_id=f.finding_id,
                                                       status="cannot_verify")
            for f in open_]


@dataclass
class RoundPlan:
    """What a round from 2 on does with its findings (plan B2 step 4).

    *carried*: the open findings that stay open, with any restatement's
    evidence appended. *new_open* / *outside*: indices into the round's
    accepted findings that are new and within the fix's reach (Critical ones
    whatever their reach) / new, outside it and not Critical. *to_fix*: what
    the fix agent is given, *carried* then the *new_open* findings."""

    assessments: list[PriorAssessment]
    carried: list[ReviewFinding] = field(default_factory=list)
    new_open: list[int] = field(default_factory=list)
    outside: list[int] = field(default_factory=list)
    to_fix: list[ReviewFinding] = field(default_factory=list)

    def status(self, finding_id: str) -> str:
        return next((a.status for a in self.assessments if a.finding_id == finding_id),
                    "cannot_verify")


def plan_round(
    accepted: list[ReviewFinding], open_: list[ReviewFinding],
    assessments: list[PriorAssessment],
) -> RoundPlan:
    """Sort a round's accepted findings against the open list (plan B2
    step 4). An accepted finding whose `duplicate_of` names an open finding
    is not new: its evidence goes on that finding, which then stays open
    even if the review called it resolved. A missing `in_fix_scope` is in
    scope; a Critical finding is fixed whatever its scope."""
    plan = RoundPlan(assessments=complete_assessments(assessments, open_))
    open_ids = {f.finding_id for f in open_}
    restated: dict[str, list[str]] = {}
    for i, f in enumerate(accepted):
        if f.duplicate_of and f.duplicate_of in open_ids:
            restated.setdefault(f.duplicate_of, []).append(f.evidence or f.claim)
        elif f.in_fix_scope is False and f.severity.strip().lower() != "critical":
            plan.outside.append(i)
        else:
            plan.new_open.append(i)
    for f in open_:
        more = restated.get(f.finding_id)
        if plan.status(f.finding_id) == "resolved" and not more:
            continue
        if more:
            f = f.model_copy(update={"evidence": "\n".join(
                [f.evidence, *(f"RESTATED: {e}" for e in more)]).strip()})
        plan.carried.append(f)
    plan.to_fix = plan.carried + [accepted[i] for i in plan.new_open]
    return plan


def open_findings_text(plan: RoundPlan) -> str:
    """The carried open findings for the fix brief, each with its status."""
    lines = []
    for f in plan.carried:
        where = f" ({f.file_path}:{f.line})" if f.file_path else ""
        lines.append(f"- {f.finding_id} [{f.severity}] {f.claim}{where} — status: "
                     f"{plan.status(f.finding_id)}")
        if f.evidence:
            lines.append(f"  evidence: {f.evidence}")
        if class_line(f):
            lines.append(class_line(f).lstrip("\n"))
    return "\n".join(lines)


def history(change_dir: Path, block_number: int) -> list[tuple[ReviewFinding, list[str]]]:
    """Every finding this block's review records accepted, by id, in the
    order first raised, with what each round said about it."""
    seen: dict[str, tuple[ReviewFinding, list[str]]] = {}
    for record in _review_records(change_dir, block_number):
        k = record["round"]
        for f in _findings(record.get("accepted"), block_number, k):
            if f.duplicate_of and f.duplicate_of in seen:
                seen[f.duplicate_of][1].append(f"round {k}: restated")
                continue
            seen.setdefault(f.finding_id, (f, []))[1].append(f"round {k}: raised")
        for entry in record.get("prior_assessments") or []:
            fid = entry.get("finding_id") if isinstance(entry, dict) else None
            if fid in seen:
                seen[fid][1].append(f"round {k}: {entry.get('status')}")
        for f in _findings(record.get("outside_fix_scope"), block_number, k):
            if f.finding_id in seen:
                seen[f.finding_id][1].append(f"round {k}: outside the last fix's scope")
    return list(seen.values())


def history_text(change_dir: Path, block_number: int) -> str:
    lines = []
    for f, events in history(change_dir, block_number):
        where = f" ({f.file_path}:{f.line})" if f.file_path else ""
        lines.append(f"- {f.finding_id} [{f.severity}] {f.claim}{where} — {'; '.join(events)}")
    return "\n".join(lines) or "(no findings recorded)"


def recurring_ids(change_dir: Path, block_number: int) -> list[str]:
    """Ids whose status was ``still_present`` or ``partially_resolved`` in
    two or more of the block's review records."""
    counts: dict[str, int] = {}
    for record in _review_records(change_dir, block_number):
        for entry in record.get("prior_assessments") or []:
            if isinstance(entry, dict) and entry.get("status") in (
                    "still_present", "partially_resolved"):
                counts[entry.get("finding_id")] = counts.get(entry.get("finding_id"), 0) + 1
    return [fid for fid, n in counts.items() if fid and n >= 2]


def update_round(path: Path | str | None, **fields) -> None:
    """Merge *fields* into the round record at *path*. A previous ``fix``
    (from an attempt a resume rolled back) moves to ``earlier_fixes`` rather
    than being replaced."""
    if path is None:
        return
    path = Path(path)
    try:
        record = json.loads(path.read_text())
        if "fix" in fields and record.get("fix") is not None:
            record.setdefault("earlier_fixes", []).append(record["fix"])
        record.update(fields)
        _write_json(path, record)
    except (OSError, ValueError) as e:
        log.warning("Could not update review round record %s: %s", path, e)


def amend_fix(path: Path | str | None, **fields) -> None:
    """Merge *fields* into the round record's current ``fix`` (its commit,
    whether it was kept), without starting a new one."""
    if path is None:
        return
    path = Path(path)
    try:
        record = json.loads(path.read_text())
        if not isinstance(record.get("fix"), dict):
            log.warning("Round record %s has no fix to amend", path)
            return
        record["fix"].update(fields)
        _write_json(path, record)
    except (OSError, ValueError) as e:
        log.warning("Could not update review round record %s: %s", path, e)


def snapshot(change_dir: Path) -> dict[Path, bytes]:
    """Every round record's bytes, to put back after a rollback: a discard
    to an earlier commit would otherwise delete the records written since."""
    directory = reviews_dir(change_dir)
    out: dict[Path, bytes] = {}
    if not directory.is_dir():
        return out
    for path in directory.glob("block-*-round-*.json"):
        try:
            out[path] = path.read_bytes()
        except OSError as e:
            log.warning("Could not read %s before a rollback: %s", path, e)
    return out


def restore(saved: dict[Path, bytes]) -> None:
    for path, data in saved.items():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as e:
            log.warning("Could not restore review round record %s: %s", path, e)


def append_unfixed(
    change_dir: Path, block: BlockInfo, findings: list[ReviewFinding], status: str,
) -> Path | None:
    """Append *findings* to ``unfixed-findings.md`` with *status* (why they
    are unfixed). The file starts with a Markdown heading, which the pr skill
    requires of a ``--section`` file."""
    if not findings:
        return None
    path = Path(change_dir) / UNFIXED_FINDINGS_FILE
    lines = []
    for f in findings:
        where = f" (`{f.file_path}:{f.line}`)" if f.file_path and f.line else (
            f" (`{f.file_path}`)" if f.file_path else "")
        claim = " ".join(f.claim.split())
        lines.append(f"- Block {block.number} ({block.name}): [{f.severity}] {claim}{where} "
                     f"— {status}\n")
    try:
        new = not path.exists()
        with path.open("a") as fh:
            if new:
                fh.write(f"{UNFIXED_HEADING}\n\nAccepted review findings that buildme left "
                         "unfixed on a block that passed review.\n\n")
            fh.writelines(lines)
        return path
    except OSError as e:
        log.warning("Could not write %s: %s", path, e)
        return None
