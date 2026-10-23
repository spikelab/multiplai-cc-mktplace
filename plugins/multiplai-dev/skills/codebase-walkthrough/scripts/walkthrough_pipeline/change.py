"""A change target's scope: which symbols it touches and the line ranges the explore agents read.

Code only, from `TargetInfo.change` and the snapshots. A function or class is
changed when its span (Python `ast`, `lineno`..`end_lineno`) holds a line the
change adds. The explore units hold one span per hunk, at head for added lines
and under the `<key>@base` snapshot for removed ones, each widened by
`CONTEXT_LINES` and merged; a file's spans stay in one unit.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path, PurePosixPath

from code_facts import Span

from .models import SymbolEntry, TargetInfo, Unit

log = logging.getLogger(__name__)

CONTEXT_LINES = 5
# Symbol kinds kept when the definition they name is changed, though their own line is not.
DEFINITION_KINDS = ("function", "class", "model", "celery-task", "management-command")


def _ranges(lines: set[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for n in sorted(lines):
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def changed_definitions(snap: Path, target: TargetInfo) -> dict[str, set[str]]:
    """Names of the functions, methods and classes each changed Python file's added lines fall in."""
    out: dict[str, set[str]] = {}
    for rel, added in target.change.added().items():
        if rel not in target.files or not rel.endswith(".py") or not added:
            continue
        try:
            tree = ast.parse(
                (snap / rel).read_text(encoding="utf-8", errors="replace"), rel
            )
        except (OSError, SyntaxError):
            log.warning("change: cannot parse %s", rel)
            continue
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and any(node.lineno <= n <= (node.end_lineno or node.lineno) for n in added)
        }
        if names:
            out[rel] = names
    return out


def change_symbols(
    entries: list[SymbolEntry], target: TargetInfo, changed: dict[str, set[str]]
) -> list[SymbolEntry]:
    """The entries the change touches: on an added line, or a definition the change edits."""
    added = target.change.added()
    out = []
    for e in entries:
        rel = e.path.partition("/")[2]
        if e.line in added.get(rel, set()) or (
            e.direction == "defines"
            and e.kind in DEFINITION_KINDS
            and (e.detail.get("function") or e.name) in changed.get(rel, set())
        ):
            out.append(e)
    return out


def change_spans(target: TargetInfo, base_lines: dict[str, int]) -> list[Span]:
    """Workspace-relative spans: each added hunk at head and each removed hunk at base, with context.

    *base_lines* is the line count of each base-side file in its snapshot (repo-relative); a base file
    that was not snapshotted (a secret, binary or large file) has no span.
    """
    ch = target.change
    spans: list[Span] = []
    for f in ch.files:
        rel = f["path"]
        if rel in target.files:
            n = target.file_lines.get(rel, 0)
            wanted = {
                x
                for a, b in f["added"]
                for x in range(max(1, a - CONTEXT_LINES), min(n, b + CONTEXT_LINES) + 1)
            }
            spans += [
                Span(path=target.ws(rel), start=a, end=b, side="head")
                for a, b in _ranges(wanted)
            ]
        old = f["old_path"] or rel
        m = base_lines.get(old, 0)
        if f["removed"] and m:
            wanted = {
                x
                for a, b in f["removed"]
                for x in range(max(1, a - CONTEXT_LINES), min(m, b + CONTEXT_LINES) + 1)
            }
            spans += [
                Span(path=f"{ch.base_key}/{old}", start=a, end=b, side="base")
                for a, b in _ranges(wanted)
            ]
    return spans


def base_line_counts(snap: Path, target: TargetInfo) -> dict[str, int]:
    """Line counts of the base-side files present in the `<key>@base` snapshot, repo-relative."""
    out: dict[str, int] = {}
    root = snap / target.change.base_key
    for f in target.change.files:
        old = f["old_path"] or f["path"]
        p = root / old
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
            out[old] = len(text.split("\n")) - (1 if text.endswith("\n") else 0)
    return out


def base_paths(target: TargetInfo) -> list[str]:
    """The base-side paths snapshotted under `<key>@base`: every file whose old lines the change touches."""
    from .target import BASE_STATUSES

    return sorted(
        {
            f["old_path"] or f["path"]
            for f in target.change.files
            if f["status"] in BASE_STATUSES
        }
    )


def change_units(spans: list[Span], limit: int) -> list[Unit]:
    """Pack each file's spans whole into units of at most *limit* span lines, by directory.

    A file whose spans alone pass the limit goes alone and is flagged `oversize`.
    """
    by_file: dict[str, list[Span]] = {}
    for s in spans:
        by_file.setdefault(s.path, []).append(s)
    units: list[Unit] = []
    current: list[Span] = []
    size = 0

    def add(group: list[Span], oversize: bool = False) -> None:
        units.append(
            Unit(
                id=f"u{len(units) + 1}",
                files=list(dict.fromkeys(s.path for s in group)),
                lines=sum(s.lines for s in group),
                oversize=oversize,
                spans=group,
            )
        )

    def order(path: str) -> tuple[str, str]:
        # Head and base spans of one file sit next to each other: sort on the path without its key.
        rel = path.partition("/")[2]
        return str(PurePosixPath(rel).parent), path

    for path in sorted(by_file, key=order):
        group = sorted(by_file[path], key=lambda s: s.start)
        n = sum(s.lines for s in group)
        if n > limit:
            add(group, oversize=True)
            continue
        if size + n > limit and current:
            add(current)
            current, size = [], 0
        current += group
        size += n
    if current:
        add(current)
    return units


def unit_line_totals(units: list[Unit]) -> dict[str, int]:
    """Span lines per file across the units: what `partition_gate` counts for a change."""
    out: dict[str, int] = {}
    for u in units:
        for s in u.spans:
            out[s.path] = out.get(s.path, 0) + s.lines
    return out
