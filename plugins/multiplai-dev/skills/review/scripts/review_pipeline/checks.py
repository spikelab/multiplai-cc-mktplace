"""The record of what each agent was given, what it did, and what it concluded.

Pure functions the stages use to build `models.AgentCheck` entries. They work
only from what `run_agent` already returns, a tool call's name and input,
never from its result, so the record can never hold file contents or fetched
page text.

`seen` says how an agent came to a line it cites:

| mark       | when |
|------------|------|
| `read`     | a `Read` of that path whose range (or the whole file) covers any cited line |
| `searched` | no such read, but a `Grep` whose path covers the file |
| `diff`     | neither, but the cited lines fall inside a hunk of the diff the prompt held |
| `fetched`  | a URL citation the agent fetched with `WebFetch` |
| `not-seen` | none of the above |
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .gates import citation_gate
from .models import Citation, TargetInfo
from .stages import relative_path

SEEN_MARKS = ("read", "searched", "diff", "fetched", "not-seen")

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def now() -> str:
    """The pipeline's clock, as an ISO UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _input(call) -> dict:
    data = getattr(call, "input", None)
    return data if isinstance(data, dict) else {}


def _line_range(data: dict) -> tuple[int, int | None] | None:
    """(first, last) lines a Read covers; last None means to the end. None is the whole file."""
    offset, limit = data.get("offset"), data.get("limit")
    if offset is None and limit is None:
        return None
    try:
        first = max(1, int(offset)) if offset is not None else 1
        last = first + int(limit) - 1 if limit is not None else None
    except (TypeError, ValueError):
        return None
    return first, last


def summarise_call(call, target: TargetInfo, snapshot: Path) -> dict:
    """One tool call as `{tool, target, detail}`, with repo-relative paths. Inputs only."""
    name = getattr(call, "name", "") or "?"
    data = _input(call)
    rel = lambda p: relative_path(str(p), target, snapshot) if p else ""  # noqa: E731
    if name == "Read":
        rng = _line_range(data)
        if rng is None:
            detail = "whole file"
        elif rng[1] is None:
            detail = f"lines {rng[0]}-end"
        else:
            detail = f"lines {rng[0]}-{rng[1]}"
        return {"tool": name, "target": rel(data.get("file_path", "")), "detail": detail}
    if name == "Grep":
        where = rel(data.get("path", "")) or "."
        extra = [f"{k} {data[k]}" for k in ("glob", "type") if data.get(k)]
        return {"tool": name, "target": str(data.get("pattern", "")), "detail": ", ".join([f"in {where}", *extra])}
    if name == "Glob":
        return {"tool": name, "target": str(data.get("pattern", "")),
                "detail": f"in {rel(data.get('path', '')) or '.'}"}
    if name == "WebFetch":
        return {"tool": name, "target": str(data.get("url", "")), "detail": ""}
    if name == "WebSearch":
        return {"tool": name, "target": str(data.get("query", "")), "detail": ""}
    return {"tool": name, "target": "", "detail": "input keys: " + ", ".join(sorted(data))}


def diff_hunks(diff: str) -> dict[str, list[tuple[int, int]]]:
    """New-side line ranges of every hunk, per path (deleted files have none)."""
    hunks: dict[str, list[tuple[int, int]]] = {}
    path = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            name = line[4:].strip()
            path = "" if name == "/dev/null" else (name[2:] if name.startswith("b/") else name)
            continue
        m = _HUNK.match(line)
        if m and path:
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            if count:
                hunks.setdefault(path, []).append((start, start + count - 1))
    return hunks


def _url_key(url: str) -> str:
    return url.strip().rstrip("/").lower()


def _covers(path: str, file: str) -> bool:
    path = path.strip().rstrip("/")
    return path in ("", ".") or file == path or file.startswith(path + "/")


def seen(citation: Citation, calls: list, hunks: dict[str, list[tuple[int, int]]],
         target: TargetInfo, snapshot: Path) -> str:
    """How the agent came to *citation*'s lines: one of SEEN_MARKS. *calls* are raw ToolCalls."""
    if citation.is_web:
        fetched = {_url_key(str(_input(c).get("url", ""))) for c in calls if getattr(c, "name", "") == "WebFetch"}
        return "fetched" if _url_key(citation.path) in fetched else "not-seen"
    searched = False
    for call in calls:
        name, data = getattr(call, "name", ""), _input(call)
        if name == "Read" and relative_path(str(data.get("file_path", "")), target, snapshot) == citation.path:
            rng = _line_range(data)
            if rng is None:
                return "read"
            first, last = rng
            if first <= citation.line_end and (last is None or last >= citation.line_start):
                return "read"
        elif name == "Grep" and _covers(relative_path(str(data.get("path", "") or ""), target, snapshot),
                                        citation.path):
            searched = True
    if searched:
        return "searched"
    for start, end in hunks.get(citation.path, []):
        if start <= citation.line_end and end >= citation.line_start:
            return "diff"
    return "not-seen"


def marked_citation(citation: Citation, calls: list, hunks: dict[str, list[tuple[int, int]]],
                    target: TargetInfo, snapshot: Path) -> dict:
    """A citation with its `gate` (pass/fail/web) and `seen` marks."""
    gate = "web" if citation.is_web else ("pass" if citation_gate(target, citation).passed else "fail")
    return {"path": citation.path, "line_start": citation.line_start, "line_end": citation.line_end,
            "quote": citation.quote, "gate": gate, "seen": seen(citation, calls, hunks, target, snapshot)}


def failure_kind(error: str) -> str:
    """A short reason kind for a failed agent call, never its text."""
    low = error.lower()
    if "timeout" in low or "timed out" in low:
        return "timeout"
    if "no valid answer" in low:
        return "no valid answer after a re-ask"
    return "agent error"


def prompt_labels(target: TargetInfo, *, diff: str | None = None, diff_limit: int = 0,
                  rules: list[tuple[str, bool]] | None = None, extra: list[str] | None = None) -> list[str]:
    """Short labels for the input blocks a prompt held.

    *diff* is the diff text when the prompt included it; *rules* the
    (path, included) pairs `conventions_blocks` returned.
    """
    labels = list(extra or [])
    if diff is not None:
        cut = f", cut at {diff_limit:,} characters" if diff_limit and len(diff) > diff_limit else ""
        labels.append(f"diff ({len(target.files)} files{cut})")
        labels.append(f"commit subjects ({len(target.commits)})")
    if target.title or target.description:
        labels.append("PR description")
    if target.base_ref:
        labels.append("branch rules" if target.branch_rules is not None else "branch rules (could not be read)")
    for path, included in rules or []:
        labels.append(f"rules: {path}" if included else f"rules skipped: {path}")
    return labels
