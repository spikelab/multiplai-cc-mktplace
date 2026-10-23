"""Assemble the walkthrough from gated sections. Markdown first; the HTML page holds the same text.

Code, not a model, turns `[[cN]]` into `path:line` links and `[[snippet:cN]]`
into the cited lines read with `git show` at the commit. Each link carries its
quote as the link title, so `check.py` can re-run `citation_gate` on a finished
file. Every Mermaid diagram is drawn by code from boundary entries. The Paths
section is rendered by code from the trace stage's `map_paths` result, which
is also written, in human mode, as `<name>-paths.json` (paths.v1) by
code_facts' `write_paths_file`.
"""

from __future__ import annotations

import html
import os
import re
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import quote as url_quote

import code_facts
from code_facts.paths import PathHop, PathsResult, TracedPath

from . import design
from .check import LINK
from .gates import CITE_ID, lines_at_commit, sections_gate
from .models import Citation, RepoInfo, WalkState, hop_id
from .stages.page import OPEN_WORDS_MAX
from .stages.write import (
    APPENDIX_KEYS,
    citations_for_write,
    human_sections,
    required_titles,
)
from .target import git, github_web_base

LANG = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".sql": "sql",
    ".sqlx": "sql",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".json": "json",
    ".md": "markdown",
    ".html": "markup",
    ".sh": "bash",
    ".toml": "toml",
}
SNIPPET_MAX_LINES = 40
TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "walkthrough.html"
HOP_ID = re.compile(r"p\d+h\d+")


class Linker:
    def __init__(self, state: WalkState, out_dir: Path):
        self.repos = state.repos
        self.out_dir = out_dir
        self.citations = citations_for_write(state)

    def _url(self, c: Citation) -> str:
        key, _, rel = c.path.partition("/")
        repo = self.repos.get(key)
        if repo is None:
            return ""
        base = github_web_base(repo.remote_url)
        anchor = f"#L{c.line_start}" + (
            f"-L{c.line_end}" if c.line_end != c.line_start else ""
        )
        if base:
            return f"{base}/blob/{repo.head_sha}/{rel}{anchor}"
        # Percent-encode the local path: a space or `)` would end the Markdown link early.
        return url_quote(os.path.relpath(Path(repo.path) / rel, self.out_dir)) + anchor

    def link(self, c: Citation) -> str:
        where = f"{c.path}:{c.line_start}" + (
            f"-{c.line_end}" if c.line_end != c.line_start else ""
        )
        # `|` is escaped too: links sit in table cells, where a bare `|` splits the row.
        title = (
            " ".join(c.quote.split())[:200]
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("|", "\\|")
        )
        return f'[`{where}`]({self._url(c)} "{title}")'

    def snippet(self, c: Citation) -> str:
        end = min(c.line_end, c.line_start + SNIPPET_MAX_LINES - 1)
        lines = lines_at_commit(self.repos, c.path, c.line_start, end) or []
        # A citation can run past the end of the file; the header names only the lines git returned.
        end = c.line_start + max(len(lines), 1) - 1
        # Drop trailing blank lines and shrink the range to match, so the
        # header names exactly the lines shown and `check` can compare them.
        while len(lines) > 1 and not lines[-1].strip():
            lines = lines[:-1]
            end -= 1
        lang = LANG.get(PurePosixPath(c.path).suffix.lower(), "")
        body = "\n".join(lines)
        fence = "````" if "```" in body else "```"
        return (
            f"<!-- snippet {c.path}:{c.line_start}-{end} -->\n{self.link(c)}\n\n"
            f"{fence}{lang}\n{body}\n{fence}"
        )

    def expand(self, text: str) -> str:
        def snip(m: re.Match) -> str:
            c = self.citations.get(m.group(1))
            return "\n\n" + self.snippet(c) + "\n\n" if c else ""

        text = re.sub(r"\[\[snippet:([a-z0-9]+)\]\]", snip, text)

        def cite(m: re.Match) -> str:
            c = self.citations.get(m.group(1))
            if c is None:
                return ""
            # A hop citation also points at the hop's anchor in the Paths section.
            return self.link(c) + (
                f" ([{c.id}](#{c.id}))" if c.id and HOP_ID.fullmatch(c.id) else ""
            )

        return CITE_ID.sub(cite, text)


def _mermaid_id(i: int) -> str:
    return f"n{i}"


def _boundary_mermaid(state: WalkState) -> str:
    name = state.target.package
    inbound: dict[str, int] = {}
    for r in state.references:
        inbound[r.repo] = inbound.get(r.repo, 0) + 1
    outbound: dict[str, int] = {}
    for e in state.symbols:
        if e.direction == "calls":
            outbound[e.kind] = outbound.get(e.kind, 0) + 1
    lines = ["graph LR", f'    T["{name}"]']
    for i, (repo, n) in enumerate(sorted(inbound.items())):
        lines.append(f'    R{i}["{repo}"] -->|{n} refs| T')
    for i, (kind, n) in enumerate(sorted(outbound.items())):
        lines.append(f'    T -->|{n}| O{i}["{kind}"]')
    return "```mermaid\n" + "\n".join(lines) + "\n```"


def boundary_flow(state: WalkState) -> dict:
    """The boundary as flow data for `flow_picture`: each calling repository, the module, each kind of call out."""
    name = state.target.package
    inbound: dict[str, int] = {}
    for r in state.references:
        inbound[r.repo] = inbound.get(r.repo, 0) + 1
    outbound: dict[str, int] = {}
    for e in state.symbols:
        if e.direction == "calls":
            outbound[e.kind] = outbound.get(e.kind, 0) + 1
    nodes = [
        {"id": f"r{i}", "label": repo, "kind": "input", "does": f"{n} refs"}
        for i, (repo, n) in enumerate(sorted(inbound.items()))
    ]
    nodes.append({"id": "t", "label": name, "kind": "code"})
    nodes += [
        {"id": f"o{i}", "label": kind, "kind": "output", "does": f"{n} calls"}
        for i, (kind, n) in enumerate(sorted(outbound.items()))
    ]
    edges = [{"from": n["id"], "to": "t"} for n in nodes if n["kind"] == "input"]
    edges += [{"from": "t", "to": n["id"]} for n in nodes if n["kind"] == "output"]
    return {"nodes": nodes, "edges": edges}


# Where the HTML page puts the boundary picture in place of the Markdown's Mermaid.
BOUNDARY_SLOT = "BOUNDARYPICTURE"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _entry_link(lk: Linker, path: str, line: int, quote: str) -> str:
    return lk.link(Citation(path=path, line_start=line, line_end=line, quote=quote))


# --- the Paths section, rendered by code from the trace stage's map_paths result ---------------

# A label per paths.v1 entry kind; a kind not listed here renders verbatim.
KIND_LABELS: dict[str, str] = {
    "argparse": "argparse command",
    "click": "click command",
    "typer": "typer command",
    "main": "`main` module",
    "script": "script",
    "skill_command": "skill command",
    "hook": "hook",
    "seed": "caller seed",
    "model": "found by the model fallback",
}


def _hop_citation(repo_key: str, hop: PathHop) -> Citation:
    return Citation(
        path=f"{repo_key}/{hop.path}",
        line_start=hop.line_start,
        line_end=hop.line_end,
        quote=hop.quote,
    )


def _line_text(repos: dict[str, RepoInfo], ws: str, line: int) -> str:
    return " ".join(lines_at_commit(repos, ws, line, line) or [])


def _path_block(state: WalkState, lk: Linker, path: TracedPath) -> list[str]:
    key = state.target.repo_key
    pid, entry, hops = path.id, path.entry, path.hops
    out = [
        f"**{pid}** — `{entry.name}` ({KIND_LABELS.get(entry.kind, entry.kind)})",
        "",
    ]
    written = state.sections.get(f"path:{pid}")
    if written:
        out += [p for para in written.paragraphs for p in (lk.expand(para), "")]
    loc = entry.location
    first = hops[0]
    if first.path == loc.path and first.line_start <= loc.line <= first.line_end:
        where = f"`{hop_id(pid, 1)}`"
    else:
        ws = f"{key}/{loc.path}"
        where = _entry_link(lk, ws, loc.line, _line_text(lk.repos, ws, loc.line))
    out.append(f"- Entry: invoked by `{entry.invoked_by}`, at {where}")
    out.append("- Hops:")
    for k, hop in enumerate(hops, start=1):
        hid = hop_id(pid, k)
        out.append(f'  - <a id="{hid}"></a>`{hid}` {lk.link(_hop_citation(key, hop))}')
    if path.stops_at is not None:
        out.append(
            f"- Stops after `{hop_id(pid, path.stops_at)}`: `{path.break_reason}`"
        )
    else:
        out.append(f"- Ends at `{hop_id(pid, len(hops))}` (trace complete).")
    counted = ", ".join(f"`{t.node_id}`" for t in path.tests.e2e) or "no e2e test"
    mocked = ", ".join(
        f"`{t.node_id}` (mocks `{hop_id(pid, 2)}`, not counted)"
        for t in path.tests.mocks_hop_2
    )
    out.append(
        f"- E2E tests: {counted}" + (f"; not counted: {mocked}" if mocked else "")
    )
    return out


# Diagnostics listed in Left out; only seed_not_found counts as an entry left out.
LEFT_OUT_DIAGNOSTICS = ("seed_not_found", "dynamic_name")


def left_out_count(result: PathsResult) -> int:
    """Entries left out: every drop, plus every seed that was not found."""
    return len(result.dropped) + sum(
        1 for d in result.diagnostics if d.reason == "seed_not_found"
    )


def render_left_out(result: PathsResult, repo_key: str, level: int = 3) -> str:
    """The `### Left out` table (`level` sets the heading's level): drops, then diagnostics, then e2e
    listing skips. "" when there is no row.

    Entries are plain text, never links: they are not claims, so `check` does not re-read them.
    """
    rows: list[tuple[str, str, str, str]] = []
    for d in result.dropped:
        loc = d.entry.location
        rows.append(
            (
                f"`{repo_key}/{loc.path}:{loc.line}` (`{d.entry.name}`)",
                d.stage,
                d.reason,
                d.detail,
            )
        )
    for g in result.diagnostics:
        if g.reason in LEFT_OUT_DIAGNOSTICS:
            where = f"{repo_key}/{g.path}" + (
                f":{g.line}" if g.line is not None else ""
            )
            rows.append((f"`{where}`", "discover", g.reason, g.detail))
    for skip in result.test_listing_skips:
        rows.append((f"`{repo_key}/{skip.file}`", "e2e", "e2e_listing", skip.reason))
    if not rows:
        return ""
    out = [
        "#" * level + " Left out",
        "",
        "| Entry | Stage | Reason | Detail |",
        "|---|---|---|---|",
    ]
    out += [
        f"| {_cell(entry)} | {_cell(stage)} | `{_cell(reason)}` | {_cell(detail)} |"
        for entry, stage, reason, detail in rows
    ]
    return "\n".join(out)


def _entries_left_out(m: int) -> str:
    return (
        "1 entry was left out" if m == 1 else f"{m} entries were left out"
    ) + "; see **Left out** under Coverage."


# Appended to the Paths lead when the model fallback failed: entries static discovery missed may be absent.
FALLBACK_FAILED = " Entry discovery was incomplete: the model fallback failed."


def render_paths_section(state: WalkState, linker: Linker) -> str:
    """The Paths body: a lead sentence, then one block per path in map_paths order."""
    if state.paths_error:
        return f"Path finding failed: {state.paths_error}"
    paths = state.paths.paths if state.paths else []
    m = left_out_count(state.paths) if state.paths else 0
    incomplete = (
        FALLBACK_FAILED
        if state.paths and state.paths.fallback.status == "failed"
        else ""
    )
    if not paths:
        return (
            f"No paths were traced. {_entries_left_out(m)}"
            if m
            else "No entry points reaching this module were found."
        ) + incomplete
    n = len(paths)
    lead = "1 path reaches this module." if n == 1 else f"{n} paths reach this module."
    out = [lead + (f" {_entries_left_out(m)}" if m else "") + incomplete, ""]
    for path in paths:
        out += _path_block(state, linker, path) + [""]
    return "\n".join(out).rstrip()


def coverage_lines(state: WalkState, left_out_level: int = 3) -> list[str]:
    t = state.target
    cited = {c.path for f in state.facts for c in f.citations}
    files = [t.ws(f) for f in t.files]
    with_fact = [f for f in files if f in cited]
    big = [f for f in files if t.file_lines.get(f.split("/", 1)[1], 0) > 30]
    big_with = [f for f in big if f in cited]
    pct = (100.0 * len(with_fact) / len(files)) if files else 0.0
    pct_big = (100.0 * len(big_with) / len(big)) if big else 0.0
    out = [
        "| Measure | Value |",
        "|---|---|",
        f"| Files with a surviving fact | {len(with_fact)} of {len(files)} ({pct:.1f}%) |",
        f"| Files over 30 lines with a surviving fact | {len(big_with)} of {len(big)} ({pct_big:.1f}%) |",
        f"| Migrations (counted, not read) | {t.migrations}"
        + (
            f"; latest `{PurePosixPath(t.latest_migration).name}`"
            if t.latest_migration
            else ""
        )
        + " |",
        f"| Boundary entries not cited by any fact | {len(state.uncovered_entries)} |",
        f"| Facts kept | {len(state.facts)} |",
    ]
    by_stage: dict[str, int] = {}
    for d in state.dropped:
        by_stage[d.stage] = by_stage.get(d.stage, 0) + 1
    for stage, n in sorted(by_stage.items()):
        out.append(f"| Claims dropped by the {stage} checks | {n} |")
    cut = sum(len(s.cut) for s in state.sections.values())
    what = (
        "Paragraphs" if state.options.get("mode") == "agent" else "Paragraphs and items"
    )
    out.append(f"| {what} cut by the write checks | {cut} |")
    if state.options.get("mode") != "agent":
        paths = state.paths.paths if state.paths else []
        out.append(f"| Paths traced | {len(paths)} |")
        out.append(
            f"| Paths cut short | {sum(1 for p in paths if p.stops_at is not None)} |"
        )
        out.append(
            f"| Entries left out | {left_out_count(state.paths) if state.paths else 0} |"
        )
    if state.lsp:
        lsp = state.lsp
        detail = (
            f"{lsp.get('status')} in {lsp.get('seconds')}s, {lsp.get('answered')} of {lsp.get('symbols')} "
            "symbols answered"
            if not str(lsp.get("status", "")).startswith("skipped")
            else lsp.get("status")
        )
        out.append(f"| pyright references | {detail} |")
    out.append("")
    left_out = (
        render_left_out(state.paths, t.repo_key, left_out_level) if state.paths else ""
    )
    if left_out:
        out += [left_out, ""]
    missing = [f for f in files if f not in cited]
    if missing:
        out.append(f"Files with no surviving fact ({len(missing)}):")
        out.append("")
        for f in missing:
            n = t.file_lines.get(f.split("/", 1)[1], 0)
            out.append(
                f"- `{f}` ({n} lines{', at or under 30: not required' if n <= 30 else ''})"
            )
        out.append("")
    if t.skipped:
        out.append(
            "Files not read: "
            + ", ".join(f"`{t.ws(p)}` ({why})" for p, why in sorted(t.skipped.items()))
        )
        out.append("")
    if state.uncovered_entries:
        out.append(
            f"Boundary entries no fact explains ({len(state.uncovered_entries)}):"
        )
        out.append("")
        out += [f"- `{e}`" for e in state.uncovered_entries[:200]]
        if len(state.uncovered_entries) > 200:
            out.append(
                f"- … and {len(state.uncovered_entries) - 200} more (see boundary.json)"
            )
        out.append("")
    notes = list(state.warnings) + list(state.errors)
    if notes:
        out.append("Warnings and errors:")
        out.append("")
        out += [f"- {_cell(n)}" for n in notes]
        out.append("")
    return out


def render_markdown(state: WalkState, out_dir: Path) -> str:
    lk = Linker(state, out_dir)
    t = state.target
    mode = state.options.get("mode", "human")
    if mode != "agent":
        return human_markdown(state, lk, out_dir)
    spent = state.budget.get("cost_usd", 0.0)
    calls = state.budget.get("calls", 0)
    md: list[str] = []
    md += [
        f"# Module: {t.ws(t.subpath)}",
        "",
        f"> Generated {date.today().isoformat()} · depth {state.options.get('depth', 'standard')} · mode {mode} · "
        f"{len(t.files)} files · spent ${spent:.2f} over {calls} agent calls",
        "",
        f"- Module directory: `{os.path.relpath(Path(t.repo_path) / t.subpath, out_dir)}`",
        f"- Depth: `{state.options.get('depth', 'standard')}`",
        f"- Mode: `{mode}`",
        "",
        "Every `path:line` below was checked against the commit listed here. "
        "Re-check with `python -m walkthrough_pipeline check <this file>`.",
        "",
        "## Commits read",
        "",
        "| Repo | Path | Commit |",
        "|---|---|---|",
    ]
    for key, repo in sorted(
        state.repos.items(), key=lambda kv: (kv[0] != t.repo_key, kv[0])
    ):
        md.append(
            f"| {key} | {os.path.relpath(repo.path, out_dir)} | `{repo.head_sha}` |"
        )
    md.append("")

    def section(key: str) -> list[str]:
        s = state.sections.get(key)
        return [lk.expand(p) + "\n" for p in s.paragraphs] if s else []

    md += [f"## Files ({len(t.files)})", ""]
    for f in t.files:
        md.append(
            f"- `{f}` — {state.file_purposes.get(t.ws(f), '(no checked purpose)')}"
        )
    md.append("")
    for key, title in (
        ("architecture", "Architecture"),
        ("patterns", "Patterns & Conventions"),
        ("interfaces", "Key Interfaces"),
        ("gotchas", "Gotchas"),
    ):
        md += [f"## {title}", ""] + section(key)
    md += ["## Coverage", ""] + coverage_lines(state)
    md += _cut_appendix(state, lk)
    return "\n".join(md).rstrip() + "\n"


def _docs_tables(state: WalkState, lk: Linker) -> list[str]:
    d = state.docs or {}
    paths = d.get("api_paths") or []
    if not paths:
        return ["No outbound API paths were found.", ""]
    out = []
    if d.get("docs_url"):
        out += [
            f"Docs: {d['docs_url']} — `llms.txt` lists {d.get('llms_pages', 0)} pages; "
            f"{len(d.get('files', {}))} fetched."
            + (
                f" Local docs: {', '.join(Path(x).name for x in d.get('local_dirs', []))}."
                if d.get("local_dirs")
                else ""
            ),
            "",
        ]
    coll = d.get("collection") or {}
    mapped = coll.get("mapped", {})
    out += [
        "| API path the code calls | Docs page | Collection request |",
        "|---|---|---|",
    ]
    for p in paths:
        pages = d.get("mappings", {}).get(p, [])
        page = "<br>".join(pages) if pages else "no doc page found"
        reqs = mapped.get(p, [])
        req = (
            "<br>".join(f"`{r['file']}` ({r['method']})" for r in reqs)
            if reqs
            else "not in the collection"
        )
        out.append(f"| `{p}` | {page} | {req} |")
    out.append("")
    if d.get("no_doc_page"):
        out += [
            "No doc page found for: " + ", ".join(f"`{p}`" for p in d["no_doc_page"]),
            "",
        ]
    if coll.get("dir"):
        out += [
            f"Not in the collection `{Path(coll['dir']).name}`: "
            + (", ".join(f"`{p}`" for p in coll.get("missing", [])) or "none"),
            "",
            "Open a request file in Bruno and run it with your own API key; this pipeline never calls the API.",
            "",
        ]
    dis = d.get("disagreements") or []
    if dis:
        out += ["Where the code and the docs disagree:", ""]
        for x in dis:
            c = Citation.model_validate(x["citation"])
            out.append(
                f"- {_cell(x['summary'])} — code {lk.link(c)}; docs {x['url']}: “{_cell(x['doc_quote'][:200])}”"
            )
        out.append("")
    return out


def _cut_appendix(state: WalkState, lk: Linker) -> list[str]:
    cut = cut_lines(state)
    if not cut:
        return []
    return [
        "## Cut by the checks",
        "",
        "These paragraphs failed the write checks twice and are not part of the walkthrough above.",
        "",
        *cut,
        "",
    ]


def cut_lines(state: WalkState) -> list[str]:
    """One line per paragraph or item the write checks cut, with its section and reasons; first the core
    stage's choice when its checks cut it."""
    out = [
        f"- (The part that does the main job) {_cell(d.what[:400])} — **{_cell(d.reason[:300])}**"
        for d in state.dropped
        if d.stage == "core"
    ]
    for s in state.sections.values():
        for c in s.cut:
            text = CITE_ID.sub("", c["text"]).replace("\n", " ")
            out.append(
                f"- ({_cell(s.title)}) {_cell(text[:400])} — **{_cell('; '.join(c['reasons'])[:300])}**"
            )
    return out


# --- the human-mode explainer: one Markdown section per H2 of HUMAN_SECTIONS -----------------------


class MdSection:
    """An H2's Markdown: the text under its heading, then its H3 subsections as (title, text)."""

    def __init__(self, body: str = "", subs: list[tuple[str, str]] | None = None):
        self.body = body
        self.subs = subs or []


def _join(lines: list[str]) -> str:
    return "\n".join(lines).strip()


def _fence(text: str, lang: str = "") -> str:
    fence = "````" if "```" in text else "```"
    return f"{fence}{lang}\n{text.strip(chr(10))}\n{fence}"


EXAMPLE_LANG = {
    "command": "bash",
    "call": "python",
    "file": "",
    "json": "json",
    "yaml": "yaml",
    "markdown": "markdown",
    "text": "text",
}


def _data(state: WalkState, key: str) -> dict:
    s = state.sections.get(key)
    return s.data if s else {}


def _cites(lk: Linker, ids: list[str]) -> str:
    return " ".join(lk.expand(f"[[{i}]]") for i in ids if i in lk.citations)


def _mermaid_text(text: str) -> str:
    return " ".join(text.replace('"', "#quot;").replace("|", "/").split())


# Each box kind's Mermaid shape, as (open, close) around the quoted label.
MERMAID_SHAPES = {
    "input": ("([", "])"),
    "output": ("([", "])"),
    "agent": ("(", ")"),
    "check": ("{{", "}}"),
    "decision": ("{", "}"),
    "code": ("[", "]"),
}


def _mermaid_box(n: dict) -> str:
    left, right = MERMAID_SHAPES.get(n.get("kind") or "code", MERMAID_SHAPES["code"])
    label = n["label"] + (f" ×{n['fan_out']}" if n.get("fan_out") else "")
    return f'{left}"{_mermaid_text(label)}"{right}'


def flow_mermaid(data: dict) -> str:
    """`flowchart TD` from the gated boxes and arrows, one shape per box kind; "" when the diagram was cut."""
    nodes = data.get("nodes") or []
    if not nodes:
        return ""
    ids = {n["id"]: f"f{i}" for i, n in enumerate(nodes, start=1)}
    lines = ["flowchart TD"] + [f"    {ids[n['id']]}{_mermaid_box(n)}" for n in nodes]
    for e in data.get("edges") or []:
        label = f'|"{_mermaid_text(e["label"])}"|' if e.get("label") else ""
        lines.append(f"    {ids[e['from']]} -->{label} {ids[e['to']]}")
    return "\n".join(lines)


def _picture_in_summary(state: WalkState) -> bool:
    """A module's or a codebase's flow diagram sits in What it is, under its paragraphs; a change's stays in
    its flow section."""
    return state.target.scope != "change"


def _md_summary(state: WalkState, lk: Linker) -> MdSection:
    paras = _data(state, "summary").get("paragraphs") or []
    body = [lk.expand(p) + "\n" for p in paras]
    src = flow_mermaid(_data(state, "flow")) if _picture_in_summary(state) else ""
    if src:
        body += ["```mermaid", src, "```"]
    return MdSection(_join(body) or "Nothing about this section passed the checks.")


def _md_usecases(state: WalkState, lk: Linker) -> MdSection:
    subs = []
    for c in _data(state, "usecases").get("cases") or []:
        ex = c["example"]
        out = []
        who = " · ".join(
            x
            for x in (
                f"**Who:** {lk.expand(c['who'])}" if c.get("who") else "",
                f"**When:** {lk.expand(c['when'])}" if c.get("when") else "",
            )
            if x
        )
        if who:
            out += [who, ""]
        out += [_fence(ex["text"], EXAMPLE_LANG.get(ex["kind"], "")), ""]
        if c.get("what_happens"):
            out += [lk.expand(c["what_happens"]), ""]
        if c.get("result"):
            out += [f"**What comes out:** {lk.expand(c['result'])}", ""]
        out += [f"Based on: {_cites(lk, c.get('cites') or [])}", ""]
        subs.append((_cell(c["title"]), _join(out)))
    return MdSection("" if subs else "No use case passed the checks.", subs)


def _md_flow(state: WalkState, lk: Linker) -> MdSection:
    data = _data(state, "flow")
    body = []
    src = flow_mermaid(data)
    if src:
        if not _picture_in_summary(state):
            body += ["```mermaid", src, "```", ""]
        body += ["Where each box is in the code:", ""]
        body += [
            f"- {_cell(n['label'])}: {_cites(lk, [n['cite']])}" for n in data["nodes"]
        ]
    elif not data.get("steps"):
        body.append("Nothing about this section passed the checks.")
    subs = []
    for i, st in enumerate(data.get("steps") or [], start=1):
        out = [lk.expand(st["summary"]), ""]
        out += [x for p in st.get("details") or [] for x in (lk.expand(p), "")]
        out += [
            x
            for cid in st.get("snippets") or []
            if cid in lk.citations
            for x in (lk.snippet(lk.citations[cid]), "")
        ]
        subs.append((f"{i}. {_cell(st['title'])}", _join(out)))
    return MdSection(_join(body), subs)


# The fields of a core part, as (field, what the page calls it).
CORE_PART_FIELDS = (
    ("looks_at", "Looks at"),
    ("given", "Is given"),
    ("produces", "Produces"),
    ("checked_by", "Checked by"),
)


def _md_core(state: WalkState, lk: Linker) -> MdSection:
    data = _data(state, "core")
    body = [lk.expand(x) + "\n" for x in [data.get("summary") or ""] if x]
    body += [lk.expand(p) + "\n" for p in data.get("paragraphs") or []]
    flow = data.get("flow") or {}
    src = flow_mermaid(flow)
    if src:
        body += ["```mermaid", src, "```", ""]
    subs = []
    for part in data.get("parts") or []:
        out = [
            f"**{head}:** {lk.expand(part[field])}\n"
            for field, head in CORE_PART_FIELDS
            if part.get(field)
        ]
        out.append(f"Where: {_cites(lk, [part['cite']])}")
        subs.append((_cell(part["name"]), _join(out)))
    for i, st in enumerate(flow.get("steps") or [], start=1):
        subs.append((f"{i}. {_cell(st['title'])}", lk.expand(st["summary"])))
    return MdSection(
        _join(body)
        or ("" if subs else "Nothing about this section passed the checks."),
        subs,
    )


def _md_outputs(state: WalkState, lk: Linker) -> MdSection:
    subs = []
    for it in _data(state, "outputs").get("items") or []:
        out = [lk.expand(it["what"]), "", f"Written at: {_cites(lk, [it['cite']])}", ""]
        ex = it.get("example")
        if ex:
            out += [
                f"Example ({ex['format']}):",
                "",
                _fence(ex["text"], EXAMPLE_LANG.get(ex["format"], "")),
                "",
            ]
        subs.append((_cell(it["name"]), _join(out)))
    return MdSection("" if subs else "No output passed the checks.", subs)


def _md_config(state: WalkState, lk: Linker) -> MdSection:
    rows = _data(state, "config").get("rows") or []
    if not rows:
        return MdSection("No setting passed the checks.")
    out = ["| Setting | What it changes | Default | Where |", "|---|---|---|---|"]
    for r in rows:
        default = f"`{_cell(r['default'])}`" if r.get("default") else ""
        out.append(
            f"| `{_cell(r['name'])}` | {lk.expand(_cell(r['effect']))} | {default} | {_cites(lk, [r['cite']])} |"
        )
    return MdSection(_join(out))


# `--scope codebase`: each table's columns, as (row field, heading, how the cell is shown).
# "text" may cite `[[cN]]`; "code" is shown as code; "cite" is the row's citation.
TABLE_COLUMNS = {
    "loops": [
        ("what", "What repeats", "text"),
        ("cap", "Cap", "code"),
        ("ends_well", "Ends well when", "text"),
        ("ends_badly", "Ends badly when", "text"),
        ("cite", "Where", "cite"),
    ],
    "exits": [
        ("signal", "Code or marker", "code"),
        ("meaning", "Means", "text"),
        ("action", "What to do", "text"),
        ("cite", "Where", "cite"),
    ],
    "resume": [
        ("stop", "Stop", "text"),
        ("on_disk", "On disk", "text"),
        ("rerun", "A re-run", "text"),
        ("cite", "Where", "cite"),
    ],
}
EMPTY_TABLE = "Nothing about this passed the checks."


def _md_table(state: WalkState, lk: Linker, key: str) -> MdSection:
    rows = _data(state, key).get("rows") or []
    if not rows:
        return MdSection(EMPTY_TABLE)
    cols = TABLE_COLUMNS[key]
    out = [
        "| " + " | ".join(head for _, head, _ in cols) + " |",
        "|" + "---|" * len(cols),
    ]
    for r in rows:
        cells = []
        for field, _, how in cols:
            value = r.get(field) or ""
            if how == "cite":
                cells.append(_cites(lk, [value]))
            elif how == "code":
                # A writer may backtick names inside the value; the whole cell is code already.
                cells.append(f"`{_cell(value.replace('`', ''))}`" if value else "")
            else:
                # Escaped before expanding: a link's title escapes its own `|`.
                cells.append(lk.expand(_cell(value)))
        out.append("| " + " | ".join(cells) + " |")
    return MdSection(_join(out))


def _md_gotchas(state: WalkState, lk: Linker) -> MdSection:
    s = state.sections.get("gotchas")
    return MdSection(
        _join([lk.expand(p) + "\n" for p in s.paragraphs])
        if s and s.paragraphs
        else "No gotcha passed the checks."
    )


def _prose(state: WalkState, lk: Linker, key: str) -> list[str]:
    s = state.sections.get(key)
    return [lk.expand(p) + "\n" for p in s.paragraphs] if s else []


def _md_affected(state: WalkState, lk: Linker) -> MdSection:
    """Who is affected: the writer's text, then the callers of what the change edits, as a table."""
    body = _prose(state, lk, "affected")
    callers = [r for r in state.references if r.kind == "caller"]
    if callers:
        body += ["| Repo | Calls | Where |", "|---|---|---|"]
        body += [
            f"| {r.repo} | `{_cell(r.symbol)}` | {_entry_link(lk, r.path, r.line, r.quote)} |"
            for r in sorted(callers, key=lambda r: (r.repo, r.path, r.line))
        ]
    return MdSection(
        _join(body)
        or "Nothing passed the checks, and no caller of a changed function or class was found."
    )


# Drop reasons of map_paths that mean an entry point's path does not go through the change.
NOT_REACHING = ("unreachable", "no_changed_line_covered")
# Entry points listed under "Entry points that miss it"; the rest are in the Left out table.
MAX_UNTOUCHED_ENTRIES = 20


def top_dirs(repo: RepoInfo) -> list[str]:
    """The top-level directories of a repository's tree at its commit."""
    out = git(repo.path, "ls-tree", "-d", "--name-only", repo.head_sha).stdout
    return sorted(d for d in out.splitlines() if d)


def _md_untouched(state: WalkState, lk: Linker) -> MdSection:
    """Written by code: the files changed, the top-level directories with none, and the entry points whose
    paths do not go through the change."""
    t = state.target
    ch = t.change
    files = ["| Status | File | Lines added | Lines removed |", "|---|---|---|---|"]
    for f in ch.files:
        moved = (
            f" (from `{_cell(f['old_path'])}`)"
            if f["old_path"] not in ("", f["path"])
            else ""
        )
        files.append(
            f"| {f['status']} | `{_cell(f['path'])}`{moved} | {sum(b - a + 1 for a, b in f['added'])} "
            f"| {sum(b - a + 1 for a, b in f['removed'])} |"
        )
    touched = {
        PurePosixPath(p).parts[0]
        for f in ch.files
        for p in (f["path"], f["old_path"])
        if p and len(PurePosixPath(p).parts) > 1
    }
    quiet = [d for d in top_dirs(state.repos[t.repo_key]) if d not in touched]
    dirs = (
        "No file changed under: " + ", ".join(f"`{_cell(d)}/`" for d in quiet) + "."
        if quiet
        else "Every top-level directory has a changed file."
    )
    drops = (
        [d for d in state.paths.dropped if d.reason in NOT_REACHING]
        if state.paths
        else []
    )
    entries = (
        [
            "Entry points whose paths do not go through the change:",
            "",
            *[
                f"- `{_cell(d.entry.name)}` (`{t.repo_key}/{d.entry.location.path}:{d.entry.location.line}`)"
                for d in drops[:MAX_UNTOUCHED_ENTRIES]
            ],
            *(
                [
                    f"- … and {len(drops) - MAX_UNTOUCHED_ENTRIES} more; see **Left out** under Coverage."
                ]
                if len(drops) > MAX_UNTOUCHED_ENTRIES
                else []
            ),
        ]
        if drops
        else [
            "No entry point was found whose path misses the change."
            if state.paths
            else "Path finding did not run, so no entry point is listed."
        ]
    )
    return MdSection(
        "This section is written by code from the diff and the traced paths, not by a model.",
        [
            ("Files changed", _join(files)),
            ("Directories with no change", dirs),
            ("Entry points that miss it", _join(entries)),
        ],
    )


def _md_reference(
    state: WalkState, lk: Linker, boundary: str | None = None
) -> MdSection:
    """Reference's subsections. *boundary* replaces the boundary's Mermaid (the HTML page's slot)."""
    subs = [("Paths", render_paths_section(state, lk))]
    tour: list[str] = []
    for u in state.units:
        s = state.sections.get(f"tour:{u.id}")
        tour += [f"#### {s.title if s else u.id}", ""] + _prose(
            state, lk, f"tour:{u.id}"
        )
    subs.append(
        (
            "Change tour" if state.target.scope == "change" else "Module tour",
            _join(tour),
        )
    )

    who = [boundary or _boundary_mermaid(state), "", "#### Who calls it", ""] + _prose(
        state, lk, "boundary:inbound"
    )
    calls = [r for r in state.references if r.kind != "dotted-string"]
    strings = [r for r in state.references if r.kind == "dotted-string"]
    for refs, head in (
        (calls, None),
        (strings, "#### Mentions in strings (not calls)"),
    ):
        if head:
            who += [
                head,
                "",
                "These name the module inside a quoted string. They are not calls.",
                "",
            ]
        if refs:
            who += [
                "| Repo | Kind | Symbol | Where | Found by |",
                "|---|---|---|---|---|",
            ]
            for r in sorted(refs, key=lambda r: (r.repo, r.kind, r.path, r.line)):
                who.append(
                    f"| {r.repo} | {r.kind} | `{_cell(r.symbol)}` | {_entry_link(lk, r.path, r.line, r.quote)} "
                    f"| {r.method} |"
                )
            who.append("")
        elif head:
            who += ["None.", ""]
    who += ["#### What it calls", ""] + _prose(state, lk, "boundary:outbound")
    out_entries = [
        e
        for e in state.symbols
        if e.direction == "calls" and e.kind in ("http", "import")
    ]
    if out_entries:
        who += ["| Kind | Target | Where |", "|---|---|---|"]
        for e in sorted(out_entries, key=lambda e: (e.kind, e.name, e.path, e.line)):
            who.append(
                f"| {e.kind} | `{_cell(e.name)}` | {_entry_link(lk, e.path, e.line, e.quote)} |"
            )
    subs.append(("Who calls it and what it calls", _join(who)))

    data = _prose(state, lk, "data")
    models = [e for e in state.symbols if e.kind == "model"]
    if models:
        data += ["| Model | Table | Defined at |", "|---|---|---|"]
        data += [
            f"| `{e.name}` | `{e.detail.get('table', '')}` | {_entry_link(lk, e.path, e.line, e.quote)} |"
            for e in models
        ]
        data.append("")
    caches = [e for e in state.symbols if e.kind == "cache"]
    if caches:
        data += ["| Cache key | Call | Where |", "|---|---|---|"]
        data += [
            f"| `{_cell(e.name)}` | {e.detail.get('method', '')} | {_entry_link(lk, e.path, e.line, e.quote)} |"
            for e in caches
        ]
        data.append("")
    defs = {e.name: e for e in state.symbols if e.kind == "setting-definition"}
    reads = [e for e in state.symbols if e.kind == "setting"]
    if reads:
        data += [
            "| Setting | Defined at | Env vars it reads | First read at |",
            "|---|---|---|---|",
        ]
        seen = set()
        for e in reads:
            if e.name in seen:
                continue
            seen.add(e.name)
            d = defs.get(e.name)
            data.append(
                f"| `{e.name}` | {_entry_link(lk, d.path, d.line, d.quote) if d else '(not found in settings)'} "
                f"| {', '.join(f'`{x}`' for x in d.detail.get('env', '').split(',') if x) if d else ''} "
                f"| {_entry_link(lk, e.path, e.line, e.quote)} |"
            )
        data.append("")
    envs = [e for e in state.symbols if e.kind == "env"]
    if envs:
        data += ["| Env var read directly | Where |", "|---|---|"]
        data += [
            f"| `{e.name}` | {_entry_link(lk, e.path, e.line, e.quote)} |" for e in envs
        ]
    subs.append(
        ("Data stores", _join(data) or "No data stores or settings were found.")
    )

    subs.append(
        ("Vendor docs", _join(_prose(state, lk, "docs") + _docs_tables(state, lk)))
    )

    gl = state.sections.get("glossary")
    if gl and gl.glossary:
        rows = ["| Term | Meaning | Where |", "|---|---|---|"]
        for term in gl.glossary:
            where = lk.link(term.citation) if term.citation else ""
            rows.append(f"| {_cell(term.term)} | {_cell(term.meaning)} | {where} |")
        subs.append(("Glossary", _join(rows)))
    else:
        subs.append(("Glossary", "No domain terms passed the checks."))
    g = state.sections.get("gotchas")
    if g and g.questions:
        subs.append(
            (
                "Questions for the owner",
                _join([f"- {lk.expand(q)}" for q in g.questions]),
            )
        )
    return MdSection(
        "Details for when you need them: every path traced, every file, every caller.",
        subs,
    )


def commit_repos(state: WalkState) -> list[str]:
    """The repositories "Commits read" lists: the target's, each one with a reference that passed the
    checks, and any other one a citation points into (so `check` can read every link)."""
    t = state.target
    keys = {t.repo_key} | {r.repo for r in state.references}
    keys |= {c.path.partition("/")[0] for c in citations_for_write(state).values()}
    if t.change:
        keys.add(t.change.base_key)
    return sorted(
        (k for k in keys if k in state.repos), key=lambda k: (k != t.repo_key, k)
    )


def searched_line(state: WalkState, out_dir: Path) -> str:
    n, m = len(state.searched_repos), len({r.repo for r in state.references})
    root = state.options.get("search_root_resolved")
    where = f" under `{os.path.relpath(root, out_dir)}`" if root else ""
    return (
        f"Searched {n} repositor{'y' if n == 1 else 'ies'}{where}; {m} had references."
    )


def _md_made(state: WalkState, out_dir: Path) -> MdSection:
    t = state.target
    commits = ["| Repo | Path | Commit |", "|---|---|---|"]
    commits += [
        f"| {k} | {os.path.relpath(state.repos[k].path, out_dir)} | `{state.repos[k].head_sha}` |"
        for k in commit_repos(state)
    ]
    if t.change:
        ch = t.change
        what = (
            f"- Change: PR #{ch.pr}"
            + (
                f" (`{_cell(ch.head_ref)}` into `{_cell(ch.base_ref)}`)"
                if ch.base_ref
                else ""
            )
            if ch.kind == "pr"
            else f"- Change: range `{_cell(ch.ref)}`"
        )
        scope = [
            what,
            f"- Read at head `{t.head_sha}` (`{t.repo_key}`) against the merge-base `{ch.base_sha}` "
            f"(`{ch.base_key}`)",
            f"- Commits: {len(ch.commits)}",
        ]
    else:
        scope = [
            f"- Module directory: `{os.path.relpath(Path(t.repo_path) / t.subpath, out_dir)}`"
        ]
        if state.options.get("scope") == "codebase":
            scope.append("- Scope: codebase")
            scope += [
                f"- Library read: `{lib['key']}/{lib['subpath']}` at `{lib['sha']}`"
                for lib in state.options.get("libs_resolved") or []
            ]
    commits += [
        "",
        searched_line(state, out_dir),
        "",
        *scope,
        f"- Depth: `{state.options.get('depth', 'standard')}`",
        f"- Mode: `{state.options.get('mode', 'human')}`",
        *(
            [
                f"- Open body of the HTML page: {state.page['words']:,} words "
                f"(limit {OPEN_WORDS_MAX:,}), written by one model call"
            ]
            if state.page
            else []
        ),
        "",
        "Every citation in this file was checked against the commit listed here. "
        "Re-check with `python -m walkthrough_pipeline check <this file>`.",
    ]
    cut = cut_lines(state)
    cut_text = (
        (
            [
                "These failed the write checks, after one rewrite, and are not part of the walkthrough above.",
                "",
            ]
            + cut
        )
        if cut
        else ["Nothing was cut."]
    )
    return MdSection(
        "",
        [
            ("Commits read", _join(commits)),
            ("Coverage", _join(coverage_lines(state, 4))),
            ("Cut by the checks", _join(cut_text)),
        ],
    )


def human_md_sections(
    state: WalkState, lk: Linker, out_dir: Path
) -> dict[str, MdSection]:
    return {
        "summary": _md_summary(state, lk),
        **(
            {
                "affected": _md_affected(state, lk),
                "untouched": _md_untouched(state, lk),
            }
            if state.target.change
            else {}
        ),
        "usecases": _md_usecases(state, lk),
        "flow": _md_flow(state, lk),
        "core": _md_core(state, lk),
        "outputs": _md_outputs(state, lk),
        "config": _md_config(state, lk),
        **{key: _md_table(state, lk, key) for key in TABLE_COLUMNS},
        "gotchas": _md_gotchas(state, lk),
        "reference": _md_reference(state, lk),
        "made": _md_made(state, out_dir),
    }


def run_line(state: WalkState) -> str:
    t = state.target
    spent, calls = state.budget.get("cost_usd", 0.0), state.budget.get("calls", 0)
    at = (
        f"`{t.change.base_sha[:12]}..{t.head_sha[:12]}`"
        if t.change
        else f"`{t.head_sha[:12]}`"
    )
    return (
        f"Generated {date.today().isoformat()} · {t.repo_key} at {at} · {len(t.files)} files · "
        f"depth {state.options.get('depth', 'standard')} · spent ${spent:.2f} over {calls} agent calls"
    )


def human_markdown(state: WalkState, lk: Linker, out_dir: Path) -> str:
    t = state.target
    kind = "change walkthrough" if t.change else "codebase walkthrough"
    md = [f"# {t.package} — {kind}", ""]
    tagline = _data(state, "summary").get("tagline")
    if tagline:
        md += [lk.expand(tagline), ""]
    md += [f"> {run_line(state)}", ""]
    sections = human_md_sections(state, lk, out_dir)
    for key, title in human_sections(state.options, state.core):
        sec = sections[key]
        md += [f"## {title}", ""]
        if sec.body:
            md += [sec.body, ""]
        for sub, text in sec.subs:
            md += [f"### {sub}", "", text, ""]
    return "\n".join(md).rstrip() + "\n"


# --- sections -------------------------------------------------------------------------------

MERMAID = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)
FENCE = re.compile(r"^(`{3,}|~{3,})")


def section_texts(md: str, title: str) -> list[str]:
    """The text under each H2 or H3 titled *title*, up to the next heading of the same or a higher level.
    Headings inside fenced code blocks do not count. Human mode puts Paths and Coverage under H2s as H3s."""
    out: list[str] = []
    level, body, fence = 0, [], ""
    for line in md.split("\n") + ["## "]:
        m = FENCE.match(line)
        h = None if fence or m else re.match(r"^(#{2,3}) (.*)$", line)
        if fence:
            if (
                m
                and m.group(1)[0] == fence[0]
                and len(m.group(1)) >= len(fence)
                and not line[len(m.group(1)) :].strip()
            ):
                fence = ""
        elif m:
            fence = m.group(1)
        if h and level and len(h.group(1)) <= level:
            out.append("\n".join(body))
            level, body = 0, []
        if h and h.group(2).strip() == title:
            level, body = len(h.group(1)), []
            continue
        if level:
            body.append(line)
    return out


# --- HTML -------------------------------------------------------------------------------


def markdown_headings(md: str, level: int = 2) -> list[str]:
    return [
        m.group(1).strip()
        for m in re.finditer(rf"^{'#' * level} (.+)$", md, re.MULTILINE)
    ]


# Characters Python-Markdown's inline patterns act on before it stashes a raw HTML tag, plus the
# table's cell separator: encoded as character references in the attributes `_raw_link` writes.
# The link text sits between tags, where emphasis applies too, so `*` and `_` are encoded there as well.
_ATTR_ENCODE = frozenset("&<>\"'`\\[]|")
_TEXT_ENCODE = _ATTR_ENCODE | {"*", "_"}


def _encode(text: str, chars: frozenset[str] = _ATTR_ENCODE) -> str:
    return "".join(f"&#{ord(ch)};" if ch in chars else ch for ch in text)


class CiteNumbers:
    """Citation numbers within one section of the page: the same link gets the same number."""

    def __init__(self):
        self.seen: dict[tuple[str, str], int] = {}

    def number(self, href: str, title: str) -> int:
        return self.seen.setdefault((href, title), len(self.seen) + 1)


def _cite_html(m: re.Match, cites: CiteNumbers) -> str:
    """One citation link as a numbered superscript whose tooltip holds `path:line` and the quote.
    Written as HTML before the Markdown conversion: Python-Markdown misreads a link title holding an
    escaped quote and drops the link."""
    where = f"{m.group(1)}:{m.group(2)}" + (f"-{m.group(3)}" if m.group(3) else "")
    quote = re.sub(r"\\(.)", r"\1", m.group(5))
    title = f"{where} — {quote}" if quote else where
    n = cites.number(m.group(4), title)
    return f'<sup><a class="cite" href="{_encode(m.group(4))}" title="{_encode(title)}">{n}</a></sup>'


def _raw_links(md: str, cites: CiteNumbers) -> str:
    """`_cite_html` on every citation link outside fenced code blocks; the Markdown file itself is unchanged."""
    out: list[str] = []
    fence = ""
    for line in md.split("\n"):
        m = FENCE.match(line)
        if fence:
            if (
                m
                and m.group(1)[0] == fence[0]
                and len(m.group(1)) >= len(fence)
                and not line[len(m.group(1)) :].strip()
            ):
                fence = ""
        elif m:
            fence = m.group(1)
        else:
            line = HOP_REF_MD.sub(
                _hop_html, LINK.sub(lambda x: _cite_html(x, cites), line)
            )
        out.append(line)
    return "\n".join(out)


# A citation link as `_cite_html` writes it; a superscript of one or more (`group_cites`); a hop pointer.
CITE_A = r'<a class="cite" href="[^"]*" title="[^"]*">\d+</a>'
CITE_LINK = re.compile(r'<a class="cite" href="([^"]*)" title="([^"]*)">\d+</a>')
CITE_SUP = rf"<sup>{CITE_A}(?:, {CITE_A})*</sup>"
CITE_RUN = re.compile(rf"{CITE_SUP}(?:\s*{CITE_SUP})+")
HOP_SUP = r'<sup><a class="hop" [^>]*>&#8618;</a></sup>'
CITE_UNIT = re.compile(rf"(\s?)({CITE_SUP})({HOP_SUP})?")


def group_cites(body: str) -> str:
    """Adjacent citation superscripts become one, their numbers separated by ", ": "1, 2, 3"."""
    return CITE_RUN.sub(
        lambda m: "<sup>" + ", ".join(re.findall(CITE_A, m.group(0))) + "</sup>", body
    )


def first_cites_only(body: str) -> str:
    """Only the first citation under each `<h2>` and `<h3>` of *body* is shown, as one link; the hop pointer
    right after a dropped citation goes with it. The kept links are numbered again from 1 in the order they
    appear. Every citation was checked before this; only what the page shows changes."""

    def prune(chunk: str) -> str:
        kept = False

        def one(m: re.Match) -> str:
            nonlocal kept
            if kept:
                return ""
            kept = True
            link = re.search(CITE_A, m.group(2)).group(0)
            return f"{m.group(1)}<sup>{link}</sup>{m.group(3) or ''}"

        return CITE_UNIT.sub(one, chunk)

    return renumber_cites("".join(prune(c) for c in re.split(r"(?=<h[23][\s>])", body)))


def renumber_cites(body: str) -> str:
    """The citation links of *body* numbered from 1 in the order they appear; the same link keeps its number."""
    numbers: dict[tuple[str, str], int] = {}

    def renumber(m: re.Match) -> str:
        n = numbers.setdefault((m.group(1), m.group(2)), len(numbers) + 1)
        return f'<a class="cite" href="{m.group(1)}" title="{m.group(2)}">{n}</a>'

    return CITE_LINK.sub(renumber, body)


# The pointer `Linker.expand` puts after a hop's citation, ` ([p2h3](#p2h3))`; the page shows it as an arrow.
HOP_REF_MD = re.compile(r" ?\(\[(p(\d+)h(\d+))\]\(#\1\)\)")


def _hop_html(m: re.Match) -> str:
    return (
        f'<sup><a class="hop" href="#{m.group(1)}" title="Step {m.group(3)} of path p{m.group(2)}, '
        f'under Reference, Paths">&#8618;</a></sup>'
    )


# The elements Python-Markdown writes with the extensions `markdown_html` uses, plus the citation's `sup`.
# Any other tag in its output came through as raw HTML from the text, most often a placeholder such as
# `changes/<name>/` written outside backticks, which a browser would swallow; it is shown as text instead.
_HTML_TAGS = frozenset(
    {
        "a",
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "code",
        "pre",
        "em",
        "strong",
        "ul",
        "ol",
        "li",
        "blockquote",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "hr",
        "br",
        "img",
        "sup",
    }
)
_TAG = re.compile(r"</?([A-Za-z][^\s/<>]*)(?:\s[^<>]*)?/?>")


def _escape_raw_tags(body: str) -> str:
    return _TAG.sub(
        lambda m: (
            m.group(0)
            if m.group(1) in _HTML_TAGS
            else html.escape(m.group(0), quote=False)
        ),
        body,
    )


def markdown_html(md: str, cites: CiteNumbers | None = None) -> str:
    """A Markdown fragment as HTML: citation links become numbered superscripts, Mermaid a diagram box,
    fenced code keeps `language-<lang>` for highlight.js, and tables scroll in their own box."""
    import markdown as mdlib

    blocks: list[str] = []

    def keep_mermaid(m: re.Match) -> str:
        blocks.append(m.group(1))
        return f"\n\nMERMAIDBLOCK{len(blocks) - 1}\n\n"

    text = _raw_links(MERMAID.sub(keep_mermaid, md), cites or CiteNumbers())
    body = _escape_raw_tags(
        mdlib.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    )
    for i, b in enumerate(blocks):
        body = body.replace(
            f"<p>MERMAIDBLOCK{i}</p>",
            f'<div class="diagram"><pre class="mermaid">{html.escape(b)}</pre></div>',
        )
    return body.replace("<table>", '<div class="tbl"><table>').replace(
        "</table>", "</table></div>"
    )


def _inline(md: str, cites: CiteNumbers) -> str:
    """`markdown_html` for one line of text, without the paragraph around it."""
    out = markdown_html(md, cites).strip()
    if out.startswith("<p>") and out.endswith("</p>") and out.count("<p>") == 1:
        out = out[3:-4]
    return out


# A `path:line` (or `path:start-end`) in the page's visible text: a file name with an extension, then a line.
LOCATION = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w-]+(?:\.[\w-]+)+):(\d+)(?:-(\d+))?(?![\w])"
)
_HIDDEN = ("pre", "script", "style")


def hide_locations(body: str) -> str:
    """Every `path:line` left in the page's visible text, outside code blocks, becomes the file's name with
    the full location in its tooltip. Citations are already superscripts; this catches the rest (left-out
    entries, uncited boundary entries, check reasons)."""
    parts = re.split(r"(<[^>]*>)", body)
    depth = 0
    for i, part in enumerate(parts):
        if part.startswith("<"):
            m = re.match(r"<(/?)([A-Za-z]+)", part)
            if m and m.group(2).lower() in _HIDDEN:
                depth += -1 if m.group(1) else 1
            continue
        if depth == 0 and part:
            parts[i] = LOCATION.sub(
                lambda x: (
                    f'<span class="loc" title="{x.group(0)}">'
                    f"{PurePosixPath(x.group(1)).name}</span>"
                ),
                part,
            )
    return "".join(parts)


def _code_block(text: str, lang: str) -> str:
    cls = f' class="language-{lang}"' if lang else ""
    return f"<pre><code{cls}>{html.escape(text.strip(chr(10)))}</code></pre>"


def _html_cites(lk: Linker, ids: list[str], cites: CiteNumbers) -> str:
    return _inline(_cites(lk, ids), cites) if ids else ""


def _html_usecases(state: WalkState, lk: Linker, cites: CiteNumbers) -> str:
    cases = _data(state, "usecases").get("cases") or []
    if not cases:
        return "<p>No use case passed the checks.</p>"
    out = ['<div class="cases">']
    for c in cases:
        ex = c["example"]
        meta = " · ".join(
            x
            for x in (
                f"<b>Who</b> {_inline(lk.expand(c['who']), cites)}"
                if c.get("who")
                else "",
                f"<b>When</b> {_inline(lk.expand(c['when']), cites)}"
                if c.get("when")
                else "",
            )
            if x
        )
        out.append('<article class="case">')
        out.append(f"<h3>{_inline(c['title'], cites)}</h3>")
        if meta:
            out.append(f'<p class="meta">{meta}</p>')
        out.append(_code_block(ex["text"], EXAMPLE_LANG.get(ex["kind"], "")))
        if c.get("what_happens"):
            out.append(f"<p>{_inline(lk.expand(c['what_happens']), cites)}</p>")
        if c.get("result"):
            out.append(
                f"<p><b>What comes out</b> {_inline(lk.expand(c['result']), cites)}</p>"
            )
        out.append(
            f'<p class="based">Based on {_html_cites(lk, c.get("cites") or [], cites)}</p>'
        )
        out.append("</article>")
    out.append("</div>")
    return "\n".join(out)


# --- the flow picture: inline HTML and SVG drawn from the gated boxes, no Mermaid and no script ----------

# What the picture calls each box kind, and its icon (a 16x16 SVG path drawn with the kind's colour).
FLOW_KIND_NAMES = {
    "input": "Input",
    "code": "Code",
    "agent": "Model agent",
    "check": "Code check",
    "decision": "Decision",
    "output": "Output",
}
FLOW_ICONS = {
    "input": "M8 2v8M5 7l3 3 3-3M3 13h10",
    "output": "M8 10V2M5 5l3-3 3 3M3 13h10",
    "code": "M6 4L2 8l4 4M10 4l4 4-4 4",
    "agent": "M8 2l1.6 4.4L14 8l-4.4 1.6L8 14l-1.6-4.4L2 8l4.4-1.6z",
    "check": "M3 8.5l3.2 3.2L13 4.8",
    "decision": "M8 2l6 6-6 6-6-6z",
}


def _icon(kind: str) -> str:
    return (
        '<svg class="fp-icon" viewBox="0 0 16 16" aria-hidden="true">'
        f'<path d="{FLOW_ICONS[kind]}"/></svg>'
    )


def _reads_writes(n: dict) -> str:
    parts = []
    for field, head in (("reads", "Reads"), ("writes", "Writes")):
        if n.get(field):
            parts.append(f"{head}: " + ", ".join(x.replace("`", "") for x in n[field]))
    return ". ".join(parts)


def _picture_box(n: dict, back: list[str], names: dict[str, str]) -> str:
    kind = n.get("kind") or "code"
    title = _reads_writes(n)
    stack = " fp-stack" if n.get("fan_out") else ""
    out = [
        f'<div class="fp-box k-{kind}{stack}" data-kind="{kind}"'
        + (f' title="{html.escape(title)}"' if title else "")
        + ">",
        f'<span class="fp-kind">{_icon(kind)}{html.escape(names[kind])}</span>',
        f'<strong class="fp-label">{html.escape(n["label"])}</strong>',
    ]
    if n.get("does"):
        out.append(f'<span class="fp-does">{html.escape(n["does"])}</span>')
    if n.get("fan_out"):
        out.append(f'<span class="fp-fan">&#215; {html.escape(n["fan_out"])}</span>')
    for label in back:
        out.append(f'<span class="fp-back">&#8630; back to {html.escape(label)}</span>')
    out.append("</div>")
    return "".join(out)


def flow_picture(
    data: dict, *, caption: str = "How it works", names: dict[str, str] | None = None
) -> str:
    """The flow as a picture: the inputs together at the start, the stages in box order, the outputs together
    at the end. Each box shows its kind (an icon and a colour from the design-system tokens), its label and
    what it does; a box with a fan-out is drawn as stacked cards; an arrow back to an earlier box is a note
    on the box, not a line. The layout is left to right on a wide screen and top to bottom on a narrow one
    (CSS in `templates/walkthrough.html`). *names* replaces what the picture calls a kind. "" when the
    diagram was cut."""
    nodes = data.get("nodes") or []
    if not nodes:
        return ""
    names = {**FLOW_KIND_NAMES, **(names or {})}
    ins = [n for n in nodes if n.get("kind") == "input"]
    outs = [n for n in nodes if n.get("kind") == "output"]
    middle = [n for n in nodes if n.get("kind") not in ("input", "output")]
    drawn = ins + middle + outs
    position = {n["id"]: i for i, n in enumerate(drawn)}
    labels = {n["id"]: n["label"] for n in nodes}
    back: dict[str, list[str]] = {}
    for e in data.get("edges") or []:
        a, b = e["from"], e["to"]
        if a in position and b in position and position[b] < position[a]:
            back.setdefault(a, []).append(labels[b])
    cells = []
    for group, cls in (
        (ins, "fp-in"),
        *(([n], "fp-stage") for n in middle),
        (outs, "fp-out"),
    ):
        if group:
            cells.append(
                f'<li class="{cls}">'
                + "".join(_picture_box(n, back.get(n["id"], []), names) for n in group)
                + "</li>"
            )
    kinds = list(dict.fromkeys(n.get("kind") or "code" for n in drawn))
    legend = "".join(
        f'<span class="fp-key k-{k}">{_icon(k)}{html.escape(names[k])}</span>'
        for k in kinds
    )
    return (
        f'<figure class="flowpic" aria-label="{html.escape(caption)}">'
        f'<ol class="fp">{"".join(cells)}</ol>'
        f'<figcaption class="fp-legend">{legend}</figcaption></figure>'
    )


def _flow_where(
    data: dict, lk: Linker, cites: CiteNumbers, *, with_cites: bool = True
) -> str:
    """The fold-out under the picture: what each box reads and writes and, *with_cites*, where it is in the
    code. "" when there is nothing to list."""
    items = []
    for n in data.get("nodes") or []:
        rw = _reads_writes(n)
        if not with_cites and not rw:
            continue
        items.append(
            f"<li>{html.escape(n['label'])}"
            + (f" — {html.escape(rw)}" if rw else "")
            + (f" {_html_cites(lk, [n['cite']], cites)}" if with_cites else "")
            + "</li>"
        )
    if not items:
        return ""
    what = "What each stage reads and writes" + (
        ", and where it is in the code" if with_cites else ""
    )
    return f'<details class="more"><summary>{what}</summary><ul>{"".join(items)}</ul></details>'


def _html_summary(state: WalkState, lk: Linker, cites: CiteNumbers) -> str:
    """What it is: its paragraphs, then the flow picture and what each stage reads and writes."""
    paras = _data(state, "summary").get("paragraphs") or []
    out = [markdown_html(lk.expand(p), cites) for p in paras]
    data = _data(state, "flow")
    picture = flow_picture(data) if _picture_in_summary(state) else ""
    if picture:
        out += [picture, _flow_where(data, lk, cites)]
    return "\n".join(out) or "<p>Nothing about this section passed the checks.</p>"


def _html_flow(state: WalkState, lk: Linker, cites: CiteNumbers) -> str:
    data = _data(state, "flow")
    out = []
    src = flow_picture(data)
    if src and not _picture_in_summary(state):
        out += [src, _flow_where(data, lk, cites)]
    steps = data.get("steps") or []
    if not out and not steps:
        return "<p>Nothing about this section passed the checks.</p>"
    if steps:
        out.append('<ol class="steps">')
        for i, st in enumerate(steps, start=1):
            out.append(f'<li id="flow-step-{i}"><h3>{_inline(st["title"], cites)}</h3>')
            out.append(f"<p>{_inline(lk.expand(st['summary']), cites)}</p>")
            more = "".join(
                markdown_html(lk.expand(p), cites) for p in st.get("details") or []
            )
            more += "".join(
                markdown_html(lk.snippet(lk.citations[cid]), cites)
                for cid in st.get("snippets") or []
                if cid in lk.citations
            )
            if more:
                out.append(
                    f'<details class="more"><summary>Details</summary>{more}</details>'
                )
            out.append("</li>")
        out.append("</ol>")
    return "\n".join(out)


def _html_core(state: WalkState, lk: Linker, cites: CiteNumbers) -> str:
    data = _data(state, "core")
    out = []
    if data.get("summary"):
        out.append(f"<p>{_inline(lk.expand(data['summary']), cites)}</p>")
    out += [markdown_html(lk.expand(p), cites) for p in data.get("paragraphs") or []]
    flow = data.get("flow") or {}
    picture = flow_picture(flow, caption=state.core.get("title") or "")
    if picture:
        out.append(picture)
    for part in data.get("parts") or []:
        rows = "".join(
            f"<dt>{head}</dt><dd>{_inline(lk.expand(part[field]), cites)}</dd>"
            for field, head in CORE_PART_FIELDS
            if part.get(field)
        )
        out.append(
            f'<div class="item"><h3>{html.escape(part["name"])}</h3><dl class="part">{rows}</dl>'
            f'<p class="based">Where {_html_cites(lk, [part["cite"]], cites)}</p></div>'
        )
    steps = flow.get("steps") or []
    if steps:
        out.append(
            '<ol class="steps">'
            + "".join(
                f"<li><h3>{html.escape(st['title'])}</h3><p>{_inline(lk.expand(st['summary']), cites)}</p></li>"
                for st in steps
            )
            + "</ol>"
        )
    return "\n".join(out) or "<p>Nothing about this section passed the checks.</p>"


def _html_outputs(state: WalkState, lk: Linker, cites: CiteNumbers) -> str:
    items = _data(state, "outputs").get("items") or []
    if not items:
        return "<p>No output passed the checks.</p>"
    out = []
    for it in items:
        out.append('<div class="item">')
        out.append(f"<h3>{_inline(it['name'], cites)}</h3>")
        out.append(
            f"<p>{_inline(lk.expand(it['what']), cites)} "
            f'<span class="based">Written at {_html_cites(lk, [it["cite"]], cites)}</span></p>'
        )
        ex = it.get("example")
        if ex:
            out.append(
                f'<details class="more"><summary>Example payload ({html.escape(ex["format"])})</summary>'
                f"{_code_block(ex['text'], EXAMPLE_LANG.get(ex['format'], ''))}</details>"
            )
        out.append("</div>")
    return "\n".join(out)


def _html_config(state: WalkState, lk: Linker, cites: CiteNumbers) -> str:
    rows = _data(state, "config").get("rows") or []
    if not rows:
        return "<p>No setting passed the checks.</p>"
    out = [
        '<div class="tbl"><table><thead><tr><th>Setting</th><th>What it changes</th><th>Default</th>'
        "<th>Where</th></tr></thead><tbody>"
    ]
    for r in rows:
        default = (
            f"<code>{html.escape(r['default'])}</code>" if r.get("default") else ""
        )
        out.append(
            f"<tr><td><code>{html.escape(r['name'])}</code></td><td>{_inline(lk.expand(r['effect']), cites)}"
            f"</td><td>{default}</td><td>{_html_cites(lk, [r['cite']], cites)}</td></tr>"
        )
    out.append("</tbody></table></div>")
    return "\n".join(out)


def _html_table(key: str):
    """The HTML builder of one `--scope codebase` table."""

    def build(state: WalkState, lk: Linker, cites: CiteNumbers) -> str:
        rows = _data(state, key).get("rows") or []
        if not rows:
            return f"<p>{EMPTY_TABLE}</p>"
        cols = TABLE_COLUMNS[key]
        out = [
            '<div class="tbl"><table><thead><tr>'
            + "".join(f"<th>{html.escape(head)}</th>" for _, head, _ in cols)
            + "</tr></thead><tbody>"
        ]
        for r in rows:
            cells = []
            for field, _, how in cols:
                value = r.get(field) or ""
                if how == "cite":
                    cells.append(_html_cites(lk, [value], cites))
                elif how == "code":
                    cells.append(
                        f"<code>{html.escape(value.replace('`', ''))}</code>"
                        if value
                        else ""
                    )
                else:
                    cells.append(_inline(lk.expand(value), cites))
            out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        out.append("</tbody></table></div>")
        return "\n".join(out)

    return build


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _html_subs(key: str, sec: MdSection, swap: dict[str, str] | None = None) -> str:
    """Each H3 of Reference or How this was made as a closed `<details>`, numbered on its own. Each key of
    *swap* in the HTML is replaced by its value."""
    out = [markdown_html(sec.body)] if sec.body else []
    for title, text in sec.subs:
        out.append(
            f'<details class="sub" id="{key}-{_slug(title)}"><summary><h3>{html.escape(title)}</h3></summary>'
            f"{markdown_html(text, CiteNumbers())}</details>"
        )
    page = "\n".join(out)
    for old, new in (swap or {}).items():
        page = page.replace(old, new)
    return page


def render_html(
    state: WalkState, out_dir: Path, styles: tuple[str, str] | None = None
) -> str:
    """The explainer as one scrolling page with a contents rail. The structured sections are drawn from
    their gated answers; the others from the same Markdown the .md file holds. *styles* is `(fonts, tokens)`
    CSS from `design.css`; None is the neutral design system's."""
    lk = Linker(state, out_dir)
    t = state.target
    sections = human_md_sections(state, lk, out_dir)
    sections["reference"] = _md_reference(state, lk, boundary=BOUNDARY_SLOT)
    swap = {
        f"<p>{BOUNDARY_SLOT}</p>": flow_picture(
            boundary_flow(state),
            caption="Who calls it and what it calls",
            names={"input": "Calls it", "code": "This code", "output": "It calls"},
        )
    }
    builders = {
        "summary": _html_summary,
        "usecases": _html_usecases,
        "flow": _html_flow,
        "core": _html_core,
        "outputs": _html_outputs,
        "config": _html_config,
        **{key: _html_table(key) for key in TABLE_COLUMNS},
    }
    written = (state.page or {}).get("sections") or {}
    # The detail sections are closed fold-outs at the bottom; a change's page keeps its own layout.
    appendix = set(APPENDIX_KEYS) if not t.change else set()
    body, nav = [], []
    for key, title in human_sections(state.options, state.core):
        cites = CiteNumbers()
        heading = f"<h2>{html.escape(title)}</h2>"
        if key in written:
            inner = page_section_html(state, lk, title, written[key], cites)
        elif key in builders:
            inner = builders[key](state, lk, cites)
        elif key in ("reference", "made", "untouched"):
            inner = _html_subs(key, sections[key], swap)
        else:
            inner = markdown_html(sections[key].body, cites)
        nav.append(f'<li><a href="#{key}">{html.escape(title)}</a></li>')
        if key in appendix:
            body.append(
                f'<section id="{key}"><details class="appendix"><summary>{heading}</summary>\n'
                f"{inner}\n</details></section>"
            )
        else:
            if key in written:
                inner = f'<div class="page-body">\n{inner}\n</div>'
            body.append(f'<section id="{key}">\n{heading}\n{inner}\n</section>')
    tagline = _data(state, "summary").get("tagline")
    header = (
        f'<header class="top"><div class="eyebrow">{"Change" if t.change else "Codebase"} walkthrough</div>'
        f"<h1>{html.escape(t.package)}</h1>"
        + (
            f'<p class="lede">{_inline(lk.expand(tagline), CiteNumbers())}</p>'
            if tagline
            else ""
        )
        + f'<p class="run">{_inline(run_line(state), CiteNumbers())}</p></header>'
    )
    fonts, tokens = styles if styles is not None else design.css(None)
    page_css = (state.page or {}).get("css", "").strip()
    values = {
        "FONTS": fonts,
        "TOKENS": tokens,
        # The page writer's CSS, nested under its sections' class so it styles nothing else.
        "PAGE_CSS": f".page-body {{\n{page_css}\n}}" if page_css and written else "",
        "TITLE": html.escape(f"{t.package} walkthrough"),
        "HEADER": hide_locations(group_cites(header)),
        "NAV": "\n".join(nav),
        "BODY": hide_locations(group_cites("\n".join(body))),
    }
    # One pass over the template: text a value holds is never taken for a later placeholder.
    return PLACEHOLDER.sub(
        lambda m: values[m.group(1)], TEMPLATE.read_text(encoding="utf-8")
    )


PLACEHOLDER = re.compile(r"\{\{(FONTS|TOKENS|PAGE_CSS|TITLE|HEADER|NAV|BODY)\}\}")
SNIPPET_TOKEN = re.compile(r"(?:<p>\s*)?\[\[snippet:([a-z0-9]+)\]\](?:\s*</p>)?")
FLOW_SLOT = re.compile(r"(?:<p>\s*)?\{\{flow(:core)?\}\}(?:\s*</p>)?")


def page_section_html(
    state: WalkState, lk: Linker, title: str, text: str, cites: CiteNumbers
) -> str:
    """One open section the page writer wrote, ready for the page: each `[[snippet:cN]]` becomes the cited
    lines, each `[[cN]]` a numbered link, adjacent links one group, only the first citation under each
    heading is shown, and `{{flow}}` / `{{flow:core}}` become the flow pictures. `page_gate` has passed it."""
    snippets: list[str] = []

    def keep(m: re.Match) -> str:
        c = lk.citations.get(m.group(1))
        snippets.append(markdown_html(lk.snippet(c), cites) if c else "")
        return f"\x00{len(snippets) - 1}\x00"

    # Snippets are set aside first: the code they show may hold text that looks like a citation.
    text = SNIPPET_TOKEN.sub(keep, text)
    text = group_cites(_raw_links(lk.expand(text), cites))
    heading = f"<h2>{html.escape(title)}</h2>"
    text = first_cites_only(heading + text)[len(heading) :]
    text = renumber_cites(
        re.sub("\x00(\\d+)\x00", lambda m: snippets[int(m.group(1))], text)
    )

    def flow(m: re.Match) -> str:
        if m.group(1):
            data = _data(state, "core").get("flow") or {}
            return flow_picture(data, caption=state.core.get("title") or "")
        data = _data(state, "flow")
        picture = flow_picture(data)
        return (
            picture + _flow_where(data, lk, cites, with_cites=False) if picture else ""
        )

    return FLOW_SLOT.sub(flow, text)


def paths_file_for(md: Path) -> Path:
    """`<name>-paths.json` beside `<name>-walkthrough.md`."""
    return md.with_name(md.name.removesuffix("-walkthrough.md") + "-paths.json")


def write_paths_output(state: WalkState, md: Path) -> Path | None:
    """Write the trace stage's result beside the walkthrough with code_facts' writer; None when there is none.

    A failed write is recorded in `state.errors` and leaves no file: the walkthrough is already written.
    """
    target = paths_file_for(md)
    if state.paths is None:
        target.unlink(
            missing_ok=True
        )  # a stale file from an earlier run would be taken for this one's
        return None
    try:
        return code_facts.write_paths_file(state.paths, target)
    except (OSError, ValueError) as e:
        state.errors.append(f"paths file {target}: {type(e).__name__}: {e}")
        target.unlink(missing_ok=True)
        return None


def unreadable_commit(state: WalkState) -> str | None:
    """`<repo_key> at <sha>: <git error>` for the first repo the outputs read that cannot show its commit, else None.

    The outputs read the target's repo and the repo of every citation; checked before any file is written.
    """
    keys = {state.target.repo_key} | {
        c.path.partition("/")[0] for c in citations_for_write(state).values()
    }
    if state.target.change:
        keys.add(state.target.change.base_key)
    for key in sorted(keys):
        repo = state.repos.get(key)
        if repo is None:
            continue
        proc = git(repo.path, "show", "-s", "--format=", repo.head_sha, check=False)
        if proc.returncode != 0:
            return f"{key} at {repo.head_sha}: {proc.stderr.strip() or f'git exited {proc.returncode}'}"
    return None


def write_outputs(
    state: WalkState, out_dir: Path, design_dir: str | Path | None = None
) -> list[Path]:
    """Write the run's files to *out_dir*. The page is styled with *design_dir*, else the run's saved
    `--design-system-dir`, else the neutral design system; a folder that fails its check raises
    `design.DesignSystemError` before any file is written."""
    styles = (
        design.css(design_dir or state.options.get("design_system_dir"))
        if state.options.get("mode") != "agent"
        else None
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    name = state.target.name
    md = render_markdown(state, out_dir)
    gate = sections_gate(
        md,
        required_titles(
            state.options.get("mode", "human"),
            state.options.get("scope", "module"),
            state.core.get("title", ""),
        ),
    )
    if not gate.passed:
        state.errors.append(gate.reason)
    md_path = out_dir / f"{name}-walkthrough.md"
    md_path.write_text(md, encoding="utf-8")
    paths = [md_path]
    if state.options.get("mode") != "agent":
        html_path = out_dir / f"{name}-walkthrough.html"
        html_path.write_text(render_html(state, out_dir, styles), encoding="utf-8")
        paths.append(html_path)
        written = write_paths_output(state, md_path)
        if written is not None:
            paths.append(written)
    return paths
