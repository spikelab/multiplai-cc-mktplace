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
from datetime import datetime, timezone
from pathlib import Path

from .models import BlockInfo, ReviewFinding, ReviewResult

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
) -> Path | None:
    """Write the next round's record for *block* and return its path.

    The file is created exclusively, so an existing round is never replaced:
    if the next number is taken (another writer, or a file this process did
    not count), the one after it is used. Returns None when nothing could be
    written."""
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
        "accepted": [f.model_dump(mode="json") for f in review.findings],
        "rejected": [
            {**f.model_dump(mode="json"), "judge_reason": _judge_reason(f)}
            for f in review.rejected_findings
        ],
        "fix_brief": None,
        "fix": None,
    }
    for _ in range(100):
        path = directory / f"block-{block.number:02d}-round-{k}.json"
        try:
            with path.open("x") as fh:
                fh.write(json.dumps({**record, "round": k}, indent=2) + "\n")
            return path
        except FileExistsError:
            k += 1
        except OSError as e:
            log.warning("Could not write %s: %s — review round not recorded", path, e)
            return None
    log.warning("No free round number for block %d in %s", block.number, directory)
    return None


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
