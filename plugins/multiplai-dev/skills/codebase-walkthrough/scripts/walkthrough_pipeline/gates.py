"""Pure-code gates. None of them calls a model.

Everything a gate checks against code is re-read from the commit with
`git show <sha>:<path>`, never taken from what an agent said it read, and
never from the snapshot an agent could see. Doc quotes are checked against the
cached text code fetched.

Names must not start with `test`, or pytest collects them.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

from multiplai_core.citations import check_quote, file_at, lines_at
from multiplai_core.citations import normalise_ws as normalise

from .models import (Citation, Fact, GateResult, Hop, Reference, RepoInfo, SymbolEntry, TargetInfo, Unit)
from .target import require_rg

SMALL_FILE_LINES = 30


def split_path(repos: dict[str, RepoInfo], ws_path: str) -> tuple[RepoInfo, str] | None:
    """`DolceEngine/DolceChannex/urls.py` -> (the DolceEngine repo, `DolceChannex/urls.py`)."""
    p = ws_path.strip().lstrip("./")
    key, _, rel = p.partition("/")
    repo = repos.get(key)
    if repo is None or not rel:
        return None
    return repo, rel


def file_at_commit(repos: dict[str, RepoInfo], ws_path: str) -> str | None:
    found = split_path(repos, ws_path)
    return None if found is None else file_at(found[0].path, found[0].head_sha, found[1])


def lines_at_commit(repos: dict[str, RepoInfo], ws_path: str, start: int, end: int) -> list[str] | None:
    found = split_path(repos, ws_path)
    return None if found is None else lines_at(found[0].path, found[0].head_sha, found[1], start, end)


# --- citations ----------------------------------------------------------------------


def citation_gate(repos: dict[str, RepoInfo], citation: Citation) -> GateResult:
    """The quote, whitespace-normalised, is inside lines line_start..line_end at the repo's commit."""
    found = split_path(repos, citation.path)
    if found is None:
        # An empty quote is named first, whatever the path, as before.
        reason = "empty quote" if not normalise(citation.quote) else "path names no known repo"
        return GateResult(passed=False, reason=reason)
    repo, rel = found
    reason = check_quote(repo.path, repo.head_sha, rel, citation.line_start, citation.line_end, citation.quote)
    if reason is not None:
        return GateResult(passed=False, reason=reason)
    return GateResult(passed=True)


def entry_citation(entry: SymbolEntry | Reference) -> Citation:
    return Citation(path=entry.path, line_start=entry.line, line_end=entry.line, quote=entry.quote)


def boundary_gate(repos: dict[str, RepoInfo], symbols: list[SymbolEntry], refs: list[Reference]
                  ) -> tuple[list[SymbolEntry], list[Reference], list[dict]]:
    """Every entry's quote is at its line at its repo's commit. Re-checks entries
    code produced too, so a parser bug cannot slip through."""
    dropped: list[dict] = []
    kept_s, kept_r = [], []
    for e in symbols:
        g = citation_gate(repos, entry_citation(e))
        (kept_s.append(e) if g.passed else dropped.append({"entry": e.model_dump(), "reason": g.reason}))
    for r in refs:
        g = citation_gate(repos, entry_citation(r))
        (kept_r.append(r) if g.passed else dropped.append({"entry": r.model_dump(), "reason": g.reason}))
    return kept_s, kept_r, dropped


# --- partition and coverage -------------------------------------------------------------


def partition_gate(units: list[Unit], files: list[str], lines: dict[str, int], limit: int) -> GateResult:
    """Every file in exactly one unit; no unit over *limit* lines unless it is one oversize file."""
    seen: dict[str, str] = {}
    for u in units:
        for f in u.files:
            if f in seen:
                return GateResult(passed=False, reason=f"{f} is in {seen[f]} and {u.id}")
            seen[f] = u.id
        total = sum(lines.get(f, 0) for f in u.files)
        if total > limit and not (len(u.files) == 1 and u.oversize):
            return GateResult(passed=False, reason=f"{u.id} has {total} lines (limit {limit})")
    missing = [f for f in files if f not in seen]
    if missing:
        return GateResult(passed=False, reason=f"{len(missing)} file(s) in no unit: {', '.join(missing[:5])}")
    extra = [f for f in seen if f not in set(files)]
    if extra:
        return GateResult(passed=False, reason=f"unknown file(s) in units: {', '.join(extra[:5])}")
    return GateResult(passed=True)


def _cited_ranges(facts: list[Fact]) -> dict[str, list[tuple[int, int]]]:
    out: dict[str, list[tuple[int, int]]] = {}
    for fact in facts:
        for c in fact.citations:
            out.setdefault(c.path, []).append((c.line_start, c.line_end))
    return out


def file_coverage_gate(ws_files: list[str], lines: dict[str, int], facts: list[Fact]) -> list[str]:
    """Files (workspace-relative) with no surviving fact that are longer than 30 lines."""
    cited = _cited_ranges(facts)
    return [f for f in ws_files if f not in cited and lines.get(f, 0) > SMALL_FILE_LINES]


def entry_key(entry: SymbolEntry | Reference) -> str:
    return f"{entry.path}:{entry.line}"


def boundary_coverage_gate(entries: list[SymbolEntry | Reference], facts: list[Fact]) -> list[str]:
    """Entry keys not cited by any surviving fact (same path, lines overlapping)."""
    cited = _cited_ranges(facts)
    missing = []
    for e in entries:
        if not any(a <= e.line <= b for a, b in cited.get(e.path, [])):
            missing.append(entry_key(e))
    return missing


# --- docs -----------------------------------------------------------------------------------


def doc_quote_gate(doc_texts: dict[str, str], url: str, doc_quote: str) -> GateResult:
    """The URL is one code fetched or read (from llms.txt or a local docs file), and the
    normalised quote is inside its cached text."""
    text = doc_texts.get(url)
    if text is None:
        return GateResult(passed=False, reason="url not in llms.txt or the local docs")
    q = normalise(doc_quote).lower()
    if len(q) < 8:
        return GateResult(passed=False, reason="doc quote too short")
    if q not in normalise(text).lower():
        return GateResult(passed=False, reason="doc quote not in the page")
    return GateResult(passed=True)


# --- trace -----------------------------------------------------------------------------------


DISPATCH_SUFFIXES = (".delay", ".apply_async", ".s", ".si", ".signature")


def called_names(symbol: str) -> list[str]:
    """Names hop k may be called by, most specific first.

    `DolceChannex_webhooks.channex_webhook_handler` -> [that, `channex_webhook_handler`];
    `self._ack(rev)` -> [`self._ack`, `_ack`]; `task.delay` -> [`task`];
    a route or a Celery task name (`dolcechannex.poll_x`) is tried whole too.
    """
    s = symbol.strip().strip("`'\" ")
    head = s.split("(")[0].strip()
    for suffix in DISPATCH_SUFFIXES:
        if head.endswith(suffix):
            head = head[: -len(suffix)]
    out = [x for x in (s, head) if x]
    ids = re.findall(r"[A-Za-z_]\w*", head)
    if ids and "/" not in head:
        out.append(ids[-1])
    return list(dict.fromkeys(out))


def _defines(window: str, name: str) -> bool:
    n = re.escape(name)
    return bool(re.search(rf"\b(?:async\s+def|def|class)\s+{n}\b|^\s*{n}\s*[:=]|['\"]{n}['\"]", window, re.M))


def trace_gate(repos: dict[str, RepoInfo], seed: SymbolEntry | Reference, hops: list[Hop],
               places: list[tuple[str, int]]) -> tuple[int, str]:
    """(number of good hops from the start, reason for the first bad one or "").

    Hop 1 is the seed's entry. For k > 1, hop k-1's quote contains the name
    hop k is called as, and hop k's lines define it, or hop k's lines hold a
    boundary.json entry (*places*: a cross-repo reference, a route, a task).
    """
    if not hops:
        return 0, "no hops"
    for k, hop in enumerate(hops):
        c = Citation(path=hop.path, line_start=hop.line_start, line_end=hop.line_end, quote=hop.quote)
        g = citation_gate(repos, c)
        if not g.passed:
            return k, f"hop {k + 1}: {g.reason}"
        if k == 0:
            if hop.path != seed.path or not (hop.line_start <= seed.line <= hop.line_end):
                return 0, f"hop 1 is not the seed {seed.path}:{seed.line}"
            continue
        prev = hops[k - 1]
        names = [n for n in called_names(hop.symbol_called) if n in normalise(prev.quote)]
        if not names:
            return k, f"hop {k}'s quote does not contain {hop.symbol_called!r}"
        window = "\n".join(lines_at_commit(repos, hop.path, hop.line_start, hop.line_end) or [])
        edge = any(hop.path == p and hop.line_start <= ln <= hop.line_end for p, ln in places)
        if not edge and not any(_defines(window, n) for n in names):
            return k, f"hop {k + 1}'s lines do not define {names[-1]!r}"
    return len(hops), ""


# --- write -----------------------------------------------------------------------------------

CITE_ID = re.compile(r"\[\[(?:snippet:)?([a-z0-9]+)\]\]")
PATH_LINE = re.compile(r"([A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+):(\d+)(?:-(\d+))?")
BACKTICK = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
MERMAID = re.compile(r"```mermaid\n(.*?)```", re.S)
MERMAID_LABEL = re.compile(r"\[\"?([^\]\"]+)\"?\]|\(\"?([^)\"]+)\"?\)|participant\s+\S+\s+as\s+(.+)$|participant\s+(\S+)$",
                           re.M)


class Corpus:
    """Where a write-gate check looks for text: the snapshots and the docs cache."""

    def __init__(self, roots: list[Path]):
        self.roots = [r for r in roots if r.exists()]
        self._cache: dict[tuple[str, bool], bool] = {}

    def contains(self, text: str, *, ignore_case: bool = False) -> bool:
        key = (text, ignore_case)
        if key in self._cache:
            return self._cache[key]
        found = False
        if text and self.roots:
            require_rg()
            argv = ["rg", "--fixed-strings", "--files-with-matches", "--max-count", "1", "--no-messages",
                    "--hidden", "--no-ignore", "--glob", "!.env*"]
            if ignore_case:
                argv.append("--ignore-case")
            argv += ["-e", text, *map(str, self.roots)]
            proc = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
            found = bool(proc.stdout.strip())
        self._cache[key] = found
        return found


def _backtick_ok(text: str, corpus: Corpus, cited_paths: set[str]) -> bool:
    t = text.strip()
    if PATH_LINE.fullmatch(t):
        return True   # checked by the path:line rule
    if t in cited_paths or any(p.endswith("/" + t) for p in cited_paths):
        return True
    for candidate in (t, t.strip("/"), t.rstrip("()"), t.split("(")[0]):
        if candidate and corpus.contains(candidate):
            return True
    return False


def write_gate(text: str, *, citations: dict[str, Citation], corpus: Corpus, labels: set[str]) -> list[str]:
    """Failure reasons for one paragraph; empty when it passes.

    - every `[[cN]]` exists
    - every `path:line` written in prose matches a gated citation
    - every backticked identifier occurs in some snapshot or the docs
    - every Mermaid node label is a file, symbol or repo from the gated facts
    """
    reasons: list[str] = []
    for cid in CITE_ID.findall(text):
        if cid not in citations:
            reasons.append(f"[[{cid}]] is not a gated citation")
    plain = CITE_ID.sub("", MERMAID.sub("", text))
    cited_paths = {c.path for c in citations.values()}
    for m in PATH_LINE.finditer(plain):
        path, line = m.group(1), int(m.group(2))
        ok = any((c.path == path or c.path.endswith("/" + path)) and c.line_start <= line <= c.line_end
                 for c in citations.values())
        if not ok:
            reasons.append(f"{m.group(0)} is not a gated citation")
    for m in BACKTICK.finditer(plain):
        if PATH_LINE.fullmatch(m.group(1).strip()):
            continue
        if not _backtick_ok(m.group(1), corpus, cited_paths):
            reasons.append(f"`{m.group(1)}` occurs in no snapshot")
    for block in MERMAID.findall(text):
        for groups in MERMAID_LABEL.findall(block):
            label = next((g for g in groups if g), "").strip()
            if label and label not in labels and PurePosixPath(label).name not in labels:
                reasons.append(f"Mermaid label {label!r} is not a file, symbol or repo from the facts")
    return reasons


def glossary_gate(term: str, corpus: Corpus) -> GateResult:
    if corpus.contains(term.strip(), ignore_case=True):
        return GateResult(passed=True)
    return GateResult(passed=False, reason=f"glossary term {term!r} occurs in no snapshot")


def sections_gate(markdown: str, required: list[str]) -> GateResult:
    headings = {re.sub(r"\s+\(\d+\)$", "", m.group(1).strip())
                for m in re.finditer(r"^##\s+(?:\d+\.\s+)?(.+)$", markdown, re.M)}
    missing = [r for r in required if r not in headings]
    if missing:
        return GateResult(passed=False, reason="missing sections: " + ", ".join(missing))
    return GateResult(passed=True)


def target_files_ws(target: TargetInfo) -> list[str]:
    return [target.ws(f) for f in target.files]
