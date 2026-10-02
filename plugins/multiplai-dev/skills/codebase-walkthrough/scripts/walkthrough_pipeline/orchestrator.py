"""target → repos → symbols → references → boundary → partition → explore → docs → trace → write → render.

`walkthrough-state.json` is written after every stage; `resume` reloads it and
continues from the first stage not yet done. Stages up to `partition` are
code only; the four agent stages store each answer as it returns, so a budget
stop keeps paid work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from multiplai_core.log_utils import log_event

from . import budget, docs as docs_mod, lsp, references, repos as repos_mod, symbols as symbols_mod
from . import target as target_mod
from .config import WalkConfig
from .gates import boundary_gate, partition_gate, target_files_ws
from .models import Dropped, WalkState
from .partition import partition, ws_lines
from .progress import ProgressWriter
from .render import write_outputs
from .stages import RunContext
from .stages.docs import run_docs
from .stages.explore import run_explore
from .stages.trace import run_trace
from .stages.write import run_write
from .state import STATE_FILE, load_state, save_state

log = logging.getLogger(__name__)

# Which rule fired, never the reason text (a reason can quote code).
_REASON_KINDS = ("quote not at cited lines", "path not at commit", "empty quote", "path names no known repo",
                 "doc quote not in the page", "url not in llms.txt", "doc quote too short",
                 "url is not a fetched docs page", "not an outbound API path")


def reason_kind(reason: str) -> str:
    return next((k for k in _REASON_KINDS if k in reason), "other")


class WalkError(Exception):
    """Bad input, an unresolvable target, or a stop-and-ask gate. Exit code 1."""


@dataclass
class RunOptions:
    target: str
    depth: str = "standard"
    mode: str = "human"
    name: str | None = None
    output: str = "."
    search_root: str | None = None
    repos: list[str] = field(default_factory=list)
    docs: str | None = None
    docs_dirs: list[str] = field(default_factory=list)
    docs_pages: int = 30
    api_collection: str | None = None
    unit_lines: int = 6000
    scenarios: int = 6
    lsp_timeout: float = 300.0


def prepare(opts: RunOptions, runs_dir: Path) -> tuple[WalkState, Path]:
    """Resolve, snapshot and gate the target; create the run directory. Raises WalkError."""
    try:
        root, sub, sha = target_mod.resolve_target(opts.target)
    except target_mod.TargetError as e:
        raise WalkError(str(e)) from e
    slug = target_mod.sanitize_slug(f"{root.name}--{sub or 'root'}--{sha[:12]}--{opts.mode}")
    run_dir = runs_dir / slug
    run_dir.mkdir(parents=True, exist_ok=True)
    for stale in (STATE_FILE, "progress.log", "boundary.json"):
        (run_dir / stale).unlink(missing_ok=True)
    try:
        info, repo = target_mod.build_target(opts.target, run_dir, name=opts.name, depth=opts.depth)
    except target_mod.TargetError as e:
        raise WalkError(str(e)) from e
    gate = target_mod.target_gate(info)
    if not gate.passed:
        raise WalkError(f"{opts.target}: {gate.reason}")
    state = WalkState(target=info, options=asdict(opts), repos={repo.key: repo})
    return state, run_dir


def _boundary_json(state: WalkState, dropped: list[dict]) -> dict:
    return {
        "target": {"repo": state.target.repo_key, "subpath": state.target.subpath, "sha": state.target.head_sha,
                   "package": state.target.package},
        "repos": {k: {"path": r.path, "sha": r.head_sha} for k, r in state.repos.items()},
        "searched_repos": state.searched_repos,
        "symbols": [e.model_dump() for e in state.symbols],
        "references": [r.model_dump() for r in state.references],
        "lsp": state.lsp,
        "dropped": dropped,
        "warnings": state.warnings,
    }


async def _code_stage(name: str, state: WalkState, run_dir: Path, ctx: RunContext) -> WalkState:
    opts = state.options
    t = state.target
    agent_mode = opts.get("mode") == "agent"
    target_repo = state.repos[t.repo_key]
    if name == "repos":
        if agent_mode:
            state.searched_repos = [t.repo_key]
        else:
            found, root, warnings = repos_mod.discover(target_repo, search_root=opts.get("search_root"),
                                                       repos=opts.get("repos") or None)
            state.repos = found
            state.searched_repos = sorted(found)
            state.warnings += warnings
            opts["search_root_resolved"] = str(root)
        ctx.repos = state.repos
        ctx.counts = {"repos": len(state.searched_repos)}
    elif name == "symbols":
        snap = ctx.snap_root / t.repo_key
        entries, warnings = symbols_mod.collect(t, snap)
        names = {e.name for e in entries if e.kind == "setting"}
        entries += symbols_mod.setting_definitions(t, snap, names)
        state.symbols = entries
        state.warnings += warnings
        ctx.counts = {"defines": sum(1 for e in entries if e.direction == "defines"),
                      "calls": sum(1 for e in entries if e.direction == "calls"),
                      "api_paths": len(symbols_mod.outbound_api_paths(entries))}
    elif name == "references":
        if agent_mode:
            state.references = []
        else:
            refs = await asyncio.to_thread(references.scan_all, state.repos, t, state.symbols, run_dir / "refscan")
            lsp_refs, report = await lsp.find_references(t, ctx.snap_root / t.repo_key, state.symbols,
                                                         timeout_s=float(opts.get("lsp_timeout") or 0))
            state.lsp = report
            if report["status"].startswith("timed out"):
                state.warnings.append(f"pyright references {report['status']}: kept the ast results "
                                      f"({report['answered']} of {report['symbols']} symbols answered)")
            state.references = references.merge_lsp(refs, lsp_refs)
        ctx.counts = {"references": len(state.references),
                      "repos_with_references": len({r.repo for r in state.references})}
    elif name == "boundary":
        kept_s, kept_r, dropped = boundary_gate(state.repos, state.symbols, state.references)
        state.symbols, state.references = kept_s, kept_r
        state.dropped += [Dropped(stage="boundary", what=f"{d['entry']['path']}:{d['entry']['line']}",
                                  reason=d["reason"]) for d in dropped]
        ctx.gate_reasons += [d["reason"] for d in dropped]
        path = run_dir / "boundary.json"
        path.write_text(json.dumps(_boundary_json(state, dropped), indent=1), encoding="utf-8")
        state.boundary_path = str(path)
        for key in sorted({r.repo for r in state.references} - {t.repo_key}):
            await asyncio.to_thread(target_mod.snapshot_repo, state.repos[key], ctx.snap_root / key)
        ctx.counts = {"symbols": len(kept_s), "references": len(kept_r), "dropped": len(dropped)}
    elif name == "partition":
        limit = int(opts.get("unit_lines") or 6000)
        units = partition(t, limit)
        gate = partition_gate(units, target_files_ws(t), ws_lines(t), limit)
        if not gate.passed:
            raise WalkError(f"partition gate failed: {gate.reason}")
        state.units = units
        oversize = [u.files[0] for u in units if u.oversize]
        if oversize:
            state.warnings.append(f"{len(oversize)} file(s) over --unit-lines went alone: {', '.join(oversize)}")
        ctx.counts = {"units": len(units), "oversize": len(oversize)}
    state.stage = name
    return state


STAGES = (
    ("repos", None), ("symbols", None), ("references", None), ("boundary", None), ("partition", None),
    ("explore", run_explore), ("docs", run_docs), ("trace", run_trace), ("write", run_write),
)


async def run_state(state: WalkState, run_dir: Path, config: WalkConfig, *, session_id: str = "") -> list[Path]:
    """Run every stage not yet done; return the files written."""
    try:
        target_mod.require_rg()
    except target_mod.MissingToolError as e:
        raise WalkError(str(e)) from e
    t = state.target
    ledger = budget.start(config.max_cost_usd, state.budget)
    progress = ProgressWriter(run_dir / "progress.log")
    slug = run_dir.name
    if state.stage == "target":
        progress.started(f"{t.repo_key}/{t.subpath}", t.head_sha, len(t.files))
        log_event("walkthrough", "start", f"walkthrough started for {slug}", session_id=session_id,
                  target=slug, sha=t.head_sha, files=len(t.files), mode=state.options.get("mode"))
    else:
        progress.line(f"RESUMED after {state.stage}")
    snap_root = run_dir / "snap"
    for key in {t.repo_key} | {r.repo for r in state.references}:
        if key in state.repos:
            await asyncio.to_thread(target_mod.snapshot_repo, state.repos[key], snap_root / key)
    ctx = RunContext(config=config, run_dir=run_dir, snap_root=snap_root, repos=state.repos, progress=progress,
                     session_id=session_id)
    save_state(state, run_dir)

    for name, fn in STAGES:
        if state.past(name):
            continue
        progress.stage(name, "started")
        ctx.counts, ctx.gate_reasons = {}, []
        try:
            state = await (fn(state, ctx) if fn else _code_stage(name, state, run_dir, ctx))
        except budget.BudgetExceededError as e:
            state.budget = ledger.to_state()
            save_state(state, run_dir)
            progress.failed(str(e))
            log_event("walkthrough", "budget_stop", str(e), session_id=session_id, level="WARNING",
                      target=slug, cost_usd=round(e.cost_usd, 4), stage=name)
            raise
        except docs_mod.DocsError as e:
            state.budget = ledger.to_state()
            save_state(state, run_dir)
            progress.failed(f"docs gate: {e}")
            raise WalkError(f"STOP (docs gate): {e}. The run is saved. Resume with a new decision: "
                            f"`resume {run_dir} --docs <url>` to use other docs, or `--no-docs` to go on "
                            f"without them.") from e
        state.budget = ledger.to_state()
        save_state(state, run_dir)
        summary = f"{name} done: " + (", ".join(f"{v} {k.replace('_', ' ')}" for k, v in ctx.counts.items())
                                       or "nothing to do")
        progress.stage(name, summary)
        print(f"{slug}: {summary}", flush=True)
        log_event("walkthrough", "stage", summary, session_id=session_id, target=slug, stage=name,
                  counts=dict(ctx.counts), cost_usd=round(ledger.cost_usd, 4))
        if ctx.gate_reasons:
            log_event("walkthrough", "gate_reject", f"{len(ctx.gate_reasons)} items rejected by gates",
                      session_id=session_id, target=slug, stage=name, count=len(ctx.gate_reasons),
                      reasons=sorted({reason_kind(r) for r in ctx.gate_reasons}))

    if not state.past("render"):
        state.budget = ledger.to_state()
        out_dir = Path(state.options.get("output") or run_dir)
        state.outputs = [str(p) for p in write_outputs(state, out_dir)]
        state.stage = "render"
        save_state(state, run_dir)
    state.stage = "done"
    save_state(state, run_dir)
    shutil.rmtree(snap_root, ignore_errors=True)
    shutil.rmtree(run_dir / "refscan", ignore_errors=True)

    covered = len(t.files) - len([f for f in target_files_ws(t) if f not in {c.path for x in state.facts
                                                                            for c in x.citations}])
    message = (f"walkthrough finished: {len(state.facts)} facts, {covered}/{len(t.files)} files with facts, "
               f"{len(state.scenarios)} scenarios")
    progress.done(f"{message} (${ledger.cost_usd:.2f})")
    print(f"{slug}: {message}; run dir {run_dir}", flush=True)
    log_event("walkthrough", "done", message, session_id=session_id, target=slug, facts=len(state.facts),
              files_covered=covered, files=len(t.files), cost_usd=round(ledger.cost_usd, 4))
    return [Path(p) for p in state.outputs]


NO_CHANGE = object()


async def resume(run_dir: Path, config: WalkConfig, *, session_id: str = "",
                 docs: str | None | object = NO_CHANGE) -> list[Path]:
    """Continue a saved run. *docs* replaces the saved `--docs` URL (None drops the docs)."""
    state = load_state(run_dir / STATE_FILE)
    if state is None:
        raise WalkError(f"no readable {STATE_FILE} in {run_dir}")
    if docs is not NO_CHANGE:
        if state.past("docs"):
            raise WalkError("the docs stage of this run is already done; --docs/--no-docs can no longer change it")
        state.options["docs"] = docs
        state.docs = {}
    return await run_state(state, run_dir, config, session_id=session_id)
