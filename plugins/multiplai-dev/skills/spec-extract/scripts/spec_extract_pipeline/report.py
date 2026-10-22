"""`report.md`, and the one way the run writes an output file.

An output (`report.md`, `facts.json`, `plan.txt`, and the files later steps
add) is written to `<file>.tmp` and `os.replace`d over the file, so a stop
mid-write leaves the old file or none, plus at most the `.tmp`, which the next
write replaces (row R6). Outputs are never read back as inputs: each is
rebuilt from `state.json` (plan, item 3).

The report takes its sections as data, so a later step appends its own
sections after these. It carries no timestamp and no spend, so a run that
stopped and resumed writes the same bytes as one that did not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .scope import TargetScope
from .target import Target

REPORT_FILE = "report.md"


@dataclass
class Section:
    heading: str
    lines: list[str] = field(default_factory=list)
    empty: str = "None."


def write_output(run_dir: Path, name: str, text: str) -> Path:
    """Write *text* to `<run>/<name>` through `<name>.tmp` and one `os.replace`."""
    path = run_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def describe(target: Target) -> str:
    """One line naming the target and the commit it was read at."""
    if target.kind == "pr":
        return f"PR #{target.pr} at head {target.head_sha} (base {target.base_sha})"
    if target.kind == "dir":
        return f"directory {target.source}, imported as commit {target.head_sha}"
    where = f", path {target.path}" if target.path else ""
    return f"commit {target.head_sha}{where}"


def scope_sections(scope: TargetScope, not_answered: list[str]) -> list[Section]:
    """PR 1b's sections: the skipped files, then what was not analysed.

    *not_answered* is the files of units explore gave no answer for, already in
    unit order then file order. Files `ast` could not parse come first, in path
    order.
    """
    skipped = [f"- `{s.path}`: {s.reason}" for s in scope.skipped]
    not_analysed = [f"- `{s.path}`: not analysed: {s.reason}" for s in scope.unparsed]
    not_analysed += [
        f"- `{p}`: not analysed: explore gave no answer in 2 attempts"
        for p in not_answered
    ]
    return [Section("Skipped files", skipped), Section("Not analysed", not_analysed)]


def render(target: Target, sections: list[Section]) -> str:
    out = [
        "# spec-extract report",
        "",
        f"Target: {describe(target)}",
        f"Commit: {target.head_sha}",
    ]
    for s in sections:
        out += ["", f"## {s.heading}", ""]
        out += s.lines or [s.empty]
    return "\n".join(out) + "\n"


def write_report(run_dir: Path, target: Target, sections: list[Section]) -> Path:
    return write_output(run_dir, REPORT_FILE, render(target, sections))
