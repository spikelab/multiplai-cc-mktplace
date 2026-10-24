"""core (module and codebase scope, human mode): the part of the code that does its main job.

Explore spreads its reading evenly across the files, so the part the code exists for gets no more of it
than the code around it. This stage runs after explore and survey, and decides from their facts:

1. One call with no tools names that part (`CoreChoice`): a plain section title, the job, the files that
   do it, why, and the ids it rests on. `core_gate` checks it; a failure gets one re-ask with the reasons.
   Both answers are stored in `state.core_answers` (`core:choice`, `core:choice:rewrite`), never in
   `explore_answers`. A choice that fails twice leaves `state.core` empty, is recorded in `state.dropped`
   (stage `core`), and the stage ends with no deep read.
2. One agent with `Read`/`Grep`/`Glob` on the snapshot reads those files closely. Its answer is stored in
   `state.explore_answers["core:read"]`, after the survey's, and `explore.gate_answers` gates it like every
   other answer, so its facts get ids after explore's and survey's and are sourced `core:read/…`.
3. The gated choice is saved as `state.core`; the write stage gives it its own section.

`--scope pr` and agent mode pass through without a call.
"""

from __future__ import annotations

import json
import logging

from code_facts import run_tasks

from .. import sdk
from ..gates import Corpus, cite_id, core_gate
from ..models import CoreChoice, Dropped, ExploreOutput, WalkState
from ..prompts import core_choice, core_read
from . import RunContext
from .explore import _ask_kwargs, gate_answers
from .survey import _libs, own_docs
from .write import _fact_line

log = logging.getLogger(__name__)

CHOICE_KEY = "core:choice"
READ_KEY = "core:read"
UNIT_FACTS = 120


def _choice_prompt(state: WalkState, ctx: RunContext, **redo) -> str:
    t = state.target
    docs = set(own_docs(t, ctx.snap_root / t.repo_key))
    doc_facts = [f for f in state.facts if any(c.path in docs for c in f.citations)]
    unit = [
        f
        for f in state.facts
        if f.source.startswith("unit:") and "/file" not in f.source
    ][:UNIT_FACTS]
    return core_choice(
        t.package,
        [(t.ws(f), t.file_lines.get(f, 0)) for f in t.files],
        [f"- {p}: {s}" for p, s in sorted(state.file_purposes.items())],
        [_fact_line(f) for f in doc_facts],
        [_fact_line(f) for f in unit],
        CoreChoice,
        **redo,
    )


async def _ask_choice(
    state: WalkState, ctx: RunContext, key: str, prompt: str
) -> CoreChoice | None:
    """The stored answer under *key*, else one call stored as it returns; None when the call failed."""
    if key in state.core_answers:
        raw = state.core_answers[key]
        return None if "error" in raw else CoreChoice.model_validate(raw)
    cfg = ctx.config
    try:
        out = await sdk.agent_call_structured(
            prompt,
            CoreChoice,
            allowed_tools=sdk.WRITE_TOOLS,
            model=cfg.write_model,
            effort=cfg.effort,
            max_turns=2,
            budget_label="core",
        )
    except sdk.RepoTrustError:
        raise
    except sdk.AgentCallError as e:
        log.exception("core %s failed", key)
        first = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
        state.core_answers[key] = {"error": first}
        state.errors.append(f"core {key}: {first}")
        return None
    state.core_answers[key] = out.model_dump()
    return out


async def run_core(state: WalkState, ctx: RunContext) -> WalkState:
    if state.past("core"):
        return state
    if state.options.get("mode") == "agent" or state.target.scope == "change":
        state.stage = "core"
        return state
    t = state.target
    gate = {
        "target_files": {t.ws(f) for f in t.files},
        "citations": state.citation_index(),
        "corpus": Corpus([ctx.snap_root, ctx.run_dir / "docs-cache"]),
        "labels": set(ctx.repos) | {e.name for e in state.symbols},
    }
    choice = await _ask_choice(state, ctx, CHOICE_KEY, _choice_prompt(state, ctx))
    reasons = core_gate(choice, **gate) if choice is not None else []
    if choice is not None and reasons:
        retry = await _ask_choice(
            state,
            ctx,
            CHOICE_KEY + ":rewrite",
            _choice_prompt(
                state,
                ctx,
                failures=reasons,
                previous=json.dumps(choice.model_dump(), ensure_ascii=False),
            ),
        )
        choice = retry
        if retry is not None:
            reasons = core_gate(retry, **gate)
            if reasons:
                state.dropped = [d for d in state.dropped if d.stage != "core"] + [
                    Dropped(
                        stage="core",
                        what=f"the part that does the main job: {retry.title!r}",
                        reason="; ".join(reasons)[:300],
                    )
                ]
                choice = None
    if choice is None:
        ctx.counts = {"core": 0}
        state.stage = "core"
        return state

    files = [f.strip() for f in choice.files if f.strip()]
    lines = {t.ws(f): t.file_lines.get(f, 0) for f in t.files}
    await run_tasks(
        [
            (
                READ_KEY,
                core_read(
                    t.package,
                    choice.model_dump(),
                    [(f, lines.get(f, 0)) for f in files],
                    _libs(state),
                    ExploreOutput,
                ),
            )
        ],
        sdk.agent_call_structured,
        state.explore_answers,
        concurrency=1,
        budget_label=lambda key: key,
        **_ask_kwargs(state, ctx),
    )
    # The deep read's answer after every other, so its facts get ids after explore's and survey's.
    answers = state.explore_answers
    state.explore_answers = {
        **{k: v for k, v in answers.items() if k != READ_KEY},
        READ_KEY: answers[READ_KEY],
    }
    gate_answers(state, ctx)
    state.core = {
        "title": choice.title.strip(),
        "job": choice.job.strip(),
        "why": choice.why.strip(),
        "files": files,
        "cites": [cite_id(c) for c in choice.cites if cite_id(c)],
    }
    ctx.counts = {
        "core": 1,
        "facts": len([f for f in state.facts if f.source.startswith(READ_KEY)]),
    }
    state.stage = "core"
    return state
