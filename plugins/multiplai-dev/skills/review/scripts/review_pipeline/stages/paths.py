"""paths: the code paths through the change, from each entry point, mapped before find.

Runs on every change review, and on a `--tree` or `--dir` review only with
`--paths`. It hands the lines under review (a change's added lines at head; a
tree's every line) to `code_facts.map_paths` with a cap of 21 paths, and
writes the result to `paths.json` beside `findings.json` with
`code_facts.write_paths_file`, so the file always validates against
`paths.v1`. The path finders, path ids on findings and the report's Paths
section read it back through `paths_io.load_paths`.

Calls go through review's `sdk.agent_call_structured` under one budget label,
`paths`, so they count towards the review's spend limit. `map_paths` keeps
every answer under `<target>/paths-cache/paths-answers/` as it returns, so a
budget stop keeps the paid answers and `resume` asks only for the rest.

What the mapping could not do is recorded with `checks.add_coverage`: scoping
drops as `info`, a failed trace, a cut trace or a failed model fallback as an
`error`. Any other failure of the mapping is recorded and the review goes on
without paths; a trust stop, a hit usage limit or a budget stop ends the run,
as in every stage.
"""

from __future__ import annotations

import logging

from code_facts import map_paths, write_paths_file

from .. import budget, sdk
from ..checks import add_coverage
from ..chunks import head_changed_lines
from ..facts import resolver
from ..models import CoverageRecord, ReviewState
from ..paths_io import PATHS_FILE, classify_broken, classify_drop
from ..rounds import PATHS_CACHE
from . import RunContext

log = logging.getLogger(__name__)

# At most this many paths are kept, the ones covering the most changed lines first.
PATH_CAP = 21
# At most this many entries reaching changed code are traced (the cap applies after tracing).
TRACE_LIMIT = 30
BUDGET_LABEL = "paths"


class _CallFailed(Exception):
    """A mapping call that failed and that `map_paths` records as a drop.

    `map_paths` treats every exception in `recoverable` as a failed call, and
    `sdk.RepoTrustError` and `sdk.UsageLimitError` are `sdk.AgentCallError`s,
    so passing `AgentCallError` would turn a trust stop or a hit usage limit
    into a dropped entry. The call function raises this instead for every
    other agent failure.
    """


async def _call(prompt: str, schema, *, budget_label: str = "", **kwargs):
    """Review's call function under one budget label, `paths`, whatever code_facts names the task."""
    try:
        return await sdk.agent_call_structured(
            prompt, schema, budget_label=BUDGET_LABEL, **kwargs
        )
    except (sdk.RepoTrustError, sdk.UsageLimitError):
        raise
    except sdk.AgentCallError as e:
        raise _CallFailed(str(e)) from e


def _line_count(text: str) -> int:
    """Lines as git numbers them: split on `\\n` only, a last line without one counted."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def tree_lines(state: ReviewState, ctx: RunContext) -> dict[str, list[int]]:
    """Every line of every reviewed file of a tree review, read from the head snapshot."""
    out: dict[str, list[int]] = {}
    for rel in state.target.files:
        try:
            n = _line_count(
                (ctx.snapshot / rel).read_text(encoding="utf-8", errors="replace")
            )
        except OSError as e:
            log.warning("paths: cannot read %s in the snapshot: %s", rel, e)
            continue
        if n:
            out[rel] = list(range(1, n + 1))
    return out


def _empty_reason(dropped: int) -> str:
    if dropped:
        return f"no entry point's path was kept ({dropped} entries dropped)"
    return "no entry point reaches the lines under review"


async def run_paths(state: ReviewState, ctx: RunContext) -> ReviewState:
    if state.past("paths"):
        return state
    t, cfg = state.target, ctx.config
    if t.is_tree and not state.paths_enabled:
        state.paths_status = "skipped"
        ctx.counts = {"paths": 0}
        state.stage = "paths"
        return state

    changed = tree_lines(state, ctx) if t.is_tree else head_changed_lines(ctx.diff)
    cache = ctx.target_dir / PATHS_CACHE if ctx.target_dir is not None else None
    try:
        result = await map_paths(
            root=ctx.snapshot,
            commit=t.head_sha,
            resolve=resolver(t),
            call=_call,
            changed_lines=changed,
            cap=PATH_CAP,
            trace_limit=TRACE_LIMIT,
            concurrency=cfg.concurrency,
            cache_dir=cache,
            recoverable=(_CallFailed,),
            call_kwargs={
                "model": cfg.finder_model,
                "effort": cfg.effort,
                "max_turns": cfg.max_turns,
            },
            progress=ctx.progress.line if ctx.progress else None,
        )
        if ctx.target_dir is not None:
            write_paths_file(result, ctx.target_dir / PATHS_FILE)
    except (sdk.RepoTrustError, sdk.UsageLimitError, budget.BudgetExceededError):
        raise
    except (
        Exception
    ) as e:  # a failed mapping must not stop the review; find runs without paths
        log.exception("paths: mapping failed")
        message = (str(e).splitlines() or [type(e).__name__])[0][:300]
        state.paths_status, state.paths_failure, state.path_ids = "failed", message, []
        add_coverage(
            state,
            CoverageRecord(
                stage="paths",
                subject="mapping",
                level="error",
                reason="mapping-failed",
                detail=message,
            ),
        )
        ctx.counts = {"paths": 0, "failed": 1}
        state.stage = "paths"
        return state

    for drop in result.dropped:
        add_coverage(state, classify_drop(drop))
    ids: list[str] = []
    for path in result.paths:
        if (
            path.id in ids
        ):  # paths.v1 numbers paths p1..pN, so this only guards a future schema
            add_coverage(
                state,
                CoverageRecord(
                    stage="paths", subject=path.id, level="error", reason="duplicate-id"
                ),
            )
            continue
        ids.append(path.id)
        if path.stops_at is not None:
            add_coverage(state, classify_broken(path))
    if result.fallback.status == "failed":
        add_coverage(
            state,
            CoverageRecord(
                stage="paths",
                subject="fallback",
                level="error",
                reason="fallback-failed",
                detail=result.fallback.reason or "",
            ),
        )
    state.path_ids = ids
    if ids:
        state.paths_status = "ok"
    else:
        state.paths_status, state.paths_failure = (
            "empty",
            _empty_reason(len(result.dropped)),
        )
        add_coverage(
            state,
            CoverageRecord(
                stage="paths", subject="mapping", level="error", reason="mapping-empty"
            ),
        )
    ctx.counts = {
        "paths": len(ids),
        "dropped": len(result.dropped),
        "broken": sum(1 for p in result.paths if p.stops_at is not None),
    }
    state.stage = "paths"
    return state
