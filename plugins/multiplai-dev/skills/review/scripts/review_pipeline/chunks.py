"""The extra finders' chunks: a change's files in diff order, about CHUNK_LINES changed lines each.

Ported from `finderChunks` in the run scripts (run-workflow-2026-10-15.js). A file at or
above the chunk size is a chunk of its own; lockfiles are left out. When there would be more
than MAX_FINDERS chunks, the chunk size grows to fit, then the adjacent pair with the fewest
lines merges until they do (greedy grouping can overshoot by one or two).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .target import LOCKFILES

CHUNK_LINES = 600
MAX_FINDERS = 6


@dataclass
class FileChange:
    path: str
    added: int
    deleted: int
    text: str = ""  # this file's part of the diff, from its `diff --git` line

    @property
    def lines(self) -> int:
        return self.added + self.deleted


@dataclass
class Chunk:
    files: list[str] = field(default_factory=list)
    lines: int = 0
    text: str = ""  # the diff of its files, in diff order


def _section_path(header: str, lines: list[str]) -> str:
    """The file a `diff --git` section is about: the new side, or the old one for a deleted file."""
    old = new = ""
    for line in lines:
        if line.startswith("@@"):
            break
        if line.startswith("--- "):
            old = line[4:].strip()
        elif line.startswith("+++ "):
            new = line[4:].strip()
    for name, prefix in ((new, "b/"), (old, "a/")):
        if name and name != "/dev/null":
            return name[len(prefix):] if name.startswith(prefix) else name
    rest = header[len("diff --git "):]
    return rest.rsplit(" b/", 1)[-1] if " b/" in rest else rest


def diff_files(diff: str) -> list[FileChange]:
    """Each file in *diff* with its added and deleted lines, in diff order (a binary file counts 0)."""
    sections: list[list[str]] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
    files = []
    for section in sections:
        added = deleted = 0
        in_hunk = False
        for line in section[1:]:
            if line.startswith("@@"):
                in_hunk = True
            elif in_hunk and line.startswith("+"):
                added += 1
            elif in_hunk and line.startswith("-"):
                deleted += 1
        files.append(FileChange(path=_section_path(section[0].rstrip("\n"), section[1:]), added=added,
                                deleted=deleted, text="".join(section)))
    return files


def _chunk(files: list[FileChange], size: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    cur = Chunk()
    for f in files:
        if f.lines >= size:
            if cur.files:
                chunks.append(cur)
            chunks.append(Chunk(files=[f.path], lines=f.lines, text=f.text))
            cur = Chunk()
            continue
        if cur.lines + f.lines > size and cur.files:
            chunks.append(cur)
            cur = Chunk()
        cur.files.append(f.path)
        cur.lines += f.lines
        cur.text += f.text
    if cur.files:
        chunks.append(cur)
    return chunks


def finder_chunks(files: list[FileChange]) -> list[Chunk]:
    """At most MAX_FINDERS chunks of about CHUNK_LINES changed lines; none when only lockfiles changed."""
    kept = [f for f in files if f.path.rsplit("/", 1)[-1] not in LOCKFILES]
    if not kept:
        return []
    total = sum(f.lines for f in kept)
    chunks = _chunk(kept, max(CHUNK_LINES, -(-total // MAX_FINDERS)))
    while len(chunks) > MAX_FINDERS:
        best = min(range(len(chunks) - 1), key=lambda i: chunks[i].lines + chunks[i + 1].lines)
        a, b = chunks[best], chunks[best + 1]
        chunks[best:best + 2] = [Chunk(files=a.files + b.files, lines=a.lines + b.lines, text=a.text + b.text)]
    return chunks


_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def head_changed_lines(diff: str) -> dict[str, list[int]]:
    """The head-side line numbers of each file's added lines, under its head path, in diff order.

    A hunk's body is counted from its `@@` lengths, never recognised by its
    first characters, so an added line reading `++ b/x` is a line, not a file
    header. The text is split on `\n` only, which is how git numbers lines. A
    deleted file, a pure deletion and a binary file contribute nothing; a
    renamed file appears under its new path.
    """
    out: dict[str, list[int]] = {}
    lines = diff.split("\n")
    path: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith("diff --git "):
            path = None
        elif line.startswith("+++ ") and path is None:
            name = line[4:].rstrip("\r")
            path = "" if name == "/dev/null" else name[2:] if name.startswith("b/") else name
        elif (m := _HUNK.match(line)) and path is not None:
            old_left = int(m.group(1)) if m.group(1) is not None else 1
            number = int(m.group(2))
            new_left = int(m.group(3)) if m.group(3) is not None else 1
            while (old_left > 0 or new_left > 0) and i < len(lines):
                body = lines[i]
                i += 1
                if body.startswith("\\"):  # "\ No newline at end of file"
                    continue
                if body.startswith("+"):
                    if path:
                        out.setdefault(path, []).append(number)
                    number += 1
                    new_left -= 1
                elif body.startswith("-"):
                    old_left -= 1
                else:
                    number += 1
                    new_left -= 1
                    old_left -= 1
    return out
