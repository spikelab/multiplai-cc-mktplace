"""explore: one agent per unit of target files, one per group of boundary entries.

Gates, all in code:
- `citation_gate` on every citation; a fact with no passing citation is dropped and counted.
- `file_coverage_gate`: each target file has a surviving fact, or is 30 lines or fewer.
- `boundary_coverage_gate`: each boundary entry is cited by a surviving fact.
Misses get one re-ask naming them; whatever is still missing goes into the coverage table.
"""

from __future__ import annotations

import logging

from .. import sdk
from ..gates import boundary_coverage_gate, citation_gate, entry_key, file_coverage_gate, target_files_ws
from ..models import Citation, Dropped, ExploreOutput, Fact, Reference, SymbolEntry, Term, WalkState
from ..partition import ws_lines
from ..prompts import explore_boundary, explore_unit
from . import RunContext, bounded, context_lines, fix_citation

log = logging.getLogger(__name__)

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
    files = [(f, lines.get(f, 0)) for f in unit.files]
    return explore_unit(state.target.package, files, ExploreOutput, state.options.get("depth", "standard"), missing)


def _group_prompt(state: WalkState, key: str, entries: list, ctx: RunContext, missing: list[str] | None = None) -> str:
    return explore_boundary(key, state.target.package, [_entry_head(e, ctx) for e in entries], ExploreOutput, missing)


async def _ask(key: str, prompt: str, state: WalkState, ctx: RunContext) -> None:
    cfg = ctx.config
    try:
        out = await sdk.agent_call_structured(
            prompt, ExploreOutput, allowed_tools=sdk.EXPLORE_TOOLS, model=cfg.explore_model, effort=cfg.effort,
            max_turns=cfg.max_turns, cwd=str(ctx.snap_root), budget_label=f"explore:{key.split(':')[0]}",
        )
        state.explore_answers[key] = out.model_dump()
    except sdk.RepoTrustError:
        raise
    except sdk.AgentCallError as e:
        log.error("explore %s failed", key, exc_info=True)
        state.explore_answers[key] = {"error": str(e).splitlines()[0][:200]}
        state.errors.append(f"explore {key}: {str(e).splitlines()[0][:200]}")
    if ctx.progress:
        n = len(state.explore_answers[key].get("facts", []))
        ctx.progress.line(f"  explore {key}: {n} facts")


def gate_answers(state: WalkState, ctx: RunContext) -> None:
    """Rebuild the gated facts from every stored answer, in task order. Deterministic."""
    facts: list[Fact] = []
    purposes: dict[str, str] = {}
    interfaces: list[int] = []
    gotchas: list[int] = []
    terms: dict[str, Term] = {}
    dropped: list[Dropped] = [d for d in state.dropped if d.stage != "explore"]

    def keep(c: Citation | None) -> Citation | None:
        if c is None:
            return None
        c = fix_citation(c, ctx.snap_root)
        g = citation_gate(ctx.repos, c)
        if g.passed:
            return c
        ctx.gate_reasons.append(g.reason)
        return None

    for key, raw in state.explore_answers.items():
        if "error" in raw:
            continue
        out = ExploreOutput.model_validate(raw)
        for bucket, items in (("fact", out.facts), ("interface", out.interfaces), ("gotcha", out.gotchas)):
            for f in items:
                good = [c for c in (keep(c) for c in f.citations) if c is not None]
                if not good:
                    dropped.append(Dropped(stage="explore", what=f.claim[:300], reason="no citation passed"))
                    continue
                facts.append(Fact(claim=f.claim, citations=good, source=f"{key}/{bucket}"))
                if bucket == "interface":
                    interfaces.append(len(facts) - 1)
                elif bucket == "gotcha":
                    gotchas.append(len(facts) - 1)
        for fp in out.files:
            c = keep(fp.citation)
            if c is not None:
                purposes.setdefault(c.path, fp.purpose)
                facts.append(Fact(claim=f"{c.path.split('/')[-1]}: {fp.purpose}", citations=[c],
                                  source=f"{key}/file"))
        for t in out.terms:
            c = keep(t.citation)
            if c is None:
                dropped.append(Dropped(stage="explore", what=f"term {t.term}", reason="no citation passed"))
                continue
            terms.setdefault(t.term.lower(), Term(term=t.term, meaning=t.meaning, citation=c))

    n = 0
    for i, f in enumerate(facts):
        f.id = f"f{i + 1}"
        for c in f.citations:
            n += 1
            c.id = f"c{n}"
    state.facts = facts
    state.file_purposes = purposes
    state.interfaces = [facts[i].id for i in interfaces]
    state.gotchas = [facts[i].id for i in gotchas]
    state.terms = list(terms.values())
    state.dropped = dropped


async def run_explore(state: WalkState, ctx: RunContext) -> WalkState:
    if state.past("explore"):
        return state
    groups = boundary_groups(state) if state.options.get("mode") != "agent" else {}
    tasks: list[tuple[str, str]] = [(f"unit:{u.id}", _unit_prompt(state, u.id)) for u in state.units]
    tasks += [(f"boundary:{k}", _group_prompt(state, k, v, ctx)) for k, v in groups.items()]

    async def one(task: tuple[str, str]) -> None:
        await _ask(task[0], task[1], state, ctx)

    await bounded([t for t in tasks if t[0] not in state.explore_answers], one, ctx.config.concurrency)
    gate_answers(state, ctx)

    # One re-ask per task that left files or entries uncovered.
    lines = ws_lines(state.target)
    files = target_files_ws(state.target)
    missing_files = set(file_coverage_gate(files, lines, state.facts))
    retry: list[tuple[str, str]] = []
    for u in state.units:
        miss = [f for f in u.files if f in missing_files]
        key = f"unit:{u.id}:reask"
        if miss and key not in state.explore_answers:
            retry.append((key, _unit_prompt(state, u.id, miss)))
    for k, entries in groups.items():
        miss_keys = set(boundary_coverage_gate(entries, state.facts))
        key = f"boundary:{k}:reask"
        if miss_keys and key not in state.explore_answers:
            missing = [e for e in entries if entry_key(e) in miss_keys]
            retry.append((key, _group_prompt(state, k, missing, ctx, sorted(miss_keys))))
    if retry:
        if ctx.progress:
            ctx.progress.line(f"  explore: re-asking {len(retry)} task(s) for uncovered files or entries")
        await bounded(retry, one, ctx.config.concurrency)
        gate_answers(state, ctx)

    state.uncovered_files = file_coverage_gate(files, lines, state.facts)
    state.uncovered_entries = boundary_coverage_gate(coverage_entries(state), state.facts) if groups else []
    ctx.counts = {"facts": len(state.facts), "dropped": len([d for d in state.dropped if d.stage == "explore"]),
                  "uncovered_files": len(state.uncovered_files), "uncovered_entries": len(state.uncovered_entries),
                  "failed_tasks": len([a for a in state.explore_answers.values() if "error" in a])}
    state.stage = "explore"
    return state
