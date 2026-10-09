"""target → find → verify → merge → repeats → assess → export → render.

`review-state.json` is written after every stage; `resume` reloads it and
continues from the first stage not yet done.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from multiplai_core.log_utils import log_event

from . import budget, rounds, target as target_mod, timings
from .config import ReviewConfig, run_config
from .export import write_checks_file, write_findings_file
from .gates import gated_need, reason_kind
from .models import SEVERITIES, ReviewState
from .progress import ProgressWriter
from .render import summary_path, write_review, write_rollups, write_runs
from .stages import RunContext
from .stages.assess import run_assess
from .stages.find import file_chars, file_groups, finder_dimensions, run_find
from .stages.merge import run_merge
from .stages.repeats import run_repeats
from .stages.verify import run_verify
from .state import load_state, save_state

log = logging.getLogger(__name__)

STAGE_FUNCTIONS = (
    ("find", run_find),
    ("verify", run_verify),
    ("merge", run_merge),
    ("repeats", run_repeats),
    ("assess", run_assess),
)

class ReviewError(Exception):
    """Bad input or an unresolvable target. Exit code 2."""


@dataclass
class TargetSpec:
    repo: str
    branch: str | None = None
    pr: int | None = None
    range: str | None = None
    tickets: list[str] = field(default_factory=list)
    deployed_in: str | None = None
    base_branch: str | None = None
    fetch: bool = False
    tree: str | None = None  # a commit-ish: review every file at it, not a change
    path: str | None = None  # with tree: only this directory
    dir: str | None = None   # a directory not under git, copied and reviewed as a tree


def _count_line(counts: dict[str, int]) -> str:
    return ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items()) or "nothing to do"


NO_USAGE_NOTE = "budget:"


def note_missing_usage(state: ReviewState, ledger: budget.ReviewBudget) -> None:
    """One line in `state.errors` when calls returned no usage; their cost and tokens count as 0."""
    state.errors = [e for e in state.errors if not e.startswith(NO_USAGE_NOTE)]
    if ledger.no_usage_calls:
        n = ledger.no_usage_calls
        state.errors.append(f"{NO_USAGE_NOTE} {n} agent call{'' if n == 1 else 's'} returned no usage; "
                            "their cost and tokens are counted as 0")


async def run_state(state: ReviewState, target_dir: Path, config: ReviewConfig, *,
                    session_id: str = "") -> Path:
    """Run every stage not yet done; return the findings.json path."""
    t = state.target
    ledger = budget.start(config.max_cost_usd, state.budget)
    progress = ProgressWriter(target_dir / "progress.log")
    if state.stage == "target":
        progress.started(t.label or t.slug, t.base_sha, t.head_sha, len(t.files))
        log_event("review", "start", f"review started for {t.slug}", session_id=session_id,
                  target=t.slug, base=t.base_sha, head=t.head_sha, files=len(t.files))
    else:
        progress.line(f"RESUMED after {state.stage}")

    snapshot = target_mod.snapshot_head(t, target_dir / "tree")
    diff = Path(t.diff_path).read_text(encoding="utf-8") if t.diff_path else ""
    ctx = RunContext(config=config, snapshot=snapshot, diff=diff, progress=progress, session_id=session_id,
                     target_dir=target_dir)
    state.run_config = run_config(config)
    timings.open_interval(state, "run")
    save_state(state, target_dir)

    for name, fn in STAGE_FUNCTIONS:
        if state.past(name):
            continue
        progress.stage(name, "started")
        ctx.counts, ctx.gate_reasons = {}, []
        timings.open_interval(state, name)
        try:
            state = await fn(state, ctx)
        except budget.BudgetExceededError as e:
            timings.close_interval(state, name)
            timings.close_interval(state, "run")
            state.budget_stops += 1
            state.budget = ledger.to_state()
            save_state(state, target_dir)
            progress.failed(str(e))
            log_event("review", "budget_stop", str(e), session_id=session_id, level="WARNING",
                      target=t.slug, cost_usd=round(e.cost_usd, 4), stage=name)
            raise
        timings.close_interval(state, name)
        timings.close_interval(state, "run")  # moved forward at each checkpoint; see timings.py
        state.budget = ledger.to_state()
        save_state(state, target_dir)
        summary = f"{name} done: {_count_line(ctx.counts)}"
        progress.stage(name, summary)
        print(f"{t.slug}: {summary}", flush=True)
        log_event("review", "stage", summary, session_id=session_id, target=t.slug, stage=name,
                  counts=dict(ctx.counts), cost_usd=round(ledger.cost_usd, 4))
        if ctx.gate_reasons:
            noun = "findings" if name == "find" else "verdicts"
            log_event("review", "gate_reject", f"{len(ctx.gate_reasons)} {noun} rejected by gates",
                      session_id=session_id, target=t.slug, stage=name, count=len(ctx.gate_reasons),
                      reasons=sorted({reason_kind(r) for r in ctx.gate_reasons}))

    timings.close_interval(state, "run")
    note_missing_usage(state, ledger)
    state.budget = ledger.to_state()
    findings_path = target_dir / "findings.json"
    if not state.past("export"):
        findings_path = write_findings_file(state, target_dir)
        write_checks_file(state, target_dir)
        state.stage = "export"
        save_state(state, target_dir)
    if not state.past("render"):
        write_review(state, target_dir, deployed=target_mod.deployed(t))
        state.stage = "render"
        save_state(state, target_dir)
    state.stage = "done"
    save_state(state, target_dir)
    shutil.rmtree(snapshot, ignore_errors=True)

    shown = [f for f in state.findings if getattr(state.verdicts.get(f.id), "status", "") in ("confirmed", "unverifiable")]
    counts = {s: sum(1 for f in shown if f.severity == s) for s in SEVERITIES}
    message = f"review finished: {counts['HIGH']} HIGH, {counts['MEDIUM']} MEDIUM, {counts['LOW']} LOW"
    progress.done(f"{message} (${ledger.cost_usd:.2f})")
    print(f"{t.slug}: {message}; output in {target_dir}", flush=True)
    print(f"summary: {summary_path(state, target_dir)}", flush=True)
    log_event("review", "done", message, session_id=session_id, target=t.slug, counts=counts,
              cost_usd=round(ledger.cost_usd, 4), findings_path=str(findings_path))
    return findings_path


def prepare(spec: TargetSpec, out_dir: Path) -> tuple[ReviewState, Path]:
    """Resolve and gate the target, then write its directory. Raises ReviewError.

    A `--dir` target is first copied into `<out>/<slug>/source/` and committed
    there (`import_dir`), then reviewed as a tree at that commit.
    """
    repo, tree, name, src = spec.repo, spec.tree, None, None
    info = None
    needs: list = []
    try:
        if spec.dir:
            src = Path(spec.dir).expanduser().resolve()
            name = target_mod.dir_name(src)
            repo = str(target_mod.import_dir(src, out_dir / target_mod.sanitize_slug(f"{name}--tree")))
            tree = "HEAD"
        resolved = target_mod.resolve(repo, branch=spec.branch, pr=spec.pr, range_=spec.range, tree=tree,
                                      path=spec.path, base_branch=spec.base_branch, fetch=spec.fetch)
        diff = None
        if resolved.kind == "tree":
            if resolved.head_sha and not resolved.problem:
                label = f"{src}: whole tree at {resolved.head_sha[:8]}" if src else None
                info = target_mod.build_target(resolved, tickets=spec.tickets, deployed_in=spec.deployed_in,
                                               name=name, label=label, needs=needs)
        elif resolved.base_sha and resolved.head_sha and not resolved.problem:
            diff = target_mod.diff_text(resolved.repo, resolved.base_sha, resolved.head_sha)
    except target_mod.TargetError as e:
        raise ReviewError(str(e)) from e
    gate = target_mod.target_gate(resolved, diff, info.files if info else None)
    if not gate.passed:
        raise ReviewError(f"{spec.dir or spec.repo}: {gate.reason}")
    if info is None:
        info = target_mod.build_target(resolved, tickets=spec.tickets, deployed_in=spec.deployed_in, needs=needs)
    target_dir = out_dir / info.slug
    # A new head in a directory an earlier round wrote: keep that round's
    # findings before this run writes over them. The same head keeps nothing.
    kept = rounds.keep_round(target_dir, info.slug, info.head_sha)
    if kept is not None:
        log.info("kept the earlier round of %s in %s", info.slug, kept)
    info = target_mod.write_target_files(info, diff or "", target_dir)
    return ReviewState(target=info, needs=[gated_need(n) for n in needs]), target_dir


def plan_text(state: ReviewState, config: ReviewConfig) -> str:
    """What a review of this target would run: one line per file group, then the total line."""
    t = state.target
    groups = file_groups(t)
    dims = finder_dimensions(t, config)
    lines = []
    for i, group in enumerate(groups, 1):
        chars = sum(file_chars(t, f) for f in group)
        where = f" ({group[0]} .. {group[-1]})" if len(group) > 1 else (f" ({group[0]})" if group else "")
        lines.append(f"group {i}: {len(group)} files, {chars} characters{where}")
    lines.append(f"groups: {len(groups)}  finder calls: {len(groups) * len(dims)}  files skipped: {len(t.skipped)}")
    return "\n".join(lines) + "\n"


def plan_only(spec: TargetSpec, out_dir: Path, config: ReviewConfig) -> tuple[str, Path]:
    """Resolve the target and write `plan.txt` beside `skipped.txt`; no model call, no ledger."""
    state, target_dir = prepare(spec, out_dir)
    text = plan_text(state, config)
    path = target_dir / "plan.txt"
    path.write_text(text, encoding="utf-8")
    return text, path


async def review(spec: TargetSpec, out_dir: Path, config: ReviewConfig, *, session_id: str = "") -> Path:
    state, target_dir = prepare(spec, out_dir)
    # A fresh review starts a fresh progress file; --plan-only leaves the last one alone.
    (target_dir / "progress.log").unlink(missing_ok=True)
    return await run_state(state, target_dir, config, session_id=session_id)


async def resume(target_dir: Path, config: ReviewConfig, *, session_id: str = "") -> Path:
    state = load_state(target_dir / "review-state.json")
    if state is None:
        raise ReviewError(f"no readable review-state.json in {target_dir}")
    return await run_state(state, target_dir, config, session_id=session_id)


# --- the assess stage alone, on a saved review ------------------------------------

FOLDED = ("repeat", "low-value")


@dataclass
class AssessOnly:
    """The assess stage's labels for one saved review, beside the person's decisions."""
    target_dir: Path
    rows: list[dict] = field(default_factory=list)  # id, severity, claim, decision, note, label, reason
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)


async def assess_only(target_dir: Path, config: ReviewConfig) -> AssessOnly:
    """Run repeats and assess on a finished review's findings, without changing the review.

    Reads `review-state.json` and `viewer/decisions.json`, runs both stages on
    a copy of the state, and returns each shown finding's label beside the
    decision already recorded for it. The decisions of this round are not
    shown to the agent: only earlier rounds' are. Writes nothing; raises
    ReviewError when there is no readable state.
    """
    loaded = load_state(target_dir / "review-state.json")
    if loaded is None:
        raise ReviewError(f"no readable review-state.json in {target_dir}")
    state = loaded.model_copy(deep=True)
    state.stage, state.errors = "merge", []
    state.repeats, state.repeats_checked, state.assessments, state.assess_answer = {}, [], {}, None
    ledger = budget.start(config.max_cost_usd)
    snapshot = target_mod.snapshot_head(state.target, target_dir / "assess-tree")
    ctx = RunContext(config=config, snapshot=snapshot, diff="", target_dir=target_dir)
    try:
        state = await run_repeats(state, ctx)
        state = await run_assess(state, ctx)
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)
    decisions = rounds.load_decisions(target_dir)
    result = AssessOnly(target_dir=target_dir, cost_usd=ledger.cost_usd, errors=list(state.errors))
    for f in state.findings:
        a = state.assessments.get(f.id)
        if a is None:
            continue
        d = decisions.get(f.id) or {}
        result.rows.append({"id": f.id, "severity": f.severity, "claim": f.claim,
                            "decision": d.get("decision") or "", "note": d.get("note") or "",
                            "label": a.label, "reason": a.reason})
    return result


def assess_report(results: list[AssessOnly], skipped: list[str]) -> str:
    """Markdown: one row per finding (decision, label, reason), then the two counts that matter."""
    def cell(text) -> str:
        return " ".join(str(text).split()).replace("|", "\\|")

    out = ["# Assess stage measured against recorded decisions", "",
           "| Review | Finding | Severity | Decision | Label | Reason |", "|---|---|---|---|---|---|"]
    for r in results:
        for row in r.rows:
            out.append(f"| {r.target_dir.name} | `{row['id']}` {cell(row['claim'])[:100]} | {row['severity']} | "
                       f"{row['decision'] or 'none'} | {row['label']} | {cell(row['reason'])} |")
    rows = [row for r in results for row in r.rows]
    right = sum(1 for row in rows if row["decision"] == "reject" and row["label"] in FOLDED)
    wrong = sum(1 for row in rows if row["decision"] == "accept" and row["label"] in FOLDED)
    rejected = sum(1 for row in rows if row["decision"] == "reject")
    accepted = sum(1 for row in rows if row["decision"] == "accept")
    out += ["", f"- Rejected findings labelled repeat or low-value: {right} of {rejected}",
            f"- Accepted findings labelled repeat or low-value (wrongly folded): {wrong} of {accepted}",
            f"- Cost: ${sum(r.cost_usd for r in results):.2f} over {len(results)} reviews"]
    errors = [f"{r.target_dir.name}: {e}" for r in results for e in r.errors]
    if errors:
        out += ["", "Errors:", ""] + [f"- {cell(e)}" for e in errors]
    if skipped:
        out += ["", "Skipped:", ""] + [f"- {s}" for s in skipped]
    return "\n".join(out) + "\n"


def load_batch(path: Path) -> list[TargetSpec]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ReviewError(f"cannot read batch file {path}: {e}") from e
    if isinstance(data, dict) and "targets" in data:
        data = data["targets"]
    if not isinstance(data, list) or not data:
        raise ReviewError(f"{path}: expected a non-empty YAML list of targets")
    specs = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict) or "repo" not in entry:
            raise ReviewError(f"{path}: entry {i} needs a `repo`")
        refs = [k for k in ("branch", "pr", "range") if entry.get(k) not in (None, "")]
        if len(refs) != 1:
            raise ReviewError(f"{path}: entry {i} needs exactly one of branch, pr, range")
        tickets = entry.get("tickets") or []
        specs.append(TargetSpec(
            repo=str(Path(str(entry["repo"])).expanduser()),
            branch=entry.get("branch"), pr=int(entry["pr"]) if entry.get("pr") not in (None, "") else None,
            range=entry.get("range"),
            tickets=[str(x) for x in (tickets if isinstance(tickets, list) else [tickets])],
            deployed_in=entry.get("deployed_in"), base_branch=entry.get("base_branch"),
            fetch=bool(entry.get("fetch", False)),
        ))
    return specs


async def batch(specs: list[TargetSpec], out_dir: Path, config: ReviewConfig, *, parallel: int = 2,
                session_id: str = "") -> tuple[list[Path], list[str]]:
    """Review every target, at most *parallel* at once. Returns (findings paths in order, failures)."""
    sem = asyncio.Semaphore(max(1, parallel))
    results: list[Path | None] = [None] * len(specs)
    failures: list[str] = []

    async def one(i: int, spec: TargetSpec) -> None:
        async with sem:
            label = f"{Path(spec.repo).name} {spec.branch or spec.pr or spec.range}"
            try:
                # Each task has its own context, so budget.start() gives this
                # target its own ledger.
                results[i] = await review(spec, out_dir, config, session_id=session_id)
            except (ReviewError, budget.BudgetExceededError) as e:
                failures.append(f"{label}: {e}")
                print(f"FAILED {label}: {e}", flush=True)
            except Exception as e:  # one broken target must not take the batch down
                log.error("review of %s failed", label, exc_info=True)
                failures.append(f"{label}: {e}")
                print(f"FAILED {label}: {e}", flush=True)

    await asyncio.gather(*(one(i, s) for i, s in enumerate(specs)))
    written = [p for p in results if p is not None]
    if written:
        write_rollups(out_dir, written)
        # Every review in out_dir, as after `review` and `resume`: a batch must
        # not drop the lines of reviews it did not run.
        write_runs(out_dir)
    return written, failures
