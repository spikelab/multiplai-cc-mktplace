"""The Markdown docs the conventions finder's rules files link, read from the review's commits.

`linked_docs` takes the `CLAUDE.md` and `coding-standards.md` files
`stages.find.conventions_blocks` read (their text at head and, on a change
review, at base) and follows every link in them, one level only, to a
Markdown file inside the repository. Each doc is read with git at head, or at
base when the change deleted it, never from the working tree, and is fenced
as untrusted text: the change's author controls it. A doc goes in whole or
not at all; one that cannot be followed is a `conventions` coverage record.
"""

from __future__ import annotations

import posixpath
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from multiplai_core.citations import GIT_ARGV, git_env

from .models import CoverageRecord
from .prompts.untrusted import fenced

INLINE_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REFERENCE_DEF = re.compile(r"^\s{0,3}\[([^\]]+)\]:\s*<?(\S+?)>?(?:\s|$)")
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
BINDING = re.compile(r"(?i)\b(bind|binding|must|required)\b")
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
MARKDOWN_SUFFIXES = (".md", ".markdown")
SYMLINK_MODE = "120000"
REGULAR_MODES = ("100644", "100755")


@dataclass(frozen=True)
class LinkSource:
    path: str
    head_text: str | None
    base_text: str | None


@dataclass(frozen=True)
class LinkedDocs:
    blocks: list[str]  # fenced(path, content), in the order of `included`
    included: list[str]
    skipped: list[CoverageRecord]


def _unfenced_lines(text: str) -> list[tuple[int, str]]:
    """(index, line) of every line outside a fenced code block."""
    out, fence = [], None
    for i, line in enumerate(text.split("\n")):
        m = FENCE_OPEN.match(line)
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line[m.end():].strip():
                fence = None
            continue
        if m:
            fence = m.group(1)
            continue
        out.append((i, line))
    return out


def links(text: str) -> list[tuple[str, bool]]:
    """(target, binding) of each link in *text*, in line order, fenced code skipped.

    A link is binding when a line linking it has one of the keywords: for a
    reference definition, the definition line or any line using its label.
    """
    lines = _unfenced_lines(text)
    found: list[tuple[int, int, str, bool]] = []
    for i, line in lines:
        definition = REFERENCE_DEF.match(line)
        if definition:
            label = definition.group(1).strip().lower()
            using = [ln for _, ln in lines if f"[{label}]" in ln.lower()]
            found.append((i, 0, definition.group(2), any(BINDING.search(ln) for ln in using)))
            continue
        for m in INLINE_LINK.finditer(line):
            found.append((i, m.start(), m.group(1), bool(BINDING.search(line))))
    return [(target, binding) for _, _, target, binding in sorted(found)]


def resolve(source: str, target: str) -> str | None:
    """The repository path *target* (linked from *source*) names, or None when it is not followed:
    a URL, or not a Markdown file. An escaping path is returned as it normalises (`../...`)."""
    if SCHEME.match(target):
        return None
    bare = unquote(re.split(r"[#?]", target, maxsplit=1)[0])
    if not bare or not bare.lower().endswith(MARKDOWN_SUFFIXES):
        return None
    joined = bare.lstrip("/") if bare.startswith("/") else posixpath.join(posixpath.dirname(source), bare)
    return posixpath.normpath(joined)


def _outside(path: str) -> bool:
    return path == ".." or path.startswith("../") or path.startswith("/")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([*GIT_ARGV, "-C", str(repo), *args], capture_output=True, stdin=subprocess.DEVNULL,
                          env=git_env(), shell=False, check=False)


def _entry(repo: Path, sha: str, path: str) -> tuple[str, str] | None:
    """(mode, type) of *path* in commit *sha*, or None when it is not there."""
    proc = _git(repo, "ls-tree", "-z", sha, "--", path)
    if proc.returncode != 0:
        return None
    for record in proc.stdout.split(b"\0"):
        meta, _, name = record.partition(b"\t")
        if name.decode("utf-8", errors="surrogateescape") == path:
            mode, kind, _ = meta.decode("ascii", errors="replace").split(" ", 2)
            return mode, kind
    return None


def _blob(repo: Path, sha: str, path: str) -> bytes | None:
    proc = _git(repo, "show", f"{sha}:{path}")
    return proc.stdout if proc.returncode == 0 else None


def read_doc(repo: Path, sha: str, path: str) -> tuple[str | None, str]:
    """(content, "") of the doc at *path* in *sha*, or (None, reason): `outside-repo` for a symlink out of
    the repository, `unreadable` for anything not a regular UTF-8 file. A symlink inside it is followed once."""
    entry = _entry(repo, sha, path)
    if entry is None:
        return None, "missing"
    mode, kind = entry
    if mode == SYMLINK_MODE:
        link = _blob(repo, sha, path)
        if link is None:
            return None, "unreadable"
        aim = link.decode("utf-8", errors="replace")
        if aim.startswith("/"):
            return None, "outside-repo"
        path = posixpath.normpath(posixpath.join(posixpath.dirname(path), aim))
        if _outside(path):
            return None, "outside-repo"
        entry = _entry(repo, sha, path)
        if entry is None:
            return None, "unreadable"
        mode, kind = entry
    if kind != "blob" or mode not in REGULAR_MODES:
        return None, "unreadable"
    data = _blob(repo, sha, path)
    if data is None:
        return None, "unreadable"
    try:
        return data.decode("utf-8"), ""
    except UnicodeDecodeError:
        return None, "unreadable"


def linked_docs(sources: list[LinkSource], *, repo: Path, head: str, base: str | None,
                remaining: int) -> LinkedDocs:
    """The docs *sources* link, fenced, binding ones first, while each whole block fits in *remaining*.

    Links come from each source at head, then (with *base*) those only at
    base. A path is followed once, and never back to a source. A doc is read
    at head when it is there, otherwise at base.
    """
    own = {s.path for s in sources}
    order: dict[str, bool] = {}
    sides = [[s.head_text for s in sources]] + ([[s.base_text for s in sources]] if base is not None else [])
    for texts in sides:
        for source, text in zip(sources, texts):
            for target, binding in links(text or ""):
                path = resolve(source.path, target)
                if path is None or path in own:
                    continue
                order[path] = order.get(path, False) or binding
    ranked = [p for p, b in order.items() if b] + [p for p, b in order.items() if not b]

    def skip(path: str, reason: str) -> None:
        skipped.append(CoverageRecord(stage="conventions", subject=path, level="info", reason=reason))

    blocks, included, skipped = [], [], []
    for path in ranked:
        if _outside(path):
            skip(path, "outside-repo")
            continue
        content, reason = read_doc(repo, head, path)
        if reason == "missing" and base is not None:
            content, reason = read_doc(repo, base, path)
        if content is None:
            skip(path, reason)
            continue
        block = fenced(path, content)
        if len(block) > remaining:
            skip(path, "budget")
            continue
        blocks.append(block)
        included.append(path)
        remaining -= len(block)
    return LinkedDocs(blocks=blocks, included=included, skipped=skipped)
