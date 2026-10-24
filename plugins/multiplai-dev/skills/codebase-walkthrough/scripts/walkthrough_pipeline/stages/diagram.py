"""diagram (module and codebase scope, human mode): a model draws the "How it works" picture as SVG, code
renders it to PNG, and a model looks at the PNGs and corrects it, for up to `LOOKS_MAX` rounds.

1. `draw`: one call with no tools (`prompts.diagram_writer`) returns two SVGs (wide and tall), their CSS and
   a text alternative (`DiagramOutput`). `gates.diagram_gate` checks it; a failure gets one re-ask with the
   reasons (`draw:rewrite`). One that still fails, or a failed call, records why in `state.errors` and leaves
   `state.diagram` empty: the page then shows the picture `render.flow_picture` draws.
2. Up to three looks. Before look *n*, code renders the current version to `<run>/diagram/round-N-*.png`
   (`diagram.render_pngs`). Look *n* (`prompts.diagram_looker`) gets `Read`, limited to that folder, and
   answers `ok` or `fix` with the whole corrected SVGs and CSS (`DiagramLook`). `ok` ends the loop. A `fix`
   that passes `diagram_gate` becomes the current version; one that fails keeps the current version, and
   its reasons go into the next look's prompt. A failed call ends the loop and keeps the current version.
3. `state.diagram` is the current version, with the number of looks made and whether a look said `ok`.

Each answer is stored in `state.diagram_answers` as it returns and reused instead of a new call, so a resume
asks only what is not yet answered. The PNGs are always drawn again from the stored answers, never read
from disk. When `resvg-py` will not import, no look is made and the page keeps the picture code draws.
`--scope pr` and agent mode pass through without a call.
"""

from __future__ import annotations

import json
import logging

from multiplai_core.untrusted import fence, markdown_notice

from .. import design, sdk
from ..diagram import DiagramUnavailable, render_pngs
from ..gates import Corpus, diagram_gate
from ..models import DiagramLook, DiagramOutput, WalkState
from ..prompts import diagram_looker, diagram_writer
from . import RunContext
from .page import _tokens
from .write import citations_for_write

log = logging.getLogger(__name__)

LOOKS_MAX = 3
LOOK_TOOLS = ["Read"]
UNAVAILABLE = "diagram: resvg-py is not installed for this platform; the page shows the picture drawn by code"


def diagram_material(state: WalkState) -> str:
    """Everything the diagram writer and looker may use, as one JSON string inside one
    `<untrusted-content>` fence: the flow section's data, the core choice's title, the summary's tagline,
    the loops table and the glossary."""
    secs = state.sections

    def data(key: str) -> dict:
        return secs[key].data if key in secs else {}

    glossary = secs.get("glossary")
    payload = {
        "flow": data("flow"),
        "core_title": state.core.get("title", ""),
        "tagline": data("summary").get("tagline", ""),
        "loops": data("loops").get("rows", []),
        "glossary": [
            {"term": t.term, "meaning": t.meaning}
            for t in (glossary.glossary if glossary else [])
        ],
    }
    return "\n".join(
        [
            markdown_notice(
                "the checked flow and tables, which quote the repository", "Material"
            ),
            *fence(
                json.dumps(payload, ensure_ascii=False, indent=1), "diagram material"
            ),
        ]
    )


async def _ask(
    state: WalkState, ctx: RunContext, key: str, prompt: str, schema, **call
):
    """The stored answer under *key*, else one call stored as it returns; None when the call failed."""
    if key in state.diagram_answers:
        raw = state.diagram_answers[key]
        return None if "error" in raw else schema.model_validate(raw)
    cfg = ctx.config
    try:
        out = await sdk.agent_call_structured(
            prompt,
            schema,
            model=cfg.write_model,
            effort=cfg.effort,
            budget_label="diagram",
            **call,
        )
    except sdk.RepoTrustError:
        raise
    except sdk.AgentCallError as e:
        log.exception("diagram %s failed", key)
        first = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
        state.diagram_answers[key] = {"error": first}
        return None
    state.diagram_answers[key] = out.model_dump()
    return out


def _done(state: WalkState, ctx: RunContext, looks: int = 0) -> WalkState:
    ctx.counts = {"diagram": 1 if state.diagram else 0, "looks": looks}
    state.stage = "diagram"
    return state


async def run_diagram(state: WalkState, ctx: RunContext) -> WalkState:
    if state.past("diagram"):
        return state
    flow = state.sections.get("flow")
    nodes = (flow.data.get("nodes") if flow else None) or []
    if (
        state.options.get("mode") == "agent"
        or state.target.scope == "change"
        or not nodes
    ):
        return _done(state, ctx)
    citations = citations_for_write(state)
    gate = {
        "labels": set(ctx.repos)
        | {c.path for c in citations.values()}
        | {e.name for e in state.symbols},
        "corpus": Corpus([ctx.snap_root, ctx.run_dir / "docs-cache"]),
        "box_labels": [n["label"] for n in nodes],
    }
    name, material, tokens = (
        state.target.package,
        diagram_material(state),
        _tokens(state),
    )
    tokens_css = design.css(state.options.get("design_system_dir"))[1]
    folder = ctx.run_dir / "diagram"
    write = {"allowed_tools": sdk.WRITE_TOOLS, "max_turns": 2}

    out = await _ask(
        state,
        ctx,
        "draw",
        diagram_writer(name, material, tokens, DiagramOutput),
        DiagramOutput,
        **write,
    )
    reasons: list[str] = []
    if out is not None:
        reasons = diagram_gate(out, **gate)
        if reasons:
            redo = diagram_writer(
                name,
                material,
                tokens,
                DiagramOutput,
                failures=reasons,
                previous=json.dumps(out.model_dump(), ensure_ascii=False),
            )
            out = await _ask(state, ctx, "draw:rewrite", redo, DiagramOutput, **write)
            if out is not None:
                reasons = diagram_gate(out, **gate)
    if out is None:
        failed = next(
            (
                a["error"]
                for a in reversed(state.diagram_answers.values())
                if "error" in a
            ),
            "no answer",
        )
        state.errors.append(f"diagram: {failed}")
        return _done(state, ctx)
    if reasons:
        state.errors.append(f"diagram: {'; '.join(reasons)[:500]}")
        return _done(state, ctx)

    current, looks, approved, failures = out, 0, False, []
    for n in range(1, LOOKS_MAX + 1):
        try:
            pngs = render_pngs(current, tokens_css, folder, n)
        except DiagramUnavailable:
            state.errors.append(UNAVAILABLE)
            return _done(state, ctx)
        except ValueError as e:
            state.errors.append(f"diagram: resvg-py could not draw it: {str(e)[:200]}")
            break
        prompt = diagram_looker(
            name,
            material,
            json.dumps(
                {"wide": current.wide, "tall": current.tall, "css": current.css},
                ensure_ascii=False,
            ),
            [str(p) for p in pngs],
            DiagramLook,
            n,
            tokens=tokens,
            failures=failures,
        )
        look = await _ask(
            state,
            ctx,
            f"look:{n}",
            prompt,
            DiagramLook,
            allowed_tools=LOOK_TOOLS,
            read_roots=[str(folder)],
            max_turns=5,
        )
        if look is None:
            state.errors.append(
                f"diagram: look {n}: {state.diagram_answers[f'look:{n}']['error']}"
            )
            break
        looks = n
        if look.verdict == "ok":
            approved = True
            break
        fixed = DiagramOutput(
            wide=look.wide, tall=look.tall, css=look.css, alt=current.alt
        )
        failures = diagram_gate(fixed, **gate)
        if not failures:
            current = fixed
    if not approved and looks == LOOKS_MAX:
        state.errors.append(f"diagram: not approved after {LOOKS_MAX} looks")
    state.diagram = {
        "wide": current.wide,
        "tall": current.tall,
        "css": current.css,
        "alt": current.alt,
        "looks": looks,
        "approved": approved,
    }
    return _done(state, ctx, looks)
