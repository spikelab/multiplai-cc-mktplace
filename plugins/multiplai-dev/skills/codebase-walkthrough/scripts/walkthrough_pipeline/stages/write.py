"""write: one agent per section, no tools. Input is gated facts, hops and doc claims only.

Agents refer to code by citation id (`[[c42]]`); `render.py` turns ids into
`path:line` links and puts in snippets taken from the commit, so a model never
types a code snippet. `write_gate` checks every paragraph. A failing paragraph
gets one rewrite with its failure list; if it still fails it is cut from the
body and listed in the appendix "Cut by the checks", with the reason.

The glossary is not written by an agent: it is the gated terms from explore,
each checked again by `glossary_gate`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .. import sdk
from ..gates import Corpus, glossary_gate, write_gate
from ..models import Fact, Section, SectionOutput, WalkState, hop_citation_id
from ..prompts import write_section
from . import RunContext, bounded

log = logging.getLogger(__name__)

MATERIAL_MAX_CHARS = 60_000

# (key, title) in document order. `write_specs` expands the per-unit and per-scenario ones.
HUMAN_SECTIONS = [
    ("overview", "Overview"),
    ("glossary", "Glossary"),
    ("scenarios", "End-to-end scenarios"),
    ("tour", "Module tour"),
    ("boundary", "Who calls it and what it calls"),
    ("data", "Data stores and config"),
    ("docs", "Vendor docs"),
    ("gotchas", "Gotchas and questions for the owner"),
    ("coverage", "Coverage"),
]
AGENT_SECTIONS = [
    ("files", "Files"),
    ("architecture", "Architecture"),
    ("patterns", "Patterns & Conventions"),
    ("interfaces", "Key Interfaces"),
    ("gotchas", "Gotchas"),
    ("coverage", "Coverage"),
]


def required_titles(mode: str) -> list[str]:
    return [t for _, t in (AGENT_SECTIONS if mode == "agent" else HUMAN_SECTIONS)]


@dataclass
class Spec:
    key: str
    title: str
    brief: str
    material: str


def _fact_line(f: Fact) -> str:
    c = f.citations[0]
    more = "".join(f" [{x.id}]" for x in f.citations[1:])
    return f"- [{c.id}]{more} {f.claim}  ({c.path}:{c.line_start})"


def _material(lines: list[str]) -> str:
    out, size = [], 0
    for line in lines:
        if size + len(line) > MATERIAL_MAX_CHARS:
            out.append(f"- ({len(lines) - len(out)} more items left out for length)")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out) or "- (nothing passed the checks for this section)"


def _facts(state: WalkState, prefixes: tuple[str, ...]) -> list[Fact]:
    return [f for f in state.facts if f.source.startswith(prefixes)]


def write_specs(state: WalkState) -> list[Spec]:
    name = state.target.package
    if state.options.get("mode") == "agent":
        all_facts = [_fact_line(f) for f in state.facts if "/file" not in f.source]
        by_id = {f.id: f for f in state.facts}
        return [
            Spec("architecture", "Architecture", "How the files connect: data flow, import chains, which file owns "
                 "which responsibility. Concrete and terse; bullet points.", _material(all_facts)),
            Spec("patterns", "Patterns & Conventions", "Naming, error handling, testing and other conventions the "
                 "facts show. Bullet points.", _material(all_facts)),
            Spec("interfaces", "Key Interfaces", "The functions and classes other code calls: what they accept and "
                 "return. Bullet points.", _material([_fact_line(by_id[i]) for i in state.interfaces if i in by_id])),
            Spec("gotchas", "Gotchas", "What would trip an agent changing this code. Bullet points.",
                 _material([_fact_line(by_id[i]) for i in state.gotchas if i in by_id])),
        ]
    specs: list[Spec] = []
    purposes = [f"- {p}: {s}" for p, s in sorted(state.file_purposes.items())]
    unit_facts = [_fact_line(f) for f in _facts(state, ("unit:",)) if "/file" not in f.source]
    bnd = {k: len(v) for k, v in _boundary_counts(state).items()}
    specs.append(Spec("overview", "Overview",
                      f"Two or three paragraphs: what `{name}` is for, what it talks to, and how its parts fit. "
                      f"Reference counts found by code: {bnd}.", _material(purposes + unit_facts[:120])))
    for i, sc in enumerate(state.scenarios):
        if not sc.hops:
            continue
        hops = [f"- [{hop_citation_id(i, k)}] hop {k + 1}: `{h.symbol_called}` — {h.note}  ({h.path}:{h.line_start})"
                for k, h in enumerate(sc.hops)]
        specs.append(Spec(f"scenario:s{i + 1}", sc.title or f"Scenario {i + 1}",
                          "One or two paragraphs telling this scenario end to end, citing each hop by its id. "
                          "The program lists the hops after your text.", _material(hops)))
    for u in state.units:
        facts = [_fact_line(f) for f in state.facts if f.source.startswith(f"unit:{u.id}/") or
                 f.source.startswith(f"unit:{u.id}:reask/")]
        files = ", ".join(p.split("/")[-1] for p in u.files[:12]) + (" …" if len(u.files) > 12 else "")
        specs.append(Spec(f"tour:{u.id}", files, "Explain these files for a newcomer: what each does, how they "
                          "work together, and the code worth seeing (use [[snippet:cN]] for one or two key "
                          "places). Several short paragraphs.", _material(facts)))
    for group, title, brief in (
            ("inbound", "Who calls it", "Explain, repository by repository, how other code uses the module."),
            ("outbound", "What it calls", "Explain what the module calls out to: other apps, external APIs."),
    ):
        facts = [_fact_line(f) for f in _facts(state, (f"boundary:{group}",))]
        specs.append(Spec(f"boundary:{group}", title, brief + " The program adds full tables after your text.",
                          _material(facts)))
    facts = [_fact_line(f) for f in _facts(state, ("boundary:data", "boundary:config"))]
    specs.append(Spec("data", "Data stores and config", "Explain the tables and cache keys the module uses, and "
                      "its configuration: which setting or env var switches what (staging vs production, keys, "
                      "feature flags).", _material(facts)))
    d = state.docs or {}
    if d.get("claims") or d.get("disagreements"):
        lines = [f"- {c['claim']} (docs: {c['url']}, quote: {c['doc_quote'][:160]!r})" for c in d.get("claims", [])]
        lines += [f"- DISAGREEMENT [{_dis_cid(state, x)}] {x['summary']} (docs: {x['url']}, quote: "
                  f"{x['doc_quote'][:160]!r})" for x in d.get("disagreements", [])]
        specs.append(Spec("docs", "Vendor docs", "Explain what the vendor docs say that matters for this code, "
                          "and where the code and the docs disagree. Name doc pages by their URL. The program adds "
                          "the endpoint tables after your text.", _material(lines)))
    by_id = {f.id: f for f in state.facts}
    specs.append(Spec("gotchas", "Gotchas and questions for the owner",
                      "List the gotchas a newcomer would trip on, then in `questions` give 5-10 questions to ask the "
                      "module's owner that the facts raise but do not answer.",
                      _material([_fact_line(by_id[i]) for i in state.gotchas if i in by_id])))
    return specs


def _boundary_counts(state: WalkState) -> dict[str, list]:
    out: dict[str, list] = {}
    for r in state.references:
        out.setdefault(f"inbound {r.repo}", []).append(r)
    for e in state.symbols:
        if e.direction == "calls":
            out.setdefault(f"outbound {e.kind}", []).append(e)
    return out


def _dis_cid(state: WalkState, dis: dict) -> str:
    """Disagreement citations get ids `d1`, `d2`… in the citation index."""
    return f"d{(state.docs.get('disagreements') or []).index(dis) + 1}"


def citations_for_write(state: WalkState) -> dict:
    from ..models import Citation
    idx = state.citation_index()
    for i, dis in enumerate((state.docs or {}).get("disagreements") or []):
        c = Citation.model_validate(dis["citation"])
        idx[f"d{i + 1}"] = c.model_copy(update={"id": f"d{i + 1}"})
    return idx


async def run_write(state: WalkState, ctx: RunContext) -> WalkState:
    if state.past("write"):
        return state
    corpus = Corpus([ctx.snap_root, ctx.run_dir / "docs-cache"])
    citations = citations_for_write(state)
    labels = set(ctx.repos) | {c.path for c in citations.values()} | {e.name for e in state.symbols}
    cfg = ctx.config
    specs = write_specs(state)
    name = state.target.package

    async def call(key: str, prompt: str) -> SectionOutput | None:
        if key in state.write_answers:
            raw = state.write_answers[key]
            return None if "error" in raw else SectionOutput.model_validate(raw)
        try:
            out = await sdk.agent_call_structured(
                prompt, SectionOutput, allowed_tools=sdk.WRITE_TOOLS, model=cfg.write_model, effort=cfg.effort,
                max_turns=2, budget_label="write")
            state.write_answers[key] = out.model_dump()
            return out
        except sdk.RepoTrustError:
            raise
        except sdk.AgentCallError as e:
            log.error("write %s failed", key, exc_info=True)
            state.write_answers[key] = {"error": str(e).splitlines()[0][:200]}
            state.errors.append(f"write {key}: {str(e).splitlines()[0][:200]}")
            return None

    async def one(spec: Spec) -> Section:
        sec = Section(key=spec.key, title=spec.title)
        out = await call(spec.key, write_section(name, spec.title, spec.brief, spec.material, SectionOutput))
        if out is None:
            return sec
        texts = [p.text for p in out.paragraphs] + [f"QUESTION: {q}" for q in out.questions]
        results = [(t, write_gate(t, citations=citations, corpus=corpus, labels=labels)) for t in texts]
        failing = [(t, r) for t, r in results if r]
        if failing:
            retry = await call(spec.key + ":rewrite", write_section(
                name, spec.title, spec.brief, spec.material, SectionOutput,
                failures=[x for _, r in failing for x in r], previous=[t for t, _ in failing]))
            if retry is not None:
                fixed = [p.text for p in retry.paragraphs] + [f"QUESTION: {q}" for q in retry.questions]
                good = [t for t in fixed if not write_gate(t, citations=citations, corpus=corpus, labels=labels)]
                still = [(t, write_gate(t, citations=citations, corpus=corpus, labels=labels)) for t in fixed]
                # The rewrite replaces the failing paragraphs; what still fails is cut.
                results = [(t, r) for t, r in results if not r] + [(t, []) for t in good]
                failing = [(t, r) for t, r in still if r]
        for t, r in results:
            if r:
                continue
            if t.startswith("QUESTION: "):
                sec.questions.append(t[len("QUESTION: "):])
            else:
                sec.paragraphs.append(t)
        sec.cut = [{"text": t, "reasons": r} for t, r in failing]
        return sec

    sections = await bounded(specs, one, cfg.concurrency)
    state.sections = {s.key: s for s in sections}
    if state.options.get("mode") != "agent":
        gl = Section(key="glossary", title="Glossary")
        for t in sorted(state.terms, key=lambda t: t.term.lower()):
            g = glossary_gate(t.term, corpus)
            if g.passed:
                gl.glossary.append(t)
            else:
                gl.cut.append({"text": f"{t.term}: {t.meaning}", "reasons": [g.reason]})
        state.sections["glossary"] = gl
    ctx.counts = {"sections": len(state.sections),
                  "paragraphs": sum(len(s.paragraphs) for s in state.sections.values()),
                  "cut": sum(len(s.cut) for s in state.sections.values())}
    state.stage = "write"
    return state
