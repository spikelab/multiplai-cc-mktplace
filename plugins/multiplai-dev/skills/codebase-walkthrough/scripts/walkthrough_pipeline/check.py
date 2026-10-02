"""`check`: re-run `citation_gate` on every `path:line` link and snippet in a finished walkthrough.

It reads the "Commits read" table in the file's header for each repo's path
and commit, then:

- every link `[`repo/path:L-M`](url "quote")` must have its quote inside
  lines L..M at that commit;
- every snippet (`<!-- snippet repo/path:L-M -->` then a fenced block) must
  equal lines L..M at that commit.

With `--at-head` each repo's current HEAD is used instead, which says whether
the code has moved since the walkthrough was written.
"""

from __future__ import annotations

import re
from pathlib import Path

from .gates import citation_gate, lines_at_commit
from .models import Citation, RepoInfo
from .target import head_sha

ROW = re.compile(r"^\|\s*([^|\s]+)\s*\|\s*([^|]+?)\s*\|\s*`([0-9a-f]{40})`\s*\|\s*$", re.M)
LINK = re.compile(r'\[`([^`\n]+?):(\d+)(?:-(\d+))?`\]\(([^)\s]*)\s+"((?:[^"\\]|\\.)*)"\)')
# Anything that starts like a link. One that LINK cannot parse is reported as a failure, never skipped.
LINK_START = re.compile(r"\[`[^`\n]+?:\d+(?:-\d+)?`\]\(")
SNIPPET = re.compile(r"<!-- snippet (\S+?):(\d+)-(\d+) -->\n.*?\n\n(`{3,})[^\n]*\n(.*?)\n\4", re.S)


def read_header(text: str, base: Path, overrides: dict[str, str] | None = None) -> dict[str, RepoInfo]:
    repos: dict[str, RepoInfo] = {}
    for key, path, sha in ROW.findall(text):
        where = Path((overrides or {}).get(key) or (base / path)).expanduser().resolve()
        repos[key] = RepoInfo(key=key, path=str(where), head_sha=sha)
    return repos


def check_text(text: str, base: Path, *, at_head: bool = False,
               repo_paths: dict[str, str] | None = None) -> tuple[int, list[str]]:
    """(number checked, failure lines)."""
    repos = read_header(text, base, repo_paths)
    failures: list[str] = []
    if not repos:
        return 0, ["no 'Commits read' table found in the header"]
    if at_head:
        for key, repo in list(repos.items()):
            sha = head_sha(repo.path)
            if sha is None:
                failures.append(f"{key}: {repo.path} has no HEAD")
            else:
                repos[key] = repo.model_copy(update={"head_sha": sha})
    checked = 0
    parsed = {m.start() for m in LINK.finditer(text)}
    for m in LINK_START.finditer(text):
        if m.start() not in parsed:
            checked += 1
            failures.append(f"link could not be parsed: {text[m.start():m.start() + 120].splitlines()[0]}")
    for m in LINK.finditer(text):
        path, start, end = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))
        quote = re.sub(r"\\(.)", r"\1", m.group(5))
        checked += 1
        if not quote.strip():
            if lines_at_commit(repos, path, start, end) in (None, []):
                failures.append(f"{path}:{start}: line not at commit")
            continue
        g = citation_gate(repos, Citation(path=path, line_start=start, line_end=end, quote=quote))
        if not g.passed:
            failures.append(f"{path}:{start}-{end}: {g.reason}")
    for m in SNIPPET.finditer(text):
        path, start, end, body = m.group(1), int(m.group(2)), int(m.group(3)), m.group(5)
        checked += 1
        lines = lines_at_commit(repos, path, start, end)
        if lines is None:
            failures.append(f"snippet {path}:{start}-{end}: path not at commit")
        elif [x.rstrip() for x in lines] != [x.rstrip() for x in body.split("\n")]:
            failures.append(f"snippet {path}:{start}-{end}: lines differ from the commit")
    return checked, failures


def check_file(path: Path, *, at_head: bool = False, repo_paths: dict[str, str] | None = None) -> tuple[int, list[str]]:
    path = path.expanduser().resolve()
    return check_text(path.read_text(encoding="utf-8"), path.parent, at_head=at_head, repo_paths=repo_paths)
