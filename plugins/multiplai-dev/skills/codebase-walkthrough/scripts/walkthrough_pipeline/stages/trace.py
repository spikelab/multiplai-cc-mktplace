"""trace: the paths that reach the module, found by `code_facts.map_paths`.

`map_paths` discovers the entry points of the module's repository (CLI
commands, scripts, skill commands, hooks), traces each one with a read-only
agent, gates every hop and ranks the paths. The walkthrough passes the whole
module as the changed lines, so every entry that reaches it is kept, up to
`--scenarios` paths. Answers are kept under `<run dir>/paths-cache/`, so a
resumed run asks only for the ones it does not have.
"""

from __future__ import annotations

import logging
import posixpath
from pathlib import Path

import code_facts

from .. import sdk
from ..models import WalkState
from . import RunContext

log = logging.getLogger(__name__)

# The most entries traced; past it, code_facts drops them as `over_trace_limit`.
TRACE_LIMIT = 30


def module_lines(state: WalkState, root: Path) -> dict[str, set[int]]:
    """Every line of every explained file, keyed by its repo-relative path, counted in the snapshot.

    Empty files are left out. Never None: an all-empty module is still a scoped run.
    """
    out: dict[str, set[int]] = {}
    for rel in state.target.files:
        try:
            data = (root / rel).read_bytes()
        except OSError:
            continue
        n = data.count(b"\n") + (0 if not data or data.endswith(b"\n") else 1)
        if n:
            out[rel] = set(range(1, n + 1))
    return out


def _resolver(state: WalkState) -> code_facts.Resolve:
    """`resolve(path, side)` for the repo-relative paths `map_paths` cites: the target repo at its commit."""
    repo = state.repos[state.target.repo_key]

    def resolve(path: str, side: str = "head") -> tuple[str, str, str] | None:
        rel = posixpath.normpath(path.strip()) if path.strip() else ""
        if not rel or rel.startswith(("/", "../")) or rel in (".", ".."):
            return None
        return repo.path, repo.head_sha, rel
    return resolve


def _trace_call(ctx: RunContext, root: Path) -> code_facts.CallFn:
    """The model call for `map_paths`: the walkthrough's sdk, read-only, on the trace model, in the snapshot."""
    cfg = ctx.config

    async def call(prompt: str, schema, **_kw):
        return await sdk.agent_call_structured(
            prompt, schema, allowed_tools=sdk.TRACE_TOOLS, model=cfg.trace_model, effort=cfg.effort,
            max_turns=cfg.max_turns, cwd=str(root), budget_label="trace")
    return call


async def run_trace(state: WalkState, ctx: RunContext) -> WalkState:
    if state.past("trace"):
        return state
    if state.options.get("mode") == "agent":
        state.stage = "trace"
        return state
    root = ctx.snap_root / state.target.repo_key
    result = await code_facts.map_paths(
        root=root,
        commit=state.target.head_sha,
        resolve=_resolver(state),
        call=_trace_call(ctx, root),
        changed_lines=module_lines(state, root),
        seeds=[],
        places=[],
        cap=int(state.options.get("scenarios") or 21),
        trace_limit=TRACE_LIMIT,
        concurrency=ctx.config.concurrency,
        cache_dir=ctx.run_dir / "paths-cache",
        recoverable=(sdk.AgentCallError,),
        progress=ctx.progress.line if ctx.progress else None,
    )
    state.paths = result
    ctx.counts = {"paths": len(result.paths),
                  "cut_short": sum(1 for p in result.paths if p.stops_at is not None)}
    state.stage = "trace"
    return state
