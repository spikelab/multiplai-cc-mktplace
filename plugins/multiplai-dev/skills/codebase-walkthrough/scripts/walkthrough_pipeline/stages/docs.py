"""docs: code fetches and maps; one agent per API resource reads the cached pages.

Gates, all in code:
- `doc_quote_gate`: the normalised doc quote is inside the cached text for that URL,
  and the URL is in llms.txt or a local docs file.
- a disagreement's code citation passes `citation_gate`.
- every outbound API path maps to a doc page or is listed as `no doc page found`.
- every outbound API path is matched to a collection request or listed as `not in the collection`
  (done by code in `docs.map_collection`).
"""

from __future__ import annotations

import logging
from pathlib import Path

from .. import docs as docs_mod
from .. import sdk
from ..gates import citation_gate, doc_quote_gate
from ..models import Dropped, DocsOutput, WalkState
from ..prompts import docs_resource
from ..symbols import normalise_api_path, outbound_api_paths
from . import RunContext, bounded, fix_citation

log = logging.getLogger(__name__)

MAX_FACTS_PER_RESOURCE = 40


def _resource_of(path: str) -> str:
    return next((s for s in path.split("/") if s and s != "{}"), "")


def _facts_for(state: WalkState, resource: str, api_paths: list[str]) -> list[str]:
    places = [(e.path, e.line) for e in state.symbols
              if e.kind == "http" and _resource_of(e.detail.get("normalised", normalise_api_path(e.name))) == resource]
    out = []
    for f in state.facts:
        hit = any(c.path == p and c.line_start <= ln <= c.line_end for c in f.citations for p, ln in places)
        if hit or resource.replace("_", " ") in f.claim.lower() or resource in f.claim:
            c = f.citations[0]
            out.append(f"[{c.id}] {f.claim} — {c.path}:{c.line_start}-{c.line_end} quote: {c.quote[:160]!r}")
        if len(out) >= MAX_FACTS_PER_RESOURCE:
            break
    return out


async def prepare_docs(state: WalkState, ctx: RunContext) -> None:
    """Code half: local docs, llms.txt, page fetches, collection mapping. Stored in state.docs."""
    opts = state.options
    api_paths = outbound_api_paths(state.symbols)
    search_root = Path(opts.get("search_root_resolved") or state.target.repo_path)
    vendor = docs_mod.vendor_word(opts["docs"]) if opts.get("docs") else ""
    cache = ctx.run_dir / "docs-cache"
    cache.mkdir(parents=True, exist_ok=True)

    local_dirs = [Path(d).expanduser().resolve() for d in opts.get("docs_dirs") or []]
    if not local_dirs and vendor:
        local_dirs = docs_mod.find_local_docs(search_root, vendor)
    local = docs_mod.read_local_docs(local_dirs)
    local_files = {}
    for key, text in local.items():
        name = "local-" + docs_mod.cache_name(key)
        (cache / name).write_text(docs_mod.fence(key, text), encoding="utf-8")
        local_files[key] = name

    coll_dir = Path(opts["api_collection"]).expanduser().resolve() if opts.get("api_collection") else (
        docs_mod.find_collection(search_root, vendor) if vendor else None)
    collection: dict = {"dir": str(coll_dir) if coll_dir else "", "mapped": {}, "missing": list(api_paths)}
    if coll_dir and coll_dir.is_dir():
        reqs, server = docs_mod.read_collection(coll_dir)
        mapped, missing = docs_mod.map_collection(api_paths, reqs)
        collection = {"dir": str(coll_dir), "server": server, "requests": len(reqs),
                      "mapped": {p: [{"file": r["file"], "method": r["method"], "name": r["name"]} for r in rs]
                                 for p, rs in mapped.items()},
                      "missing": missing}

    pages: dict[str, list[str]] = {}
    texts: dict[str, str] = {}
    files: dict[str, str] = {}
    report: dict = {}
    llms_count = 0
    if opts.get("docs"):
        _, links = await docs_mod.fetch_llms(opts["docs"])   # DocsError -> the orchestrator stops
        llms_count = len(links)
        pages = docs_mod.choose_pages(links, api_paths, int(opts.get("docs_pages") or 30))
        wanted = list(dict.fromkeys(u for us in pages.values() for u in us))
        texts, files, report = await docs_mod.fetch_docs(opts["docs"], wanted, cache)
        if ctx.progress:
            ctx.progress.line(f"  docs: llms.txt lists {llms_count} pages; fetched {len(texts)}, "
                              f"failed {len(report.get('failed', {}))}")

    state.docs = {
        "api_paths": api_paths, "docs_url": opts.get("docs") or "", "llms_pages": llms_count,
        "pages": pages, "files": files, "fetch_failed": report.get("failed", {}),
        "local_dirs": [str(d) for d in local_dirs], "local_files": local_files,
        "collection": collection, "cache": str(cache),
    }


def _doc_texts(state: WalkState) -> dict[str, str]:
    """Defanged text by URL or local key, re-read from the cache (fence lines removed)."""
    cache = Path(state.docs["cache"])
    out = {}
    for key, name in {**state.docs.get("files", {}), **state.docs.get("local_files", {})}.items():
        try:
            text = (cache / name).read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.split("\n")
        out[key] = "\n".join(lines[1:-2]) if lines and lines[0].startswith("<untrusted-content") else text
    return out


def gate_docs(state: WalkState, ctx: RunContext) -> None:
    texts = _doc_texts(state)
    fetched = set(state.docs.get("files", {}))
    claims, mappings, disagreements = [], {}, []
    dropped = [d for d in state.dropped if d.stage != "docs"]
    api_paths = state.docs.get("api_paths", [])
    norm_paths = {normalise_api_path(p): p for p in api_paths}
    for key, raw in state.docs_answers.items():
        if "error" in raw:
            continue
        out = DocsOutput.model_validate(raw)
        for c in out.claims:
            g = doc_quote_gate(texts, c.url, c.doc_quote)
            if g.passed:
                claims.append({"resource": key, "claim": c.claim, "url": c.url, "doc_quote": c.doc_quote})
            else:
                dropped.append(Dropped(stage="docs", what=c.claim[:300], reason=g.reason))
        for m in out.mappings:
            api = norm_paths.get(normalise_api_path(m.api_path))
            if api is None:
                dropped.append(Dropped(stage="docs", what=f"mapping {m.api_path}", reason="not an outbound API path"))
                continue
            g = doc_quote_gate(texts, m.url, m.doc_quote)
            if m.url not in fetched:
                g = g.model_copy(update={"passed": False, "reason": "url is not a fetched docs page"})
            if g.passed:
                mappings.setdefault(api, [])
                if m.url not in mappings[api]:
                    mappings[api].append(m.url)
            else:
                dropped.append(Dropped(stage="docs", what=f"mapping {m.api_path} -> {m.url}", reason=g.reason))
        for d in out.disagreements:
            g = doc_quote_gate(texts, d.url, d.doc_quote)
            cit = fix_citation(d.citation, ctx.snap_root)
            gc = citation_gate(ctx.repos, cit)
            if g.passed and gc.passed:
                disagreements.append({"summary": d.summary, "url": d.url, "doc_quote": d.doc_quote,
                                      "citation": cit.model_dump()})
            else:
                dropped.append(Dropped(stage="docs", what=d.summary[:300], reason=g.reason or gc.reason))
    state.docs["claims"] = claims
    state.docs["mappings"] = mappings
    state.docs["no_doc_page"] = [p for p in api_paths if p not in mappings]
    state.docs["disagreements"] = disagreements
    state.dropped = dropped


async def run_docs(state: WalkState, ctx: RunContext) -> WalkState:
    if state.past("docs"):
        return state
    opts = state.options
    if not state.docs:
        await prepare_docs(state, ctx)
    pages: dict[str, list[str]] = state.docs.get("pages", {})
    files: dict[str, str] = state.docs.get("files", {})
    api_paths: list[str] = state.docs.get("api_paths", [])
    union = list(dict.fromkeys(u for us in pages.values() for u in us))
    local = list(state.docs.get("local_files", {}).items())
    cfg = ctx.config
    resources = docs_mod.resources(api_paths) if (files or local) else []
    cache = Path(state.docs["cache"])

    async def one(res: str) -> None:
        own = pages.get(res) or union
        page_list = [(u, files[u]) for u in own if u in files] + local
        if not page_list:
            return
        paths = [p for p in api_paths if _resource_of(p) == res]
        prompt = docs_resource(state.target.package, res, paths, _facts_for(state, res, api_paths), page_list,
                               DocsOutput)
        try:
            out = await sdk.agent_call_structured(
                prompt, DocsOutput, allowed_tools=sdk.DOCS_TOOLS, model=cfg.docs_model, effort=cfg.effort,
                max_turns=cfg.max_turns, cwd=str(cache), budget_label="docs")
            state.docs_answers[res] = out.model_dump()
        except sdk.RepoTrustError:
            raise
        except sdk.AgentCallError as e:
            log.error("docs %s failed", res, exc_info=True)
            state.docs_answers[res] = {"error": str(e).splitlines()[0][:200]}
            state.errors.append(f"docs {res}: {str(e).splitlines()[0][:200]}")

    if opts.get("mode") != "agent":
        await bounded([r for r in resources if r not in state.docs_answers], one, cfg.concurrency)
    gate_docs(state, ctx)
    ctx.counts = {"api_paths": len(api_paths), "pages": len(files), "claims": len(state.docs.get("claims", [])),
                  "mapped": len(state.docs.get("mappings", {})), "no_doc_page": len(state.docs.get("no_doc_page", [])),
                  "not_in_collection": len(state.docs.get("collection", {}).get("missing", [])),
                  "dropped": len([d for d in state.dropped if d.stage == "docs"])}
    state.stage = "docs"
    return state
