"""page (module and codebase scope, human mode): one call writes the open body of the HTML page.

The write stage's sections are checked one item at a time, so each is written alone, and a page built from
them repeats itself and runs long. This stage hands one writer every gated section and fact at once and
asks for the page's open sections as HTML fragments (`PageOutput`), in the order of
`write.human_sections`, without the sections the page shows closed (`write.APPENDIX_KEYS`).

1. One call with no tools (`prompts.page_writer`). Its answer is stored in `state.page_answers["page"]`.
2. `gates.page_gate` checks it, and `gates.page_words` counts its visible words. A failure, or a count over
   `OPEN_WORDS_MAX`, gets one re-ask with the reasons, stored under `page:rewrite`.
3. An answer that passes `page_gate` is saved as `state.page`, even over the limit (recorded in
   `state.errors`). One that still fails, or a failed call, leaves `state.page` empty and records why in
   `state.errors`: `render.render_html` then draws every section with its own builder.

The Markdown is never built from this answer. `--scope pr` and agent mode pass through without a call.
"""

from __future__ import annotations

import json
import logging
import re

from multiplai_core.untrusted import fence, markdown_notice

from .. import design, sdk
from ..gates import Corpus, page_gate, page_words
from ..models import PageOutput, WalkState
from ..prompts import page_writer
from . import RunContext
from .write import APPENDIX_KEYS, citations_for_write, human_sections

log = logging.getLogger(__name__)

# v3 of the review walkthrough, the page this stage aims at, has 3,562 open words by `page_words`.
OPEN_WORDS_MAX = 3600
KEY = "page"
TOKEN = re.compile(r"(--[\w-]+)\s*:")


def open_sections(state: WalkState) -> list[tuple[str, str]]:
    """(key, title) of the page's open sections, in order."""
    return [
        (k, t)
        for k, t in human_sections(state.options, state.core)
        if k not in APPENDIX_KEYS
    ]


def page_material(state: WalkState) -> str:
    """Everything the page writer may use, as one JSON string inside one `<untrusted-content>` fence: the
    gated data of the open structured sections, the gotchas, the codebase tables, the core choice, every
    fact with its first citation, and the glossary."""
    secs = state.sections

    def data(key: str) -> dict:
        return secs[key].data if key in secs else {}

    gotchas = secs.get("gotchas")
    glossary = secs.get("glossary")
    payload = {
        "sections": {
            k: data(k)
            for k in ("summary", "flow", "core", "outputs", "config", "usecases")
            if data(k)
        },
        "gotchas": {
            "paragraphs": gotchas.paragraphs if gotchas else [],
            "questions": gotchas.questions if gotchas else [],
        },
        "tables": {k: data(k) for k in ("loops", "exits", "resume") if data(k)},
        "core": state.core,
        "facts": [
            {
                "id": f.citations[0].id,
                "claim": f.claim,
                "path": f.citations[0].path,
                "line": f.citations[0].line_start,
            }
            for f in state.facts
            if f.citations and "/file" not in f.source
        ],
        "glossary": [
            {"term": t.term, "meaning": t.meaning}
            for t in (glossary.glossary if glossary else [])
        ],
    }
    return "\n".join(
        [
            markdown_notice(
                "the checked sections and facts, which quote the repository", "Material"
            ),
            *fence(json.dumps(payload, ensure_ascii=False, indent=1), "page material"),
        ]
    )


def _tokens(state: WalkState) -> list[str]:
    """The design system's custom property names, from the CSS the page will carry."""
    fonts, tokens = design.css(state.options.get("design_system_dir"))
    return list(dict.fromkeys(TOKEN.findall(fonts + tokens)))


async def _ask(
    state: WalkState, ctx: RunContext, key: str, prompt: str
) -> PageOutput | None:
    """The stored answer under *key*, else one call stored as it returns; None when the call failed."""
    if key in state.page_answers:
        raw = state.page_answers[key]
        return None if "error" in raw else PageOutput.model_validate(raw)
    cfg = ctx.config
    try:
        out = await sdk.agent_call_structured(
            prompt,
            PageOutput,
            allowed_tools=sdk.WRITE_TOOLS,
            model=cfg.write_model,
            effort=cfg.effort,
            max_turns=2,
            budget_label="page",
        )
    except sdk.RepoTrustError:
        raise
    except sdk.AgentCallError as e:
        log.exception("page %s failed", key)
        first = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
        state.page_answers[key] = {"error": first}
        return None
    state.page_answers[key] = out.model_dump()
    return out


async def run_page(state: WalkState, ctx: RunContext) -> WalkState:
    if state.past("page"):
        return state
    if state.options.get("mode") == "agent" or state.target.scope == "change":
        state.stage = "page"
        return state
    sections = open_sections(state)
    citations = citations_for_write(state)
    gate = {
        "open_keys": [k for k, _ in sections],
        "core_flow": bool(
            state.sections.get("core") and state.sections["core"].data.get("flow")
        ),
        "citations": citations,
        "corpus": Corpus([ctx.snap_root, ctx.run_dir / "docs-cache"]),
        "labels": set(ctx.repos)
        | {c.path for c in citations.values()}
        | {e.name for e in state.symbols},
    }
    material, tokens = page_material(state), _tokens(state)
    summary = state.sections.get("summary")
    tagline = (summary.data.get("tagline") if summary else "") or ""

    def prompt(**redo) -> str:
        return page_writer(
            state.target.package,
            sections,
            material,
            tokens,
            PageOutput,
            words_max=OPEN_WORDS_MAX,
            tagline=tagline,
            **redo,
        )

    def check(out: PageOutput) -> tuple[list[str], int]:
        return page_gate(out, **gate), sum(page_words(s.html) for s in out.sections)

    reasons, words = [], 0
    out = await _ask(state, ctx, KEY, prompt())
    if out is not None:
        reasons, words = check(out)
        if reasons or words > OPEN_WORDS_MAX:
            over = (
                [f"the open body is {words} words; the limit is {OPEN_WORDS_MAX}"]
                if words > OPEN_WORDS_MAX
                else []
            )
            out = await _ask(
                state,
                ctx,
                KEY + ":rewrite",
                prompt(
                    failures=reasons + over,
                    previous=json.dumps(out.model_dump(), ensure_ascii=False),
                ),
            )
            if out is not None:
                reasons, words = check(out)
    failed = next(
        (a["error"] for a in reversed(state.page_answers.values()) if "error" in a),
        None,
    )
    if out is None:
        state.errors.append(f"page: {failed or 'no answer'}")
    elif reasons:
        state.errors.append(f"page: {'; '.join(reasons)[:500]}")
    else:
        if words > OPEN_WORDS_MAX:
            state.errors.append(
                f"page: open body {words} words, limit {OPEN_WORDS_MAX}"
            )
        state.page = {
            "css": out.css,
            "sections": {s.key: s.html for s in out.sections},
            "words": words,
        }
    ctx.counts = {"page": 1 if state.page else 0, "words": state.page.get("words", 0)}
    state.stage = "page"
    return state
