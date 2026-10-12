"""explore: one agent per unit of target files, one per group of boundary entries.

Gates, all in code:
- every citation is re-read at its repo's commit (`code_facts`, through `multiplai_core.check_quote`);
  a fact with no passing citation is dropped and counted.
- `file_coverage_gate`: each target file has a surviving fact, or is 30 lines or fewer.
- `boundary_coverage_gate`: each boundary entry is cited by a surviving fact.
Misses get one re-ask naming them; whatever is still missing goes into the coverage table.
"""

from __future__ import annotations

from code_facts import Span, citation_keeper, gate_answers as gate_explore_answers, reask_units, run_tasks

from .. import sdk
from ..gates import boundary_coverage_gate, entry_key, file_coverage_gate, repo_resolver, target_files_ws
from ..models import ExploreOutput, Reference, SymbolEntry, WalkState
from ..partition import ws_lines
from ..prompts import WS_PATH_RULE, explore_boundary, explore_unit
from . import RunContext, context_lines

GROUP_SIZE = 60
OUTBOUND_KINDS = ("http", "import")
DATA_KINDS = ("model", "cache")
CONFIG_KINDS = ("setting", "env", "setting-definition")


def boundary_groups(state: WalkState) -> dict[str, list[SymbolEntry | Reference]]:
    groups: dict[str, list] = {}

    def chunk(prefix: str, entries: list) -> None:
        for i in range(0, len(entries), GROUP_SIZE):
            groups[f"{prefix}:{i // GROUP_SIZE + 1}"] = entries[i:i + GROUP_SIZE]

    by_repo: dict[str, list[Reference]] = {}
    for r in state.references:
        by_repo.setdefault(r.repo, []).append(r)
    for repo in sorted(by_repo):
        chunk(f"inbound:{repo}", by_repo[repo])
    chunk("outbound", [e for e in state.symbols if e.direction == "calls" and e.kind in OUTBOUND_KINDS])
    chunk("data", [e for e in state.symbols if e.kind in DATA_KINDS])
    chunk("config", [e for e in state.symbols if e.kind in CONFIG_KINDS])
    return groups


def coverage_entries(state: WalkState) -> list[SymbolEntry | Reference]:
    return [e for entries in boundary_groups(state).values() for e in entries]


def _entry_head(e: SymbolEntry | Reference, ctx: RunContext) -> tuple[str, str]:
    how = getattr(e, "method", "ast")
    name = getattr(e, "symbol", None) or getattr(e, "name", "")
    extra = ""
    if isinstance(e, SymbolEntry) and e.detail:
        extra = " " + ", ".join(f"{k}={v}" for k, v in e.detail.items())
    head = f"{e.kind} `{name}`{extra} (found by {how})\n{context_lines(ctx.snap_root, e.path, e.line)}"
    return entry_key(e), head


def _unit_prompt(state: WalkState, unit_id: str, missing: list[str] | None = None) -> str:
    unit = next(u for u in state.units if u.id == unit_id)
    lines = ws_lines(state.target)
    spans = [Span.whole(f, lines.get(f, 0)) for f in unit.files]
    return explore_unit(state.target.package, spans, ExploreOutput, state.options.get("depth", "standard"), missing,
                        path_rule=WS_PATH_RULE)


def _group_prompt(state: WalkState, key: str, entries: list, ctx: RunContext, missing: list[str] | None = None) -> str:
    return explore_boundary(key, state.target.package, [_entry_head(e, ctx) for e in entries], ExploreOutput, missing)


def _ask_kwargs(state: WalkState, ctx: RunContext) -> dict:
    """How code_facts asks: walkthrough's call settings, and which failures leave a task empty."""
    cfg = ctx.config
    return dict(
        call_kwargs=dict(allowed_tools=sdk.EXPLORE_TOOLS, model=cfg.explore_model, effort=cfg.effort,
                         max_turns=cfg.max_turns, cwd=str(ctx.snap_root)),
        errors=state.errors, recoverable=(sdk.AgentCallError,), fatal=(sdk.RepoTrustError,),
        progress=ctx.progress.line if ctx.progress else None,
    )


def gate_answers(state: WalkState, ctx: RunContext) -> None:
    """Rebuild the gated facts from every stored answer, in task order. Deterministic."""
    keep = citation_keeper(repo_resolver(ctx.repos), root=ctx.snap_root, reasons=ctx.gate_reasons)
    g = gate_explore_answers(state.explore_answers, keep)
    state.facts = g.facts
    state.file_purposes = g.purposes
    state.interfaces = g.interfaces
    state.gotchas = g.gotchas
    state.terms = g.terms
    state.dropped = [d for d in state.dropped if d.stage != "explore"] + g.dropped


async def run_explore(state: WalkState, ctx: RunContext) -> WalkState:
    if state.past("explore"):
        return state
    groups = boundary_groups(state) if state.options.get("mode") != "agent" else {}
    tasks: list[tuple[str, str]] = [(f"unit:{u.id}", _unit_prompt(state, u.id)) for u in state.units]
    tasks += [(f"boundary:{k}", _group_prompt(state, k, v, ctx)) for k, v in groups.items()]

    # The walkthrough's own call function: on an unusable answer it re-asks once to reformat.
    call = sdk.agent_call_structured
    ask = _ask_kwargs(state, ctx)
    await run_tasks(tasks, call, state.explore_answers, concurrency=ctx.config.concurrency, **ask)
    gate_answers(state, ctx)

    # One re-ask per task that left files or entries uncovered.
    lines = ws_lines(state.target)
    files = target_files_ws(state.target)
    missing_files = set(file_coverage_gate(files, lines, state.facts))
    retry = reask_units(state.units, missing_files, state.explore_answers,
                        lambda u, miss: _unit_prompt(state, u.id, miss))
    for k, entries in groups.items():
        miss_keys = set(boundary_coverage_gate(entries, state.facts))
        key = f"boundary:{k}:reask"
        if miss_keys and key not in state.explore_answers:
            missing = [e for e in entries if entry_key(e) in miss_keys]
            retry.append((key, _group_prompt(state, k, missing, ctx, sorted(miss_keys))))
    if retry:
        if ctx.progress:
            ctx.progress.line(f"  explore: re-asking {len(retry)} task(s) for uncovered files or entries")
        await run_tasks(retry, call, state.explore_answers, concurrency=ctx.config.concurrency, **ask)
        gate_answers(state, ctx)

    state.uncovered_files = file_coverage_gate(files, lines, state.facts)
    state.uncovered_entries = boundary_coverage_gate(coverage_entries(state), state.facts) if groups else []
    ctx.counts = {"facts": len(state.facts), "dropped": len([d for d in state.dropped if d.stage == "explore"]),
                  "uncovered_files": len(state.uncovered_files), "uncovered_entries": len(state.uncovered_entries),
                  "failed_tasks": len([a for a in state.explore_answers.values() if "error" in a])}
    state.stage = "explore"
    return state
