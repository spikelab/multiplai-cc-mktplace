"""trace: code picks scenario seeds from boundary.json; one agent per scenario returns hops.

Seeds, in order, capped at `--scenarios`: webhook routes; routes called from
other repositories (seeded at the caller, so the trace crosses repos); Celery
beat entries naming the target's tasks; the remaining routes.

`trace_gate` checks every hop. A break gets one re-ask naming the hop; if it
still breaks, the scenario keeps its hops up to the last good one and is
marked `trace stops at hop k`.
"""

from __future__ import annotations

import logging

from .. import sdk
from ..gates import trace_gate
from ..models import Reference, Scenario, SymbolEntry, TraceOutput, WalkState
from ..prompts import trace_scenario
from . import RunContext, bounded, context_lines, ws_path

log = logging.getLogger(__name__)


def pick_seeds(state: WalkState, cap: int) -> list[SymbolEntry | Reference]:
    urls = [e for e in state.symbols if e.kind == "url"]
    seeds: list[SymbolEntry | Reference] = []
    taken: set[str] = set()

    def add(entry, key: str) -> None:
        if key not in taken and len(seeds) < cap:
            taken.add(key)
            seeds.append(entry)

    for e in urls:
        hay = (e.name + " " + e.detail.get("view", "") + " " + e.detail.get("url_name", "")).lower()
        if "webhook" in hay or "wbhk" in hay:
            add(e, "route:" + e.name)
    for r in state.references:
        if r.kind == "http-route" and r.repo != state.target.repo_key:
            add(r, "route:" + r.symbol)
    for r in state.references:
        if r.kind == "celery-name":
            add(r, "task:" + r.symbol)
    for e in urls:
        add(e, "route:" + e.name)
    return seeds


def boundary_places(state: WalkState) -> list[tuple[str, int]]:
    return ([(r.path, r.line) for r in state.references]
            + [(e.path, e.line) for e in state.symbols if e.kind in ("url", "celery-task", "http")])


def _seed_head(seed: SymbolEntry | Reference, ctx: RunContext) -> str:
    name = getattr(seed, "symbol", None) or getattr(seed, "name", "")
    detail = ""
    if isinstance(seed, SymbolEntry) and seed.detail:
        detail = " " + ", ".join(f"{k}={v}" for k, v in seed.detail.items())
    return f"{seed.path}:{seed.line} — {seed.kind} `{name}`{detail}\n{context_lines(ctx.snap_root, seed.path, seed.line, 4)}"


def _fix(out: TraceOutput, ctx: RunContext) -> TraceOutput:
    return out.model_copy(update={"hops": [h.model_copy(update={"path": ws_path(h.path, ctx.snap_root)})
                                          for h in out.hops]})


async def run_trace(state: WalkState, ctx: RunContext) -> WalkState:
    if state.past("trace"):
        return state
    if state.options.get("mode") == "agent":
        state.stage = "trace"
        return state
    seeds = pick_seeds(state, int(state.options.get("scenarios") or 6))
    places = boundary_places(state)
    cfg = ctx.config

    async def ask(key: str, prompt: str) -> TraceOutput | None:
        if key in state.trace_answers:
            raw = state.trace_answers[key]
            return None if "error" in raw else TraceOutput.model_validate(raw)
        try:
            out = await sdk.agent_call_structured(
                prompt, TraceOutput, allowed_tools=sdk.TRACE_TOOLS, model=cfg.trace_model, effort=cfg.effort,
                max_turns=cfg.max_turns, cwd=str(ctx.snap_root), budget_label="trace")
            out = _fix(out, ctx)
            state.trace_answers[key] = out.model_dump()
            return out
        except sdk.RepoTrustError:
            raise
        except sdk.AgentCallError as e:
            log.error("trace %s failed", key, exc_info=True)
            state.trace_answers[key] = {"error": str(e).splitlines()[0][:200]}
            state.errors.append(f"trace {key}: {str(e).splitlines()[0][:200]}")
            return None

    async def one(i: int) -> Scenario:
        seed = seeds[i]
        head = _seed_head(seed, ctx)
        out = await ask(f"s{i + 1}", trace_scenario(state.target.package, head, seed.path, seed.line, TraceOutput))
        if out is None:
            return Scenario(seed=seed.model_dump(), stops_at=0, break_reason="the trace agent failed")
        good, reason = trace_gate(ctx.repos, seed, out.hops, places)
        if good < len(out.hops) or not out.hops:
            retry = await ask(f"s{i + 1}:reask", trace_scenario(state.target.package, head, seed.path, seed.line,
                                                                TraceOutput, broke=reason))
            if retry is not None:
                g2, r2 = trace_gate(ctx.repos, seed, retry.hops, places)
                if g2 > good or (g2 == len(retry.hops) and retry.hops):
                    out, good, reason = retry, g2, r2
        hops = out.hops[:good]
        broke = good < len(out.hops) or not out.hops
        return Scenario(seed=seed.model_dump(), title=out.title, hops=hops,
                        stops_at=good if broke else None, break_reason=reason if broke else "")

    scenarios = await bounded(range(len(seeds)), one, cfg.concurrency)
    state.scenarios = [s for s in scenarios]
    ctx.counts = {"scenarios": len(scenarios), "complete": sum(1 for s in scenarios if s.stops_at is None),
                  "hops": sum(len(s.hops) for s in scenarios),
                  "cut": sum(1 for s in scenarios if s.stops_at is not None)}
    state.stage = "trace"
    return state
