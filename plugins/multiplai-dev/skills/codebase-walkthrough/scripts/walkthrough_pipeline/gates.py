"""Pure-code gates. None of them calls a model.

Everything a gate checks against code is re-read from the commit with
`git show <sha>:<path>`, never taken from what an agent said it read, and
never from the snapshot an agent could see. Doc quotes are checked against the
cached text code fetched.

Names must not start with `test`, or pytest collects them.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path, PurePosixPath

from multiplai_core.citations import check_quote, file_at, lines_at
from multiplai_core.citations import normalise_ws as normalise

from .models import (Citation, Fact, GateResult, Reference, RepoInfo, SymbolEntry, TargetInfo, Unit)
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


def repo_resolver(repos: dict[str, RepoInfo]):
    """`resolve(path, side)` for code_facts: a workspace path at its repo's commit (one side only)."""
    def resolve(ws_path: str, side: str = "head") -> tuple[str, str, str] | None:
        found = split_path(repos, ws_path)
        return None if found is None else (found[0].path, found[0].head_sha, found[1])
    return resolve


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


# --- the human-mode explainer's structured sections ---------------------------------------------
#
# Each gate returns failure reasons; an empty list passes. A model-written example, flag, node and
# key is checked here or cut, and none of these calls a model.

MAX_FLOW_NODES = 14
MAX_LABEL_CHARS = 60
MAX_EXAMPLE_LINES = 80
MAX_EXAMPLE_CHARS = 4000
FLAG = re.compile(r"-{1,2}[A-Za-z][\w-]*")
# Words in a command that name a program, a subcommand, a module or a slash command.
COMMAND_WORD = re.compile(r"/?[A-Za-z][\w.:-]*")
ENV_ASSIGN = re.compile(r"([A-Z_][A-Z0-9_]*)=.*")
# A word ending in one of these is a file the user names, not a subcommand; it is not checked.
FILE_SUFFIX = re.compile(r"\.(md|json|ya?ml|txt|toml|html|csv|log|py|sh|js|ts|cfg|ini|lock)$", re.I)
CALLED = re.compile(r"([A-Za-z_][\w.]*)\s*\(")
KEYWORD_ARG = re.compile(r"[(,]\s*([A-Za-z_]\w*)\s*=")
LINE_KEY = re.compile(r"^\s*-?\s*([A-Za-z_][\w.-]*)\s*:", re.M)


def cite_id(raw: str) -> str:
    """A cite field as the id alone: `[[c12]]`, `[[snippet:c12]]` and ` c12 ` are all `c12`."""
    return raw.strip().strip("[]").removeprefix("snippet:").strip()


def explainer_text_gate(text: str, *, citations: dict[str, Citation], corpus: Corpus,
                        labels: set[str]) -> list[str]:
    """`write_gate`, plus the explainer's bans: no `path:line` typed in the text even when it is a gated
    one, no code block and no Mermaid."""
    reasons = write_gate(text, citations=citations, corpus=corpus, labels=labels)
    plain = CITE_ID.sub("", text)
    for m in PATH_LINE.finditer(plain):
        reason = f"{m.group(0)} is a path with a line number typed in the text"
        if not any(r.startswith(m.group(0)) for r in reasons):
            reasons.append(reason)
    if "```" in text or "~~~" in text:
        reasons.append("the text holds a code block; the program inserts code")
    return reasons


def _quotes(ids: list[str], citations: dict[str, Citation]) -> str:
    return " ".join(citations[i].quote for i in ids if i in citations)


def _known_word(word: str, quotes: str, corpus: Corpus) -> bool:
    return word in quotes or corpus.contains(word)


def example_words(kind: str, text: str) -> list[str]:
    """The words of an example a gate checks: flags and command words in a command, called names and
    keyword arguments in a call, keys and flags in a file. Placeholders in `<...>`, file names, numbers
    and quoted values are left out."""
    import shlex
    words: list[str] = []
    if kind == "command":
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                tokens = shlex.split(line, comments=True)
            except ValueError:
                tokens = line.split()
            for tok in tokens:
                if tok.startswith("-"):
                    name = tok.split("=", 1)[0]
                    if FLAG.fullmatch(name):
                        words.append(name)
                elif ENV_ASSIGN.fullmatch(tok):
                    words.append(ENV_ASSIGN.fullmatch(tok).group(1))
                elif COMMAND_WORD.fullmatch(tok) and not FILE_SUFFIX.search(tok):
                    words.append(tok.rstrip(":"))
    elif kind == "call":
        words += CALLED.findall(text) + KEYWORD_ARG.findall(text)
        words += [m.group(0) for m in re.finditer(r"(?<![\w-])--[A-Za-z][\w-]*", text)]
    else:
        words += LINE_KEY.findall(text)
        words += [m.group(0) for m in re.finditer(r"(?<![\w-])--[A-Za-z][\w-]*", text)]
    out: list[str] = []
    for w in words:
        if w and w not in out:
            out.append(w)
    return out


def usecase_gate(case, *, citations: dict[str, Citation], corpus: Corpus, labels: set[str]) -> list[str]:
    """One use case: it cites at least one known id, its text passes `explainer_text_gate`, and every
    flag and command word of its example is in a cited quote or in the corpus."""
    reasons: list[str] = []
    ids = [cite_id(c) for c in case.cites if cite_id(c)]
    if not ids:
        reasons.append("the use case cites nothing")
    reasons += [f"[[{i}]] is not a gated citation" for i in ids if i not in citations]
    for field in ("title", "who", "when", "what_happens", "result"):
        reasons += explainer_text_gate(getattr(case, field), citations=citations, corpus=corpus, labels=labels)
    text = case.example.text.strip()
    if not text:
        reasons.append("the example is empty")
    elif len(text) > MAX_EXAMPLE_CHARS or len(text.splitlines()) > MAX_EXAMPLE_LINES:
        reasons.append("the example is too long")
    quotes = _quotes(ids, citations)
    for word in example_words(case.example.kind, text):
        if not _known_word(word, quotes, corpus):
            reasons.append(f"`{word}` in the example is in no cited quote and no snapshot")
    return reasons


def _plain_label(label: str) -> str | None:
    """Why a diagram label is not plain words, or None."""
    t = label.strip()
    if not t:
        return "empty label"
    if len(t) > MAX_LABEL_CHARS:
        return f"label over {MAX_LABEL_CHARS} characters"
    if "`" in t or "[[" in t or PATH_LINE.search(t) or FILE_SUFFIX.search(t.split()[-1]):
        return "label is not plain words"
    return None


def flow_gate(flow, *, citations: dict[str, Citation], corpus: Corpus, labels: set[str]) -> dict:
    """The flow's gated parts: `nodes`, `edges` and `steps` that passed, `cut` ({text, reasons}) for each
    part dropped, and `graph` (reasons the graph as a whole fails: no node, over 14 nodes, not connected).

    A node is dropped when its cite is unknown or its label is not plain words; an edge when it joins an
    unknown node (its label alone is dropped when not plain); a step when its summary fails, a details
    paragraph when it fails, a snippet cite when it is unknown."""
    nodes, edges, steps, cut = [], [], [], []
    seen: set[str] = set()
    for n in flow.nodes:
        nid = n.id.strip()
        why = []
        cid = cite_id(n.cite)
        if not cid or cid not in citations:
            why.append(f"[[{cid}]] is not a gated citation" if cid else "the node cites nothing")
        bad = _plain_label(n.label)
        if bad:
            why.append(bad)
        if not re.fullmatch(r"[A-Za-z][\w-]*", nid) or nid in seen:
            why.append(f"node id {nid!r} is not a new plain id")
        if why:
            cut.append({"text": f"diagram box {n.label!r}", "reasons": why})
            continue
        seen.add(nid)
        nodes.append({"id": nid, "label": n.label.strip(), "cite": cid})
    for e in flow.edges:
        a, b = e.source.strip(), e.to.strip()
        if a not in seen or b not in seen:
            cut.append({"text": f"diagram arrow {a} -> {b}", "reasons": ["the arrow joins a box that is not drawn"]})
            continue
        label = e.label.strip()
        if label and _plain_label(label):
            cut.append({"text": f"label of the arrow {a} -> {b}: {label!r}", "reasons": [_plain_label(label)]})
            label = ""
        edges.append({"from": a, "to": b, "label": label})
    graph: list[str] = []
    if not nodes:
        graph.append("no diagram box passed the checks")
    if len(nodes) > MAX_FLOW_NODES:
        graph.append(f"{len(nodes)} boxes; at most {MAX_FLOW_NODES}")
    if len(nodes) > 1:
        adj: dict[str, set[str]] = {n["id"]: set() for n in nodes}
        for e in edges:
            adj[e["from"]].add(e["to"])
            adj[e["to"]].add(e["from"])
        reached, todo = {nodes[0]["id"]}, [nodes[0]["id"]]
        while todo:
            for nxt in adj[todo.pop()] - reached:
                reached.add(nxt)
                todo.append(nxt)
        alone = [n["label"] for n in nodes if n["id"] not in reached]
        if alone:
            graph.append("the diagram is not connected: no arrow reaches " + ", ".join(repr(x) for x in alone))
    gate = {"citations": citations, "corpus": corpus, "labels": labels}
    for s in flow.steps:
        why = explainer_text_gate(s.title, **gate) + explainer_text_gate(s.summary, **gate)
        if not s.summary.strip():
            why.append("the step has no summary")
        if why:
            cut.append({"text": f"step {s.title!r}: {s.summary}", "reasons": why})
            continue
        details = []
        for p in s.details_paragraphs:
            r = explainer_text_gate(p, **gate)
            (cut.append({"text": p, "reasons": r}) if r else details.append(p))
        snippets = []
        for raw in s.snippet_cites:
            cid = cite_id(raw)
            if cid in citations:
                snippets.append(cid)
            else:
                cut.append({"text": f"snippet of step {s.title!r}", "reasons": [f"[[{cid}]] is not a gated citation"]})
        steps.append({"title": s.title.strip(), "summary": s.summary, "details": details, "snippets": snippets})
    return {"nodes": nodes, "edges": edges, "steps": steps, "cut": cut, "graph": graph}


def _keys(value) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            out.append(str(k))
            out += _keys(v)
    elif isinstance(value, list):
        for v in value:
            out += _keys(v)
    return out


def payload_gate(payload, quote: str, corpus: Corpus) -> list[str]:
    """Why an example payload is dropped: JSON or YAML that does not parse, a key in neither the cited
    quote nor the corpus, a Markdown identifier or text key that is in neither, or a payload too long."""
    text = payload.text.strip("\n")
    if not text.strip():
        return ["the example is empty"]
    if len(text) > MAX_EXAMPLE_CHARS or len(text.splitlines()) > MAX_EXAMPLE_LINES:
        return ["the example is too long"]
    if "```" in text:
        return ["the example holds a code fence"]
    if payload.format in ("json", "yaml"):
        try:
            if payload.format == "json":
                data = json.loads(text)
            else:
                import yaml
                data = yaml.safe_load(text)
        except Exception as e:  # noqa: BLE001 - any parse error drops the example
            return [f"the {payload.format} does not parse: {type(e).__name__}"]
        if not isinstance(data, (dict, list)):
            return [f"the {payload.format} is not an object or a list"]
        keys = _keys(data)
    else:
        keys = LINE_KEY.findall(text) + [m.group(1).strip() for m in BACKTICK.finditer(text)]
    missing = []
    for k in dict.fromkeys(keys):
        if k and not _known_word(k, quote, corpus):
            missing.append(k)
    return [f"key `{k}` is in no cited quote and no snapshot" for k in missing]


def output_gate(item, *, citations: dict[str, Citation], corpus: Corpus, labels: set[str]) -> tuple[list[str], list[str]]:
    """(reasons the item is cut, reasons its example alone is dropped)."""
    cid = cite_id(item.written_by_cite)
    reasons: list[str] = []
    if not cid or cid not in citations:
        reasons.append(f"[[{cid}]] is not a gated citation" if cid else "the item cites nothing")
    for text in (item.name, item.what):
        reasons += explainer_text_gate(text, citations=citations, corpus=corpus, labels=labels)
    if not item.name.strip():
        reasons.append("the item has no name")
    example: list[str] = []
    if not reasons and item.example is not None:
        example = payload_gate(item.example, citations[cid].quote, corpus)
    return reasons, example


def config_gate(row, *, citations: dict[str, Citation], corpus: Corpus, labels: set[str]) -> tuple[list[str], bool]:
    """(reasons the row is cut, whether its default is kept). The name is in the cited quote or the corpus;
    so is the default, or the default alone is dropped."""
    cid = cite_id(row.cite)
    reasons: list[str] = []
    if not cid or cid not in citations:
        reasons.append(f"[[{cid}]] is not a gated citation" if cid else "the row cites nothing")
    name = row.name.strip().strip("`").strip()
    quote = citations[cid].quote if cid in citations else ""
    if not name:
        reasons.append("the row has no name")
    elif not _known_word(name, quote, corpus):
        reasons.append(f"`{name}` is in no cited quote and no snapshot")
    reasons += explainer_text_gate(row.effect, citations=citations, corpus=corpus, labels=labels)
    default = row.default.strip().strip("`").strip()
    keep_default = bool(default) and _known_word(default, quote, corpus)
    return reasons, keep_default
