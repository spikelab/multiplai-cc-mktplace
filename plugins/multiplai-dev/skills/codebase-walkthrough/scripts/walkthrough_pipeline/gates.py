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
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

from multiplai_core.citations import check_quote, file_at, lines_at
from multiplai_core.citations import normalise_ws as normalise

from .models import (
    Citation,
    Fact,
    GateResult,
    Reference,
    RepoInfo,
    SymbolEntry,
    TargetInfo,
    Unit,
)
from .target import require_rg

SMALL_FILE_LINES = 30


def split_path(repos: dict[str, RepoInfo], ws_path: str) -> tuple[RepoInfo, str] | None:
    """`DolceEngine/DolceChannex/urls.py` -> (the DolceEngine repo, `DolceChannex/urls.py`)."""
    p = ws_path.strip().removeprefix("./")
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
    return (
        None if found is None else file_at(found[0].path, found[0].head_sha, found[1])
    )


def lines_at_commit(
    repos: dict[str, RepoInfo], ws_path: str, start: int, end: int
) -> list[str] | None:
    found = split_path(repos, ws_path)
    return (
        None
        if found is None
        else lines_at(found[0].path, found[0].head_sha, found[1], start, end)
    )


# --- citations ----------------------------------------------------------------------


def citation_gate(repos: dict[str, RepoInfo], citation: Citation) -> GateResult:
    """The quote, whitespace-normalised, is inside lines line_start..line_end at the repo's commit."""
    found = split_path(repos, citation.path)
    if found is None:
        # An empty quote is named first, whatever the path, as before.
        reason = (
            "empty quote"
            if not normalise(citation.quote)
            else "path names no known repo"
        )
        return GateResult(passed=False, reason=reason)
    repo, rel = found
    reason = check_quote(
        repo.path,
        repo.head_sha,
        rel,
        citation.line_start,
        citation.line_end,
        citation.quote,
    )
    if reason is not None:
        return GateResult(passed=False, reason=reason)
    return GateResult(passed=True)


def entry_citation(entry: SymbolEntry | Reference) -> Citation:
    return Citation(
        path=entry.path, line_start=entry.line, line_end=entry.line, quote=entry.quote
    )


def boundary_gate(
    repos: dict[str, RepoInfo], symbols: list[SymbolEntry], refs: list[Reference]
) -> tuple[list[SymbolEntry], list[Reference], list[dict]]:
    """Every entry's quote is at its line at its repo's commit. Re-checks entries
    code produced too, so a parser bug cannot slip through."""
    dropped: list[dict] = []
    kept_s, kept_r = [], []
    for e in symbols:
        g = citation_gate(repos, entry_citation(e))
        (
            kept_s.append(e)
            if g.passed
            else dropped.append({"entry": e.model_dump(), "reason": g.reason})
        )
    for r in refs:
        g = citation_gate(repos, entry_citation(r))
        (
            kept_r.append(r)
            if g.passed
            else dropped.append({"entry": r.model_dump(), "reason": g.reason})
        )
    return kept_s, kept_r, dropped


# --- partition and coverage -------------------------------------------------------------


def partition_gate(
    units: list[Unit], files: list[str], lines: dict[str, int], limit: int
) -> GateResult:
    """Every file in exactly one unit; no unit over *limit* lines unless it is one oversize file."""
    seen: dict[str, str] = {}
    for u in units:
        for f in u.files:
            if f in seen:
                return GateResult(
                    passed=False, reason=f"{f} is in {seen[f]} and {u.id}"
                )
            seen[f] = u.id
        total = sum(lines.get(f, 0) for f in u.files)
        if total > limit and not (len(u.files) == 1 and u.oversize):
            return GateResult(
                passed=False, reason=f"{u.id} has {total} lines (limit {limit})"
            )
    missing = [f for f in files if f not in seen]
    if missing:
        return GateResult(
            passed=False,
            reason=f"{len(missing)} file(s) in no unit: {', '.join(missing[:5])}",
        )
    extra = [f for f in seen if f not in set(files)]
    if extra:
        return GateResult(
            passed=False, reason=f"unknown file(s) in units: {', '.join(extra[:5])}"
        )
    return GateResult(passed=True)


def _cited_ranges(facts: list[Fact]) -> dict[str, list[tuple[int, int]]]:
    out: dict[str, list[tuple[int, int]]] = {}
    for fact in facts:
        for c in fact.citations:
            out.setdefault(c.path, []).append((c.line_start, c.line_end))
    return out


def file_coverage_gate(
    ws_files: list[str], lines: dict[str, int], facts: list[Fact]
) -> list[str]:
    """Files (workspace-relative) with no surviving fact that are longer than 30 lines."""
    cited = _cited_ranges(facts)
    return [
        f for f in ws_files if f not in cited and lines.get(f, 0) > SMALL_FILE_LINES
    ]


def entry_key(entry: SymbolEntry | Reference) -> str:
    return f"{entry.path}:{entry.line}"


def boundary_coverage_gate(
    entries: list[SymbolEntry | Reference], facts: list[Fact]
) -> list[str]:
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
MERMAID = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)
MERMAID_LABEL = re.compile(
    r"\[\"?([^\]\"]+)\"?\]|\(\"?([^)\"]+)\"?\)|participant\s+\S+\s+as\s+(.+)$|participant\s+(\S+)$",
    re.MULTILINE,
)


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
            argv = [
                "rg",
                "--fixed-strings",
                "--files-with-matches",
                "--max-count",
                "1",
                "--no-messages",
                "--hidden",
                "--no-ignore",
                "--glob",
                "!.env*",
            ]
            if ignore_case:
                argv.append("--ignore-case")
            argv += ["-e", text, *map(str, self.roots)]
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                check=False,
            )
            found = bool(proc.stdout.strip())
        self._cache[key] = found
        return found


def _backtick_ok(text: str, corpus: Corpus, cited_paths: set[str]) -> bool:
    t = text.strip()
    if PATH_LINE.fullmatch(t):
        return True  # checked by the path:line rule
    if t in cited_paths or any(p.endswith("/" + t) for p in cited_paths):
        return True
    for candidate in (t, t.strip("/"), t.rstrip("()"), t.split("(")[0]):
        if candidate and corpus.contains(candidate):
            return True
    return False


def write_gate(
    text: str, *, citations: dict[str, Citation], corpus: Corpus, labels: set[str]
) -> list[str]:
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
        ok = any(
            (c.path == path or c.path.endswith("/" + path))
            and c.line_start <= line <= c.line_end
            for c in citations.values()
        )
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
            if (
                label
                and label not in labels
                and PurePosixPath(label).name not in labels
            ):
                reasons.append(
                    f"Mermaid label {label!r} is not a file, symbol or repo from the facts"
                )
    return reasons


def glossary_gate(term: str, corpus: Corpus) -> GateResult:
    if corpus.contains(term.strip(), ignore_case=True):
        return GateResult(passed=True)
    return GateResult(
        passed=False, reason=f"glossary term {term!r} occurs in no snapshot"
    )


def sections_gate(markdown: str, required: list[str]) -> GateResult:
    headings = {
        re.sub(r"\s+\(\d+\)$", "", m.group(1).strip())
        for m in re.finditer(r"^##\s+(?:\d+\.\s+)?(.+)$", markdown, re.MULTILINE)
    }
    missing = [r for r in required if r not in headings]
    if missing:
        return GateResult(
            passed=False, reason="missing sections: " + ", ".join(missing)
        )
    return GateResult(passed=True)


def target_files_ws(target: TargetInfo) -> list[str]:
    return [target.ws(f) for f in target.files]


# --- the human-mode explainer's structured sections ---------------------------------------------
#
# Each gate returns failure reasons; an empty list passes. A model-written example, flag, node and
# key is checked here or cut, and none of these calls a model.

MAX_FLOW_NODES = 14
MAX_LABEL_CHARS = 60
MAX_DOES_CHARS = 80
# Boxes a step need not explain: what goes in and what comes out.
FLOW_ENDS = ("input", "output")
MAX_EXAMPLE_LINES = 80
MAX_EXAMPLE_CHARS = 4000
FLAG = re.compile(r"-{1,2}[A-Za-z][\w-]*")
# Words in a command that name a program, a subcommand, a module or a slash command.
COMMAND_WORD = re.compile(r"/?[A-Za-z][\w.:-]*")
ENV_ASSIGN = re.compile(r"([A-Z_][A-Z0-9_]*)=.*")
# A word ending in one of these is a file the user names, not a subcommand; it is not checked.
FILE_SUFFIX = re.compile(
    r"\.(md|json|ya?ml|txt|toml|html|csv|log|py|sh|js|ts|cfg|ini|lock)$", re.IGNORECASE
)
CALLED = re.compile(r"([A-Za-z_][\w.]*)\s*\(")
KEYWORD_ARG = re.compile(r"[(,]\s*([A-Za-z_]\w*)\s*=")
LINE_KEY = re.compile(r"^\s*-?\s*([A-Za-z_][\w.-]*)\s*:", re.MULTILINE)


def cite_id(raw: str) -> str:
    """A cite field as the id alone: `[[c12]]`, `[[snippet:c12]]` and ` c12 ` are all `c12`."""
    return raw.strip().strip("[]").removeprefix("snippet:").strip()


def explainer_text_gate(
    text: str, *, citations: dict[str, Citation], corpus: Corpus, labels: set[str]
) -> list[str]:
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


TITLE_CITATION = "citation in a title"


def title_gate(text: str) -> list[str]:
    """A title or heading holds no citation: the page writes it as a heading, where a link is noise."""
    return [TITLE_CITATION] if "[[" in text else []


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


def usecase_gate(
    case, *, citations: dict[str, Citation], corpus: Corpus, labels: set[str]
) -> list[str]:
    """One use case: it cites at least one known id, its text passes `explainer_text_gate`, and every
    flag and command word of its example is in a cited quote or in the corpus."""
    reasons: list[str] = []
    ids = [cite_id(c) for c in case.cites if cite_id(c)]
    if not ids:
        reasons.append("the use case cites nothing")
    reasons += [f"[[{i}]] is not a gated citation" for i in ids if i not in citations]
    reasons += title_gate(case.title)
    for field in ("title", "who", "when", "what_happens", "result"):
        reasons += explainer_text_gate(
            getattr(case, field), citations=citations, corpus=corpus, labels=labels
        )
    text = case.example.text.strip()
    if not text:
        reasons.append("the example is empty")
    elif len(text) > MAX_EXAMPLE_CHARS or len(text.splitlines()) > MAX_EXAMPLE_LINES:
        reasons.append("the example is too long")
    quotes = _quotes(ids, citations)
    for word in example_words(case.example.kind, text):
        if not _known_word(word, quotes, corpus):
            reasons.append(
                f"`{word}` in the example is in no cited quote and no snapshot"
            )
    return reasons


def _plain_label(label: str) -> str | None:
    """Why a diagram label is not plain words, or None."""
    t = label.strip()
    if not t:
        return "empty label"
    if len(t) > MAX_LABEL_CHARS:
        return f"label over {MAX_LABEL_CHARS} characters"
    if (
        "`" in t
        or "[[" in t
        or PATH_LINE.search(t)
        or FILE_SUFFIX.search(t.split()[-1])
    ):
        return "label is not plain words"
    return None


def _box_extras(n, cid: str, gate: dict) -> tuple[dict, list[dict]]:
    """A kept box's `does`, `reads`, `writes` and `fan_out` that pass, and a cut entry for each one dropped.

    Each is dropped alone, the box kept: `does` when it fails `explainer_text_gate` or is over 80
    characters; a `reads` or `writes` entry when it fails `explainer_text_gate`; `fan_out` when it fails
    `explainer_text_gate` or holds a number in neither the cited quote nor the corpus (as `loop_gate`
    drops a cap)."""
    cut: list[dict] = []
    label = n.label.strip()
    does = n.does.strip()
    if does:
        why = explainer_text_gate(does, **gate)
        if len(does) > MAX_DOES_CHARS:
            why.append(f"over {MAX_DOES_CHARS} characters")
        if why:
            cut.append({"text": f"what box {label!r} does: {does!r}", "reasons": why})
            does = ""
    lists = {}
    for field in ("reads", "writes"):
        kept = []
        for item in getattr(n, field):
            item = item.strip()
            if not item:
                continue
            why = explainer_text_gate(item, **gate)
            if why:
                cut.append(
                    {"text": f"{field} of box {label!r}: {item!r}", "reasons": why}
                )
            else:
                kept.append(item)
        lists[field] = kept
    fan_out = n.fan_out.strip()
    if fan_out:
        quote = gate["citations"][cid].quote
        why = explainer_text_gate(fan_out, **gate)
        why += [
            f"`{x}` is in no cited quote and no snapshot"
            for x in NUMBER.findall(fan_out)
            if not _number_in(x, quote) and not gate["corpus"].contains(x)
        ]
        if why:
            cut.append(
                {
                    "text": f"fan-out of box {label!r}: {fan_out!r} (the box is kept)",
                    "reasons": why,
                }
            )
            fan_out = ""
    return {"does": does, **lists, "fan_out": fan_out}, cut


def flow_gate(
    flow,
    *,
    citations: dict[str, Citation],
    corpus: Corpus,
    labels: set[str],
    steps_follow_boxes: bool = False,
) -> dict:
    """The flow's gated parts: `nodes`, `edges` and `steps` that passed, `cut` ({text, reasons}) for each
    part dropped, `graph` (reasons the graph as a whole fails: no node, over 14 nodes, not connected) and
    `order` (reasons the steps do not match the boxes; nothing is cut for them).

    A node is dropped when its cite is unknown or its label is not plain words; its `does`, `reads`,
    `writes` and `fan_out` are checked by `_box_extras`. An edge is dropped when it joins an unknown node
    (its label alone is dropped when not plain); a step when its summary fails, a details paragraph when it
    fails, a snippet cite when it is unknown. With *steps_follow_boxes* (module and codebase scope), each
    step's `node` must name a drawn box, every drawn box that is not `input` or `output` must have exactly
    one step, and the steps must follow the boxes' order."""
    nodes, edges, steps, cut = [], [], [], []
    seen: set[str] = set()
    gate = {"citations": citations, "corpus": corpus, "labels": labels}
    for n in flow.nodes:
        nid = n.id.strip()
        why = []
        cid = cite_id(n.cite)
        if not cid or cid not in citations:
            why.append(
                f"[[{cid}]] is not a gated citation"
                if cid
                else "the node cites nothing"
            )
        bad = _plain_label(n.label)
        if bad:
            why.append(bad)
        if not re.fullmatch(r"[A-Za-z][\w-]*", nid) or nid in seen:
            why.append(f"node id {nid!r} is not a new plain id")
        if why:
            cut.append({"text": f"diagram box {n.label!r}", "reasons": why})
            continue
        seen.add(nid)
        extras, dropped = _box_extras(n, cid, gate)
        cut += dropped
        nodes.append(
            {"id": nid, "label": n.label.strip(), "cite": cid, "kind": n.kind, **extras}
        )
    for e in flow.edges:
        a, b = e.source.strip(), e.to.strip()
        if a not in seen or b not in seen:
            cut.append(
                {
                    "text": f"diagram arrow {a} -> {b}",
                    "reasons": ["the arrow joins a box that is not drawn"],
                }
            )
            continue
        label = e.label.strip()
        if label and _plain_label(label):
            cut.append(
                {
                    "text": f"label of the arrow {a} -> {b}: {label!r}",
                    "reasons": [_plain_label(label)],
                }
            )
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
            graph.append(
                "the diagram is not connected: no arrow reaches "
                + ", ".join(repr(x) for x in alone)
            )
    for s in flow.steps:
        why = (
            title_gate(s.title)
            + explainer_text_gate(s.title, **gate)
            + explainer_text_gate(s.summary, **gate)
        )
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
                cut.append(
                    {
                        "text": f"snippet of step {s.title!r}",
                        "reasons": [f"[[{cid}]] is not a gated citation"],
                    }
                )
        steps.append(
            {
                "title": s.title.strip(),
                "summary": s.summary,
                "details": details,
                "snippets": snippets,
                "node": s.node.strip(),
            }
        )
    order = _step_order(nodes, steps) if steps_follow_boxes else []
    return {
        "nodes": nodes,
        "edges": edges,
        "steps": steps,
        "cut": cut,
        "graph": graph,
        "order": order,
    }


def _step_order(nodes: list[dict], steps: list[dict]) -> list[str]:
    """Why the steps do not walk the boxes one by one: a step naming no drawn box, a box that is not an
    input or an output with no step or with several, or steps out of the boxes' order."""
    reasons: list[str] = []
    position = {n["id"]: i for i, n in enumerate(nodes)}
    named = []
    for s in steps:
        if s["node"] in position:
            named.append(s["node"])
        else:
            reasons.append(
                f"step {s['title']!r} names no drawn box"
                + (f" ({s['node']!r})" if s["node"] else "")
            )
    for n in nodes:
        if n["kind"] in FLOW_ENDS:
            continue
        k = named.count(n["id"])
        if k != 1:
            reasons.append(
                f"box {n['label']!r} has {k} steps; each box that is not an input or an output has one"
            )
    if named != sorted(named, key=position.get):
        reasons.append("the steps are not in the order of the boxes")
    return reasons


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
        keys = LINE_KEY.findall(text) + [
            m.group(1).strip() for m in BACKTICK.finditer(text)
        ]
    missing = []
    for k in dict.fromkeys(keys):
        if k and not _known_word(k, quote, corpus):
            missing.append(k)
    return [f"key `{k}` is in no cited quote and no snapshot" for k in missing]


def output_gate(
    item, *, citations: dict[str, Citation], corpus: Corpus, labels: set[str]
) -> tuple[list[str], list[str]]:
    """(reasons the item is cut, reasons its example alone is dropped)."""
    cid = cite_id(item.written_by_cite)
    reasons: list[str] = []
    if not cid or cid not in citations:
        reasons.append(
            f"[[{cid}]] is not a gated citation" if cid else "the item cites nothing"
        )
    for text in (item.name, item.what):
        reasons += explainer_text_gate(
            text, citations=citations, corpus=corpus, labels=labels
        )
    if not item.name.strip():
        reasons.append("the item has no name")
    reasons += title_gate(item.name)
    example: list[str] = []
    if not reasons and item.example is not None:
        example = payload_gate(item.example, citations[cid].quote, corpus)
    return reasons, example


def config_gate(
    row, *, citations: dict[str, Citation], corpus: Corpus, labels: set[str]
) -> tuple[list[str], bool]:
    """(reasons the row is cut, whether its default is kept). The name is in the cited quote or the corpus;
    so is the default, or the default alone is dropped."""
    cid = cite_id(row.cite)
    reasons: list[str] = []
    if not cid or cid not in citations:
        reasons.append(
            f"[[{cid}]] is not a gated citation" if cid else "the row cites nothing"
        )
    name = row.name.strip().strip("`").strip()
    quote = citations[cid].quote if cid in citations else ""
    if not name:
        reasons.append("the row has no name")
    elif not _known_word(name, quote, corpus):
        reasons.append(f"`{name}` is in no cited quote and no snapshot")
    reasons += explainer_text_gate(
        row.effect, citations=citations, corpus=corpus, labels=labels
    )
    default = row.default.strip().strip("`").strip()
    keep_default = bool(default) and _known_word(default, quote, corpus)
    return reasons, keep_default


# --- `--scope codebase`: the three tables ------------------------------------------------------

NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?::[A-Z_]+)?")


def _row_head(
    row, first: str, texts: tuple[str, ...], *, citations, corpus, labels
) -> tuple[list[str], str]:
    """(reasons, cited quote) shared by every table row: a known cite, a first field, and text fields
    that pass `explainer_text_gate`."""
    cid = cite_id(row.cite)
    reasons: list[str] = []
    if not cid or cid not in citations:
        reasons.append(
            f"[[{cid}]] is not a gated citation" if cid else "the row cites nothing"
        )
    if not getattr(row, first).strip():
        reasons.append(f"the row has no {first}")
    for field in texts:
        reasons += explainer_text_gate(
            getattr(row, field), citations=citations, corpus=corpus, labels=labels
        )
    return reasons, citations[cid].quote if cid in citations else ""


def _unknown_tokens(text: str, quote: str, corpus: Corpus) -> list[str]:
    """Each number in *text* missing from the cited quote, and each identifier of 3+ characters in
    neither the quote nor the corpus. Numbers must be in the quote: any snapshot holds every digit."""
    plain = text.replace("`", " ")
    out = [n for n in NUMBER.findall(plain) if not _number_in(n, quote)]
    out += [
        w
        for w in IDENT.findall(plain)
        if len(w) >= 3 and not w.isdigit() and not _known_word(w, quote, corpus)
    ]
    return list(dict.fromkeys(out))


def _number_in(number: str, quote: str) -> bool:
    return any(n == number for n in NUMBER.findall(quote))


def loop_gate(row, *, citations, corpus, labels) -> tuple[list[str], bool]:
    """(reasons the row is cut, whether its cap is kept). The cap is kept only when its numbers are in the
    cited quote and its identifiers in the quote or the corpus; otherwise it alone is dropped."""
    reasons, quote = _row_head(
        row,
        "what",
        ("what", "ends_well", "ends_badly"),
        citations=citations,
        corpus=corpus,
        labels=labels,
    )
    cap = row.cap.strip()
    keep_cap = bool(cap) and not _unknown_tokens(cap, quote, corpus)
    return reasons, keep_cap


def exit_gate(row, *, citations, corpus, labels) -> list[str]:
    """An exit code or marker row: the signal's numbers are in the cited quote and its identifiers in the
    quote or the corpus; its meaning and action pass the explainer checks."""
    reasons, quote = _row_head(
        row,
        "signal",
        ("meaning", "action"),
        citations=citations,
        corpus=corpus,
        labels=labels,
    )
    for tok in _unknown_tokens(row.signal, quote, corpus):
        reasons.append(f"`{tok}` in the signal is in no cited quote and no snapshot")
    return reasons


def resume_gate(row, *, citations, corpus, labels) -> list[str]:
    """A state-and-resume row: a known cite and text that passes the explainer checks."""
    reasons, _ = _row_head(
        row,
        "stop",
        ("stop", "on_disk", "rerun"),
        citations=citations,
        corpus=corpus,
        labels=labels,
    )
    return reasons


# --- the part of the code that does its main job (the core stage and its section) -----------------------

MAX_CORE_TITLE_CHARS = 60


def core_gate(
    choice,
    *,
    target_files: set[str],
    citations: dict[str, Citation],
    corpus: Corpus,
    labels: set[str],
) -> list[str]:
    """The core stage's choice: a plain title of at most 60 characters (no backticks, citation or path), at
    least one file and every file one of the target's (workspace paths), every cite a known id, and a `job`
    and a `why` that pass `explainer_text_gate`."""
    reasons: list[str] = []
    title = choice.title.strip()
    if not title:
        reasons.append("the title is empty")
    elif len(title) > MAX_CORE_TITLE_CHARS:
        reasons.append(f"the title is over {MAX_CORE_TITLE_CHARS} characters")
    if "`" in title:
        reasons.append("the title holds backticks")
    reasons += title_gate(title)
    if PATH_LINE.search(title) or "/" in title or FILE_SUFFIX.search(title):
        reasons.append("the title holds a path")
    files = [f.strip() for f in choice.files if f.strip()]
    if not files:
        reasons.append("no file is named")
    reasons += [
        f"{f} is not a file of the target" for f in files if f not in target_files
    ]
    for raw in choice.cites:
        cid = cite_id(raw)
        if cid not in citations:
            reasons.append(f"[[{cid}]] is not a gated citation")
    gate = {"citations": citations, "corpus": corpus, "labels": labels}
    for field in ("job", "why"):
        text = getattr(choice, field).strip()
        if not text:
            reasons.append(f"the {field} is empty")
        reasons += explainer_text_gate(text, **gate)
    return reasons


def core_part_gate(part, *, citations, corpus, labels) -> list[str]:
    """A part of the core section: a known cite, a name with no citation, and text that passes the explainer
    checks."""
    reasons, _ = _row_head(
        part,
        "name",
        ("name", "looks_at", "given", "produces", "checked_by"),
        citations=citations,
        corpus=corpus,
        labels=labels,
    )
    return reasons + title_gate(part.name)


# --- the page writer's open body (stages/page.py) -------------------------------------------------------

PAGE_TAGS = frozenset(
    [
        "p",
        "ul",
        "ol",
        "li",
        "h3",
        "h4",
        "strong",
        "em",
        "code",
        "a",
        "details",
        "summary",
        "div",
        "span",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "figure",
        "figcaption",
        "br",
        "svg",
        "g",
        "path",
        "rect",
        "circle",
        "line",
        "polyline",
        "polygon",
        "text",
        "tspan",
        "title",
        "defs",
        "symbol",
        "use",
    ]
)
PAGE_ATTRS = frozenset(("class", "id", "role", "open", "colspan", "rowspan", "href"))
# SVG geometry and paint attributes, as `html.parser` gives them (lower case).
SVG_ATTRS = frozenset(
    [
        "viewbox",
        "width",
        "height",
        "x",
        "y",
        "x1",
        "y1",
        "x2",
        "y2",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "d",
        "points",
        "transform",
        "dx",
        "dy",
        "fill",
        "stroke",
        "stroke-width",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-dasharray",
        "stroke-opacity",
        "fill-opacity",
        "opacity",
        "text-anchor",
        "dominant-baseline",
        "font-size",
        "font-weight",
        "preserveaspectratio",
        "xmlns",
    ]
)
FLOW_PLACEHOLDER = "{{flow}}"
CORE_FLOW_PLACEHOLDER = "{{flow:core}}"
CSS_BANNED = ("url(", "@import", "expression(", "</")
CSS_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(", re.IGNORECASE)
WORD = re.compile(r"[A-Za-z0-9][\w'’-]*")


class _PageParser(HTMLParser):
    """What `page_gate` reads from one fragment: tags and attributes outside the allow-list, citations
    inside a heading or a summary, `<code>` texts, and the visible text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bad: list[str] = []
        self.open: list[str] = []
        self.codes: list[str] = []
        self.text: list[str] = []
        self.heading_cites: list[str] = []
        self._code: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag not in PAGE_TAGS:
            self.bad.append(f"the tag <{tag}> is not allowed")
        for name, value in attrs:
            value = value or ""
            if name == "href":
                if tag not in ("a", "use") or not value.startswith("#"):
                    self.bad.append(
                        f"href={value!r} on <{tag}> is not a link inside the page"
                    )
            elif name.startswith("on") or name == "style":
                self.bad.append(f"the attribute {name} is not allowed")
            elif not (
                name in PAGE_ATTRS or name.startswith("aria-") or name in SVG_ATTRS
            ):
                self.bad.append(f"the attribute {name} on <{tag}> is not allowed")
            elif "url(" in value.lower() or "javascript:" in value.lower():
                self.bad.append(f"the attribute {name} holds {value!r}")
        if tag not in ("br",):
            self.open.append(tag)
        if tag == "code":
            self._code = []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.open and self.open[-1] == tag:
            self.open.pop()

    def handle_endtag(self, tag):
        if tag == "code" and self._code is not None:
            self.codes.append("".join(self._code))
            self._code = None
        if tag in self.open:
            while self.open and self.open.pop() != tag:
                pass

    def handle_data(self, data):
        if self._code is not None:
            self._code.append(data)
        if any(t in ("h3", "h4", "summary") for t in self.open) and CITE_ID.search(
            data
        ):
            self.heading_cites.append(data.strip()[:80])
        hidden = "details" in self.open and not (self.open and "summary" in self.open)
        self.text.append((data, hidden))

    def handle_comment(self, data):
        self.bad.append("an HTML comment is not allowed")

    def handle_decl(self, decl):
        self.bad.append(f"<!{decl}> is not allowed")

    def handle_pi(self, data):
        self.bad.append("a processing instruction is not allowed")


def _parse_page(html_text: str) -> _PageParser:
    p = _PageParser()
    p.feed(html_text)
    p.close()
    return p


def page_words(html_text: str) -> int:
    """Visible words of one fragment: not inside `<details>` (its summary counts), with the flow
    placeholders, snippets and citations taken out first."""
    plain = CITE_ID.sub(
        " ",
        html_text.replace(CORE_FLOW_PLACEHOLDER, " ").replace(FLOW_PLACEHOLDER, " "),
    )
    return sum(
        len(WORD.findall(t)) for t, hidden in _parse_page(plain).text if not hidden
    )


def css_gate(css: str) -> list[str]:
    """The page writer's CSS: nothing that loads or runs anything, no colour typed as a literal (colours
    come from the design system's tokens), and braces that balance, so it cannot close the block it is
    nested in."""
    reasons = [f"the CSS holds {b!r}" for b in CSS_BANNED if b in css.lower()]
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    declarations = re.findall(r":([^;{}]*)", plain)
    for value in declarations:
        m = CSS_COLOUR.search(value)
        if m:
            reasons.append(f"the CSS types the colour {m.group(0)!r}; use var(--token)")
            break
    depth = 0
    for ch in plain:
        depth += {"{": 1, "}": -1}.get(ch, 0)
        if depth < 0:
            break
    if depth != 0:
        reasons.append("the CSS braces do not balance")
    return reasons


def page_gate(
    out,
    *,
    open_keys: list[str],
    core_flow: bool,
    citations: dict[str, Citation],
    corpus: Corpus,
    labels: set[str],
) -> list[str]:
    """Failure reasons for the page writer's answer; empty when it passes. Never calls a model.

    The section keys are exactly *open_keys*, in order; `{{flow}}` is in the summary once and nowhere else,
    and `{{flow:core}}` appears only when the core section has a flow (*core_flow*), at most once; every
    `[[id]]` and `[[snippet:id]]` is a known citation; no citation sits in a heading or a summary; every
    tag and attribute is on the allow-list; the CSS passes `css_gate`; every `<code>` text is in the corpus
    or the labels; no `path:line` is typed in the text."""
    reasons: list[str] = []
    keys = [s.key for s in out.sections]
    if keys != open_keys:
        reasons.append(
            f"the sections are {keys}; they must be exactly {open_keys}, in that order"
        )
    texts = {s.key: s.html for s in out.sections}
    joined = "\n".join(texts.values())
    if (
        joined.count(FLOW_PLACEHOLDER) != 1
        or texts.get("summary", "").count(FLOW_PLACEHOLDER) != 1
    ):
        reasons.append(f"{FLOW_PLACEHOLDER} must appear exactly once, in the summary")
    n_core = joined.count(CORE_FLOW_PLACEHOLDER)
    if n_core and not core_flow:
        reasons.append(
            f"{CORE_FLOW_PLACEHOLDER} is used, but the core section has no flow"
        )
    elif n_core > 1:
        reasons.append(f"{CORE_FLOW_PLACEHOLDER} appears {n_core} times; at most once")
    for cid in dict.fromkeys(CITE_ID.findall(joined)):
        if cid not in citations:
            reasons.append(f"[[{cid}]] is not a gated citation")
    reasons += css_gate(out.css)
    cited_paths = {c.path for c in citations.values()}
    for key, text in texts.items():
        parsed = _parse_page(text)
        reasons += [f"{key}: {b}" for b in dict.fromkeys(parsed.bad)]
        reasons += [
            f"{key}: a citation in a heading or summary: {h!r}"
            for h in parsed.heading_cites
        ]
        for code in dict.fromkeys(parsed.codes):
            c = code.strip()
            if c and c not in labels and not _backtick_ok(c, corpus, cited_paths):
                reasons.append(f"{key}: `{c}` occurs in no snapshot")
        visible = CITE_ID.sub(" ", " ".join(t for t, _ in parsed.text))
        for m in PATH_LINE.finditer(visible):
            reasons.append(
                f"{key}: {m.group(0)} is a path with a line number typed in the text"
            )
    return reasons


# --- the diagram stage's picture (stages/diagram.py) ------------------------------------------------------

DIAGRAM_TAGS = frozenset(
    [
        "svg",
        "g",
        "path",
        "rect",
        "circle",
        "ellipse",
        "line",
        "polyline",
        "polygon",
        "text",
        "tspan",
        "title",
        "desc",
        "defs",
        "marker",
        "use",
        "a",
    ]
)
DIAGRAM_ATTRS = frozenset(
    (
        "class",
        "id",
        "role",
        "href",
        "markerwidth",
        "markerheight",
        "refx",
        "refy",
        "orient",
        "markerunits",
        "marker-start",
        "marker-end",
    )
)
# The viewBox width of each layout: wide for screens of 700 px and more, tall for phones.
DIAGRAM_WIDTHS = {"wide": 1000, "tall": 420}
DIAGRAM_MAX_CHARS = 40_000
DIAGRAM_ID = re.compile(r"[a-z][a-z0-9-]*")
DIAGRAM_REF = re.compile(r"url\(#([^)]*)\)")
# A token of a label that looks like code: an underscore or a call, camelCase, or a dot or slash between
# word characters.
CODE_TOKEN = re.compile(r"_|\(|[a-z][A-Z]|\w[./]\w")
NUMBER = re.compile(r"[\d.,]+%?")


class _SvgParser(HTMLParser):
    """What `diagram_gate` reads from one SVG: the elements at the top level, tags and attributes outside
    the allow-list, the ids, every `#id` reference, and the text inside `<text>`, `<title>` and `<desc>`."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bad: list[str] = []
        self.roots: list[tuple[str, dict[str, str]]] = []
        self.stray = False
        self.ids: list[str] = []
        self.refs: list[str] = []
        self.texts: list[str] = []
        self.depth = 0
        self._in_text = 0

    def handle_starttag(self, tag, attrs):
        values = {name: value or "" for name, value in attrs}
        if self.depth == 0:
            self.roots.append((tag, values))
        if tag not in DIAGRAM_TAGS:
            self.bad.append(f"the tag <{tag}> is not allowed")
        for name, value in values.items():
            low = value.lower()
            if name.startswith("on") or name == "style":
                self.bad.append(f"the attribute {name} is not allowed")
            elif not (
                name in DIAGRAM_ATTRS or name.startswith("aria-") or name in SVG_ATTRS
            ):
                self.bad.append(f"the attribute {name} on <{tag}> is not allowed")
            elif name == "href":
                if not value.startswith("#"):
                    self.bad.append(f"href={value!r} is not a reference inside the SVG")
                else:
                    self.refs.append(value[1:])
            elif name in ("marker-start", "marker-end"):
                m = DIAGRAM_REF.fullmatch(value.strip())
                if m:
                    self.refs.append(m.group(1))
                else:
                    self.bad.append(f"{name}={value!r} is not url(#id)")
            elif name in ("fill", "stroke") and low.strip() not in (
                "none",
                "currentcolor",
            ):
                self.bad.append(
                    f"{name}={value!r} on <{tag}>: a colour goes in the CSS as var(--token)"
                )
            elif "url(" in low or "javascript:" in low:
                self.bad.append(f"the attribute {name} holds {value!r}")
            if name == "id":
                self.ids.append(value)
        self.depth += 1
        if tag in ("text", "title", "desc"):
            self._in_text += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        self.depth = max(0, self.depth - 1)
        if tag in ("text", "title", "desc") and self._in_text:
            self._in_text -= 1
            self.texts.append("\n")

    def handle_data(self, data):
        if self.depth == 0 and data.strip():
            self.stray = True
        elif self._in_text:
            self.texts.append(data)

    def handle_comment(self, data):
        self.bad.append("a comment is not allowed")

    def handle_decl(self, decl):
        self.bad.append(f"<!{decl}> is not allowed")

    def unknown_decl(self, data):
        self.bad.append(f"<![{data[:20]}> is not allowed")

    def handle_pi(self, data):
        self.bad.append("a processing instruction is not allowed")


def _view_box_width(value: str) -> float | None:
    parts = value.replace(",", " ").split()
    try:
        return float(parts[2]) if len(parts) == 4 else None
    except ValueError:
        return None


def _code_tokens(text: str) -> list[str]:
    """The tokens of a label that look like code, with the punctuation around a word taken off."""
    out = []
    for raw in text.split():
        t = raw.lstrip("(\"'“‘").rstrip(".,;:!?\"'”’…")
        if t.endswith(")") and "(" not in t:
            t = t.rstrip(")")
        if t and not NUMBER.fullmatch(t) and CODE_TOKEN.search(t):
            out.append(t)
    return out


def diagram_css_gate(css: str) -> list[str]:
    """`css_gate`, and every selector starts with `.wd-`, so the diagram's CSS styles only the diagram."""
    reasons = css_gate(css)
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    depth, prelude = 0, []
    for ch in plain:
        if ch == "{":
            if depth == 0:
                for sel in "".join(prelude).split(","):
                    sel = sel.strip()
                    if not sel.startswith(".wd-"):
                        reasons.append(
                            f"the CSS selector {sel[:60]!r} does not start with .wd-"
                        )
            depth += 1
            prelude = []
        elif ch == "}":
            depth = max(0, depth - 1)
            prelude = []
        elif depth == 0:
            prelude.append(ch)
    return reasons


def diagram_svg_text(svg: str) -> str:
    """The text of one SVG's `<text>`, `<title>` and `<desc>` elements, whitespace collapsed."""
    p = _SvgParser()
    p.feed(svg)
    p.close()
    return " ".join("".join(p.texts).split())


def diagram_gate(
    out, *, labels: set[str], corpus: Corpus, box_labels: list[str]
) -> list[str]:
    """Failure reasons for the diagram writer's (or a look's) SVGs and CSS; empty when they pass. Never
    calls a model.

    Each of `wide` and `tall` is one well-formed root `<svg>` with a viewBox of the layout's width
    (`DIAGRAM_WIDTHS`), at most `DIAGRAM_MAX_CHARS` characters; tags and attributes are on the allow-list
    (no script, style, foreignObject, image, `on*` or comment; `href` only `#…`; `marker-*` only `url(#id)`;
    `fill` and `stroke` only `none` or `currentColor`); ids are unique, lower case and every reference points
    at one of them or at an icon of `render.ICON_SPRITE`; the text holds no `path:line`, and every token
    that looks like code is in *labels* or the corpus; every flow box label (*box_labels*) is in the text;
    and the CSS passes `diagram_css_gate`. Parsed with `html.parser`."""
    import xml.etree.ElementTree as ET

    # Imported here: render imports this module.
    from .render import ICON_SPRITE

    icons = set(re.findall(r'<symbol id="(i-[a-z]+)"', ICON_SPRITE))
    reasons: list[str] = []
    for layout, width in DIAGRAM_WIDTHS.items():
        svg = getattr(out, layout).strip()
        if not svg:
            reasons.append(f"{layout}: the SVG is empty")
            continue
        if len(svg) > DIAGRAM_MAX_CHARS:
            reasons.append(
                f"{layout}: the SVG is {len(svg):,} characters; at most {DIAGRAM_MAX_CHARS:,}"
            )
        p = _SvgParser()
        p.feed(svg)
        p.close()
        reasons += [f"{layout}: {b}" for b in dict.fromkeys(p.bad)]
        if len(p.roots) != 1 or p.roots[0][0] != "svg" or p.stray:
            reasons.append(f"{layout}: it must be exactly one root <svg> element")
        else:
            got = _view_box_width(p.roots[0][1].get("viewbox", ""))
            if got != width:
                reasons.append(
                    f'{layout}: the root <svg> needs viewBox="0 0 {width} H" (width {width})'
                )
        if "<!" not in svg and "<?" not in svg:
            try:
                ET.fromstring(svg)
            except ET.ParseError as e:
                reasons.append(f"{layout}: the SVG is not well-formed XML ({e})")
        ids = set(p.ids)
        for i in dict.fromkeys(p.ids):
            if not DIAGRAM_ID.fullmatch(i):
                reasons.append(
                    f"{layout}: the id {i!r} is not lower-case letters, digits and -"
                )
            if p.ids.count(i) > 1:
                reasons.append(f"{layout}: the id {i!r} is used more than once")
        for ref in dict.fromkeys(p.refs):
            if ref not in ids and ref not in icons:
                reasons.append(
                    f"{layout}: #{ref} points at no id of this SVG and no icon"
                )
        text = " ".join("".join(p.texts).split())
        for m in PATH_LINE.finditer(text):
            reasons.append(
                f"{layout}: {m.group(0)} is a path with a line number typed in the text"
            )
        for tok in dict.fromkeys(_code_tokens(text)):
            if tok not in labels and not _backtick_ok(tok, corpus, set()):
                reasons.append(
                    f"{layout}: {tok!r} looks like code and occurs in no snapshot"
                )
        low = text.lower()
        for label in box_labels:
            if " ".join(label.split()).lower() not in low:
                reasons.append(
                    f"{layout}: the box label {label!r} is not in the picture's text"
                )
    reasons += diagram_css_gate(out.css)
    return reasons
