"""target → repos → symbols → references → boundary → partition → explore → survey → core → docs → trace
→ write → page → diagram → render.

`walkthrough-state.json` is written after every stage; `resume` reloads it and
continues from the first stage not yet done. Stages up to `partition` are
code only; the agent stages store each answer as it returns, so a budget
stop keeps paid work. `survey` asks only for `--scope codebase`; `core`, `page` and `diagram` only for a
module or a codebase in human mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from multiplai_core.log_utils import log_event

from . import budget, design, lsp, references
from . import change as change_mod
from . import docs as docs_mod
from . import repos as repos_mod
from . import symbols as symbols_mod
from . import target as target_mod
from .config import WalkConfig
from .gates import boundary_gate, partition_gate, target_files_ws
from .models import STAGES as STAGES_ORDER
from .models import Dropped, RepoInfo, WalkState
from .partition import partition, ws_lines
from .progress import ProgressWriter
from .render import write_outputs
from .stages import RunContext
from .stages.core import run_core
from .stages.diagram import run_diagram
from .stages.docs import run_docs
from .stages.explore import run_explore
from .stages.page import run_page
from .stages.survey import run_survey
from .stages.trace import PATHS_CACHE, rewind_legacy_trace, run_trace
from .stages.write import run_write
from .state import STATE_FILE, load_state, save_state

log = logging.getLogger(__name__)

# Which rule fired, never the reason text (a reason can quote code).
_REASON_KINDS = (
    "quote not at cited lines",
    "path not at commit",
    "empty quote",
    "path names no known repo",
    "doc quote not in the page",
    "url not in llms.txt",
    "doc quote too short",
    "url is not a fetched docs page",
    "not an outbound API path",
)


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
    repo_key: str | None = (
        None  # the module repository's key; default its directory's name
    )
    docs: str | None = None
    docs_dirs: list[str] = field(default_factory=list)
    docs_pages: int = 30
    api_collection: str | None = None
    unit_lines: int = 6000
    scenarios: int = 21  # the path cap
    scenarios_given: bool = False  # `--scenarios` was passed; else a resume uses the default of its own version
    lsp_timeout: float = 300.0
    scope: str = (
        "module"  # "module", "codebase", or "pr" for a change (`--pr` / `--range`)
    )
    commit: str | None = (
        None  # module or codebase scope: the commit read (default HEAD)
    )
    libs: list[str] = field(
        default_factory=list
    )  # codebase scope: library directories the survey also reads
    pr: int | None = None
    range: str | None = None
    fetch: bool = False
    expect_head: str | None = None
    design_system_dir: str | None = (
        None  # `--design-system-dir`: the page's design-system folder; None is the neutral design system
    )


def _keep_previous_progress(run_dir: Path) -> None:
    """Move an earlier run's ``progress.log`` to ``progress-<UTC time>.log``.

    A new run starts a fresh ``progress.log``, so the skill's wait for its
    ``DONE`` or ``FAILED`` line never matches the earlier run's. The earlier
    log keeps its whole content under the dated name, taken from its last
    write. ``os.link`` refuses an existing name, so nothing is overwritten;
    if the dated name is taken, the log stays where it is and the new run
    appends to it.
    """
    log = run_dir / "progress.log"
    try:
        stamp = datetime.fromtimestamp(log.stat().st_mtime, UTC).strftime(
            "%Y%m%dT%H%M%SZ"
        )
        os.link(log, run_dir / f"progress-{stamp}.log")
        log.unlink()
    except OSError:
        pass


def _fresh_run_dir(runs_dir: Path, slug: str) -> Path:
    run_dir = runs_dir / slug
    run_dir.mkdir(parents=True, exist_ok=True)
    for stale in (STATE_FILE, "boundary.json"):
        (run_dir / stale).unlink(missing_ok=True)
    # The stored trace answers belong to the state just deleted: a new run asks for its own.
    shutil.rmtree(run_dir / PATHS_CACHE, ignore_errors=True)
    _keep_previous_progress(run_dir)
    return run_dir


def prepare(opts: RunOptions, runs_dir: Path) -> tuple[WalkState, Path]:
    """Resolve, snapshot and gate the target; create the run directory. Raises WalkError.

    A change is resolved (gh, merge-base, --expect-head) before the run directory exists, so a
    refusal leaves nothing behind.
    """
    try:
        # First, before the run directory or its state is touched: a bad folder costs nothing.
        design.check(opts.design_system_dir)
    except design.DesignSystemError as e:
        raise WalkError(str(e)) from e
    if opts.scope == "pr":
        return _prepare_change(opts, runs_dir)
    try:
        root, sub, sha = target_mod.resolve_target(opts.target, opts.commit)
        libs = target_mod.resolve_libs(opts.libs, root, opts.repo_key or root.name, sha)
    except target_mod.TargetError as e:
        raise WalkError(str(e)) from e
    # A codebase run has its own run directory, so it never deletes a module run's state.
    suffix = "codebase" if opts.scope == "codebase" else opts.mode
    run_dir = _fresh_run_dir(
        runs_dir,
        target_mod.sanitize_slug(f"{root.name}--{sub or 'root'}--{sha[:12]}--{suffix}"),
    )
    try:
        info, repo = target_mod.build_target(
            opts.target,
            run_dir,
            name=opts.name,
            depth=opts.depth,
            key=opts.repo_key,
            commit=opts.commit,
        )
    except target_mod.TargetError as e:
        raise WalkError(str(e)) from e
    gate = target_mod.target_gate(info)
    if not gate.passed:
        raise WalkError(f"{opts.target}: {gate.reason}")
    options = asdict(opts)
    options["libs_resolved"] = [lib.to_dict() for lib in libs]
    state = WalkState(
        target=info, options=options, repos={repo.key: repo, **lib_repos(options)}
    )
    return state, run_dir


def lib_repos(options: dict) -> dict[str, RepoInfo]:
    """The repositories of the `--lib` directories outside the target's repository, keyed as saved."""
    return {
        lib["key"]: RepoInfo(
            key=lib["key"],
            path=lib["root"],
            head_sha=lib["sha"],
            remote_url=target_mod.remote_url(lib["root"]),
        )
        for lib in options.get("libs_resolved") or []
        if not lib["in_target_repo"]
    }


def _prepare_change(opts: RunOptions, runs_dir: Path) -> tuple[WalkState, Path]:
    try:
        found = target_mod.resolve_change(
            opts.target,
            pr=opts.pr,
            range_=opts.range,
            fetch=opts.fetch,
            expect_head=opts.expect_head,
        )
    except target_mod.TargetError as e:
        raise WalkError(str(e)) from e
    run_dir = _fresh_run_dir(
        runs_dir,
        target_mod.sanitize_slug(
            f"{found.root.name}--{found.slug_part()}--{opts.mode}"
        ),
    )
    try:
        info, repo, base = target_mod.build_change_target(
            found, run_dir, name=opts.name, depth=opts.depth, key=opts.repo_key
        )
    except target_mod.TargetError as e:
        raise WalkError(str(e)) from e
    gate = target_mod.target_gate(info)
    if not gate.passed:
        raise WalkError(f"{found.ref}: {gate.reason}")
    state = WalkState(
        target=info, options=asdict(opts), repos={repo.key: repo, base.key: base}
    )
    return state, run_dir


def _boundary_json(state: WalkState, dropped: list[dict]) -> dict:
    return {
        "target": {
            "repo": state.target.repo_key,
            "subpath": state.target.subpath,
            "sha": state.target.head_sha,
            "package": state.target.package,
        },
        "repos": {
            k: {"path": r.path, "sha": r.head_sha} for k, r in state.repos.items()
        },
        "searched_repos": state.searched_repos,
        "symbols": [e.model_dump() for e in state.symbols],
        "references": [r.model_dump() for r in state.references],
        "lsp": state.lsp,
        "dropped": dropped,
        "warnings": state.warnings,
    }


async def _code_stage(
    name: str, state: WalkState, run_dir: Path, ctx: RunContext
) -> WalkState:
    opts = state.options
    t = state.target
    agent_mode = opts.get("mode") == "agent"
    target_repo = state.repos[t.repo_key]
    base_repo = state.repos.get(t.change.base_key) if t.change else None
    if name == "repos":
        if agent_mode:
            state.searched_repos = [t.repo_key]
        else:
            found, root, warnings = repos_mod.discover(
                target_repo,
                search_root=opts.get("search_root"),
                repos=opts.get("repos") or None,
            )
            state.repos = found
            if base_repo is not None:
                # The base commit is read under its own key; it is never searched for references.
                state.repos = {**found, base_repo.key: base_repo}
            # A library is read at the commit saved when the run started, under its own key.
            state.repos = {**state.repos, **lib_repos(opts)}
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
        if t.scope == "change":
            entries = change_mod.change_symbols(
                entries, t, change_mod.changed_definitions(snap, t)
            )
        state.symbols = entries
        state.warnings += warnings
        ctx.counts = {
            "defines": sum(1 for e in entries if e.direction == "defines"),
            "calls": sum(1 for e in entries if e.direction == "calls"),
            "api_paths": len(symbols_mod.outbound_api_paths(entries)),
        }
    elif name == "references":
        if agent_mode:
            state.references = []
        elif t.scope == "change":
            changed = change_mod.changed_definitions(ctx.snap_root / t.repo_key, t)
            state.references = await asyncio.to_thread(
                references.scan_callers,
                state.repos,
                t,
                changed,
            )
            state.lsp = {
                "status": "skipped (a change: callers come from git grep and ast)"
            }
        else:
            refs = await asyncio.to_thread(
                references.scan_all, state.repos, t, state.symbols, run_dir / "refscan"
            )
            lsp_refs, report = await lsp.find_references(
                t,
                ctx.snap_root / t.repo_key,
                state.symbols,
                timeout_s=float(opts.get("lsp_timeout") or 0),
            )
            state.lsp = report
            if report["status"].startswith("timed out"):
                state.warnings.append(
                    f"pyright references {report['status']}: kept the ast results "
                    f"({report['answered']} of {report['symbols']} symbols answered)"
                )
            state.references = references.merge_lsp(refs, lsp_refs)
        ctx.counts = {
            "references": len(state.references),
            "repos_with_references": len({r.repo for r in state.references}),
        }
    elif name == "boundary":
        kept_s, kept_r, dropped = boundary_gate(
            state.repos, state.symbols, state.references
        )
        state.symbols, state.references = kept_s, kept_r
        state.dropped += [
            Dropped(
                stage="boundary",
                what=f"{d['entry']['path']}:{d['entry']['line']}",
                reason=d["reason"],
            )
            for d in dropped
        ]
        ctx.gate_reasons += [d["reason"] for d in dropped]
        path = run_dir / "boundary.json"
        path.write_text(
            json.dumps(_boundary_json(state, dropped), indent=1), encoding="utf-8"
        )
        state.boundary_path = str(path)
        for key in sorted(
            {r.repo for r in state.references}
            - {t.repo_key}
            - {t.change.base_key if t.change else ""}
        ):
            await asyncio.to_thread(
                target_mod.snapshot_repo, state.repos[key], ctx.snap_root / key
            )
        ctx.counts = {
            "symbols": len(kept_s),
            "references": len(kept_r),
            "dropped": len(dropped),
        }
    elif name == "partition":
        limit = int(opts.get("unit_lines") or 6000)
        if t.scope == "change":
            spans = change_mod.change_spans(
                t, change_mod.base_line_counts(ctx.snap_root, t)
            )
            units = change_mod.change_units(spans, limit)
            totals = change_mod.unit_line_totals(units)
            gate = partition_gate(units, list(totals), totals, limit)
        else:
            units = partition(t, limit)
            gate = partition_gate(units, target_files_ws(t), ws_lines(t), limit)
        if not gate.passed:
            raise WalkError(f"partition gate failed: {gate.reason}")
        state.units = units
        oversize = [u.files[0] for u in units if u.oversize]
        if oversize:
            state.warnings.append(
                f"{len(oversize)} file(s) over --unit-lines went alone: {', '.join(oversize)}"
            )
        ctx.counts = {"units": len(units), "oversize": len(oversize)}
    state.stage = name
    return state


STAGES = (
    ("repos", None),
    ("symbols", None),
    ("references", None),
    ("boundary", None),
    ("partition", None),
    ("explore", run_explore),
    ("survey", run_survey),
    ("core", run_core),
    ("docs", run_docs),
    ("trace", run_trace),
    ("write", run_write),
    ("page", run_page),
    ("diagram", run_diagram),
)


async def run_state(
    state: WalkState, run_dir: Path, config: WalkConfig, *, session_id: str = ""
) -> list[Path]:
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
        log_event(
            "walkthrough",
            "start",
            f"walkthrough started for {slug}",
            session_id=session_id,
            target=slug,
            sha=t.head_sha,
            files=len(t.files),
            mode=state.options.get("mode"),
        )
    else:
        progress.line(f"RESUMED after {state.stage}")
    snap_root = run_dir / "snap"
    try:
        for key in (
            {t.repo_key}
            | {r.repo for r in state.references}
            | set(lib_repos(state.options))
        ):
            if key in state.repos:
                await asyncio.to_thread(
                    target_mod.snapshot_repo, state.repos[key], snap_root / key
                )
        if t.change and t.change.base_key in state.repos:
            await asyncio.to_thread(
                target_mod.snapshot_paths,
                state.repos[t.change.base_key],
                snap_root / t.change.base_key,
                change_mod.base_paths(t),
            )
    except target_mod.TargetError as e:
        raise WalkError(f"cannot read a commit this run needs: {e}") from e
    ctx = RunContext(
        config=config,
        run_dir=run_dir,
        snap_root=snap_root,
        repos=state.repos,
        progress=progress,
        session_id=session_id,
    )
    save_state(state, run_dir)

    for name, fn in STAGES:
        if state.past(name):
            continue
        progress.stage(name, "started")
        ctx.counts, ctx.gate_reasons = {}, []
        try:
            state = await (
                fn(state, ctx) if fn else _code_stage(name, state, run_dir, ctx)
            )
        except budget.BudgetExceededError as e:
            state.budget = ledger.to_state()
            save_state(state, run_dir)
            progress.failed(str(e))
            log_event(
                "walkthrough",
                "budget_stop",
                str(e),
                session_id=session_id,
                level="WARNING",
                target=slug,
                cost_usd=round(e.cost_usd, 4),
                stage=name,
            )
            raise
        except docs_mod.DocsError as e:
            state.budget = ledger.to_state()
            save_state(state, run_dir)
            progress.failed(f"docs gate: {e}")
            raise WalkError(
                f"STOP (docs gate): {e}. The run is saved. Resume with a new decision: "
                f"`resume {run_dir} --docs <url>` to use other docs, or `--no-docs` to go on "
                f"without them."
            ) from e
        state.budget = ledger.to_state()
        save_state(state, run_dir)
        summary = f"{name} done: " + (
            ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in ctx.counts.items())
            or "nothing to do"
        )
        progress.stage(name, summary)
        print(f"{slug}: {summary}", flush=True)
        log_event(
            "walkthrough",
            "stage",
            summary,
            session_id=session_id,
            target=slug,
            stage=name,
            counts=dict(ctx.counts),
            cost_usd=round(ledger.cost_usd, 4),
        )
        if ctx.gate_reasons:
            log_event(
                "walkthrough",
                "gate_reject",
                f"{len(ctx.gate_reasons)} items rejected by gates",
                session_id=session_id,
                target=slug,
                stage=name,
                count=len(ctx.gate_reasons),
                reasons=sorted({reason_kind(r) for r in ctx.gate_reasons}),
            )

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

    covered = len(t.files) - len(
        [
            f
            for f in target_files_ws(t)
            if f not in {c.path for x in state.facts for c in x.citations}
        ]
    )
    message = (
        f"walkthrough finished: {len(state.facts)} facts, {covered}/{len(t.files)} files with facts, "
        f"{len(state.paths.paths) if state.paths else 0} paths"
    )
    progress.done(f"{message} (${ledger.cost_usd:.2f})")
    print(f"{slug}: {message}; run dir {run_dir}", flush=True)
    log_event(
        "walkthrough",
        "done",
        message,
        session_id=session_id,
        target=slug,
        facts=len(state.facts),
        files_covered=covered,
        files=len(t.files),
        cost_usd=round(ledger.cost_usd, 4),
    )
    return [Path(p) for p in state.outputs]


NO_CHANGE = object()


def rewind_for_rewrite(state: WalkState, run_dir: Path) -> WalkState:
    """`resume --rewrite`: send a run past its trace stage back to the stage before `write`, dropping what
    the write stage made (its sections, its answers, its entries in `dropped`), the page stage's answers
    and page, and the diagram stage's answers and picture (the flow it drew is written again). The explore,
    survey, core, docs and trace answers are kept, so only the write, page, diagram and render stages run
    again. Raises WalkError, and changes
    nothing, when the run has not finished its trace stage."""
    if not state.past("trace"):
        raise WalkError(f"resume --rewrite: {run_dir} has not finished its trace stage")
    state.stage = STAGES_ORDER[STAGES_ORDER.index("write") - 1]
    state.sections = {}
    state.write_answers = {}
    state.page_answers = {}
    state.page = {}
    state.diagram_answers = {}
    state.diagram = {}
    state.dropped = [d for d in state.dropped if not d.stage.startswith("write")]
    return state


def rewind_for_repage(state: WalkState, run_dir: Path) -> WalkState:
    """`resume --repage`: send a finished run back to the stage before `page`, dropping the page stage's
    answers and page. Every other answer is kept, the diagram stage's too (the flow it drew does not change),
    so only the page, diagram and render stages run again: one page call, and one more when the page fails
    its checks; the diagram stage reuses its stored answers and makes no call. Raises WalkError, and changes nothing, when the run
    has not finished its write stage."""
    if not state.past("write"):
        raise WalkError(f"resume --repage: {run_dir} has not finished its write stage")
    state.stage = STAGES_ORDER[STAGES_ORDER.index("page") - 1]
    state.page_answers = {}
    state.page = {}
    # The diagram stage runs again from its stored answers and records its errors again.
    state.errors = [e for e in state.errors if not e.startswith(("page:", "diagram:"))]
    return state


def rewind_for_redraw(state: WalkState, run_dir: Path) -> WalkState:
    """`resume --redraw`: send a run past its page stage back to `page`, dropping the diagram stage's answers,
    its picture and its errors, so only the diagram and render stages run again. Raises WalkError, and
    changes nothing, when the run has not finished its page stage."""
    if not state.past("page"):
        raise WalkError(f"resume --redraw: {run_dir} has not finished its page stage")
    state.stage = "page"
    state.diagram_answers = {}
    state.diagram = {}
    state.errors = [e for e in state.errors if not e.startswith("diagram:")]
    return state


async def resume(
    run_dir: Path,
    config: WalkConfig,
    *,
    session_id: str = "",
    docs: str | None | object = NO_CHANGE,
    rewrite: bool = False,
    repage: bool = False,
    redraw: bool = False,
    design_system_dir: str | None = None,
) -> list[Path]:
    """Continue a saved run. *docs* replaces the saved `--docs` URL (None drops the docs). *rewrite* runs the
    write and render stages again on a run past its trace stage (`rewind_for_rewrite`); *repage* runs the page
    and render stages again on a run past its write stage (`rewind_for_repage`); *redraw* runs the diagram and
    render stages again on a run past its page stage (`rewind_for_redraw`). *design_system_dir*
    replaces the saved `--design-system-dir`; the folder in use is checked before anything else."""
    state = load_state(run_dir / STATE_FILE)
    if state is None:
        raise WalkError(f"no readable {STATE_FILE} in {run_dir}")
    if design_system_dir is not None:
        state.options["design_system_dir"] = design_system_dir
    try:
        design.check(state.options.get("design_system_dir"))
    except design.DesignSystemError as e:
        raise WalkError(str(e)) from e
    if docs is not NO_CHANGE:
        if state.past("docs"):
            raise WalkError(
                "the docs stage of this run is already done; --docs/--no-docs can no longer change it"
            )
        state.options["docs"] = docs
        state.docs = {}
    if rewrite:
        # Saved at once: a stop before the first write call leaves a run a plain `resume` finishes.
        save_state(rewind_for_rewrite(state, run_dir), run_dir)
    elif repage:
        save_state(rewind_for_repage(state, run_dir), run_dir)
    elif redraw:
        save_state(rewind_for_redraw(state, run_dir), run_dir)
    rewind_legacy_trace(state)
    return await run_state(state, run_dir, config, session_id=session_id)
