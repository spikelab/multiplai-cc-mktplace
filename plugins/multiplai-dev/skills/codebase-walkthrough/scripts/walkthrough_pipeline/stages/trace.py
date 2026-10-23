"""trace: the paths that reach the module, found by `code_facts.map_paths`.

`map_paths` discovers the entry points of the module's repository (CLI
commands, scripts, skill commands, hooks), traces each one with a read-only
agent, gates every hop and ranks the paths. The walkthrough passes the whole
module as the changed lines, so every entry that reaches it is kept, up to
`--scenarios` paths; for a change (`--scope pr`) it passes the lines the change
adds, so the paths kept are the ones through the change. Answers are kept under `<run dir>/paths-cache/`, so a
resumed run asks only for the ones it does not have.

Web entries are not found by `map_paths`; the boundary stage found them, and
they are passed as caller seeds in the old seed order: webhook routes, routes
called from other repositories, Celery beat entries, then the other routes.
Each is seeded at its handler's `def`, or at its registration line when the
handler cannot be named. The boundary edges (routes, tasks, the target side
of cross-repository references) are the gate's known places.

Failures. An agent failure on one entry is a drop that `map_paths` reports.
Any other failure of `map_paths` is recorded in `state.paths_error` and
`state.errors`, and the walkthrough is still written. A trust or budget stop
halts the run: every model call goes through `_trace_call`, whose latch keeps
the first such stop and raises it again on every later call, and `run_trace`
raises it whatever `map_paths` made of it.
"""

from __future__ import annotations

import logging
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

import code_facts
from code_facts import Location, Seed

from .. import budget, sdk
from ..models import STAGES, Reference, SymbolEntry, WalkState
from ..references import full_routes
from ..render import left_out_count
from . import RunContext

log = logging.getLogger(__name__)

# The most entries traced; past it, code_facts drops them as `over_trace_limit`.
TRACE_LIMIT = 30
# `map_paths`' answer cache, under the run directory. Only `resume` reuses it; `run` starts it empty.
PATHS_CACHE = "paths-cache"
# The path cap when `--scenarios` was not passed, and that default before it became 21.
DEFAULT_CAP = 21
OLD_DEFAULT_CAP = 6


def path_cap(options: dict) -> int:
    """The saved `--scenarios` cap when the user passed it, else DEFAULT_CAP.

    A state saved before `scenarios_given` existed does not say whether the flag was passed: its cap counts
    as passed unless it is the old default.
    """
    saved = options.get("scenarios")
    given = options.get("scenarios_given")
    if given is None:
        given = saved is not None and saved != OLD_DEFAULT_CAP
    return int(saved) if given and saved else DEFAULT_CAP


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


def change_lines(state: WalkState) -> dict[str, set[int]]:
    """A change's added lines per explained file at head: the paths kept are those that pass through them."""
    added = state.target.change.added()
    return {f: added[f] for f in state.target.files if added.get(f)}


def _rel(state: WalkState, ws: str) -> str | None:
    """The repo-relative form of a workspace path in the target repo, else None."""
    key, _, rel = ws.partition("/")
    return rel if key == state.target.repo_key and rel else None


def _route_name(route: str) -> str:
    return "HTTP /" + route.lstrip("/")


def _view_handler(state: WalkState, url: SymbolEntry) -> tuple[str, str] | None:
    """(repo-relative file, name) of the function or class a route's view names, when exactly one matches."""
    dotted = re.match(r"\s*([A-Za-z_][\w.]*)", url.detail.get("view", ""))
    if not dotted or dotted.group(1) in ("include", "path", "re_path"):
        return None
    parts = [p for p in dotted.group(1).split(".") if p not in ("as_view", "view")]
    if not parts:
        return None
    name, qualifier = parts[-1], parts[-2] if len(parts) > 1 else ""
    found = [
        e
        for e in state.symbols
        if e.kind in ("function", "class") and e.name == name and _rel(state, e.path)
    ]
    if qualifier:
        found = [
            e
            for e in found
            if posixpath.splitext(posixpath.basename(e.path))[0] == qualifier
        ] or found
    return (_rel(state, found[0].path), name) if len(found) == 1 else None


def _route_seed(state: WalkState, url: SymbolEntry, name: str) -> Seed | None:
    """A route's seed at its handler, else at its registration line."""
    handler = _view_handler(state, url)
    if handler:
        return Seed(path=handler[0], symbol=handler[1], name=name)
    rel = _rel(state, url.path)
    return Seed(path=rel, line=url.line, name=name) if rel else None


def _matching_route(state: WalkState, route: str) -> SymbolEntry | None:
    """The target's url entry a full route (with its mount prefix) names.

    The boundary stage names a called route by its key in `full_routes`, so that table is asked first. Else
    the longest url whose segments end the route: a bare suffix match would take `status/` for `items/status/`.
    """
    found = full_routes(state.symbols, state.references).get(route.lstrip("/"))
    if found is not None:
        return found
    want = route.strip("/")
    urls = [e for e in state.symbols if e.kind == "url" and e.name.strip("/")]
    ends = [
        e
        for e in urls
        if want == e.name.strip("/") or want.endswith("/" + e.name.strip("/"))
    ]
    return max(ends, key=lambda e: len(e.name.strip("/")), default=None)


def _task(state: WalkState, task: str) -> SymbolEntry | None:
    return next(
        (e for e in state.symbols if e.kind == "celery-task" and e.name == task), None
    )


def web_seeds(state: WalkState) -> list[Seed]:
    """Caller seeds for the web entries the boundary stage found, defined in the target repo only."""
    urls = [e for e in state.symbols if e.kind == "url"]
    seeds: list[Seed] = []
    taken: set[str] = set()

    def add(key: str, seed: Seed | None) -> None:
        if seed is not None and key not in taken:
            taken.add(key)
            seeds.append(seed)

    for e in urls:
        hay = (
            e.name + " " + e.detail.get("view", "") + " " + e.detail.get("url_name", "")
        ).lower()
        if "webhook" in hay or "wbhk" in hay:
            add("route:" + e.name, _route_seed(state, e, _route_name(e.name)))
    for r in state.references:
        if r.kind == "http-route" and r.repo != state.target.repo_key:
            url = _matching_route(state, r.symbol)
            if url is not None:
                add(
                    "route:" + url.name,
                    _route_seed(state, url, f"{r.repo}: {_route_name(r.symbol)}"),
                )
    for r in state.references:
        if r.kind == "celery-name":
            task, name = _task(state, r.symbol), f"{r.symbol} (beat)"
            if (
                task is not None
                and task.detail.get("function")
                and _rel(state, task.path)
            ):
                add(
                    "task:" + r.symbol,
                    Seed(
                        path=_rel(state, task.path),
                        symbol=task.detail["function"],
                        name=name,
                    ),
                )
            elif _rel(state, r.path):
                add(
                    "task:" + r.symbol,
                    Seed(path=_rel(state, r.path), line=r.line, name=name),
                )
    for e in urls:
        add("route:" + e.name, _route_seed(state, e, _route_name(e.name)))
    return seeds


def known_places(state: WalkState) -> list[Location]:
    """The boundary edges under the target repo's root: the target side of each cross-repository
    reference, then every route, then every task and beat entry. First-seen order, no duplicates."""
    found: list[tuple[str, SymbolEntry | Reference | None]] = []
    for r in state.references:
        if r.repo == state.target.repo_key:
            continue
        if r.kind == "http-route":
            found.append((r.kind, _matching_route(state, r.symbol)))
        elif r.kind == "celery-name":
            found.append((r.kind, _task(state, r.symbol)))
    found += [(e.kind, e) for e in state.symbols if e.kind == "url"]
    found += [(e.kind, e) for e in state.symbols if e.kind == "celery-task"]
    found += [
        (r.kind, r)
        for r in state.references
        if r.kind == "celery-name" and r.repo == state.target.repo_key
    ]
    out: list[Location] = []
    for kind, e in found:
        rel = _rel(state, e.path) if e is not None else None
        if not rel:
            # Only the type: the edge's text is the repository's.
            log.info(
                "known places: left out a %s edge with no location in the target repo",
                kind,
            )
            continue
        place = Location(path=rel, line=e.line)
        if place not in out:
            out.append(place)
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


# The stops that halt the run; any other exception of a call is for map_paths to handle.
FATAL = (sdk.RepoTrustError, budget.BudgetExceededError)


@dataclass
class _FatalLatch:
    """The first trust or budget stop raised by a trace call, kept so no later call is made."""

    exc: BaseException | None = None


def _trace_call(ctx: RunContext, root: Path, latch: _FatalLatch) -> code_facts.CallFn:
    """The model call for `map_paths`: the walkthrough's sdk, read-only, on the trace model, reading only the
    target repo's snapshot."""
    cfg = ctx.config

    async def call(prompt: str, schema, **_kw):
        if latch.exc is not None:
            raise latch.exc
        try:
            return await sdk.agent_call_structured(
                prompt,
                schema,
                allowed_tools=sdk.TRACE_TOOLS,
                model=cfg.trace_model,
                effort=cfg.effort,
                max_turns=cfg.max_turns,
                cwd=str(root),
                budget_label="trace",
                read_roots=[str(root)],
            )
        except FATAL as e:
            latch.exc = e
            raise

    return call


def rewind_legacy_trace(state: WalkState) -> WalkState:
    """A human-mode state past `trace` with neither paths nor a paths error was written before path
    finding: send it back to the stage before `trace`, so a resume traces instead of rendering no paths."""
    if (
        state.options.get("mode") != "agent"
        and state.past("trace")
        and state.paths is None
        and state.paths_error is None
    ):
        state.stage = STAGES[STAGES.index("trace") - 1]
    return state


async def run_trace(state: WalkState, ctx: RunContext) -> WalkState:
    if state.past("trace"):
        return state
    if state.options.get("mode") == "agent":
        state.stage = "trace"
        return state
    root = ctx.snap_root / state.target.repo_key
    latch = _FatalLatch()
    budget.check(stage="trace")
    try:
        result = await code_facts.map_paths(
            root=root,
            commit=state.target.head_sha,
            resolve=_resolver(state),
            call=_trace_call(ctx, root, latch),
            changed_lines=change_lines(state)
            if state.target.scope == "change"
            else module_lines(state, root),
            seeds=web_seeds(state),
            places=known_places(state),
            cap=path_cap(state.options),
            trace_limit=TRACE_LIMIT,
            concurrency=ctx.config.concurrency,
            cache_dir=ctx.run_dir / PATHS_CACHE,
            recoverable=(sdk.AgentCallError,),
            progress=ctx.progress.line if ctx.progress else None,
        )
    except Exception as e:
        if latch.exc is not None:
            raise latch.exc
        if isinstance(e, FATAL):
            raise
        # Only the type is logged: a message can quote code.
        log.error("trace: map_paths failed (%s)", type(e).__name__)
        if ctx.progress:
            ctx.progress.line(f"  trace: failed ({type(e).__name__})")
        state.errors.append(f"trace: {type(e).__name__}: {e}")
        state.paths, state.paths_error = None, f"{type(e).__name__}: {e}"
        ctx.counts = {"failed": 1}
        state.stage = "trace"
        return state
    if latch.exc is not None:
        raise latch.exc
    state.paths, state.paths_error = result, None
    ctx.counts = {
        "paths": len(result.paths),
        "cut_short": sum(1 for p in result.paths if p.stops_at is not None),
        "left_out": left_out_count(result),
    }
    state.stage = "trace"
    return state
