"""Read-only git access: the diff rows and the full-file view for one path.

Every git call here runs with a fixed argv, no shell, and stdin closed. Nothing
in this module checks out, fetches, or writes to the repository — the server
reads history, it never changes it.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .models import FindingsFile, Target

log = logging.getLogger(__name__)

# A file deleted at head shows this many of its old lines, not all of them:
# the point is to recognise the file, not to review what no longer exists.
DELETED_PREVIEW_LINES = 120

# Above this size the view is the hunks plus the cited ranges, not the file.
MAX_FULL_LINES = 4000
CITED_CONTEXT = 12
HUNK_CONTEXT = 3

LANGUAGES = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".json": "json", ".md": "markdown", ".html": "xml",
    ".htm": "xml", ".xml": "xml", ".svg": "xml", ".css": "css", ".scss": "scss",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".yml": "yaml",
    ".yaml": "yaml", ".toml": "ini", ".ini": "ini", ".cfg": "ini", ".sql": "sql",
    ".go": "go", ".rs": "rust", ".swift": "swift", ".java": "java",
    ".kt": "kotlin", ".rb": "ruby", ".php": "php", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp", ".cs": "csharp",
    ".dockerfile": "dockerfile", ".tf": "hcl", ".lua": "lua", ".r": "r",
}
FILENAMES = {"Dockerfile": "dockerfile", "Makefile": "makefile"}

RowKind = Literal["ctx", "add", "del", "gap"]


class GitError(RuntimeError):
    """A git command failed; the message carries git's stderr."""


class PathNotInReview(LookupError):
    """The path is neither changed, cited, nor the file of a finding."""


@dataclass
class Row:
    k: RowKind
    o: int | None
    n: int | None
    t: str


@dataclass
class FileView:
    path: str
    language: str
    rows: list[Row] = field(default_factory=list)
    cited_ranges: list[tuple[int, int]] = field(default_factory=list)
    truncated: bool = False
    deleted: bool = False
    binary: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def git(repo: str | Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], shell=False, stdin=subprocess.DEVNULL,
        capture_output=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise GitError(f"git {args[0]} failed: {proc.stderr.strip()}")
    return proc.stdout


def parse_unified(text: str) -> list[Row]:
    """Turn a unified diff into display rows.

    Each row is `{k: ctx|add|del|gap, o: old line no, n: new line no, t: text}`.
    A `gap` row marks the jump between two hunks and carries the `@@` header.
    """
    rows: list[Row] = []
    old_no = new_no = 0
    in_body = False
    for raw in text.splitlines():
        if raw.startswith("@@"):
            in_body = True
            head = raw.split("@@")[1].strip()
            try:
                old_part, new_part = head.split(" ")
                old_no = int(old_part[1:].split(",")[0])
                new_no = int(new_part[1:].split(",")[0])
            except (ValueError, IndexError):
                old_no = new_no = 0
            if rows:
                rows.append(Row("gap", None, None, raw))
            continue
        if not in_body:
            continue
        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        marker, body = raw[:1], raw[1:]
        if marker == "+":
            rows.append(Row("add", None, new_no, body))
            new_no += 1
        elif marker == "-":
            rows.append(Row("del", old_no, None, body))
            old_no += 1
        else:
            rows.append(Row("ctx", old_no, new_no, body))
            old_no += 1
            new_no += 1
    return rows


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _hunks(diff_text: str) -> tuple[set[int], dict[int, list[Row]]]:
    """Added new-side line numbers, and deleted rows keyed by the new-side line
    they appear before. Expects a `--unified=0` diff."""
    added: set[int] = set()
    deleted_before: dict[int, list[Row]] = {}
    anchor = old_no = new_no = 0
    for raw in diff_text.splitlines():
        m = _HUNK_RE.match(raw)
        if m:
            new_start = int(m.group(3))
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            # A pure deletion reports the new-side line *after which* the lines
            # went; any other hunk reports the first line it touches.
            anchor = new_start + 1 if new_len == 0 else new_start
            old_no = int(m.group(1))
            new_no = new_start
            continue
        if not anchor:
            continue
        if raw.startswith("-") and not raw.startswith("---"):
            deleted_before.setdefault(anchor, []).append(Row("del", old_no, None, raw[1:]))
            old_no += 1
        elif raw.startswith("+") and not raw.startswith("+++"):
            added.add(new_no)
            new_no += 1
    return added, deleted_before


def language_for(path: str) -> str:
    p = Path(path)
    if p.name in FILENAMES:
        return FILENAMES[p.name]
    return LANGUAGES.get(p.suffix.lower(), "plaintext")


def allowed_paths(target: Target, findings: FindingsFile | None) -> set[str]:
    paths = set(target.files_changed)
    if findings is not None:
        for f in findings.findings:
            paths.add(f.file)
            paths.update(c.path for c in f.citations)
            if f.fix:
                paths.update(p.citation.path for p in f.fix.premises if p.citation)
    return paths


def _cited_ranges(path: str, findings: FindingsFile | None) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    if findings is None:
        return ranges
    for f in findings.findings:
        if f.file == path:
            ranges.append((f.line_start, f.line_end))
        ranges.extend((c.line_start, c.line_end) for c in f.citations if c.path == path)
    return sorted(set(ranges))


def _is_binary(repo: str, base: str, head: str, path: str) -> bool:
    out = git(repo, "diff", "--numstat", base, head, "--", path)
    return any(line.startswith("-\t-\t") for line in out.splitlines())


def _exists_at(repo: str, sha: str, path: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", repo, "cat-file", "-e", f"{sha}:{path}"], shell=False,
        stdin=subprocess.DEVNULL, capture_output=True)
    return proc.returncode == 0


def file_view(target: Target, path: str, findings: FindingsFile | None = None,
              allowed: set[str] | None = None) -> FileView:
    """The file at head, with the diff folded in and cited ranges attached."""
    if allowed is None:
        allowed = allowed_paths(target, findings)
    if path not in allowed:
        raise PathNotInReview(path)
    repo, base, head = target.repo_path, target.base_sha, target.head_sha
    view = FileView(path=path, language=language_for(path),
                    cited_ranges=_cited_ranges(path, findings))

    if _is_binary(repo, base, head, path):
        view.binary = True
        return view

    if not _exists_at(repo, head, path):
        if not _exists_at(repo, base, path):
            raise GitError(f"{path} exists at neither {base[:8]} nor {head[:8]}")
        old = git(repo, "show", f"{base}:{path}").splitlines()
        view.deleted = True
        view.truncated = len(old) > DELETED_PREVIEW_LINES
        view.rows = [Row("del", i, None, t)
                     for i, t in enumerate(old[:DELETED_PREVIEW_LINES], 1)]
        return view

    lines = git(repo, "show", f"{head}:{path}").splitlines()
    added, deleted_before = _hunks(
        git(repo, "diff", "--unified=0", base, head, "--", path))

    keep: set[int] | None = None
    if len(lines) > MAX_FULL_LINES:
        view.truncated = True
        keep = set()
        touched = added | set(deleted_before)
        for n in touched:
            keep.update(range(n - HUNK_CONTEXT, n + HUNK_CONTEXT + 1))
        for start, end in view.cited_ranges:
            keep.update(range(start - CITED_CONTEXT, end + CITED_CONTEXT + 1))

    rows: list[Row] = []
    last = 0
    for n in range(1, len(lines) + 2):
        if keep is not None and n not in keep:
            continue
        if keep is not None and last and n != last + 1:
            rows.append(Row("gap", None, None, f"… lines {last + 1}–{n - 1} not shown"))
        rows.extend(deleted_before.get(n, []))
        if n <= len(lines):
            rows.append(Row("add" if n in added else "ctx", None, n, lines[n - 1]))
        last = n
    view.rows = rows
    return view


def diff_target(repo: str | Path, rev_range: str) -> Target:
    """A Target for plain-diff mode: `<base>..<head>` resolved to full shas."""
    if ".." not in rev_range:
        raise ValueError(f"expected <base>..<head>, got {rev_range!r}")
    base_ref, head_ref = rev_range.split("..", 1)
    repo_path = Path(repo).resolve()
    base = git(repo_path, "rev-parse", "--verify", f"{base_ref or 'HEAD'}^{{commit}}").strip()
    head = git(repo_path, "rev-parse", "--verify", f"{head_ref or 'HEAD'}^{{commit}}").strip()
    files = [f for f in git(repo_path, "diff", "--name-only", base, head).splitlines() if f]
    return Target(
        slug=f"{repo_path.name}--{base[:8]}..{head[:8]}",
        label=f"{repo_path.name} {rev_range}",
        repo_path=str(repo_path), remote_url=None,
        base_sha=base, head_sha=head, files_changed=files)


def diff_findings(target: Target) -> FindingsFile:
    """The in-memory FindingsFile for plain-diff mode: a target and no findings."""
    return FindingsFile(
        schema_version=1, generated_at=datetime.now(timezone.utc),
        producer="review-viewer:diff", target=target, findings=[])
