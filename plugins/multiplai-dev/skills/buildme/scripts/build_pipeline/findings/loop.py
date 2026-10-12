"""The findings round loop: fetch → triage → spec → code → publish → review.

State lives in `<change>/.findings-state.json`, a `BuildState` with only
`findings` set; the build's `.build-state.json` is never recreated (the pr
skill's `edit` reads that file's presence as a build in progress). Each step
is recorded as started before it runs and finished after, so a resume skips
finished steps and restarts the first unfinished one:

- triage keeps every verdict as it lands, so a resume calls no agent twice;
- spec and triage, when they fail or were cut off, first keep whatever the
  tree holds under `refs/buildme/wip/<change>/r<N>-<step>`, and only then
  discard back to where the step started;
- code re-enters `run_tdd_engine`, which resumes from its own checkpoint;
- publish records each reply before sending the next.

The loop stops when a fetch finds no new comment to act on
(`FINDINGS:DONE:<rounds>`) or after `findings.max_rounds` rounds
(`FINDINGS:MAX_ROUNDS:<n>`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from .. import git_ops
from ..change_manager import normalize_change_name
from ..config import BuildConfig
from ..models import BuildPhase
from ..state import BuildState, FindingsRound, FindingsState
from . import code, fetch, publish, spec, triage
from .models import TriageRecord

log = logging.getLogger(__name__)

STATE_FILE = ".findings-state.json"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_WAITING = 3

# Steps whose half-done edits are kept under a wip ref and discarded on
# resume. The code step is not one: the TDD engine resumes its own blocks.
REWIND_STEPS = ("triage", "spec")


class StepFailed(RuntimeError):
    def __init__(self, step: str, reason: str, exit_code: int = EXIT_FAILED):
        super().__init__(reason)
        self.step = step
        self.exit_code = exit_code


@dataclass
class ReviewOutcome:
    run_dir: str | None
    ok: bool
    detail: str = ""


ReviewRunner = Callable[["FindingsRun", FindingsRound], ReviewOutcome]
ReviewPoster = Callable[["FindingsRun", str], tuple[bool, str]]


@dataclass
class Seams:
    """Injection points for tests; None means the real call."""

    triage_call: Callable[..., Awaitable] | None = None
    spec_call: Callable[..., Awaitable] | None = None
    engine: Callable[..., Awaitable[int]] | None = None
    review: ReviewRunner | None = None
    post: ReviewPoster | None = None


@dataclass
class FindingsRun:
    config: BuildConfig          # bound to the PR's worktree
    pr: int
    repo: str                    # owner/name
    branch: str
    change_dir: Path
    auto: bool = False
    trust_repo: bool = False
    seams: Seams = field(default_factory=Seams)
    emit: Callable[[str], None] = print

    @property
    def project_dir(self) -> Path:
        return Path(self.config.project_dir)

    @property
    def state_path(self) -> Path:
        return Path(self.change_dir) / STATE_FILE


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(run: FindingsRun) -> BuildState:
    if run.state_path.exists():
        st = BuildState.load(run.state_path)
        if st.findings is not None:
            return st
    c = run.config
    return BuildState(
        change_name=c.change_name, mode=c.mode, tier=c.tier, phase=BuildPhase.COMPLETE,
        state_file=str(run.state_path),
        findings=FindingsState(pr=run.pr, repo=run.repo, branch=run.branch,
                               change_dir=str(run.change_dir)),
    )


def all_records(fs: FindingsState) -> list[TriageRecord]:
    out: list[TriageRecord] = []
    for r in fs.rounds:
        out += publish.records_of(r)
    return out


def _write_findings_file(run: FindingsRun, fs: FindingsState, rnd: FindingsRound) -> Path:
    return triage.write_findings(run.change_dir, rnd.number, all_records(fs), rnd.spec_changes)


# --- wip refs -----------------------------------------------------------------


def wip_ref(run: FindingsRun, rnd: FindingsRound, step: str) -> str:
    return f"refs/buildme/wip/{normalize_change_name(run.config.change_name)}/r{rnd.number}-{step}"


def keep_and_rewind(run: FindingsRun, rnd: FindingsRound, step: str) -> None:
    """Keep the step's edits under its wip ref, then discard back to the
    commit the step started from. Never discards when the ref could not be
    written."""
    start = rnd.step_heads.get(step)
    if not start:
        return
    repo = run.project_dir
    if git_ops.tree_is_clean(repo) and git_ops.rev_parse_head(repo) == start:
        return
    ref = wip_ref(run, rnd, step)
    if git_ops.save_wip_ref(repo, ref, f"wip(findings-r{rnd.number}): {step}, kept before discard") is None:
        raise StepFailed(step, f"could not write {ref}, so the tree was left as it is; "
                               f"inspect {repo} before re-running")
    run.emit(f"FINDINGS:WIP:{ref}")
    if not git_ops.discard_to(repo, start, f"findings r{rnd.number} {step}"):
        raise StepFailed(step, f"edits kept under {ref}, but the discard back to "
                               f"{start[:12]} was refused; inspect {repo}")


async def _step(run: FindingsRun, st: BuildState, rnd: FindingsRound, name: str,
                body: Callable[[], Awaitable[None]]) -> None:
    if name in rnd.finished:
        return
    save = lambda: st.checkpoint(run.state_path)  # noqa: E731
    if name in rnd.started and name in REWIND_STEPS:
        keep_and_rewind(run, rnd, name)
    if name not in rnd.started:
        rnd.started.append(name)
    head = git_ops.rev_parse_head(run.project_dir)
    if head:
        rnd.step_heads[name] = head
    save()
    log.info("START findings_step round=%d step=%s", rnd.number, name)
    try:
        await body()
    except Exception:
        if name in REWIND_STEPS:
            try:
                keep_and_rewind(run, rnd, name)
            except StepFailed as e:
                log.error("findings r%d %s: %s", rnd.number, name, e)
        save()
        raise
    rnd.finished.append(name)
    save()
    log.info("DONE findings_step round=%d step=%s", rnd.number, name)


# --- review -------------------------------------------------------------------


def _review_skill_dir() -> Path:
    from ..orchestrator import _pr_skill_dir
    return _pr_skill_dir().parent.parent / "review" / "scripts"


def review_argv(run: FindingsRun) -> list[str]:
    argv = ["uv", "run", "--directory", str(_review_skill_dir()),
            "python", "-m", "review_pipeline", "review",
            "--repo", str(run.project_dir), "--pr", str(run.pr)]
    if run.trust_repo:
        argv.append("--trust-repo")
    return argv


def run_review(run: FindingsRun, rnd: FindingsRound) -> ReviewOutcome:
    res = git_ops._run_review_skill(review_argv(run), cwd=run.project_dir)
    run_dir = None
    for line in (res.stdout or "").splitlines():
        if line.startswith("findings: "):
            run_dir = str(Path(line[len("findings: "):].strip()).parent)
    if not res.ok or not run_dir:
        tail = " ".join((res.stderr or res.stdout or "").split())[-400:]
        return ReviewOutcome(None, False, f"review exited {res.returncode}: {tail}")
    return ReviewOutcome(run_dir, True)


def post_review(run: FindingsRun, run_dir: str) -> tuple[bool, str]:
    """Unattended (`--auto`): post what the review lists by its section rule.
    `post` skips any finding already marked on the PR, so a re-run is safe."""
    argv = ["uv", "run", "--directory", str(_review_skill_dir()),
            "python", "-m", "review_pipeline", "post", run_dir]
    res = git_ops._run_review_skill(argv, cwd=run.project_dir)
    return res.ok, " ".join((res.stderr or res.stdout or "").split())[-400:]


# --- one round ----------------------------------------------------------------


async def run_round(run: FindingsRun, st: BuildState, rnd: FindingsRound) -> int:
    fs = st.findings
    assert fs is not None
    seams = run.seams
    save = lambda: st.checkpoint(run.state_path)  # noqa: E731
    n = rnd.number

    async def do_triage() -> None:
        await triage.triage_round(run.config, run.change_dir, n, rnd.comments,
                                  done=rnd.triaged, on_triaged=lambda *_: save(),
                                  call=seams.triage_call)
        _write_findings_file(run, fs, rnd)

    async def do_spec() -> None:
        findings_file = _write_findings_file(run, fs, rnd)
        changes, sha = await spec.run_spec_step(
            run.config, run.change_dir, n, publish.records_of(rnd),
            extra_paths=[findings_file], call=seams.spec_call)
        rnd.spec_changes = changes
        rnd.spec_commit = sha
        if changes:
            rnd.delta_path = str(spec.delta_path(run.change_dir, n))
        _write_findings_file(run, fs, rnd)

    async def do_code() -> None:
        records = publish.records_of(rnd)
        findings_file = _write_findings_file(run, fs, rnd)
        paths = [findings_file]
        if code.code_fixes(records):
            tasks, blocks = code.write_tasks(run.change_dir, n, records)
            rnd.tasks_path, rnd.blocks = str(tasks), blocks
            rnd.engine_state_path = str(Path(run.change_dir) / code.engine_state_name(n))
            save()
            paths.append(tasks)
        code.commit_round_files(run.project_dir, n, paths)
        if not rnd.blocks:
            return
        result = await code.run_code_step(run.config, run.change_dir, n, records,
                                          blocks=rnd.blocks, engine=seams.engine)
        rnd.fixes = result.fixes
        save()
        if result.exit_code != 0:
            raise StepFailed("code", f"the TDD engine stopped (exit {result.exit_code}); "
                                     f"its checkpoint is {rnd.engine_state_path}",
                             exit_code=result.exit_code)

    async def do_publish() -> None:
        try:
            publish.publish_round(fs, rnd, project_dir=run.project_dir,
                                  change_dir=run.change_dir, save=save, emit=run.emit)
        except publish.PublishError as e:
            raise StepFailed("publish", str(e)) from e

    async def do_review() -> None:
        if rnd.review_run_dir is None:
            outcome = (seams.review or run_review)(run, rnd)
            if not outcome.ok:
                raise StepFailed("review", outcome.detail)
            rnd.review_run_dir = outcome.run_dir
            save()
        if run.auto:
            ok, detail = (seams.post or post_review)(run, rnd.review_run_dir)
            if not ok:
                raise StepFailed("review", f"review post failed: {detail}")
        elif not rnd.review_waiting:
            rnd.review_waiting = True
            save()
            raise _Waiting()
        rnd.review_finished_at = _now()

    for name, body in (("triage", do_triage), ("spec", do_spec), ("code", do_code),
                       ("publish", do_publish), ("review", do_review)):
        try:
            await _step(run, st, rnd, name, body)
        except _Waiting:
            run.emit(f"FINDINGS:REVIEW_READY:{rnd.review_run_dir}")
            return EXIT_WAITING
        except StepFailed as e:
            run.emit(f"FINDINGS:FAILED:{e.step}:{' '.join(str(e).split())}")
            return e.exit_code
        except Exception as e:  # an agent or model call that raised: resumable
            log.exception("findings round %d: step %s failed", n, name)
            run.emit(f"FINDINGS:FAILED:{name}:{type(e).__name__}: {' '.join(str(e).split())}")
            return EXIT_FAILED
    run.emit(f"FINDINGS:ROUND_DONE:{n}")
    return EXIT_OK


class _Waiting(Exception):
    """The review ran; a human accepts its findings in the viewer and posts
    them, then re-runs the command to finish the round."""


def _round_done(rnd: FindingsRound) -> bool:
    return "review" in rnd.finished


async def run_findings(run: FindingsRun) -> int:
    """The loop. Returns the process exit code."""
    st = load_state(run)
    fs = st.findings
    assert fs is not None
    cache: dict[str, bool] = {}
    max_rounds = run.config.findings.max_rounds
    while True:
        rnd = fs.rounds[-1] if fs.rounds and not _round_done(fs.rounds[-1]) else None
        if rnd is None:
            done = len(fs.rounds)
            if done >= max_rounds:
                run.emit(f"FINDINGS:MAX_ROUNDS:{done}")
                return EXIT_OK
            since = fs.rounds[-1].review_finished_at if fs.rounds else None
            try:
                comments = fetch.fetch_comments(fs.repo, fs.pr, since, cwd=run.project_dir)
            except fetch.FetchError as e:
                run.emit(f"FINDINGS:FAILED:fetch:{' '.join(str(e).split())}")
                return EXIT_FAILED
            fetch.prepare(comments, run.config.findings, cache, repo=fs.repo, cwd=run.project_dir)
            seen = fs.seen_ids()
            new = [c for c in comments if c.id not in seen]
            if not any(triage.should_triage(c)[0] for c in new):
                for c in new:
                    log.info("findings: comment %d by %s not acted on: %s",
                             c.id, c.author, triage.should_triage(c)[1])
                run.emit(f"FINDINGS:DONE:{done}")
                return EXIT_OK
            rnd = FindingsRound(number=done + 1, since=since, comments=new,
                                started=["fetch"], finished=["fetch"])
            fs.rounds.append(rnd)
            st.checkpoint(run.state_path)
            acted = sum(1 for c in new if triage.should_triage(c)[0])
            run.emit(f"FINDINGS:ROUND:{rnd.number}:{acted} to act on, {len(new) - acted} not")
        code_ = await run_round(run, st, rnd)
        if code_ != EXIT_OK:
            return code_
