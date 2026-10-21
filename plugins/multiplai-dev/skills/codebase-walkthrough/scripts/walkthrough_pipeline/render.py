"""Assemble the walkthrough from gated sections. Markdown first; the HTML page and walkthrough.json hold the same text.

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
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote as url_quote

import code_facts
from code_facts.paths import PathHop, PathsResult, TracedPath

from .check import LINK
from .gates import CITE_ID, file_at_commit, lines_at_commit, sections_gate
from .models import Citation, RepoInfo, WalkState
from .stages.write import citations_for_write, required_titles
from .target import git, github_web_base, tree_files

LANG = {".py": "python", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "jsx", ".sql": "sql",
        ".sqlx": "sql", ".yml": "yaml", ".yaml": "yaml", ".json": "json", ".md": "markdown", ".html": "markup",
        ".sh": "bash", ".toml": "toml"}
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
        anchor = f"#L{c.line_start}" + (f"-L{c.line_end}" if c.line_end != c.line_start else "")
        if base:
            return f"{base}/blob/{repo.head_sha}/{rel}{anchor}"
        # Percent-encode the local path: a space or `)` would end the Markdown link early.
        return url_quote(os.path.relpath(Path(repo.path) / rel, self.out_dir)) + anchor

    def link(self, c: Citation) -> str:
        where = f"{c.path}:{c.line_start}" + (f"-{c.line_end}" if c.line_end != c.line_start else "")
        # `|` is escaped too: links sit in table cells, where a bare `|` splits the row.
        title = (" ".join(c.quote.split())[:200].replace("\\", "\\\\").replace('"', '\\"')
                 .replace("|", "\\|"))
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
        return (f"<!-- snippet {c.path}:{c.line_start}-{end} -->\n{self.link(c)}\n\n"
                f"{fence}{lang}\n{body}\n{fence}")

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
            return self.link(c) + (f" ([{c.id}](#{c.id}))" if c.id and HOP_ID.fullmatch(c.id) else "")
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
    return Citation(path=f"{repo_key}/{hop.path}", line_start=hop.line_start, line_end=hop.line_end, quote=hop.quote)


def _line_text(repos: dict[str, RepoInfo], ws: str, line: int) -> str:
    return " ".join(lines_at_commit(repos, ws, line, line) or [])


def _path_block(state: WalkState, lk: Linker, path: TracedPath) -> list[str]:
    key = state.target.repo_key
    pid, entry, hops = path.id, path.entry, path.hops
    out = [f"**{pid}** — `{entry.name}` ({KIND_LABELS.get(entry.kind, entry.kind)})", ""]
    written = state.sections.get(f"path:{pid}")
    if written:
        out += [p for para in written.paragraphs for p in (lk.expand(para), "")]
    loc = entry.location
    first = hops[0]
    if first.path == loc.path and first.line_start <= loc.line <= first.line_end:
        where = f"`{pid}h1`"
    else:
        ws = f"{key}/{loc.path}"
        where = _entry_link(lk, ws, loc.line, _line_text(lk.repos, ws, loc.line))
    out.append(f"- Entry: invoked by `{entry.invoked_by}`, at {where}")
    out.append("- Hops:")
    for k, hop in enumerate(hops, start=1):
        hid = f"{pid}h{k}"
        out.append(f'  - <a id="{hid}"></a>`{hid}` {lk.link(_hop_citation(key, hop))}')
    if path.stops_at is not None:
        out.append(f"- Stops after `{pid}h{path.stops_at}`: `{path.break_reason}`")
    else:
        out.append(f"- Ends at `{pid}h{len(hops)}` (trace complete).")
    counted = ", ".join(f"`{t.node_id}`" for t in path.tests.e2e) or "no e2e test"
    mocked = ", ".join(f"`{t.node_id}` (mocks `{pid}h2`, not counted)" for t in path.tests.mocks_hop_2)
    out.append(f"- E2E tests: {counted}" + (f"; not counted: {mocked}" if mocked else ""))
    return out


# Diagnostics listed in Left out; only seed_not_found counts as an entry left out.
LEFT_OUT_DIAGNOSTICS = ("seed_not_found", "dynamic_name")


def left_out_count(result: PathsResult) -> int:
    """Entries left out: every drop, plus every seed that was not found."""
    return len(result.dropped) + sum(1 for d in result.diagnostics if d.reason == "seed_not_found")


def render_left_out(result: PathsResult, repo_key: str) -> str:
    """The `### Left out` table: drops, then diagnostics, then e2e listing skips. "" when there is no row.

    Entries are plain text, never links: they are not claims, so `check` does not re-read them.
    """
    rows: list[tuple[str, str, str, str]] = []
    for d in result.dropped:
        loc = d.entry.location
        rows.append((f"`{repo_key}/{loc.path}:{loc.line}` (`{d.entry.name}`)", d.stage, d.reason, d.detail))
    for g in result.diagnostics:
        if g.reason in LEFT_OUT_DIAGNOSTICS:
            where = f"{repo_key}/{g.path}" + (f":{g.line}" if g.line is not None else "")
            rows.append((f"`{where}`", "discover", g.reason, g.detail))
    for skip in result.test_listing_skips:
        rows.append((f"`{repo_key}/{skip.file}`", "e2e", "e2e_listing", skip.reason))
    if not rows:
        return ""
    out = ["### Left out", "", "| Entry | Stage | Reason | Detail |", "|---|---|---|---|"]
    out += [f"| {_cell(entry)} | {_cell(stage)} | `{_cell(reason)}` | {_cell(detail)} |"
            for entry, stage, reason, detail in rows]
    return "\n".join(out)


def _entries_left_out(m: int) -> str:
    return ("1 entry was left out" if m == 1 else f"{m} entries were left out") + \
        "; see **Left out** under Coverage."


def render_paths_section(state: WalkState, linker: Linker) -> str:
    """The Paths body: a lead sentence, then one block per path in map_paths order."""
    if state.paths_error:
        return f"Path finding failed: {state.paths_error}"
    paths = state.paths.paths if state.paths else []
    m = left_out_count(state.paths) if state.paths else 0
    if not paths:
        return f"No paths were traced. {_entries_left_out(m)}" if m else \
            "No entry points reaching this module were found."
    n = len(paths)
    lead = "1 path reaches this module." if n == 1 else f"{n} paths reach this module."
    out = [lead + (f" {_entries_left_out(m)}" if m else ""), ""]
    for path in paths:
        out += _path_block(state, linker, path) + [""]
    return "\n".join(out).rstrip()


def coverage_lines(state: WalkState) -> list[str]:
    t = state.target
    cited = {c.path for f in state.facts for c in f.citations}
    files = [t.ws(f) for f in t.files]
    with_fact = [f for f in files if f in cited]
    big = [f for f in files if t.file_lines.get(f.split("/", 1)[1], 0) > 30]
    big_with = [f for f in big if f in cited]
    pct = (100.0 * len(with_fact) / len(files)) if files else 0.0
    pct_big = (100.0 * len(big_with) / len(big)) if big else 0.0
    out = [
        "| Measure | Value |", "|---|---|",
        f"| Files with a surviving fact | {len(with_fact)} of {len(files)} ({pct:.1f}%) |",
        f"| Files over 30 lines with a surviving fact | {len(big_with)} of {len(big)} ({pct_big:.1f}%) |",
        f"| Migrations (counted, not read) | {t.migrations}" + (f"; latest `{PurePosixPath(t.latest_migration).name}`"
                                                              if t.latest_migration else "") + " |",
        f"| Boundary entries not cited by any fact | {len(state.uncovered_entries)} |",
        f"| Facts kept | {len(state.facts)} |",
    ]
    by_stage: dict[str, int] = {}
    for d in state.dropped:
        by_stage[d.stage] = by_stage.get(d.stage, 0) + 1
    for stage, n in sorted(by_stage.items()):
        out.append(f"| Claims dropped by the {stage} checks | {n} |")
    cut = sum(len(s.cut) for s in state.sections.values())
    out.append(f"| Paragraphs cut by the write checks | {cut} |")
    if state.options.get("mode") != "agent":
        paths = state.paths.paths if state.paths else []
        out.append(f"| Paths traced | {len(paths)} |")
        out.append(f"| Paths cut short | {sum(1 for p in paths if p.stops_at is not None)} |")
        out.append(f"| Entries left out | {left_out_count(state.paths) if state.paths else 0} |")
    if state.lsp:
        lsp = state.lsp
        detail = (f"{lsp.get('status')} in {lsp.get('seconds')}s, {lsp.get('answered')} of {lsp.get('symbols')} "
                  "symbols answered" if not str(lsp.get("status", "")).startswith("skipped") else lsp.get("status"))
        out.append(f"| pyright references | {detail} |")
    out.append("")
    left_out = render_left_out(state.paths, t.repo_key) if state.paths else ""
    if left_out:
        out += [left_out, ""]
    missing = [f for f in files if f not in cited]
    if missing:
        out.append(f"Files with no surviving fact ({len(missing)}):")
        out.append("")
        for f in missing:
            n = t.file_lines.get(f.split("/", 1)[1], 0)
            out.append(f"- `{f}` ({n} lines{', at or under 30: not required' if n <= 30 else ''})")
        out.append("")
    if t.skipped:
        out.append("Files not read: " + ", ".join(f"`{t.ws(p)}` ({why})" for p, why in sorted(t.skipped.items())))
        out.append("")
    if state.uncovered_entries:
        out.append(f"Boundary entries no fact explains ({len(state.uncovered_entries)}):")
        out.append("")
        out += [f"- `{e}`" for e in state.uncovered_entries[:200]]
        if len(state.uncovered_entries) > 200:
            out.append(f"- … and {len(state.uncovered_entries) - 200} more (see boundary.json)")
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
    spent = state.budget.get("cost_usd", 0.0)
    calls = state.budget.get("calls", 0)
    md: list[str] = []
    title = f"Module: {t.ws(t.subpath)}" if mode == "agent" else f"{t.package} — codebase walkthrough"
    md += [f"# {title}", "",
           f"> Generated {date.today().isoformat()} · depth {state.options.get('depth', 'standard')} · mode {mode} · "
           f"{len(t.files)} files · spent ${spent:.2f} over {calls} agent calls", "",
           f"- Module directory: `{os.path.relpath(Path(t.repo_path) / t.subpath, out_dir)}`",
           f"- Depth: `{state.options.get('depth', 'standard')}`",
           f"- Mode: `{mode}`", "",
           "Every `path:line` below was checked against the commit listed here. "
           "Re-check with `python -m walkthrough_pipeline check <this file>`.", "",
           "## Commits read", "", "| Repo | Path | Commit |", "|---|---|---|"]
    for key, repo in sorted(state.repos.items(), key=lambda kv: (kv[0] != t.repo_key, kv[0])):
        md.append(f"| {key} | {os.path.relpath(repo.path, out_dir)} | `{repo.head_sha}` |")
    md.append("")

    def section(key: str) -> list[str]:
        s = state.sections.get(key)
        return [lk.expand(p) + "\n" for p in s.paragraphs] if s else []

    if mode == "agent":
        md += [f"## Files ({len(t.files)})", ""]
        for f in t.files:
            md.append(f"- `{f}` — {state.file_purposes.get(t.ws(f), '(no checked purpose)')}")
        md.append("")
        for key, title in (("architecture", "Architecture"), ("patterns", "Patterns & Conventions"),
                           ("interfaces", "Key Interfaces"), ("gotchas", "Gotchas")):
            md += [f"## {title}", ""] + section(key)
        md += ["## Coverage", ""] + coverage_lines(state)
        md += _cut_appendix(state, lk)
        return "\n".join(md).rstrip() + "\n"

    md += ["## Overview", ""] + section("overview")
    md += ["## Glossary", ""]
    gl = state.sections.get("glossary")
    if gl and gl.glossary:
        md += ["| Term | Meaning | Where |", "|---|---|---|"]
        for term in gl.glossary:
            where = lk.link(term.citation) if term.citation else ""
            md.append(f"| {_cell(term.term)} | {_cell(term.meaning)} | {where} |")
    else:
        md.append("No domain terms passed the checks.")
    md.append("")

    md += ["## Paths", "", render_paths_section(state, lk), ""]

    md += ["## Module tour", ""]
    for u in state.units:
        s = state.sections.get(f"tour:{u.id}")
        md += [f"### {s.title if s else u.id}", ""] + section(f"tour:{u.id}")

    md += ["## Who calls it and what it calls", "", _boundary_mermaid(state), ""]
    md += ["### Who calls it", ""] + section("boundary:inbound")
    if state.references:
        md += ["| Repo | Kind | Symbol | Where | Found by |", "|---|---|---|---|---|"]
        for r in sorted(state.references, key=lambda r: (r.repo, r.kind, r.path, r.line)):
            md.append(f"| {r.repo} | {r.kind} | `{_cell(r.symbol)}` | {_entry_link(lk, r.path, r.line, r.quote)} "
                      f"| {r.method} |")
    md += ["", "### What it calls", ""] + section("boundary:outbound")
    out_entries = [e for e in state.symbols if e.direction == "calls" and e.kind in ("http", "import")]
    if out_entries:
        md += ["| Kind | Target | Where |", "|---|---|---|"]
        for e in sorted(out_entries, key=lambda e: (e.kind, e.name, e.path, e.line)):
            md.append(f"| {e.kind} | `{_cell(e.name)}` | {_entry_link(lk, e.path, e.line, e.quote)} |")
    md.append("")

    md += ["## Data stores and config", ""] + section("data")
    models = [e for e in state.symbols if e.kind == "model"]
    if models:
        md += ["| Model | Table | Defined at |", "|---|---|---|"]
        md += [f"| `{e.name}` | `{e.detail.get('table', '')}` | {_entry_link(lk, e.path, e.line, e.quote)} |"
               for e in models]
        md.append("")
    caches = [e for e in state.symbols if e.kind == "cache"]
    if caches:
        md += ["| Cache key | Call | Where |", "|---|---|---|"]
        md += [f"| `{_cell(e.name)}` | {e.detail.get('method', '')} | {_entry_link(lk, e.path, e.line, e.quote)} |"
               for e in caches]
        md.append("")
    defs = {e.name: e for e in state.symbols if e.kind == "setting-definition"}
    reads = [e for e in state.symbols if e.kind == "setting"]
    if reads:
        md += ["| Setting | Defined at | Env vars it reads | First read at |", "|---|---|---|---|"]
        seen = set()
        for e in reads:
            if e.name in seen:
                continue
            seen.add(e.name)
            d = defs.get(e.name)
            md.append(f"| `{e.name}` | {_entry_link(lk, d.path, d.line, d.quote) if d else '(not found in settings)'} "
                      f"| {', '.join(f'`{x}`' for x in d.detail.get('env', '').split(',') if x) if d else ''} "
                      f"| {_entry_link(lk, e.path, e.line, e.quote)} |")
        md.append("")
    envs = [e for e in state.symbols if e.kind == "env"]
    if envs:
        md += ["| Env var read directly | Where |", "|---|---|"]
        md += [f"| `{e.name}` | {_entry_link(lk, e.path, e.line, e.quote)} |" for e in envs]
        md.append("")

    md += ["## Vendor docs", ""] + section("docs")
    md += _docs_tables(state, lk)

    md += ["## Gotchas and questions for the owner", ""] + section("gotchas")
    g = state.sections.get("gotchas")
    if g and g.questions:
        md += ["Questions to ask the owner:", ""] + [f"- {lk.expand(q)}" for q in g.questions] + [""]

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
        out += [f"Docs: {d['docs_url']} — `llms.txt` lists {d.get('llms_pages', 0)} pages; "
                f"{len(d.get('files', {}))} fetched." + (f" Local docs: {', '.join(Path(x).name for x in d.get('local_dirs', []))}."
                                                         if d.get("local_dirs") else ""), ""]
    coll = d.get("collection") or {}
    mapped = coll.get("mapped", {})
    out += ["| API path the code calls | Docs page | Collection request |", "|---|---|---|"]
    for p in paths:
        pages = d.get("mappings", {}).get(p, [])
        page = "<br>".join(pages) if pages else "no doc page found"
        reqs = mapped.get(p, [])
        req = "<br>".join(f"`{r['file']}` ({r['method']})" for r in reqs) if reqs else "not in the collection"
        out.append(f"| `{p}` | {page} | {req} |")
    out.append("")
    if d.get("no_doc_page"):
        out += ["No doc page found for: " + ", ".join(f"`{p}`" for p in d["no_doc_page"]), ""]
    if coll.get("dir"):
        out += [f"Not in the collection `{Path(coll['dir']).name}`: " +
                (", ".join(f"`{p}`" for p in coll.get("missing", [])) or "none"), "",
                "Open a request file in Bruno and run it with your own API key; this pipeline never calls the API.", ""]
    dis = d.get("disagreements") or []
    if dis:
        out += ["Where the code and the docs disagree:", ""]
        for x in dis:
            c = Citation.model_validate(x["citation"])
            out.append(f"- {_cell(x['summary'])} — code {lk.link(c)}; docs {x['url']}: “{_cell(x['doc_quote'][:200])}”")
        out.append("")
    return out


def _cut_appendix(state: WalkState, lk: Linker) -> list[str]:
    cut = [(s.title, c) for s in state.sections.values() for c in s.cut]
    if not cut:
        return []
    out = ["## Cut by the checks", "",
           "These paragraphs failed the write checks twice and are not part of the walkthrough above.", ""]
    for title, c in cut:
        text = CITE_ID.sub("", c["text"]).replace("\n", " ")
        out.append(f"- ({_cell(title)}) {_cell(text[:400])} — **{_cell('; '.join(c['reasons'])[:300])}**")
    out.append("")
    return out


# --- walkthrough.json, for the review-viewer ------------------------------------------------

# git's empty tree (`git hash-object -t tree /dev/null`). A walkthrough.json whose base is this
# opens in the review-viewer as a tree target: every file under the module directory is "changed".
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MERMAID = re.compile(r"```mermaid\n(.*?)```", re.S)
# walkthrough.v1.schema.json: Diagram.source maxLength.
DIAGRAM_MAX_CHARS = 20000
# Sections that go in overview_md, not in a step: the commits table and the coverage table.
OVERVIEW_SECTIONS = ("Commits read", "Coverage")
FENCE = re.compile(r"^(`{3,}|~{3,})")


def split_sections(md: str) -> tuple[str, list[tuple[str, str]]]:
    """(the text before the first H2, [(H2 title, text under it)]), in order.

    A `## ` line inside a fenced code block (a snippet of a Markdown file) is not a heading.
    """
    intro: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    fence = ""
    for line in md.split("\n"):
        m = FENCE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line[len(m.group(1)):].strip():
                fence = ""
        elif m:
            fence = m.group(1)
        elif line.startswith("## "):
            sections.append((line[3:].strip(), []))
            continue
        (sections[-1][1] if sections else intro).append(line)
    return "\n".join(intro), [(title, "\n".join(body)) for title, body in sections]


def _step_id(title: str, used: set[str], n: int) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:36].strip("-") or f"step-{n}"
    sid, k = base, 2
    while sid in used:
        sid, k = f"{base}-{k}", k + 1
    used.add(sid)
    return sid


def _line_count(text: str) -> int:
    """Lines the way git and the viewer number them: split on `\\n`, a final newline ends the last line."""
    lines = text.split("\n")
    return len(lines) - (1 if lines and lines[-1] == "" else 0)


def _anchors(text: str, state: WalkState, in_tree: set[str], counts: dict[str, int | None]) -> list[dict]:
    """The section's links into the module's own repo, under the module directory, as viewer anchors."""
    t = state.target
    out: list[dict] = []
    seen: set[tuple[str, int, int]] = set()
    for m in LINK.finditer(text):
        key, _, rel = m.group(1).partition("/")
        if key != t.repo_key or rel not in in_tree:
            continue
        if rel not in counts:
            body = file_at_commit(state.repos, m.group(1))
            counts[rel] = None if body is None else _line_count(body)
        n = counts[rel]
        start = int(m.group(2))
        if n is None or start > n:
            continue
        end = min(int(m.group(3) or start), n)
        if (rel, start, end) not in seen:
            seen.add((rel, start, end))
            out.append({"path": rel, "side": "head", "line_start": start, "line_end": end})
    return out


def _skip_reason(state: WalkState, rel: str) -> str:
    t = state.target
    if rel in t.skipped:
        return f"not read: {t.skipped[rel]}"
    if "migrations" in PurePosixPath(rel).parts[:-1]:
        return "in a migrations directory: not read (the coverage table counts migrations)"
    if rel in t.files:
        return "read, but no step of this walkthrough cites it"
    return ("not read: not in the snapshot the agents read (secrets, binaries, vendored directories, "
            "symlinks and files over 2 MB are left out)")


def render_walkthrough_json(state: WalkState, md: str) -> dict:
    """The walkthrough as a review-viewer `walkthrough.json` (v1) over the module's tree at its commit.

    Each H2 section becomes a step anchored on its links into the module directory. Links into
    other repos stay in the text. The introduction, the commits and coverage tables, and any
    section with no link into the module directory go in `overview_md`, because a v1 step needs
    at least one anchor. Every file under the module directory that no step anchors is listed in
    `skipped` with the reason, so the viewer counts the walkthrough as covering the whole tree.
    """
    t = state.target
    in_tree = set(tree_files(t.repo_path, t.head_sha, t.subpath))
    counts: dict[str, int | None] = {}
    intro, sections = split_sections(md)
    overview = [intro.strip()]
    steps: list[dict] = []
    used: set[str] = set()
    for title, text in sections:
        anchors = [] if title in OVERVIEW_SECTIONS else _anchors(text, state, in_tree, counts)
        if not anchors:
            overview.append(f"## {title}\n\n{text.strip()}")
            continue
        step = {"id": _step_id(title, used, len(steps) + 1), "title": title, "anchors": anchors}
        m = MERMAID.search(text)
        if m and len(m.group(1)) <= DIAGRAM_MAX_CHARS:
            step["diagram"] = {"kind": "mermaid", "source": m.group(1).rstrip("\n")}
            # Only the gap the diagram leaves is closed: blank lines inside a snippet are code.
            text = text[:m.start()].rstrip("\n") + "\n\n" + text[m.end():].lstrip("\n")
        step["body_md"] = text.strip()
        steps.append(step)
    anchored = {a["path"] for s in steps for a in s["anchors"]}
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_sha": EMPTY_TREE,
        "head_sha": t.head_sha,
        "overview_md": "\n\n".join(p for p in overview if p) + "\n",
        "steps": steps,
        "skipped": [{"path": f, "reason": _skip_reason(state, f)} for f in sorted(in_tree - anchored)],
        "complete": True,
    }


# --- HTML -------------------------------------------------------------------------------


def markdown_headings(md: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^## (.+)$", md, re.M)]


# Characters Python-Markdown's inline patterns act on before it stashes a raw HTML tag, plus the
# table's cell separator: encoded as character references in the attributes `_raw_link` writes.
# The link text sits between tags, where emphasis applies too, so `*` and `_` are encoded there as well.
_ATTR_ENCODE = frozenset("&<>\"'`\\[]|")
_TEXT_ENCODE = _ATTR_ENCODE | {"*", "_"}


def _encode(text: str, chars: frozenset[str] = _ATTR_ENCODE) -> str:
    return "".join(f"&#{ord(ch)};" if ch in chars else ch for ch in text)


def _raw_link(m: re.Match) -> str:
    """One citation link as a raw `<a>`. Python-Markdown misreads a link title holding an escaped quote
    and drops the link, so every citation link is written as HTML before the conversion."""
    where = f"{m.group(1)}:{m.group(2)}" + (f"-{m.group(3)}" if m.group(3) else "")
    title = re.sub(r"\\(.)", r"\1", m.group(5))
    return f'<a href="{_encode(m.group(4))}" title="{_encode(title)}"><code>{_encode(where, _TEXT_ENCODE)}</code></a>'


def _raw_links(md: str) -> str:
    """`_raw_link` on every citation link outside fenced code blocks; the Markdown file itself is unchanged."""
    out: list[str] = []
    fence = ""
    for line in md.split("\n"):
        m = FENCE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line[len(m.group(1)):].strip():
                fence = ""
        elif m:
            fence = m.group(1)
        else:
            line = LINK.sub(_raw_link, line)
        out.append(line)
    return "\n".join(out)


# The elements Python-Markdown writes with the extensions `render_html` uses. Any other tag in its
# output came through as raw HTML from the text, most often a placeholder such as `changes/<name>/`
# written outside backticks, which a browser would swallow; it is shown as text instead.
_HTML_TAGS = frozenset({"a", "p", "h1", "h2", "h3", "h4", "h5", "h6", "code", "pre", "em", "strong", "ul", "ol",
                        "li", "blockquote", "table", "thead", "tbody", "tr", "th", "td", "hr", "br", "img"})
_TAG = re.compile(r"</?([A-Za-z][^\s/<>]*)(?:\s[^<>]*)?/?>")


def _escape_raw_tags(body: str) -> str:
    return _TAG.sub(lambda m: m.group(0) if m.group(1) in _HTML_TAGS else html.escape(m.group(0), quote=False), body)


def render_html(md: str, title: str) -> str:
    """The Markdown as one page with a step per H2. Fenced code keeps `language-<lang>` for highlight.js."""
    import markdown as mdlib

    blocks: list[str] = []

    def keep_mermaid(m: re.Match) -> str:
        blocks.append(m.group(1))
        return f"\n\nMERMAIDBLOCK{len(blocks) - 1}\n\n"

    text = _raw_links(MERMAID.sub(keep_mermaid, md))
    body = _escape_raw_tags(mdlib.markdown(text, extensions=["tables", "fenced_code", "sane_lists"]))
    for i, b in enumerate(blocks):
        body = body.replace(f"<p>MERMAIDBLOCK{i}</p>", f'<pre class="mermaid">{html.escape(b)}</pre>')
    parts = re.split(r"(?=<h2>)", body)
    intro, steps = parts[0], parts[1:]
    nav, sections = [], []
    for i, s in enumerate(steps, start=1):
        h = re.match(r"<h2>(.*?)</h2>", s)
        label = re.sub(r"<[^>]+>", "", h.group(1)) if h else f"Step {i}"
        nav.append(f'<li><a href="#step-{i}" data-step="{i}">{i}. {label}</a></li>')
        sections.append(f'<section class="step" id="step-{i}" data-step="{i}">{s}'
                        f'<nav class="pager"><button class="prev" type="button">Previous</button>'
                        f'<button class="next" type="button">Next</button></nav></section>')
    page = TEMPLATE.read_text(encoding="utf-8")
    return (page.replace("{{TITLE}}", html.escape(title)).replace("{{INTRO}}", intro)
            .replace("{{NAV}}", "\n".join(nav)).replace("{{STEPS}}", "\n".join(sections)))


def paths_file_for(md: Path) -> Path:
    """`<name>-paths.json` beside `<name>-walkthrough.md`."""
    return md.with_name(md.name.removesuffix("-walkthrough.md") + "-paths.json")


def write_paths_output(state: WalkState, md: Path) -> Path | None:
    """Write the trace stage's result beside the walkthrough with code_facts' writer; None when there is none.

    A failed write is recorded in `state.errors` and leaves no file: the walkthrough is already written.
    """
    target = paths_file_for(md)
    if state.paths is None:
        target.unlink(missing_ok=True)       # a stale file from an earlier run would be taken for this one's
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
    keys = {state.target.repo_key} | {c.path.partition("/")[0] for c in citations_for_write(state).values()}
    for key in sorted(keys):
        repo = state.repos.get(key)
        if repo is None:
            continue
        proc = git(repo.path, "show", "-s", "--format=", repo.head_sha, check=False)
        if proc.returncode != 0:
            return f"{key} at {repo.head_sha}: {proc.stderr.strip() or f'git exited {proc.returncode}'}"
    return None


def write_outputs(state: WalkState, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = state.target.name
    md = render_markdown(state, out_dir)
    gate = sections_gate(md, [t for t in required_titles(state.options.get("mode", "human"))])
    if not gate.passed:
        state.errors.append(gate.reason)
    md_path = out_dir / f"{name}-walkthrough.md"
    md_path.write_text(md, encoding="utf-8")
    paths = [md_path]
    if state.options.get("mode") != "agent":
        json_path = out_dir / f"{name}-walkthrough.json"
        json_path.write_text(json.dumps(render_walkthrough_json(state, md), indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        paths.append(json_path)
        html_path = out_dir / f"{name}-walkthrough.html"
        html_path.write_text(render_html(md, f"{state.target.package} walkthrough"), encoding="utf-8")
        paths.append(html_path)
        written = write_paths_output(state, md_path)
        if written is not None:
            paths.append(written)
    return paths
