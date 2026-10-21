"""What the `explain` stage hands to `code_facts`, and the `facts.json` it writes.

The scope (`scope_for`). For a change: every changed hunk at head, widened by
CONTEXT_LINES on each side and clipped to the file, and every run of removed
lines at base (`side="base"`). Binary files have no hunk in the diff, so they
are left out. For a tree review: every file under review, whole, at head, with
no base side; the same scope codebase-walkthrough explores.

The resolver (`resolver`) places a citation's path at a commit: head lines at
`head_sha`, base lines at `base_sha`. A tree review has no base side.

`facts.json` sits beside `findings.json`. Its contract is
`review/schema/facts.v1.schema.json`, generated from `FactsFile`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from code_facts import Resolve, Scope, Side, Span
from pydantic import Field

from .chunks import header_path
from .models import ExplainedFacts, TargetInfo

CONTEXT_LINES = 20
FACTS_FILE = "facts.json"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "facts.v1.schema.json"

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


# --- the scope ---------------------------------------------------------------------


def _diff_path(name: str) -> str:
    """A path from a `---`/`+++` line: "" for /dev/null, quotes and the a/ or b/ prefix removed."""
    name = header_path(name.rstrip("\n"))
    if name == "/dev/null":
        return ""
    return name[2:] if name[:2] in ("a/", "b/") else name


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Overlapping or touching ranges joined, in order."""
    out: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if out and start <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def diff_ranges(diff: str) -> tuple[dict[str, list[tuple[int, int]]], dict[str, list[tuple[int, int]]]]:
    """(head hunks per new path, removed-line runs per old path), each as (start, end) ranges.

    A hunk's lines are counted from its `@@` header, so a removed line that
    reads like a `---` header is still part of the hunk.
    """
    head: dict[str, list[tuple[int, int]]] = {}
    base: dict[str, list[tuple[int, int]]] = {}
    old_path = new_path = ""
    old_left = new_left = 0
    old_line = 0
    run_start = 0

    def close_run(last: int) -> None:
        nonlocal run_start
        if run_start and old_path:
            base.setdefault(old_path, []).append((run_start, last))
        run_start = 0

    for line in diff.splitlines():
        if old_left > 0 or new_left > 0:
            mark = line[:1]
            if mark == "\\":  # "\ No newline at end of file"
                continue
            if mark == "-":
                if not run_start:
                    run_start = old_line
                old_line += 1
                old_left -= 1
                continue
            close_run(old_line - 1)
            if mark == "+":
                new_left -= 1
            else:  # context, or an empty line git wrote for an empty context line
                old_line += 1
                old_left -= 1
                new_left -= 1
            continue
        close_run(old_line - 1)
        if line.startswith("--- "):
            old_path = _diff_path(line[4:])
        elif line.startswith("+++ "):
            new_path = _diff_path(line[4:])
        elif line.startswith("diff --git "):
            old_path = new_path = ""
        else:
            m = _HUNK.match(line)
            if m:
                old_start, old_count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
                new_start, new_count = int(m.group(3)), int(m.group(4) if m.group(4) is not None else 1)
                old_line, old_left, new_left = old_start, old_count, new_count
                if new_count and new_path:
                    head.setdefault(new_path, []).append((new_start, new_start + new_count - 1))
    close_run(old_line - 1)
    return head, base


def line_count(path: Path) -> int | None:
    """Lines in a file as git numbers them (a last line without a newline counts), or None if unreadable."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def diff_scope(diff: str, snapshot: Path, context: int = CONTEXT_LINES) -> Scope:
    """Changed hunks at head (widened, clipped to the file) and removed lines at base."""
    head, base = diff_ranges(diff)
    spans: list[Span] = []
    for path in sorted(head):
        last = line_count(snapshot / path)
        widened = []
        for start, end in head[path]:
            end = end + context
            widened.append((max(1, start - context), min(end, last) if last else end))
        spans += [Span(path=path, start=s, end=e, side="head") for s, e in _merge(widened) if e >= s]
    for path in sorted(base):
        spans += [Span(path=path, start=s, end=e, side="base") for s, e in _merge(base[path])]
    return Scope(spans=spans)


def tree_scope(target: TargetInfo, snapshot: Path) -> Scope:
    """Every file under review, whole, at head."""
    return Scope.whole_files([(f, line_count(snapshot / f) or 0) for f in target.files])


def scope_for(target: TargetInfo, diff: str, snapshot: Path) -> Scope:
    return tree_scope(target, snapshot) if target.is_tree else diff_scope(diff, snapshot)


# --- placing a citation ---------------------------------------------------------------


def resolver(target: TargetInfo) -> Resolve:
    """`resolve(path, side) -> (repo, sha, path)`; None for an empty path or a base side in a tree review."""
    def resolve(path: str, side: Side) -> tuple[str, str, str] | None:
        if not path:
            return None
        if side == "base":
            if target.is_tree:
                return None
            return target.repo_path, target.base_sha, path
        return target.repo_path, target.head_sha, path

    return resolve


# --- facts.json -----------------------------------------------------------------------


class FactsFile(ExplainedFacts):
    """`facts.json`: the explain stage's checked facts for one base..head.

    `facts` holds every fact that passed, with ids `f<N>` and citation ids
    `c<N>`. `interfaces`, `gotchas` and `conventions` repeat some of those
    facts, with the same ids. `files` gives each file's purpose with the
    citation that passed. `uncovered` lists files in the scope that no fact
    cites; `dropped` records each claim whose citations all failed.
    """
    schema_version: Literal[1]
    generated_at: str
    base_sha: str
    head_sha: str
    kind: Literal["change", "tree"] = Field(description="`tree` for a review of files as they stand, no base side")


def schema_text() -> str:
    """The JSON Schema for FactsFile, in its committed form."""
    schema = FactsFile.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "facts.v1.schema.json"
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def write_facts_file(target: TargetInfo, facts: ExplainedFacts, target_dir: Path) -> Path:
    data = FactsFile(
        schema_version=1,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        base_sha=target.base_sha, head_sha=target.head_sha, kind="tree" if target.is_tree else "change",
        **facts.model_dump(),
    )
    path = target_dir / FACTS_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(data.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
