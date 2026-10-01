"""Assemble the walkthrough from gated sections. Markdown first; the HTML holds the same text.

Code, not a model, turns `[[cN]]` into `path:line` links and `[[snippet:cN]]`
into the cited lines read with `git show` at the commit. Each link carries its
quote as the link title, so `check.py` can re-run `citation_gate` on a finished
file. Every Mermaid diagram is drawn by code from gated hops and boundary
entries.
"""

from __future__ import annotations

import html
import os
import re
from datetime import date
from pathlib import Path, PurePosixPath

from .gates import CITE_ID, lines_at_commit, sections_gate
from .models import Citation, RepoInfo, WalkState
from .stages.write import citations_for_write, required_titles
from .target import github_web_base

LANG = {".py": "python", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "jsx", ".sql": "sql",
        ".sqlx": "sql", ".yml": "yaml", ".yaml": "yaml", ".json": "json", ".md": "markdown", ".html": "markup",
        ".sh": "bash", ".toml": "toml"}
SNIPPET_MAX_LINES = 40
TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "walkthrough.html"


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
        return os.path.relpath(Path(repo.path) / rel, self.out_dir) + anchor

    def link(self, c: Citation) -> str:
        where = f"{c.path}:{c.line_start}" + (f"-{c.line_end}" if c.line_end != c.line_start else "")
        quote = " ".join(c.quote.split())[:200].replace("\\", "\\\\").replace('"', '\\"')
        return f'[`{where}`]({self._url(c)} "{quote}")'

    def snippet(self, c: Citation) -> str:
        end = min(c.line_end, c.line_start + SNIPPET_MAX_LINES - 1)
        lines = lines_at_commit(self.repos, c.path, c.line_start, end) or []
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
        return CITE_ID.sub(lambda m: self.link(self.citations[m.group(1)]) if m.group(1) in self.citations else "",
                           text)


def _mermaid_id(i: int) -> str:
    return f"n{i}"


def _scenario_mermaid(hops) -> str:
    parts: list[str] = []
    for h in hops:
        label = PurePosixPath(h.path).name
        if label not in parts:
            parts.append(label)
    lines = ["sequenceDiagram"]
    for i, p in enumerate(parts):
        lines.append(f'    participant {_mermaid_id(i)} as {p}')
    for a, b in zip(hops, hops[1:]):
        ia, ib = parts.index(PurePosixPath(a.path).name), parts.index(PurePosixPath(b.path).name)
        msg = re.sub(r"[;#:\n]", " ", b.symbol_called)[:60]
        lines.append(f"    {_mermaid_id(ia)}->>{_mermaid_id(ib)}: {msg}")
    return "```mermaid\n" + "\n".join(lines) + "\n```"


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
    stops = [i + 1 for i, s in enumerate(state.scenarios) if s.stops_at is not None]
    if state.scenarios:
        out.append(f"| Scenarios cut short by the trace check | {len(stops)} of {len(state.scenarios)} |")
    if state.lsp:
        lsp = state.lsp
        detail = (f"{lsp.get('status')} in {lsp.get('seconds')}s, {lsp.get('answered')} of {lsp.get('symbols')} "
                  "symbols answered" if not str(lsp.get("status", "")).startswith("skipped") else lsp.get("status"))
        out.append(f"| pyright references | {detail} |")
    out.append("")
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

    md += ["## End-to-end scenarios", ""]
    if not state.scenarios:
        md += ["No scenario was traced.", ""]
    for i, sc in enumerate(state.scenarios):
        md += [f"### {i + 1}. {sc.title or 'Scenario ' + str(i + 1)}", ""]
        md += section(f"scenario:s{i + 1}")
        if len(sc.hops) > 1:
            md += [_scenario_mermaid(sc.hops), ""]
        for k, h in enumerate(sc.hops):
            c = Citation(path=h.path, line_start=h.line_start, line_end=h.line_end, quote=h.quote)
            md.append(f"{k + 1}. `{_cell(h.symbol_called)}` — {_cell(h.note)} {lk.link(c)}")
        md.append("")
        if sc.stops_at is not None:
            md += [f"> trace stops at hop {sc.stops_at}: {sc.break_reason}", ""]

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


# --- HTML -------------------------------------------------------------------------------


def markdown_headings(md: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^## (.+)$", md, re.M)]


def render_html(md: str, title: str) -> str:
    import markdown as mdlib

    blocks: list[str] = []

    def keep_mermaid(m: re.Match) -> str:
        blocks.append(m.group(1))
        return f"\n\nMERMAIDBLOCK{len(blocks) - 1}\n\n"

    text = re.sub(r"```mermaid\n(.*?)```", keep_mermaid, md, flags=re.S)
    body = mdlib.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
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
        html_path = out_dir / f"{name}-walkthrough.html"
        html_path.write_text(render_html(md, f"{state.target.package} walkthrough"), encoding="utf-8")
        paths.append(html_path)
    return paths
