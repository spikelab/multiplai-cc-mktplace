"""explain: what the code under review does, as checked and cited facts (optional).

Runs before find, and only when the config sets `explain: true`. It hands the
review's scope (`facts.scope_for`: changed hunks at head with context, removed
lines at base; a tree review's whole files) to `code_facts`, one agent per
unit, and keeps a fact only when one of its citations is found at its commit
(`multiplai_core.check_quote`, through `code_facts`). A unit that left a file
with no fact gets one re-ask naming it. The result goes to `state.facts` and to
`facts.json` beside `findings.json`.

The agents read the head snapshot, like every other stage. Lines a change
removed are read from copies of those files at base, extracted beside the
snapshot (`target.snapshot_base`) and deleted when the stage ends. When they
cannot be extracted, the stage explains the head side only and adds an
`explain base: ...` line to `state.errors`, as it does for a failed unit.

Calls go through review's `sdk.agent_call_structured`, so they count towards
the review's spend limit, and its retry policy (one reformat re-ask) is the
one that runs. Answers are stored in `state.explain_answers` as they return,
so a budget stop keeps them and `resume` asks only for the rest.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from code_facts import (Citation, ExploreOutput, Fact, FilePurpose, Scope, citation_keeper, explore_unit, gate_answers,
                        partition, reask_units, run_tasks, unit_key, ws_path)

from .. import sdk, target as target_mod
from ..facts import resolver, scope_for, write_facts_file
from ..models import ExplainedFacts, ReviewState, TargetInfo
from . import RunContext

# Lines per explore unit; codebase-walkthrough's default `unit_lines`.
UNIT_LINES = 6000
# A file whose lines in scope are this few needs no fact of its own (walkthrough's file coverage rule).
SMALL_FILE_LINES = 30
BASE_DIR = "base-tree"

PATH_RULE = ("- `path` is the path exactly as listed above, relative to the repository root. For lines before "
             "the change too: cite the listed path, not the copy you read them from.")


def base_dir(ctx: RunContext) -> Path:
    """Where the files at base are extracted: beside the head snapshot."""
    return ctx.snapshot.parent / BASE_DIR


def _place(path: str, target: TargetInfo, snapshot: Path, base: Path) -> tuple[str, bool]:
    """(the repo-relative path, whether the agent wrote it under the base copy)."""
    p = path.strip().strip("`")
    for root, is_base in ((base, True), (snapshot, False), (Path(target.repo_path), False)):
        for r in (str(root.resolve()), str(root)):
            r = r.rstrip("/") + "/"
            if p.startswith(r):
                return p[len(r):].lstrip("/"), is_base
    return ws_path(p, snapshot), False


def keeper(target: TargetInfo, ctx: RunContext):
    """A citation keeper for code_facts: paths made repo-relative, a base-copy path read at base."""
    check = citation_keeper(resolver(target))
    base = base_dir(ctx)

    def keep(c: Citation | None) -> Citation | None:
        if c is None:
            return None
        path, is_base = _place(c.path, target, ctx.snapshot, base)
        update = {"path": path, **({"side": "base"} if is_base else {})}
        return check(c.model_copy(update=update))

    return keep


def uncovered(scope: Scope, facts: list[Fact]) -> list[str]:
    """Paths in the scope, over SMALL_FILE_LINES lines in it, that no surviving fact cites."""
    lines: dict[str, int] = {}
    for s in scope.spans:
        lines[s.path] = lines.get(s.path, 0) + s.lines
    cited = {c.path for f in facts for c in f.citations}
    return [p for p, n in lines.items() if n > SMALL_FILE_LINES and p not in cited]


async def _call(prompt: str, schema, *, budget_label: str = "", **kwargs):
    """Review's call function under one budget label, `explain`, whatever code_facts names the task."""
    return await sdk.agent_call_structured(prompt, schema, budget_label="explain", **kwargs)


def _explained(answers: dict[str, dict], keep, scope: Scope) -> ExplainedFacts:
    g = gate_answers(answers, keep)
    by_id = {f.id: f for f in g.facts}
    files = []
    for path, purpose in g.purposes.items():
        cite = next((f.citations[0] for f in g.facts if f.source.endswith("/file") and f.citations[0].path == path),
                    None)
        files.append(FilePurpose(path=path, purpose=purpose, citation=cite))
    return ExplainedFacts(
        facts=g.facts, files=files,
        interfaces=[by_id[i] for i in g.interfaces], gotchas=[by_id[i] for i in g.gotchas],
        conventions=[by_id[i] for i in g.conventions], terms=g.terms,
        uncovered=uncovered(scope, g.facts), dropped=g.dropped,
    )


async def run_explain(state: ReviewState, ctx: RunContext) -> ReviewState:
    if state.past("explain") or not ctx.config.explain:
        return state
    t, cfg = state.target, ctx.config
    scope = scope_for(t, ctx.diff, ctx.snapshot)
    base = base_dir(ctx)
    base_paths = sorted({s.path for s in scope.spans if s.side == "base"})
    base_error = ""
    if base_paths:
        try:
            target_mod.snapshot_base(t, base, base_paths)
        except target_mod.TargetError as e:
            # The head side is still explained; the lines the change removed are left out, and said so.
            base_error = f"explain base: {e}; the lines the change removed were not explained"
            scope = Scope(spans=[s for s in scope.spans if s.side != "base"])
    units = partition(scope, UNIT_LINES)
    name = t.label or t.slug

    def prompt_for(unit, missing: list[str] | None = None) -> str:
        has_base = any(s.side == "base" for s in unit.spans)
        return explore_unit(name, unit.spans, ExploreOutput, "standard", missing, path_rule=PATH_RULE,
                            base_dir=str(base) if has_base else "")

    ask = dict(
        call_kwargs=dict(allowed_tools=sdk.READ_ONLY_TOOLS, model=cfg.finder_model, effort=cfg.effort,
                         max_turns=cfg.max_turns, cwd=str(ctx.snapshot)),
        errors=[], recoverable=(sdk.AgentCallError,), fatal=(sdk.RepoTrustError,),
        progress=ctx.progress.line if ctx.progress else None,
    )
    keep = keeper(t, ctx)
    try:
        tasks = [(unit_key(u), prompt_for(u)) for u in units]
        await run_tasks(tasks, _call, state.explain_answers, concurrency=cfg.concurrency, **ask)
        explained = _explained(state.explain_answers, keep, scope)
        retry = reask_units(units, set(explained.uncovered), state.explain_answers, prompt_for)
        if retry:
            if ctx.progress:
                ctx.progress.line(f"  explain: re-asking {len(retry)} unit(s) for files with no fact")
            await run_tasks(retry, _call, state.explain_answers, concurrency=cfg.concurrency, **ask)
            explained = _explained(state.explain_answers, keep, scope)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    state.facts = explained
    if ctx.target_dir is not None:
        write_facts_file(t, explained, ctx.target_dir)
    # From the stored answers, not this run's calls, so a resumed stage reports every failed unit once.
    failed = [(k, a["error"]) for k, a in state.explain_answers.items() if "error" in a]
    state.errors = [e for e in state.errors if not e.startswith("explain ")]
    if base_error:
        state.errors.append(base_error)
    state.errors.extend(f"explain {k}: {err}" for k, err in failed)
    ctx.counts = {"units": len(units), "facts": len(explained.facts), "dropped": len(explained.dropped),
                  "uncovered_files": len(explained.uncovered), "failed_units": len(failed)}
    state.stage = "explain"
    return state
