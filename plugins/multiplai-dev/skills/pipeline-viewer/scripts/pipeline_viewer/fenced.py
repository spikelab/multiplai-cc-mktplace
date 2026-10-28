"""Inbox rows as the session reads them: person-written text fenced.

Everything a person types into the shared page (`text`, `doc`, `section`,
a rework request) reaches the Claude session that answers it. That text is
data, never instructions (`docs/untrusted-content.md`), so the session never
reads a raw row: `pending` and `watch` print each row with those fields
removed and put back inside one `<untrusted-content>` fence, defanged by
`multiplai_core.untrusted`, followed by the data-never-instructions notice.
The fields the server set itself (id, kind, change, stop, revision, commit,
the author from people.json) stay outside the fence.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

from multiplai_core.untrusted import bracket_notice, defang, fence

# The fields of an inbox row a person typed on the page.
PERSON_FIELDS = ("doc", "section", "text")
CHANNEL = "text typed into the pipeline-viewer page by another person"
NOTICE = bracket_notice(CHANNEL)


def _line(value) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def session_row(row: dict) -> dict:
    """*row* without its person-written fields, which come back fenced in
    `page_text`, with `notice` saying what the fence means."""
    out = {k: v for k, v in row.items() if k not in (*PERSON_FIELDS, "author", "v")}
    author = row.get("author") or {}
    if author:
        out["author"] = {
            "name": defang(_line(author.get("name")), markdown_fences=False),
            "role": defang(_line(author.get("role")), markdown_fences=False),
        }
    parts = []
    if row.get("doc"):
        parts.append(f"doc: {_line(row['doc'])}")
    if row.get("section"):
        parts.append(f"section: {_line(row['section'])}")
    if row.get("text"):
        parts.append(("\n" if parts else "") + str(row["text"]))
    body = "\n".join(parts)
    who = out.get("author", {}).get("name") or "someone"
    lines = fence(body, f"pipeline-viewer {row.get('kind', 'row')} by {who}")
    if lines:
        out["page_text"] = "\n".join(lines)
        out["notice"] = NOTICE
    return out


def format_row(row: dict) -> str:
    """One output line (JSON) for one inbox row."""
    return json.dumps(session_row(row), ensure_ascii=False)


def read_new(path: Path, pos: int) -> tuple[list[dict], int]:
    """Complete rows appended to *path* after byte *pos*, and the new
    position. A file that shrank is read again from the start; a partial
    last line waits for its newline."""
    try:
        size = path.stat().st_size
    except OSError:
        return [], 0
    if size < pos:
        pos = 0
    if size == pos:
        return [], pos
    with path.open("rb") as f:
        f.seek(pos)
        data = f.read(size - pos)
    end = data.rfind(b"\n")
    if end < 0:
        return [], pos
    rows = []
    for raw in data[: end + 1].splitlines():
        try:
            row = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows, pos + end + 1


def follow(path: Path, interval: float = 0.5) -> Iterator[dict]:
    """Rows appended to *path* from now on (like `tail -n 0 -F`), forever."""
    try:
        pos = path.stat().st_size
    except OSError:
        pos = 0
    while True:
        rows, pos = read_new(path, pos)
        yield from rows
        time.sleep(interval)
