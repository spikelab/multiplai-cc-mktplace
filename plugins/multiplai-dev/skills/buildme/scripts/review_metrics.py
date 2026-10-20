#!/usr/bin/env python3
"""Measure buildme's per-block review loop from the round records it writes.

Give it one or more directories. It finds every
``reviews/block-<NN>-round-<k>.json`` under them, at any depth, so a project's
``specs/`` directory covers both ``specs/changes/`` and ``specs/archive/``.
For each change directory it reports, per block:

- the number of records with ``kind == "review"`` (fix-on-pass reviews are
  listed but not counted);
- the round of the first ``review`` record whose gate passed, or ``none``;
- the accepted and rejected findings of every round;
- for each finding accepted in round 2 or later that names a file and a line,
  where that line came from. ``git blame`` at that round's ``head_sha`` names
  the commit that wrote the line; when that commit is an ancestor of round
  1's ``head_sha`` the line was ``present at first review``, otherwise it was
  ``written by a fix``. A failed git command, or a missing ``head_sha``, gives
  ``unknown``.

The totals line gives the mean rounds-to-pass, the share of blocks passing at
round 1, and the share of round-2+ findings present at first review, over all
records and over ``review`` records alone.

The git commands run in the project, which is the directory above ``specs/``.

The script prints Markdown to stdout and writes nothing. It uses no network
and makes no model calls; the only commands it runs are ``git blame`` and
``git merge-base --is-ancestor``, both read-only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROUND_FILE_RE = re.compile(r"^block-(\d+)-round-(\d+)\.json$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

PRESENT = "present at first review"
BY_FIX = "written by a fix"
UNKNOWN = "unknown"
NO_LINE = "no line"
ORIGINS = (PRESENT, BY_FIX, UNKNOWN, NO_LINE)

GIT_TIMEOUT_S = 30


@dataclass
class Record:
    block: int
    round: int
    data: dict

    @property
    def kind(self) -> str:
        return str(self.data.get("kind") or "")

    @property
    def head_sha(self) -> str | None:
        sha = self.data.get("head_sha")
        return sha if isinstance(sha, str) and sha else None

    def findings(self, key: str) -> list[dict]:
        items = self.data.get(key)
        return [f for f in items if isinstance(f, dict)] if isinstance(items, list) else []


@dataclass
class BlockMetrics:
    block: int
    records: list[Record]
    origins: Counter = field(default_factory=Counter)
    # The same count over records with `kind == "review"` only, leaving out
    # fix-on-pass reviews.
    review_origins: Counter = field(default_factory=Counter)

    @property
    def rounds(self) -> int:
        return sum(1 for r in self.records if r.kind == "review")

    @property
    def passed_at(self) -> int | None:
        for r in self.records:
            gate = r.data.get("gate")
            if r.kind == "review" and isinstance(gate, dict) and gate.get("passed") is True:
                return r.round
        return None


@dataclass
class ChangeMetrics:
    change_dir: Path
    project_dir: Path | None
    record_count: int
    blocks: list[BlockMetrics]


def find_records(directories: list[Path]) -> tuple[dict[Path, list[Record]], list[str]]:
    """Every round record under *directories*, grouped by change directory
    (the parent of ``reviews/``). Returns the groups and a warning for each
    file that could not be read."""
    seen: set[Path] = set()
    groups: dict[Path, list[Record]] = {}
    warnings: list[str] = []
    for directory in directories:
        for path in sorted(directory.rglob("block-*-round-*.json")):
            m = ROUND_FILE_RE.match(path.name)
            if not m or path.parent.name != "reviews" or not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                warnings.append(f"skipped {path}: {e}")
                continue
            if not isinstance(data, dict):
                warnings.append(f"skipped {path}: not a JSON object")
                continue
            record = Record(block=int(m.group(1)), round=int(m.group(2)), data=data)
            groups.setdefault(path.parent.parent, []).append(record)
    return groups, warnings


def project_dir_of(change_dir: Path) -> Path | None:
    """The directory above the nearest ``specs/`` that holds *change_dir*."""
    for ancestor in (change_dir, *change_dir.parents):
        if ancestor.name == "specs":
            return ancestor.parent
    return None


def _git(project: Path, *argv: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(project), *argv], capture_output=True,
                              text=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None


def line_origin(project: Path | None, file_path: str, line: int,
                head_sha: str | None, first_head_sha: str | None) -> str:
    """Whether *file_path*:*line* at *head_sha* was already in round 1's
    ``head_sha`` (``PRESENT``), was written after it (``BY_FIX``), or cannot
    be told (``UNKNOWN``)."""
    if project is None or head_sha is None or first_head_sha is None:
        return UNKNOWN
    blame = _git(project, "blame", "-L", f"{line},{line}", "--porcelain", head_sha,
                 "--", file_path)
    if blame is None or blame.returncode != 0 or not blame.stdout.strip():
        return UNKNOWN
    blamed = blame.stdout.split()[0]
    if not _SHA_RE.match(blamed):
        return UNKNOWN
    ancestor = _git(project, "merge-base", "--is-ancestor", blamed, first_head_sha)
    if ancestor is None:
        return UNKNOWN
    if ancestor.returncode == 0:
        return PRESENT
    if ancestor.returncode == 1:
        return BY_FIX
    return UNKNOWN


def _location(finding: dict) -> tuple[str, int] | None:
    file_path, line = finding.get("file_path"), finding.get("line")
    if not isinstance(file_path, str) or not file_path:
        return None
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        return None
    return file_path, line


def measure_block(block: int, records: list[Record], project: Path | None) -> BlockMetrics:
    records = sorted(records, key=lambda r: r.round)
    metrics = BlockMetrics(block=block, records=records)
    first = next((r for r in records if r.round == 1), None)
    first_head = first.head_sha if first else None
    for record in records:
        if record.round < 2:
            continue
        for finding in record.findings("accepted"):
            where = _location(finding)
            origin = NO_LINE if where is None else line_origin(
                project, *where, record.head_sha, first_head)
            metrics.origins[origin] += 1
            if record.kind == "review":
                metrics.review_origins[origin] += 1
    return metrics


def measure(directories: list[Path]) -> tuple[list[ChangeMetrics], list[str]]:
    groups, warnings = find_records(directories)
    changes = []
    for change_dir in sorted(groups):
        records = groups[change_dir]
        project = project_dir_of(change_dir)
        by_block: dict[int, list[Record]] = {}
        for record in records:
            by_block.setdefault(record.block, []).append(record)
        blocks = [measure_block(n, by_block[n], project) for n in sorted(by_block)]
        changes.append(ChangeMetrics(change_dir, project, len(records), blocks))
    return changes, warnings


def _share(part: int, whole: int) -> str:
    return f"{part} of {whole} ({part / whole:.0%})" if whole else f"{part} of 0"


def _round_cell(block: BlockMetrics) -> str:
    cells = []
    for r in block.records:
        mark = "" if r.kind == "review" else "†"
        cells.append(f"r{r.round}{mark} {len(r.findings('accepted'))}/"
                     f"{len(r.findings('rejected'))}")
    return ", ".join(cells)


def render(changes: list[ChangeMetrics], directories: list[Path]) -> str:
    out = ["# buildme review-loop metrics", ""]
    out.append("Scanned: " + ", ".join(f"`{d}`" for d in directories))
    out.append("")
    if not changes:
        out.append("No review round records found.")
        return "\n".join(out) + "\n"
    all_blocks: list[BlockMetrics] = []
    for change in changes:
        all_blocks.extend(change.blocks)
        project = f"`{change.project_dir}`" if change.project_dir else "none found (no `specs/` above it)"
        out += [f"## `{change.change_dir}`", "",
                f"{change.record_count} round records, {len(change.blocks)} blocks. "
                f"Project: {project}.", "",
                "| Block | Review rounds | Passed at | Accepted/rejected per round "
                "| Round 2+: present at first review | Round 2+: written by a fix "
                "| Round 2+: unknown | Round 2+: no line |",
                "|---|---|---|---|---|---|---|---|"]
        for b in change.blocks:
            passed = str(b.passed_at) if b.passed_at is not None else "none"
            out.append(f"| {b.block} | {b.rounds} | {passed} | {_round_cell(b)} | "
                       + " | ".join(str(b.origins[o]) for o in ORIGINS) + " |")
        out += ["", "† a record whose `kind` is not `review` (for example "
                "`fix_on_pass_review`): listed, not counted in Review rounds or Passed at.", ""]

    passed = [b.passed_at for b in all_blocks if b.passed_at is not None]
    mean = f"{sum(passed) / len(passed):.2f}" if passed else "n/a"
    at_one = sum(1 for p in passed if p == 1)
    totals: Counter = Counter()
    review_totals: Counter = Counter()
    for b in all_blocks:
        totals.update(b.origins)
        review_totals.update(b.review_origins)
    known = totals[PRESENT] + totals[BY_FIX]
    review_known = review_totals[PRESENT] + review_totals[BY_FIX]
    out.append(
        f"**Totals** ({len(changes)} change(s), {len(all_blocks)} blocks): "
        f"mean rounds-to-pass {mean} (over {len(passed)} blocks that passed; "
        f"{len(all_blocks) - len(passed)} did not pass); "
        f"blocks passing at round 1: {_share(at_one, len(all_blocks))}; "
        f"round-2+ findings present at first review: {_share(totals[PRESENT], known)} "
        f"with a known origin ({totals[BY_FIX]} written by a fix, "
        f"{totals[UNKNOWN]} unknown, {totals[NO_LINE]} with no line); "
        f"in `review` records only: {_share(review_totals[PRESENT], review_known)} "
        f"({review_totals[BY_FIX]} written by a fix, {review_totals[UNKNOWN]} unknown, "
        f"{review_totals[NO_LINE]} with no line)."
    )
    return "\n".join(out) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review_metrics.py",
        description="Report buildme's per-block review loop from the round records "
                    "(reviews/block-NN-round-K.json) under the given directories. "
                    "Prints Markdown; writes nothing.",
    )
    parser.add_argument("directories", nargs="+", type=Path, metavar="DIR",
                        help="a directory to search recursively, such as a project's specs/")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    missing = [d for d in args.directories if not d.is_dir()]
    if missing:
        parser.error("not a directory: " + ", ".join(str(d) for d in missing))
    changes, warnings = measure(args.directories)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    sys.stdout.write(render(changes, args.directories))
    return 0


if __name__ == "__main__":
    sys.exit(main())
