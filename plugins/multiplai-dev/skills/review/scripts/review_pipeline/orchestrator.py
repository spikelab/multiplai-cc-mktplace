"""target → [explain] → paths → find → verify → recheck → merge → repeats → assess → write → export → render.

`review-state.json` is written after every stage; `resume` reloads it and
continues from the first stage not yet done.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml
from multiplai_core.log_utils import log_event

from . import budget, deps, incremental, rounds, runlock, sdk, timings, window
from . import ledger as review_ledger
from . import target as target_mod
from .chunks import split_pieces
from .config import SESSION_DEFAULT, ReviewConfig, run_config
from .costs import cost_lines, estimate, kept_line
from .export import (
    need_coverage,
    producer,
    run_record,
    to_findings_file,
    write_checks_file,
    write_findings_file,
)
from .facts import FACTS_FILE
from .gates import gated_need, reason_kind
from .models import MODES, SEVERITIES, ReviewState, RunStart
from .progress import ProgressWriter
from .render import (
    answers_lines,
    count_line,
    listed,
    not_reviewed_count,
    summary_path,
    write_review,
    write_rollups,
    write_runs,
)
from .stages import RunContext
from .stages.assess import reconcile_earlier, run_assess
from .stages.explain import run_explain
from .stages.find import (
    DEFAULT_CHANGE_DIMENSIONS,
    change_dimensions,
    file_chars,
    file_groups,
    finder_dimensions,
    run_find,
    split_for_callers,
)
from .stages.merge import run_merge
from .stages.paths import PATH_CAP, run_paths
from .stages.recheck import run_recheck
from .stages.repeats import run_repeats
from .stages.verify import VerifyIncomplete, run_verify
from .stages.write import run_write
from .state import STATE_FILE, load_state, save_state

log = logging.getLogger(__name__)

# With no file to re-review, only recheck runs before export: the carried findings keep their
# verdicts and plain-language text from the round they were carried from.
NOTHING_CHANGED_SKIPS = frozenset(
    {"explain", "paths", "find", "verify", "merge", "repeats", "assess", "write"}
)

STAGE_FUNCTIONS = (
    ("explain", run_explain),  # only with `explain: true`
    ("paths", run_paths),  # every change review; a tree review only with `--paths`
    ("find", run_find),
    ("verify", run_verify),
    (
        "recheck",
        run_recheck,
    ),  # earlier rounds' open findings; skipped on a tree and with none open
    ("merge", run_merge),
    ("repeats", run_repeats),
    ("assess", run_assess),
    ("write", run_write),  # every shown finding in plain words
)


class ReviewError(Exception):
    """Bad input or an unresolvable target. Exit code 2."""


class InterruptedRun(ReviewError):
    """The ledger shows a run of this target and head that started and never ended, or that the
    usage limit or the verifier stopped, and its saved state is still in the target directory, so
    `resume` can continue it. Exit code 4."""

    def __init__(self, target_dir: Path):
        self.target_dir = target_dir
        super().__init__(
            f"interrupted run found; continue with: python -m review_pipeline resume {target_dir}"
        )


class RunInProgress(ReviewError):
    """Another process is running a review of this target directory now (runlock.py). Exit code 2."""

    def __init__(self, locked: runlock.Locked):
        self.target_dir = locked.target_dir
        super().__init__(str(locked))


def _hold(stack: contextlib.ExitStack, target_dir: Path) -> None:
    """Take the target directory's run lock for as long as *stack* is open; raise RunInProgress if held."""
    try:
        stack.enter_context(runlock.hold(target_dir))
    except runlock.Locked as e:
        raise RunInProgress(e) from None


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
    dir: str | None = None  # a directory not under git, copied and reviewed as a tree
    mode: str = "full"  # `full` or `critical` (models.MODES)
    restart: bool = False  # start over even when the ledger shows an interrupted run
    full: bool = (
        False  # review every file even on a re-push (`--full`; `incremental.py`)
    )
    trim: bool = False  # a tree review also runs the trim finder (`--trim`)
    extra_finders: bool = (
        False  # `--extra-finders` was given: accepted, ignored, noticed once
    )
    paths: bool = False  # a tree review also maps its paths (`--paths`); a change review always does
    # A change review leaves paths matching these git globs out (`--exclude`).
    exclude: list[str] = field(default_factory=list)


def _count_line(counts: dict[str, int]) -> str:
    return (
        ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items())
        or "nothing to do"
    )


NO_USAGE_NOTE = "budget:"


def note_missing_usage(state: ReviewState, ledger: budget.ReviewBudget) -> None:
    """One line in `state.errors` when calls returned no usage; their cost and tokens count as 0."""
    state.errors = [e for e in state.errors if not e.startswith(NO_USAGE_NOTE)]
    if ledger.no_usage_calls:
        n = ledger.no_usage_calls
        state.errors.append(
            f"{NO_USAGE_NOTE} {n} agent call{'' if n == 1 else 's'} returned no usage; "
            "their cost and tokens are counted as 0"
        )


# Stopped, not failed: the run can be resumed, so the ledger gets no end line.
INTERRUPTIONS = (KeyboardInterrupt, asyncio.CancelledError, SystemExit)


async def run_state(
    state: ReviewState, target_dir: Path, config: ReviewConfig, *, session_id: str = ""
) -> Path:
    """Run every stage not yet done; return the findings.json path.

    Saves this run's state, then appends a `started` line to the review
    ledger, so `resume` of a run that stops at once reads this run's state,
    not the last round's. Appends a `finished` line when the run ends
    (`stopped` names a budget stop, a verify stop, a usage-limit stop or a
    failure). A run that is
    stopped rather than failing writes no end line, which is how the ledger
    marks an interrupted run: a process killed outright, Ctrl-C or a
    cancelled task (`KeyboardInterrupt`, `asyncio.CancelledError`), or an
    exiting process (`SystemExit`).
    """
    t = state.target
    number = f"{rounds.round_number(target_dir):02d}"
    state.runs.append(
        RunStart(producer=producer(), session_id=session_id, started_at=timings.now())
    )
    save_state(state, target_dir)
    review_ledger.started(
        slug=t.slug, target_dir=target_dir, head_sha=t.head_sha, session_id=session_id
    )
    stopped: str | None = "failed"
    interrupted = False
    try:
        path = await _run_stages(state, target_dir, config, session_id=session_id)
        stopped = None
        return path
    except budget.BudgetExceededError:
        stopped = "budget"
        raise
    except VerifyIncomplete:
        stopped = "verify-incomplete"
        raise
    except sdk.UsageLimitError:
        stopped = "usage-limit"
        raise
    except INTERRUPTIONS:
        interrupted = True
        raise
    except BaseException as e:
        stopped = f"failed: {type(e).__name__}"
        raise
    finally:
        if not interrupted:
            _finish(
                state, target_dir, session_id=session_id, number=number, stopped=stopped
            )


def _finish(
    state: ReviewState,
    target_dir: Path,
    *,
    session_id: str,
    number: str,
    stopped: str | None,
) -> None:
    """The `finished` ledger line of a run that ended."""
    t = state.target
    try:
        run = run_record(state)
    except Exception:  # the ledger line must not hide the run's own error
        log.warning("cannot build the run record for the ledger", exc_info=True)
        run = None
    review_ledger.finished(
        producer=producer(),
        session_id=session_id,
        slug=t.slug,
        target_dir=target_dir,
        round_=number,
        head_sha=t.head_sha,
        base_sha=t.base_sha,
        kind=t.kind,
        pr=t.pr,
        stopped=stopped,
        run=run,
    )


def _with_deps(state: ReviewState, snapshot: Path, config: ReviewConfig) -> None:
    """Extract the git dependencies' source into *snapshot* (`deps.ensure`) and record the result.

    `state.target.deps` names the extracted ones for the gates; each one that
    could not be extracted becomes a need, once.
    """
    manifest = deps.ensure(state.target, snapshot, config)
    state.target.deps = manifest.ok()
    known = {n.what for n in state.needs}
    state.needs.extend(
        n for n in map(gated_need, deps.needs(manifest)) if n.what not in known
    )


async def _run_stages(
    state: ReviewState, target_dir: Path, config: ReviewConfig, *, session_id: str = ""
) -> Path:
    t = state.target
    ledger = budget.start(config.max_cost_usd, state.budget)
    progress = ProgressWriter(target_dir / "progress.log")
    window.progress_line = progress.line  # the usage window's readings and NOTICE
    if state.stage == "target":
        progress.started(
            producer(),
            t.label or t.slug,
            t.base_sha,
            t.head_sha,
            len(t.files),
            deps=len(deps.locked_git_deps(t)) if config.deps else 0,
        )
        log_event(
            "review",
            "start",
            f"review started for {t.slug}",
            session_id=session_id,
            target=t.slug,
            base=t.base_sha,
            head=t.head_sha,
            files=len(t.files),
        )
    else:
        progress.line(f"RESUMED after {state.stage}")

    snapshot = target_mod.snapshot_head(t, target_dir / "tree", note=progress.line)
    _with_deps(state, snapshot, config)
    diff = Path(t.diff_path).read_text(encoding="utf-8") if t.diff_path else ""
    if incremental.is_incremental(state):
        # The agents see only the re-reviewed files' patches; diff.patch keeps the whole change.
        diff = incremental.limit_diff(diff, state.scope.reviewed_files)
    ctx = RunContext(
        config=config,
        snapshot=snapshot,
        diff=diff,
        progress=progress,
        session_id=session_id,
        target_dir=target_dir,
    )
    state.run_config = run_config(config)
    timings.open_interval(state, "run")
    save_state(state, target_dir)
    # A checkpoint saved after assess and before export: the assess stage will not run
    # again, and one written before 0.94 has no earlier statuses or lineage (row S3).
    reconcile_now = state.past("assess") and not state.past("export")

    if incremental.nothing_changed(state) and not state.past("recheck"):
        progress.line(f"nothing changed since round {state.scope.since_number}")
    for name, fn in STAGE_FUNCTIONS:
        if state.past(name) or (name == "explain" and not config.explain):
            continue
        if incremental.nothing_changed(state) and name in NOTHING_CHANGED_SKIPS:
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
            log_event(
                "review",
                "budget_stop",
                str(e),
                session_id=session_id,
                level="WARNING",
                target=t.slug,
                cost_usd=round(e.cost_usd, 4),
                stage=name,
            )
            e.kept = kept_line(state)  # the STOPPED message's last line
            raise
        except VerifyIncomplete as e:
            # The verdicts that came back are kept; `resume` asks only for the rest.
            timings.close_interval(state, name)
            timings.close_interval(state, "run")
            state.budget = ledger.to_state()
            save_state(state, target_dir)
            progress.failed(str(e))
            log_event(
                "review",
                "verify_incomplete",
                f"verify stopped: {len(e.finding_ids)} findings have no verdict",
                session_id=session_id,
                level="WARNING",
                target=t.slug,
                findings=e.finding_ids,
            )
            raise
        except sdk.UsageLimitError as e:
            # Every answer that came back is in the state; `resume` after the reset asks for the rest.
            timings.close_interval(state, name)
            timings.close_interval(state, "run")
            state.budget = ledger.to_state()
            save_state(state, target_dir)
            progress.failed(str(e))
            log_event(
                "review",
                "usage_limit",
                str(e),
                session_id=session_id,
                level="WARNING",
                target=t.slug,
                stage=name,
                resets=e.resets,
            )
            e.target_dir = target_dir  # for the STOPPED message's resume command
            raise
        timings.close_interval(state, name)
        timings.close_interval(
            state, "run"
        )  # moved forward at each checkpoint; see timings.py
        state.budget = ledger.to_state()
        save_state(state, target_dir)
        summary = f"{name} done: {_count_line(ctx.counts)}"
        progress.stage(name, summary)
        print(f"{t.slug}: {summary}", flush=True)
        log_event(
            "review",
            "stage",
            summary,
            session_id=session_id,
            target=t.slug,
            stage=name,
            counts=dict(ctx.counts),
            cost_usd=round(ledger.cost_usd, 4),
        )
        if ctx.gate_reasons:
            noun = "findings" if name == "find" else "verdicts"
            log_event(
                "review",
                "gate_reject",
                f"{len(ctx.gate_reasons)} {noun} rejected by gates",
                session_id=session_id,
                target=t.slug,
                stage=name,
                count=len(ctx.gate_reasons),
                reasons=sorted({reason_kind(r) for r in ctx.gate_reasons}),
            )

    # With nothing to re-review the assess stage is skipped, so its reconcile runs here.
    if reconcile_now or (
        incremental.nothing_changed(state) and not state.past("export")
    ):
        reconcile_earlier(state, ctx)
    timings.close_interval(state, "run")
    note_missing_usage(state, ledger)
    state.budget = ledger.to_state()
    findings_path = target_dir / "findings.json"
    if not state.past("export"):
        need_coverage(state)
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

    data = to_findings_file(state)
    rows = data["findings"]
    shown = listed(rows, state.mode)
    counts = {s: sum(1 for f in shown if f["severity"] == s) for s in SEVERITIES}
    message = f"review finished: {count_line(rows, state.mode)}"
    missing = not_reviewed_count(state)
    progress.done(
        f"{message} (${ledger.cost_usd:.2f})"
        + (
            f"; {missing} file{'' if missing == 1 else 's'} not reviewed"
            if missing
            else ""
        )
    )
    print(f"{t.slug}: {message}; output in {target_dir}", flush=True)
    print(f"summary: {summary_path(state, target_dir)}", flush=True)
    for line in answers_lines(data, target_dir):
        print(line, flush=True)
    log_event(
        "review",
        "done",
        message,
        session_id=session_id,
        target=t.slug,
        counts=counts,
        cost_usd=round(ledger.cost_usd, 4),
        findings_path=str(findings_path),
    )
    return findings_path


def prepare(
    spec: TargetSpec,
    out_dir: Path,
    *,
    check_interrupted: bool = False,
    lock: contextlib.ExitStack | None = None,
    incremental_on: bool = True,
    plan: bool = False,
) -> tuple[ReviewState, Path]:
    """Resolve and gate the target, then write its directory. Raises ReviewError.

    With *lock*, the target directory's run lock is taken on it before the
    ledger check and before anything is written there (for a `--dir` target,
    before the copy into `source/`), and stays held until the caller closes
    the stack: a review running in that directory now raises RunInProgress,
    so it is never read as interrupted and never written over.

    The directory is `<out>/<repo>/<object>/` (`_choose_folder`). A `--dir`
    target is first copied into `<out>/<basename>/dir-<basename>-<6 hex>/source/`
    and committed there (`import_dir`), then reviewed as a tree at that commit.
    With *check_interrupted* (a review, not `--plan-only`) and no
    `spec.restart`, a run of this directory and head that the ledger shows
    started and never ended, or stopped by the usage limit or the verifier,
    raises InterruptedRun before anything is
    written, when `resume` can continue it (`_resumable`); otherwise this run
    starts over. While the chosen directory has no saved state yet, the
    directory multiplai-dev 0.46 used for this target (`<out>/<its slug>/`) is
    checked the same way, under its own lock.

    With *plan* (`--plan-only`) on a directory holding a run `resume` can
    continue, no round is kept and the diff and lists go to a temporary
    directory held by *lock*, so that run's files stay as it wrote them.
    """
    repo, tree, name, src = spec.repo, spec.tree, None, None
    info = None
    needs: list = []
    import_to: Path | None = None
    if lock is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
    try:
        if spec.dir:
            src = Path(spec.dir).expanduser().resolve()
            name = target_mod.dir_name(src)
            repo_name, obj = target_mod.dir_object(src)
            import_to = out_dir / repo_name / obj
            if lock is not None:
                # hold() creates `.locks/` but not its parents: without this it runs unlocked.
                import_to.parent.mkdir(parents=True, exist_ok=True)
                _hold(lock, import_to)
            repo = str(target_mod.import_dir(src, import_to))
            tree = "HEAD"
        resolved = target_mod.resolve(
            repo,
            branch=spec.branch,
            pr=spec.pr,
            range_=spec.range,
            tree=tree,
            path=spec.path,
            base_branch=spec.base_branch,
            fetch=spec.fetch,
        )
        diff = None
        if resolved.kind == "tree":
            if resolved.head_sha and not resolved.problem:
                label = f"{src}: whole tree at {resolved.head_sha[:8]}" if src else None
                info = target_mod.build_target(
                    resolved,
                    tickets=spec.tickets,
                    deployed_in=spec.deployed_in,
                    name=name,
                    label=label,
                    needs=needs,
                )
        elif resolved.base_sha and resolved.head_sha and not resolved.problem:
            diff = target_mod.diff_text(
                resolved.repo, resolved.base_sha, resolved.head_sha, spec.exclude
            )
            if spec.exclude and not diff.strip():
                raise ReviewError(
                    f"{spec.repo}: nothing left to review: --exclude "
                    f"{' '.join(spec.exclude)} leaves out every changed path"
                )
    except target_mod.TargetError as e:
        raise ReviewError(str(e)) from e
    gate = target_mod.target_gate(resolved, diff, info.files if info else None)
    if not gate.passed:
        raise ReviewError(f"{spec.dir or spec.repo}: {gate.reason}")
    if info is None:
        info = target_mod.build_target(
            resolved,
            tickets=spec.tickets,
            deployed_in=spec.deployed_in,
            needs=needs,
            exclude=spec.exclude,
        )
    target_dir = (
        import_to if import_to is not None else _choose_folder(out_dir, resolved, lock)
    )
    if check_interrupted and not spec.restart:
        if _resumable(target_dir, info.head_sha):
            raise InterruptedRun(target_dir)
        old = out_dir / info.slug
        if (
            old.is_dir()
            and old != target_dir
            and not (target_dir / STATE_FILE).exists()
        ):
            with contextlib.ExitStack() as old_lock:
                if (
                    lock is not None
                ):  # a run still going there is running, not interrupted
                    _hold(old_lock, old)
                if _resumable(old, info.head_sha):
                    raise InterruptedRun(old)
    files_dir = target_dir
    if plan and _holds_stopped_run(target_dir):
        # `--plan-only` must not change what a later `resume` of that run reads.
        files_dir = Path(
            (lock or contextlib.ExitStack()).enter_context(
                tempfile.TemporaryDirectory(prefix="review-plan-")
            )
        )
    else:
        # A directory an earlier round wrote: keep that round's findings before
        # this run writes over them, also when this run is on the same head (a rerun).
        kept = rounds.keep_round(target_dir, info.slug, info.head_sha)
        if kept is not None:
            log.info("kept the earlier round of %s in %s", info.slug, kept)
    info = target_mod.write_target_files(info, diff or "", files_dir)
    # What this round reviews, once, from the round just kept: `resume` reuses the stored scope.
    scope, carried = None, None
    if not info.is_tree:
        scope, carried = incremental.decide(
            target_dir,
            info,
            diff or "",
            mode=spec.mode,
            full=spec.full,
            enabled=incremental_on,
        )
    # A change review's pieces, once: `resume` reuses the stored ones, so piece ids stay stable.
    pieces = None
    if not info.is_tree:
        reviewed = (
            incremental.limit_diff(diff or "", scope.reviewed_files)
            if scope is not None and scope.kind == "incremental"
            else diff or ""
        )
        try:
            pieces = split_pieces(
                reviewed,
                head_tree=target_mod.tree_paths(info.repo_path, info.head_sha),
                base_tree=target_mod.tree_paths(info.repo_path, info.base_sha),
            )
        except target_mod.TargetError as e:
            raise ReviewError(str(e)) from e
    state = ReviewState(
        target=info,
        mode=spec.mode,
        trim=spec.trim,
        paths_enabled=spec.paths and info.is_tree,
        pieces=pieces,
        needs=[gated_need(n) for n in needs],
    )
    if scope is not None and carried is not None:
        incremental.apply(state, scope, carried)
    return state, target_dir


def _same_remote(url: str | None, here: tuple[str, str] | None) -> bool:
    """True when *url* names the repository *here* is (`(owner, repo)`), compared without case,
    so the `https://` and `git@` forms of one repository are one remote."""
    pair = target_mod.origin_owner_repo(url)
    return (tuple(x.lower() for x in pair) if pair else None) == (
        tuple(x.lower() for x in here) if here else None
    )


def _try_folder(
    folder: Path, lock: contextlib.ExitStack | None, passed_over: Callable[[dict], bool]
) -> tuple[contextlib.ExitStack, dict]:
    """Take *folder*'s run lock in a stack of its own (with *lock*), then read whose it is.

    When another process holds the lock, read whose the folder is without it: a folder
    *passed_over* (another remote's or another ref's) is returned unlocked, so the caller
    moves on to the next one; for any other folder RunInProgress stands.
    """
    held = contextlib.ExitStack()
    if lock is not None:
        folder.parent.mkdir(parents=True, exist_ok=True)
        try:
            _hold(held, folder)
        except RunInProgress:
            held.close()
            t = rounds.folder_target(folder)
            if passed_over(t):
                return contextlib.ExitStack(), t
            raise
        except BaseException:
            held.close()
            raise
    return held, rounds.folder_target(folder)


def _choose_folder(
    out_dir: Path, resolved: target_mod.Resolved, lock: contextlib.ExitStack | None
) -> Path:
    """`<out>/<repo>/<object>/` for a PR, branch, range or tree, never a folder another
    remote's or another ref's review wrote.

    A folder whose `review-state.json` (else `findings.json`) names another remote
    is left alone: the run tries `<repo>@<owner>/<object>/` (`@local` with no
    origin). For a branch or range, a folder of this remote (or of none) that
    names another raw ref (`feat/x` and `feat_x` clean to one name) gives way to
    `<object>-<6 hex of the ref>/`. A folder that names no remote is used. At
    most three folders are tried; with *lock*, each is locked before it is read,
    and only the chosen one stays locked (on *lock*).
    """
    repo_name, owner = target_mod.checkout_repo_part(resolved.repo)
    here = target_mod.origin_owner_repo(target_mod.remote_url(resolved.repo))
    obj = target_mod.object_name(resolved)
    hashed = resolved.kind in ("branch", "range")

    def other_remote(t: dict) -> bool:
        return bool(t.get("remote_url")) and not _same_remote(t.get("remote_url"), here)

    def other_ref(t: dict) -> bool:
        return hashed and bool(t.get("ref")) and str(t.get("ref")) != resolved.ref

    def passed_over(t: dict) -> bool:
        return other_remote(t) or other_ref(t)

    folder = out_dir / repo_name / obj
    held, t = _try_folder(folder, lock, passed_over)
    if other_remote(t):
        held.close()
        folder = out_dir / f"{repo_name.split('@')[0]}@{owner or 'local'}" / obj
        held, t = _try_folder(folder, lock, passed_over)
    if not other_remote(t) and other_ref(t):
        held.close()
        folder = (
            folder.parent
            / f"{obj}-{hashlib.sha256(resolved.ref.encode()).hexdigest()[:6]}"
        )
        held, t = _try_folder(folder, lock, passed_over)
    if other_remote(t) or other_ref(t):
        held.close()
        raise ReviewError(
            f"{folder} holds a review of {t.get('remote_url') or 'no remote'} {t.get('ref') or ''}".rstrip()
            + f", not of {target_mod.remote_url(resolved.repo) or resolved.repo} {resolved.ref}; "
            "pass --out to write this review somewhere else"
        )
    if lock is not None:
        lock.enter_context(held.pop_all())
    return folder


def _holds_stopped_run(target_dir: Path) -> bool:
    """True when *target_dir* holds a run on its saved head that `resume` can continue."""
    path = target_dir / STATE_FILE
    saved = load_state(path) if path.is_file() else None
    return saved is not None and review_ledger.resumable(
        target_dir, saved.target.head_sha
    )


def _resumable(target_dir: Path, head_sha: str) -> bool:
    """True when the ledger shows a run of *target_dir* and *head_sha* that never ended, or
    that ended stopped by the usage limit or the verifier (`ledger.resumable`), and the
    directory still holds a readable `review-state.json` on that head.

    `run_state` saves the state before it writes the `started` line, so a run
    that left the line left its state too, unless the directory or the file
    was deleted since, or a later run on another head wrote over it. Then
    `resume` would fail or continue the wrong head's review, and nothing
    would ever close the line: this run starts over instead, and its own end
    line closes it.
    """
    if not review_ledger.resumable(target_dir, head_sha):
        return False
    path = target_dir / STATE_FILE
    saved = load_state(path) if path.is_file() else None
    if saved is not None and saved.target.head_sha == head_sha:
        return True
    found = (
        f"a saved state on {saved.target.head_sha[:12]}"
        if saved is not None
        else f"no readable {STATE_FILE}"
    )
    log.warning(
        "%s: the ledger shows an unfinished run on %s, but the directory holds %s; starting over",
        target_dir,
        head_sha[:12],
        found,
    )
    return False


def plan_text(state: ReviewState, config: ReviewConfig) -> str:
    """What a review of this target would run: a tree's file groups or a change's pieces, then the totals."""
    t = state.target
    found = (
        deps.locked_git_deps(t) if config.deps else []
    )  # read from git only; nothing is fetched
    lines = []
    if t.is_tree:
        groups = file_groups(t)
        dims = finder_dimensions(t, config, tree_trim=state.trim)
        for i, group in enumerate(groups, 1):
            chars = sum(file_chars(t, f) for f in group)
            where = (
                f" ({group[0]} .. {group[-1]})"
                if len(group) > 1
                else (f" ({group[0]})" if group else "")
            )
            lines.append(f"group {i}: {len(group)} files, {chars} characters{where}")
        total = f"groups: {len(groups)}  finder calls: {len(groups) * len(dims)}  files skipped: {len(t.skipped)}"
    else:
        total = f"files: {len(t.files)}  files skipped: {len(t.skipped)}"
    if found:
        total += f"  git dependencies: {len(found)}"
    lines.append(total)
    lines.extend(piece_plan_lines(state))
    return "\n".join(lines) + "\n"


def piece_plan_lines(state: ReviewState) -> list[str]:
    """One line per piece, then the finder calls (a tree review: path finders with --paths)."""
    lines = []
    for p in state.pieces or []:
        label = "meta" if p.meta else (p.group or ".")
        noun = "file" if len(p.files) == 1 else "files"
        parts = [f"piece {p.id}: {len(p.files)} {noun}, {p.chars} characters [{label}]"]
        if p.files:
            parts.append(", ".join(p.files))
        if p.omitted:
            parts.append("omitted: " + ", ".join(p.omitted))
        lines.append(" ".join(parts))
    each = "(each one report call and one convert call)"
    if not state.target.is_tree:
        dims = change_dimensions(state)
        pieces = len([p for p in state.pieces or [] if p.files])
        lines.append(
            f"finder calls: {pieces} pieces x {len(dims)} dimensions ({', '.join(dims)}) + up to "
            f"{PATH_CAP} paths {each}; {estimate(state).calls} calls at most"
        )
    elif state.paths_enabled:
        lines.append(f"path finder calls: up to {PATH_CAP} paths {each}")
    return lines


def change_review_dimensions(state: ReviewState, config: ReviewConfig) -> None:
    """Store the dimensions a change review runs on every piece, from `review.yaml`, before its first save."""
    if not state.target.is_tree:
        state.dimensions = list(finder_dimensions(state.target, config))


def callers_split(state: ReviewState) -> None:
    """Before the first save: halve each piece that changes too many existing definitions for one callers
    finder (`stages/find.split_for_callers`), over the diff the finders will read."""
    if state.target.is_tree or not state.target.diff_path:
        return
    diff = Path(state.target.diff_path).read_text(encoding="utf-8")
    if incremental.is_incremental(state):
        diff = incremental.limit_diff(diff, state.scope.reviewed_files)
    for piece in split_for_callers(state, diff):
        log.info(
            "piece %d split for callers before the finders: pieces %s",
            piece.id,
            " and ".join(str(i) for i in piece.split_into),
        )


DIMENSIONS_NOTICE = (
    "NOTICE: dimensions {saved} kept from the stopped run; review.yaml says {new}"
)


def kept_dimensions(state: ReviewState, config: ReviewConfig) -> None:
    """On resume: a checkpoint with no dimensions runs the default set, and a stored list wins over a
    changed `review.yaml`, with one NOTICE."""
    if state.target.is_tree:
        return
    if state.dimensions is None:
        state.dimensions = list(DEFAULT_CHANGE_DIMENSIONS)
    new = list(finder_dimensions(state.target, config))
    if new != state.dimensions:
        print(
            DIMENSIONS_NOTICE.format(
                saved=", ".join(state.dimensions), new=", ".join(new)
            ),
            file=sys.stderr,
            flush=True,
        )


FINDER_MODEL_NOTICE = "NOTICE: finder model {saved} kept from the stopped run; this run's settings say {new}"


def kept_finder_model(state: ReviewState, config: ReviewConfig) -> None:
    """On resume: the finder model the stopped run saved (`run_config`, `find`) wins over this run's
    settings, `--model` included, with one NOTICE. A checkpoint that saved none keeps this run's."""
    saved = ((state.run_config or {}).get("stages") or {}).get("find", {}).get("model")
    if not saved:
        return
    model = None if saved == SESSION_DEFAULT else saved
    if model != config.finder_model:
        print(
            FINDER_MODEL_NOTICE.format(
                saved=saved, new=config.finder_model or SESSION_DEFAULT
            ),
            file=sys.stderr,
            flush=True,
        )
        config.finder_model = model


def plan_only(
    spec: TargetSpec, out_dir: Path, config: ReviewConfig
) -> tuple[str, Path]:
    """Resolve the target and write `plan.txt` beside `skipped.txt`; no model call, no ledger."""
    extra_finders_notice(config, spec)
    with contextlib.ExitStack() as lock:
        state, target_dir = prepare(spec, out_dir, lock=lock, plan=True)
        change_review_dimensions(state, config)
        callers_split(state)
        print_cost_lines(state, config, resuming=False)
        text = plan_text(state, config)
        path = target_dir / "plan.txt"
        path.write_text(text, encoding="utf-8")
    return text, path


def print_cost_lines(
    state: ReviewState, config: ReviewConfig, *, resuming: bool
) -> None:
    """The piece and path finders' estimate, WARNING and NOTICE (`costs.cost_lines`) on stderr. Advisory only."""
    for line in cost_lines(
        state,
        budget=config.max_cost_usd or 0.0,
        budget_is_default=config.max_cost_source == "default",
        resuming=resuming,
    ):
        print(line, file=sys.stderr, flush=True)


EXTRA_FINDERS_NOTICE = (
    "review: --extra-finders has no effect; per-piece finders now run by default"
)


def extra_finders_notice(config: ReviewConfig, spec: TargetSpec | None = None) -> None:
    """One line on stderr when `--extra-finders` or `review.yaml`'s `extra_finders` was given; both are ignored."""
    if config.extra_finders or (spec is not None and spec.extra_finders):
        print(EXTRA_FINDERS_NOTICE, file=sys.stderr, flush=True)


def _set_aside_progress(target_dir: Path) -> None:
    """Clear the way for a fresh ``progress.log`` without losing the last one.

    ``keep_round`` has already copied the last run's log into ``rounds/`` when
    it kept that run, and then the log is deleted here. A log no kept round
    holds (a run that stopped before it saved any state, or a second start of a
    round already kept) moves to ``progress-<UTC time>.log``, named from its
    last write. ``os.link`` refuses an existing name, so nothing is
    overwritten; if the dated name is taken, the log stays and the new run
    appends to it.
    """
    log = target_dir / "progress.log"
    try:
        text = log.read_bytes()
    except OSError:
        return
    if any(
        kept.read_bytes() == text
        for kept in (target_dir / rounds.ROUNDS_DIR).glob("*/progress.log")
    ):
        log.unlink(missing_ok=True)
        return
    try:
        stamp = datetime.fromtimestamp(log.stat().st_mtime, UTC).strftime(
            "%Y%m%dT%H%M%SZ"
        )
        os.link(log, target_dir / f"progress-{stamp}.log")
        log.unlink()
    except OSError:
        pass


async def review(
    spec: TargetSpec, out_dir: Path, config: ReviewConfig, *, session_id: str = ""
) -> Path:
    extra_finders_notice(config, spec)
    with contextlib.ExitStack() as lock:
        state, target_dir = prepare(
            spec,
            out_dir,
            check_interrupted=True,
            lock=lock,
            incremental_on=config.incremental,
        )
        change_review_dimensions(state, config)
        callers_split(state)
        print_cost_lines(
            state, config, resuming=False
        )  # after the pieces, before the paths stage
        # A fresh review starts a fresh progress file; --plan-only leaves the last one alone.
        _set_aside_progress(target_dir)
        # The last round's facts.json is kept in rounds/ by prepare; the explain stage writes
        # this run's one, and a run with explain off must not leave the old one beside its findings.
        (target_dir / FACTS_FILE).unlink(missing_ok=True)
        # The same for the last run's finder reports, paths.json and the paths answer cache:
        # this run maps again from an empty cache; `resume` keeps them.
        rounds.clear_stale_outputs(target_dir)
        return await run_state(state, target_dir, config, session_id=session_id)


async def resume(
    target_dir: Path, config: ReviewConfig, *, session_id: str = ""
) -> Path:
    """Continue the run saved in *target_dir*. Refuses (RunInProgress) while a process is still running it."""
    with contextlib.ExitStack() as lock:
        _hold(lock, target_dir)
        state = load_state(target_dir / "review-state.json")
        if state is None:
            raise ReviewError(f"no readable review-state.json in {target_dir}")
        extra_finders_notice(config)
        kept_dimensions(state, config)
        kept_finder_model(state, config)
        if not state.past("find"):
            print_cost_lines(state, config, resuming=True)
        return await run_state(state, target_dir, config, session_id=session_id)


# --- the assess stage alone, on a saved review ------------------------------------

FOLDED = ("repeat", "low-value")


@dataclass
class AssessOnly:
    """The assess stage's labels for one saved review, beside the person's decisions."""

    target_dir: Path
    rows: list[dict] = field(
        default_factory=list
    )  # id, severity, claim, decision, note, label, reason
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
    state.repeats, state.repeats_checked, state.assessments, state.assess_answer = (
        {},
        [],
        {},
        None,
    )
    ledger = budget.start(config.max_cost_usd)
    snapshot = target_mod.snapshot_head(state.target, target_dir / "assess-tree")
    _with_deps(state, snapshot, config)
    ctx = RunContext(config=config, snapshot=snapshot, diff="", target_dir=target_dir)
    try:
        state = await run_repeats(state, ctx)
        state = await run_assess(state, ctx)
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)
    decisions = rounds.load_decisions(target_dir)
    result = AssessOnly(
        target_dir=target_dir, cost_usd=ledger.cost_usd, errors=list(state.errors)
    )
    for f in state.findings:
        a = state.assessments.get(f.id)
        if a is None:
            continue
        d = decisions.get(f.id) or {}
        result.rows.append(
            {
                "id": f.id,
                "severity": f.severity,
                "claim": f.claim,
                "decision": d.get("decision") or "",
                "note": d.get("note") or "",
                "label": a.label,
                "reason": a.reason,
            }
        )
    return result


def assess_report(results: list[AssessOnly], skipped: list[str]) -> str:
    """Markdown: one row per finding (decision, label, reason), then the two counts that matter."""

    def cell(text) -> str:
        return " ".join(str(text).split()).replace("|", "\\|")

    out = [
        "# Assess stage measured against recorded decisions",
        "",
        "| Review | Finding | Severity | Decision | Label | Reason |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        for row in r.rows:
            out.append(
                f"| {r.target_dir.name} | `{row['id']}` {cell(row['claim'])[:100]} | {row['severity']} | "
                f"{row['decision'] or 'none'} | {row['label']} | {cell(row['reason'])} |"
            )
    rows = [row for r in results for row in r.rows]
    right = sum(
        1 for row in rows if row["decision"] == "reject" and row["label"] in FOLDED
    )
    wrong = sum(
        1 for row in rows if row["decision"] == "accept" and row["label"] in FOLDED
    )
    rejected = sum(1 for row in rows if row["decision"] == "reject")
    accepted = sum(1 for row in rows if row["decision"] == "accept")
    out += [
        "",
        f"- Rejected findings labelled repeat or low-value: {right} of {rejected}",
        f"- Accepted findings labelled repeat or low-value (wrongly folded): {wrong} of {accepted}",
        f"- Cost: ${sum(r.cost_usd for r in results):.2f} over {len(results)} reviews",
    ]
    errors = [f"{r.target_dir.name}: {e}" for r in results for e in r.errors]
    if errors:
        out += ["", "Errors:", ""] + [f"- {cell(e)}" for e in errors]
    if skipped:
        out += ["", "Skipped:", ""] + [f"- {s}" for s in skipped]
    return "\n".join(out) + "\n"


def load_batch(path: Path, default_mode: str = "full") -> list[TargetSpec]:
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
            raise ReviewError(
                f"{path}: entry {i} needs exactly one of branch, pr, range"
            )
        tickets = entry.get("tickets") or []
        specs.append(
            TargetSpec(
                repo=str(Path(str(entry["repo"])).expanduser()),
                branch=entry.get("branch"),
                pr=int(entry["pr"]) if entry.get("pr") not in (None, "") else None,
                range=entry.get("range"),
                tickets=[
                    str(x)
                    for x in (tickets if isinstance(tickets, list) else [tickets])
                ],
                deployed_in=entry.get("deployed_in"),
                base_branch=entry.get("base_branch"),
                fetch=bool(entry.get("fetch", False)),
                mode=str(entry.get("mode") or default_mode),
            )
        )
        if specs[-1].mode not in MODES:
            raise ReviewError(
                f"{path}: entry {i} has mode {specs[-1].mode!r}; expected one of {', '.join(MODES)}"
            )
    return specs


async def batch(
    specs: list[TargetSpec],
    out_dir: Path,
    config: ReviewConfig,
    *,
    parallel: int = 2,
    session_id: str = "",
) -> tuple[list[Path], list[str]]:
    """Review every target, at most *parallel* at once. Returns (findings paths in order, failures).

    After a target hits the Claude usage limit, no target that has not started
    yet is started: each would fail the same way. They are listed as failures,
    and the first `sdk.UsageLimitError` is raised once the running ones end,
    with `stopped_dirs` naming every target directory the limit stopped.
    """
    sem = asyncio.Semaphore(max(1, parallel))
    results: list[Path | None] = [None] * len(specs)
    failures: list[str] = []
    limits: list[sdk.UsageLimitError] = []

    async def one(i: int, spec: TargetSpec) -> None:
        async with sem:
            label = f"{Path(spec.repo).name} {spec.branch or spec.pr or spec.range}"
            if limits:
                failures.append(f"{label}: not started: {limits[0]}")
                print(f"FAILED {label}: not started: {limits[0]}", flush=True)
                return
            try:
                # Each task has its own context, so budget.start() gives this
                # target its own ledger.
                results[i] = await review(spec, out_dir, config, session_id=session_id)
            except (ReviewError, budget.BudgetExceededError, VerifyIncomplete) as e:
                failures.append(f"{label}: {e}")
                print(f"FAILED {label}: {e}", flush=True)
            except sdk.UsageLimitError as e:
                limits.append(e)
                failures.append(f"{label}: {e}")
                print(f"STOPPED {label}: {e}", flush=True)
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
    if limits:
        first = limits[0]
        first.written, first.failures = written, failures
        first.stopped_dirs = [
            d for e in limits if (d := getattr(e, "target_dir", None)) is not None
        ]
        raise first
    return written, failures
