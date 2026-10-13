"""The coordinator: an approved epic builds its children
(plan-buildme-epic-mode-2026-10-12.md, items 11-16a).

`run_epic` runs in the approved build's process, under the epic lock that
process already holds (item 6), and never takes it again. Each child is its own
`python -m build_pipeline build` subprocess, in its own process group, with its
stdout and stderr going to the attempt's log file (never a pipe), so a child
outlives a killed coordinator. A child is ready once every child it depends on
has merged; at most `epic.max_parallel` build at once, and one starts only
after the last one printed `WORKTREE:` or exited.

Every run starts from disk: the records under `agents/` (`records.py`) say
what each child did, and a started record with no result is an attempt that
was stopped; `_recover` writes its result once its process is gone, then
looks for the PR it may have opened. What each stop does on resume is PR B1's
table in the plan. Every wait goes through `CLOCK`.

Each child whose PR is open is reviewed by the review skill (PR C, items
21-24; `_review`), at most once per head: a head the review ledger already
holds a complete review of is never reviewed again. A review counts against
`epic.max_parallel` like a build. Its stop and resume rows are PR C's table.

Spend (PR B2): before each launch, when `epic.max_usd` is set and the epic's
spend (`spend.py`: the records' `spend` plus the review ledger) has reached
it, nothing more is launched; the run waits for the running children and
exits 3 with `EPIC:BUDGET:<spent>`. `epic.json` and each summary hold
`spent_usd` and `spend_complete`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .. import git_ops, intake
from ..config import git_toggles
from . import records, review, spend, store

log = logging.getLogger(__name__)

SCRIPTS_DIR = Path(__file__).resolve().parents[2]
POLL = 1.0           # seconds between looks at a running child
TICK = 30.0          # seconds between reads of the waited-on PRs
STOP_WAIT = 30.0     # seconds the children get to exit on SIGINT or SIGTERM
FETCH_TIMEOUT = 300  # seconds `git fetch origin` gets before a launch

MARKER_SUMMARY = "EPIC:SUMMARY"
MARKER_MERGE_WAITING = "EPIC:MERGE_WAITING"
MARKER_BUDGET = "EPIC:BUDGET"
PR_CLOSED = "PR closed without a merge"
NO_PR = "no PR opened"
REVIEW_KINDS = ("review", "resume")
PR_FIELDS = "number,state,headRefOid,mergeable,mergeStateStatus"


class ReadFailed(Exception):
    """A `git`, `gh` or `ps` read failed, so what it would have said is not known."""


class Clock:
    """The one sleep-and-clock seam: every wait the coordinator makes."""

    def now(self) -> float:
        return asyncio.get_running_loop().time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


CLOCK = Clock()


def _tick(coord: "Coordinator") -> None:
    """Called on every look at the running children. A seam for the row tests."""


def _spawn(argv: list[str], log_path: Path) -> subprocess.Popen:
    """Start one child in its own process group, its output in *log_path*."""
    with log_path.open("ab") as out:
        return subprocess.Popen(argv, cwd=SCRIPTS_DIR, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)


def _spawn_review(argv: list[str], log_path: Path, cwd: Path) -> subprocess.Popen:
    """Start one review or resume in its own process group, its output in
    *log_path*, so it outlives a killed coordinator (C.S2)."""
    with log_path.open("ab") as out:
        return subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)


# --- where a child builds -------------------------------------------------------

def source_project_dir(config, state) -> Path:
    """The parent's project directory in its source checkout: the repo root
    plus the project's offset inside the parent's worktree, so a subdirectory
    project's children build in that subdirectory."""
    if state.source_repo and state.worktree_path:
        from ..orchestrator import _project_offset
        return Path(state.source_repo) / _project_offset(Path(config.project_dir), Path(state.worktree_path))
    return Path(config.project_dir)


def _source_repo(config, state) -> Path:
    if state.source_repo:
        return Path(state.source_repo)
    return git_ops.repo_root(Path(config.project_dir)) or Path(config.project_dir)


def _git(repo: Path, *args: str, timeout: int = git_ops._GIT_TIMEOUT) -> git_ops.GitResult:
    return git_ops._run(["git", *args], cwd=repo, timeout=timeout)


def _gh(repo: Path, *args: str) -> object | None:
    """A `gh` read, its JSON output; None when it fails."""
    proc = git_ops._run_gh(["gh", *args], cwd=repo, timeout=60)
    if not proc.ok:
        log.warning("gh %s failed: %s", " ".join(args), proc.stderr.strip()[-400:])
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


async def _fetch_origin(repo: Path) -> str | None:
    """`git fetch origin` as a subprocess the event loop awaits, so a signal or
    a running child's exit is handled while it runs. It is killed after
    FETCH_TIMEOUT seconds, or when the run is stopped. None when it worked,
    else why not."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "fetch", "origin", cwd=str(repo),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        return str(e)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), FETCH_TIMEOUT)
    except TimeoutError:
        return f"timed out after {FETCH_TIMEOUT}s"
    finally:
        if proc.returncode is None:       # timed out, or the run was stopped
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        return err.decode(errors="replace").strip()[-400:] or f"exit {proc.returncode}"
    return None


async def base_sha(repo: Path, *, need_fetch: bool) -> str | None:
    """`git fetch origin`, then origin's default branch's sha (item 12). A
    fetch that fails or times out raises ReadFailed when *need_fetch* (a child
    with dependencies, whose base must hold their merges); otherwise the
    remote ref is used as it is. No ref at all gives None."""
    failed = await _fetch_origin(repo)
    if failed is not None:
        if need_fetch:
            raise ReadFailed(f"git fetch origin failed: {failed}")
        log.warning("git fetch origin in %s failed: %s", repo, failed)
    default = git_ops.default_branch(repo)
    sha = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{default}^{{commit}}")
    if sha.returncode == 0 and sha.stdout.strip():
        return sha.stdout.strip()
    log.warning("no origin/%s in %s: the child branches from the source checkout's HEAD", default, repo)
    return None


def in_place_reason(project_dir: Path) -> str | None:
    """Why a child would not build in its own worktree and open a PR: the
    project's specs/config.yaml, which every child reads, turns off
    `git.worktree` or `git.push` (B1.S23). None when neither is off."""
    path = Path(project_dir) / "specs" / "config.yaml"
    try:
        data = yaml.safe_load(path.read_text()) if path.is_file() else {}
    except (OSError, yaml.YAMLError):
        data = {}
    git = git_toggles(data if isinstance(data, dict) else {})
    off = [f"git.{key}: false" for key in ("worktree", "push") if not getattr(git, key)]
    if not off:
        return None
    return (f"{path} sets {' and '.join(off)}, so each child would build in place or open no PR; "
            "an epic needs each child in its own worktree with a pushed PR. Remove the setting and re-run.")


# --- is a stopped child still running --------------------------------------------

def _has(line: str, text: str) -> bool:
    return re.search(re.escape(text) + r"(?=\s|$)", line) is not None


def build_pid(plan_path: Path) -> int | None:
    """The pid of a live build of *plan_path*: one `ps` line holding both
    `build_pipeline` and `--plan-file <plan>`, each followed by a space or the
    line's end (item 14). Raises ReadFailed when `ps` fails: a live child must
    never be taken for a gone one."""
    try:
        ps = subprocess.run(["ps", "-ax", "-ww", "-o", "pid=,command="], capture_output=True,
                            text=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ReadFailed(f"ps failed: {e}") from None
    if ps.returncode != 0 or not ps.stdout.strip():
        raise ReadFailed(f"ps failed: exit {ps.returncode} {ps.stderr.strip()[-200:]}".rstrip())
    for line in ps.stdout.splitlines():
        if _has(line, "build_pipeline") and _has(line, f"--plan-file {plan_path}"):
            pid, _, _ = line.strip().partition(" ")
            if pid.isdigit():
                return int(pid)
    return None


def _plan_of(argv: list[str]) -> Path | None:
    return Path(argv[argv.index("--plan-file") + 1]) if "--plan-file" in argv[:-1] else None


# --- the coordinator ---------------------------------------------------------------

@dataclass
class Child:
    id: str
    depends_on: list[str]
    status: str = "pending"
    pr: str | None = None
    reason: str = ""
    argv: list[str] | None = None        # the command to relaunch with, when set
    proc: subprocess.Popen | None = None
    attempt: records.Attempt | None = None
    relaunched: bool = False             # a failure was relaunched in this invocation
    pr_round: int | None = None          # item 23, cached in epic.json
    conflicts: bool | None = None
    reviewed: bool = False               # its review step ran in this invocation
    task: asyncio.Task | None = None     # the review step, while it runs
    rproc: subprocess.Popen | None = None        # the review or resume process
    rattempt: records.Attempt | None = None


@dataclass
class Coordinator:
    config: object
    state: object
    args: object
    epic_dir: Path
    epic: store.Epic
    children: dict[str, Child] = field(default_factory=dict)
    signal_sent: int | None = None
    budget_stop: float | None = None     # the spend that stopped launches, once reached
    _repo_slug: str | None = None

    def __post_init__(self) -> None:
        self.repo = _source_repo(self.config, self.state)
        self.project_dir = source_project_dir(self.config, self.state)
        self.settings = self.config.epic
        self.slug = store.slug_repo_name(self.repo)
        for c in self.epic.children:
            self.children[c.id] = Child(c.id, list(c.depends_on))
            cached = self.epic.child_status.get(c.id)
            if cached is not None:
                self.children[c.id].pr_round, self.children[c.id].conflicts = cached.pr_round, cached.conflicts

    # -- argv -----------------------------------------------------------------

    def new_argv(self, cid: str, base: str | None) -> list[str]:
        """Item 11's command for a first launch. All paths are absolute."""
        change_dir = Path(self.config.change_dir).absolute()
        context = [str(change_dir / name) for name in ("interview.md", "research.md")
                   if (change_dir / name).is_file()]
        research = change_dir / "research.md"
        argv = ["python", "-m", "build_pipeline", "--session-id",
                getattr(self.args, "session_id", "") or "", "build",
                "--mode", "brief", "--change", f"{self.epic.name}-{cid}",
                "--plan-file", str(store.plan_path(self.epic_dir, cid).absolute())]
        if context:
            argv += ["--context-files", *context]
        # The parent's research decision stands: no child makes a second one. A
        # parent run with --skip-research advances past RESEARCH and writes no
        # research.md, so its children skip research too.
        if research.is_file():
            argv += ["--research-path", str(research)]
        else:
            argv += ["--skip-research"]
        argv += ["--auto", "--trust-repo", "--no-epic-split", "--pr-ready",
                 "--project-dir", str(self.project_dir.absolute())]
        if base:
            argv += ["--base-ref", base]
        return argv

    # -- reading the records ----------------------------------------------------

    def _find_pr(self, tries: list[records.Attempt]) -> str | None:
        """The PR any attempt opened: a `PR:` line in a log; else `gh pr list
        --head` for every branch a `BRANCH:` line or `git worktree list` gives
        for a `WORKTREE:` path; no `WORKTREE:` line anywhere means no PR.
        Raises ReadFailed when a `git` or `gh` read fails, which is not "no PR"."""
        found = [m for a in tries for m in records.markers(a.log)]
        prs = records.marker(found, "PR")
        if prs:
            return prs[-1]
        worktrees = records.marker(found, "WORKTREE")
        if not worktrees:
            return None
        branches = list(dict.fromkeys(records.marker(found, "BRANCH")))
        porcelain = _git(self.repo, "worktree", "list", "--porcelain")
        if not porcelain.ok:
            raise ReadFailed(f"git worktree list failed: {porcelain.stderr.strip()[-200:]}")
        wanted = {Path(w).resolve() for w in worktrees}
        for block in porcelain.stdout.split("\n\n"):
            lines = block.splitlines()
            path = next((ln[len("worktree "):] for ln in lines if ln.startswith("worktree ")), None)
            ref = next((ln[len("branch refs/heads/"):] for ln in lines if ln.startswith("branch refs/heads/")), None)
            if path and Path(path).resolve() in wanted and ref and ref not in branches:
                branches.append(ref)
        for branch in branches:
            listed = _gh(self.repo, "pr", "list", "--head", branch, "--state", "all", "--json", "url")
            if not isinstance(listed, list):
                raise ReadFailed(f"gh pr list --head {branch} failed")
            if listed and isinstance(listed[0], dict) and listed[0].get("url"):
                return listed[0]["url"]
        return None

    def _classify(self, c: Child, tries: list[records.Attempt]) -> None:
        """A child's status from its finished attempts (item 15)."""
        last = tries[-1].result or {}
        c.argv = None
        if last.get("interrupted"):
            try:
                c.pr = self._find_pr(tries)
            except ReadFailed as e:       # B1.S20: no launch until a read answers
                c.status, c.reason = "unknown", f"cannot tell whether it opened a PR: {e}"
                return
            if c.pr:
                c.status, c.reason = "built", ""
            else:
                c.status, c.argv = "pending", tries[-1].started.get("argv")
            return
        code = last.get("exit_code")
        c.pr = last.get("pr")
        if code == 0 and c.pr:
            c.status, c.reason = "built", ""
        elif code == 0:
            c.status, c.reason = "parked", NO_PR
        elif code == 3:
            c.status, c.reason = "parked", records.wait_marker(last.get("markers", [])) or "exit 3"
        else:
            c.status, c.reason = "failed", f"exit {code}"
            c.argv = tries[-1].started.get("argv")

    def _recover(self) -> None:
        """Rebuild every child's status from the records: a stopped attempt whose
        process is alive is adopted (polled, never relaunched); one that is gone
        gets its result written from its log first (`exit_code: null`)."""
        by_child: dict[str, list[records.Attempt]] = {}
        for a in records.attempts(self.epic_dir):
            if a.kind == "build" and a.child in self.children:
                by_child.setdefault(a.child, []).append(a)
        for cid, tries in by_child.items():
            c = self.children[cid]
            for a in tries:
                if a.result is not None:
                    continue
                plan = _plan_of(a.started.get("argv", []))
                try:
                    live = a is tries[-1] and plan is not None and build_pid(plan) is not None
                except ReadFailed as e:       # B1.S21: never relaunch what may still run
                    c.status, c.reason = "unknown", f"cannot tell whether its build is still running: {e}"
                    break
                if live:
                    c.status, c.attempt = "running", a
                    log.info("EPIC child %s: its build from an earlier run is alive; waiting for it", cid)
                    break
                records.finish(a, None, interrupted=True)
            else:
                self._classify(c, tries)
        for a in records.attempts(self.epic_dir):    # a review's park stays (C.S6, C.S12)
            c = self.children.get(a.child)
            if c is not None and a.kind in REVIEW_KINDS and (a.result or {}).get("parked"):
                c.status, c.reason = "parked", a.result["parked"]

    # -- dependencies and merges -------------------------------------------------

    def _waited_on(self) -> list[Child]:
        """Children with a PR that a child not yet done depends on: open, or
        `unknown` because the last read of it failed."""
        deps = {d for c in self.children.values() if c.status in ("pending", "waiting", "failed")
                for d in c.depends_on}
        return [c for c in self.children.values()
                if c.id in deps and (c.status == "built" or (c.status == "unknown" and c.pr))]

    def _read_prs(self, which: list[Child]) -> None:
        """`gh pr view` each PR. A failed read is not "still open": the child is
        `unknown` until a read answers (B1.S24)."""
        for c in which:
            got = _gh(self.repo, "pr", "view", c.pr, "--json", "state")
            state = got.get("state") if isinstance(got, dict) else None
            if state == "MERGED":
                c.status, c.reason = "merged", ""
            elif state == "CLOSED":
                c.status, c.reason = "parked", PR_CLOSED
            elif state is None:
                c.status, c.reason = "unknown", "cannot read its PR's state: gh pr view failed"
            else:
                c.status, c.reason = "built", ""

    def _resolve(self) -> list[Child]:
        """Mark skipped and waiting children; return the ones ready to launch."""
        changed = True
        while changed:
            changed = False
            for c in self.children.values():
                if c.status not in ("pending", "waiting", "failed"):
                    continue
                stuck = [d for d in c.depends_on if self.children[d].status in ("parked", "skipped")]
                if stuck:
                    c.status, c.reason, changed = "skipped", f"dependency {stuck[0]} {self.children[stuck[0]].status}", True
        ready = []
        for c in self.children.values():
            if c.status not in ("pending", "waiting", "failed"):
                continue
            deps = [self.children[d].status for d in c.depends_on]
            if all(s == "merged" for s in deps):
                if c.status == "waiting":
                    c.status = "pending"
                if c.status == "pending" or not c.relaunched:
                    ready.append(c)
            elif c.status == "pending" and ("built" in deps or "reviewing" in deps):
                c.status = "waiting"
        return ready

    # -- launching and watching -------------------------------------------------

    async def _launch(self, c: Child) -> bool:
        """Start *c*. False when it was not started, with nothing written for
        it but its status: a first launch whose base could not be fetched, or
        whose specs/config.yaml is not the base's (B1.S22). The next run then
        fetches again."""
        if c.status == "failed":
            c.relaunched = True
        argv = c.argv
        if argv is None:
            try:
                base = await base_sha(self.repo, need_fetch=bool(c.depends_on))
            except ReadFailed as e:       # no launch until a fetch answers
                c.status, c.reason = "unknown", f"cannot get a base that holds its dependencies' merges: {e}"
                self._save()
                return False
            if base and intake.config_yaml_off_ref(self.project_dir, base):
                c.status, c.relaunched = "failed", True
                c.reason = f"in {self.project_dir}: {intake.CONFIG_NOT_ON_BASE}"
                self._save()
                return False
            argv = self.new_argv(c.id, base)
        plan = _plan_of(argv)
        head = argv[argv.index("--base-ref") + 1] if "--base-ref" in argv[:-1] else None
        c.attempt = records.start(self.epic_dir, "build", c.id, argv, head_sha=head)
        c.status, c.argv, c.reason = "running", argv, ""
        self._save()
        c.proc = _spawn(argv, c.attempt.log)
        log.info("EPIC child %s: launched pid %d (plan %s)", c.id, c.proc.pid, plan)
        # One start at a time: worktree creation in one repo is not run concurrently.
        while c.proc.poll() is None and not records.marker(records.markers(c.attempt.log), "WORKTREE"):
            await CLOCK.sleep(POLL)
        return True

    def _done(self, c: Child, exit_code: int | None, *, interrupted: bool) -> None:
        records.finish(c.attempt, exit_code, interrupted=interrupted)
        tries = [a for a in records.attempts(self.epic_dir) if a.kind == "build" and a.child == c.id]
        c.proc = None
        self._classify(c, tries)
        log.info("EPIC child %s: %s %s", c.id, c.status, c.pr or c.reason)
        self._save()

    def _poll(self) -> None:
        for c in self.children.values():
            if c.status != "running":
                continue
            if c.proc is not None:
                code = c.proc.poll()
                if code is not None:
                    self._done(c, code, interrupted=False)
            else:
                try:
                    gone = build_pid(_plan_of(c.attempt.started["argv"])) is None
                except ReadFailed as e:       # B1.S21
                    c.status, c.reason, c.attempt = "unknown", f"cannot tell whether its build is still running: {e}", None
                    self._save()
                    continue
                if gone:
                    self._done(c, None, interrupted=True)

    # -- reviewing a child's PR (PR C) ----------------------------------------------

    def _review_due(self) -> list[Child]:
        """Children with an open PR whose review step has not run in this invocation."""
        return [c for c in self.children.values() if c.status == "built" and not c.reviewed]

    def _start_review(self, c: Child) -> None:
        c.status, c.reviewed = "reviewing", True
        c.task = asyncio.create_task(self._review(c))
        self._save()

    def _reap_reviews(self) -> None:
        """Collect finished review steps; an unexpected error in one is raised here."""
        for c in self.children.values():
            if c.task is not None and c.task.done():
                task, c.task = c.task, None
                task.result()

    def _worktree(self, c: Child) -> Path:
        """The child's worktree: the last `WORKTREE:` line of its builds; the
        source repo when there is none or it is gone (removed by hand)."""
        found = [m for a in records.attempts(self.epic_dir) if a.kind == "build" and a.child == c.id
                 for m in records.markers(a.log)]
        trees = records.marker(found, "WORKTREE")
        return Path(trees[-1]) if trees and Path(trees[-1]).is_dir() else self.repo

    async def _run_review(self, c: Child, kind: str, argv: list[str], head: str) -> tuple[int, str]:
        """Run one `review` or `resume` to its end, as a pair of records; its
        exit code and output. The process is in its own group with its output
        in the attempt's log, so it outlives a killed coordinator (C.S2)."""
        c.rattempt = records.start(self.epic_dir, kind, c.id, argv, head_sha=head, pr_round=c.pr_round)
        self._save()
        c.rproc = _spawn_review(argv, c.rattempt.log, self._worktree(c))
        while c.rproc.poll() is None:
            await CLOCK.sleep(POLL)
        code, c.rproc = c.rproc.returncode, None
        return code, review.log_text(c.rattempt.log)

    def _fields(self, c: Child, line: dict, review_dir: str, number: int, *, reused: bool) -> dict:
        """Item 22's fields of a review result, from its ledger line; with
        `parked` when the PR is at the round cap with blockers (item 23) or its
        findings cannot be read."""
        c.pr_round = review.pr_round(review.ledger_lines(), self.slug, number)
        blocking = review.blockers(review_dir)
        fields = {"head_sha": line.get("head_sha"), "base_sha": line.get("base_sha"), "review_dir": review_dir,
                  "cost_usd": (line.get("run") or {}).get("cost_usd"), "pr_round": c.pr_round,
                  "reused": reused, "conflicts": c.conflicts, "blocking": blocking or [],
                  "reused_pipeline_run": ({"target_dir": line.get("target_dir"), "round": line.get("round")}
                                          if reused else None)}
        cap = self.settings.max_review_rounds
        if blocking is None:
            fields["parked"] = f"cannot read the review's findings in {review_dir}"
        elif blocking and c.pr_round >= cap:
            fields["parked"] = (f"review round {c.pr_round} of {cap} left {len(blocking)} blocker(s): "
                                + "; ".join(blocking))
        return fields

    def _reviewed(self, c: Child, fields: dict) -> None:
        if fields.get("parked"):
            c.status, c.reason = "parked", fields["parked"]
        else:
            c.status, c.reason = "built", ""
        log.info("EPIC child %s: reviewed at %s, round %s, %d blocker(s)%s", c.id, fields.get("head_sha"),
                 fields.get("pr_round"), len(fields.get("blocking") or []),
                 f"; parked: {c.reason}" if c.status == "parked" else "")

    def _reuse(self, c: Child, line: dict, head: str, number: int) -> None:
        """Item 24, found: no review. A started record for this head with no
        result gets its result now (C.S2, C.S4); when no record of this head
        holds a review's fields (none at all, or only the exit-2 `is running
        now` asks), one pair is written; a head whose review is recorded
        writes nothing."""
        tries = [a for a in records.attempts(self.epic_dir) if a.child == c.id and a.kind in REVIEW_KINDS
                 and a.started.get("head_sha") == head]
        fields = self._fields(c, line, str(line.get("target_dir")), number, reused=True)
        open_ = [a for a in tries if a.result is None]
        for a in open_[:-1]:
            records.finish(a, None, interrupted=True)
        if open_:
            records.finish(open_[-1], None, interrupted=True, extra=fields)
        elif not any("blocking" in a.result for a in tries):
            argv = review.review_argv(self._worktree(c), number)
            a = records.start(self.epic_dir, "review", c.id, argv, head_sha=head, pr_round=c.pr_round)
            records.finish(a, 0, interrupted=False, extra=fields)
        self._reviewed(c, fields)

    async def _review(self, c: Child) -> None:
        """Review *c*'s PR at its current head (items 21-24) and set its status:
        `built` (reviewed; blockers go to the fixer, PR D2), `parked`,
        `merged`, or `unknown` when its PR cannot be read, here or by the
        review itself (C.S20)."""
        try:
            while True:
                view = _gh(self.repo, "pr", "view", c.pr, "--json", PR_FIELDS)
                if not isinstance(view, dict) or not view.get("headRefOid") or not isinstance(view.get("number"), int):
                    c.status, c.reason = "unknown", "cannot read its PR: gh pr view failed"
                    return
                if view.get("state") in ("MERGED", "CLOSED"):
                    c.status, c.reason = ("merged", "") if view["state"] == "MERGED" else ("parked", PR_CLOSED)
                    return
                number, head = view["number"], view["headRefOid"]
                c.conflicts = review.conflicts(view)
                lines = review.ledger_lines()
                running = review.running_dir(lines, self.slug, head)
                if running is not None:
                    if review.lock_held(running):     # C.S2: it runs now; never two at once
                        await CLOCK.sleep(review.REVIEW_WAIT)
                        continue
                    # Its lock is free, so it ended: read again, as its `finished`
                    # line may have come after the read above.
                    lines = review.ledger_lines()
                c.pr_round = review.pr_round(lines, self.slug, number)
                line = review.finished_line(lines, self.slug, number, head)
                if line is not None:
                    self._reuse(c, line, head, number)
                    return
                if self._over_budget():           # Decided 6: before each launch
                    c.status, c.reason = "built", ""
                    return
                stale = [a for a in records.attempts(self.epic_dir)
                         if a.child == c.id and a.kind in REVIEW_KINDS and a.result is None]
                code, out = await self._run_review(c, "review", review.review_argv(self._worktree(c), number), head)
                if code == 2 and review.RUNNING_NOW in out:       # C.S2: never two at once
                    records.finish(c.rattempt, code, interrupted=False)
                    await CLOCK.sleep(review.REVIEW_WAIT)
                    continue
                for a in stale:           # `review` answered, so an earlier one is gone
                    records.finish(a, None, interrupted=True)
                hint = review.INTERRUPTED.search(out) if code == 4 else None
                if hint:                  # C.S3
                    records.finish(c.rattempt, code, interrupted=False)
                    if self._over_budget():
                        c.status, c.reason = "built", ""
                        return
                    code, out = await self._run_review(c, "resume", review.resume_argv(hint.group(1).strip()), head)
                    if code == 2 and review.RUNNING_NOW in out:
                        records.finish(c.rattempt, code, interrupted=False)
                        await CLOCK.sleep(review.REVIEW_WAIT)
                        continue
                found = review.findings_path(out)
                line = review.finished_line(review.ledger_lines(), self.slug, number, head)
                if code == 0 and found and line is not None:
                    fields = self._fields(c, line, str(Path(found).parent), number, reused=False)
                    records.finish(c.rattempt, code, interrupted=False, extra=fields)
                    self._reviewed(c, fields)
                    return
                if code == 0 and found:
                    # The review reads the PR's head itself, after its fetch: a
                    # commit pushed since the read above makes it review and
                    # record that newer head (C.S8, C.S19). Read the PR again.
                    again = _gh(self.repo, "pr", "view", c.pr, "--json", PR_FIELDS)
                    if not isinstance(again, dict) or not again.get("headRefOid"):
                        records.finish(c.rattempt, code, interrupted=False)
                        c.status, c.reason = "unknown", "cannot read its PR: gh pr view failed"
                        return
                    if again["headRefOid"] != head:
                        records.finish(c.rattempt, code, interrupted=False)
                        continue
                if code == 2 and review.READ_FAILED.search(out):     # C.S20: a failed read, not a park
                    records.finish(c.rattempt, code, interrupted=False)
                    c.status, c.reason = "unknown", f"its review could not read git or GitHub: {review.tail(out)}"
                    log.warning("EPIC child %s: %s", c.id, c.reason)
                    return
                why = (f"review exited {code}: {review.tail(out)}" if code != 0 else
                       f"review exited 0 without a findings line and a finished ledger line: {review.tail(out)}")
                records.finish(c.rattempt, code, interrupted=False, extra={"parked": why})
                c.status, c.reason = "parked", why
                log.warning("EPIC child %s: parked: %s", c.id, why)
                return
        finally:
            if c.rproc is None:
                c.rattempt = None
            self._save()

    def _save(self) -> None:
        self.epic.child_status = {cid: store.ChildStatus(status=c.status, pr=c.pr, reason=c.reason,
                                                         pr_round=c.pr_round, conflicts=c.conflicts)
                                  for cid, c in self.children.items()}
        self.epic.spent_usd, self.epic.spend_complete = self.spent()
        store.save_epic(self.epic_dir, self.epic)

    # -- spend (PR B2) ---------------------------------------------------------------

    def _review_slugs(self) -> set[str]:
        if self._repo_slug is None:
            self._repo_slug = store.slug_repo_name(self.repo)
        return spend.review_slugs(self._repo_slug, [c.pr for c in self.children.values()])

    def spent(self) -> tuple[float, bool]:
        """(USD, complete): every record's spend plus the child PRs' reviews (item 19)."""
        from multiplai_core.paths import get_paths

        usd, complete = spend.attempts_spend(records.attempts(self.epic_dir))
        reviews, reviews_complete = spend.review_spend(Path(get_paths().data_dir) / spend.LEDGER,
                                                       self._review_slugs())
        return round(usd + reviews, 6), complete and reviews_complete

    def _over_budget(self) -> bool:
        """True once `epic.max_usd` is set and reached; then for the rest of the run."""
        if self.budget_stop is not None:
            return True
        ceiling = self.settings.max_usd
        if ceiling is None:
            return False
        usd, _ = self.spent()
        if usd < ceiling:
            return False
        self.budget_stop = usd
        log.warning("EPIC spend $%.2f has reached epic.max_usd $%.2f: launching nothing more", usd, ceiling)
        return True

    async def _stop_children(self, sig: int) -> None:
        """Signal the group of every running child, the ones this run started
        and the ones it adopted from an earlier run (a session leader, so its
        pid is its group), give them STOP_WAIT seconds, and write the results of
        those that exited with `interrupted: true`. One still alive, or one `ps`
        cannot see, keeps no result, so the next run waits for it."""
        running = [c for c in self.children.values() if c.status == "running"]
        reviewing = [c for c in self.children.values() if c.rproc is not None]

        def alive(c: Child) -> int | None:
            if c.proc is not None:
                return c.proc.pid if c.proc.poll() is None else None
            try:
                return build_pid(_plan_of(c.attempt.started["argv"]))
            except ReadFailed:
                return -1
        for c in running:
            pid = alive(c)
            try:
                if pid and pid > 0:
                    os.killpg(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        for c in reviewing:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                if c.rproc.poll() is None:
                    os.killpg(c.rproc.pid, sig)
        until = CLOCK.now() + STOP_WAIT
        while (any(alive(c) for c in running) or any(c.rproc.poll() is None for c in reviewing)) \
                and CLOCK.now() < until:
            await CLOCK.sleep(0.5)
        for c in reviewing:       # a review still running keeps no result: the next run asks it
            if c.rproc.poll() is None:
                log.warning("EPIC child %s: its review is still running after %ss", c.id, STOP_WAIT)
                continue
            records.finish(c.rattempt, c.rproc.poll(), interrupted=True)
            c.rproc = c.rattempt = None
        for c in self.children.values():
            if c.status == "reviewing":
                c.status = "built"
        for c in running:
            if alive(c):
                log.warning("EPIC child %s is still running after %ss; the next run waits for it", c.id, STOP_WAIT)
                continue
            records.finish(c.attempt, c.proc.poll() if c.proc is not None else None, interrupted=True)
            c.status, c.argv, c.proc = "pending", c.attempt.started["argv"], None
        self._save()

    async def _cancel_reviews(self) -> None:
        """Stop every review step; a review process it started keeps running
        until `_stop_children` signals it."""
        tasks = [c.task for c in self.children.values() if c.task is not None]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for c in self.children.values():
            c.task = None

    # -- the run ------------------------------------------------------------------

    async def run(self) -> int:
        refused = in_place_reason(self.project_dir)
        if refused:
            log.error("EPIC %s", refused)
            print(f"ERROR:{refused}", file=sys.stderr, flush=True)
            return 2
        run_dir = _new_run_dir(self.epic_dir)
        self._recover()
        self._read_prs([c for c in self.children.values() if c.status == "built"])
        self._resolve()
        self._save()
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()

        def on_signal(sig: int) -> None:
            self.signal_sent = sig
            task.cancel()

        # SIGHUP (a closed terminal) ends only the coordinator: each child runs
        # in a session of its own, so it keeps running and the next run waits
        # for it (B1.S4). Handled here, not by the SystemExit `__main__.main`
        # makes of it, which would cancel this task as if by Ctrl-C. An ignored
        # signal (SIGHUP under `nohup`) stays ignored, as `_exit_on_termination`
        # leaves it: asyncio's handler would replace SIG_IGN.
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
        previous = {sig: h for sig, h in previous.items() if h != signal.SIG_IGN}
        for sig in previous:
            try:
                loop.add_signal_handler(sig, on_signal, sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        try:
            rc = await self._loop()
        except (KeyboardInterrupt, asyncio.CancelledError):
            sig = self.signal_sent or signal.SIGINT
            await self._cancel_reviews()
            if sig == signal.SIGHUP:
                log.warning("EPIC stopped by SIGHUP: leaving the children running; the next run waits for them")
            else:
                log.warning("EPIC stopped by %s: stopping the children", signal.Signals(sig).name)
                await self._stop_children(sig)
            self._summary(run_dir)
            return 128 + sig
        except Exception:
            await self._cancel_reviews()
            self._summary(run_dir)        # where each child stood when the run broke
            raise
        finally:
            for sig, handler in previous.items():     # asyncio would leave SIG_DFL
                try:
                    if loop.remove_signal_handler(sig) and handler is not None:
                        signal.signal(sig, handler)
                except (NotImplementedError, RuntimeError, ValueError):
                    pass
        self._summary(run_dir)
        bad = [c for c in self.children.values() if c.status in ("failed", "unknown")]
        for c in bad:       # beside EPIC:MERGE_WAITING too, not only in the summary
            print(f"EPIC: child {c.id} {c.status}: {c.reason}", file=sys.stderr, flush=True)
        if self.budget_stop is not None:      # the spend at exit, the running children's included
            print(f"{MARKER_BUDGET}:{self.spent()[0]:.2f}", flush=True)
            return 3
        open_prs = [c.pr for c in self._waited_on() if c.status == "built"]
        if open_prs:
            print(f"{MARKER_MERGE_WAITING}:{','.join(open_prs)}", flush=True)
            return 3
        return 1 if bad else 0

    async def _loop(self) -> int:
        last_read = CLOCK.now()
        while True:
            self._reap_reviews()
            ready = self._resolve()
            running = sum(c.status in ("running", "reviewing") for c in self.children.values())
            for c in self._review_due():        # a review before a new build: it unblocks dependants
                if running >= self.settings.max_parallel or self._over_budget():
                    break
                self._start_review(c)
                running += 1
            for c in ready:
                if running >= self.settings.max_parallel or self._over_budget():
                    break
                if await self._launch(c):
                    running += 1
            if running:
                await CLOCK.sleep(POLL)
                _tick(self)
                self._poll()
                if CLOCK.now() - last_read >= TICK:
                    self._read_prs(self._waited_on())
                    last_read = CLOCK.now()
                    self._save()
                continue
            if self.budget_stop is not None:      # nothing runs: the budget stop ends the run
                self._save()
                return 0
            if ready:
                continue
            # Nothing runs and nothing is ready: read once more (item 15a).
            self._read_prs(self._waited_on())
            if self._resolve() or self._review_due():
                continue
            self._save()
            return 0

    def _summary(self, run_dir: Path) -> Path:
        children = [{"id": cid, "status": c.status, "pr": c.pr, "rounds": c.pr_round or 0, "reason": c.reason}
                    for cid, c in self.children.items()]
        totals: dict[str, int] = {}
        for c in children:
            totals[c["status"]] = totals.get(c["status"], 0) + 1
        usd, complete = self.spent()
        path = run_dir / "summary.json"
        store._write_atomic(path, json.dumps({"children": children, "totals": totals, "spent_usd": usd,
                                              "spend_complete": complete}, indent=2) + "\n")
        print(f"{MARKER_SUMMARY}:{path}", flush=True)
        return path


def _new_run_dir(epic_dir: Path) -> Path:
    """`runs/<NN>/`, the next free number, made at the start of each invocation."""
    runs = Path(epic_dir) / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    n = max((int(p.name) for p in runs.iterdir() if p.name.isdigit()), default=0) + 1
    path = runs / f"{n:02d}"
    path.mkdir()
    return path


async def run_epic(config, state, args, epic_dir: Path, epic: store.Epic) -> int:
    """Build an approved epic's children (item 11). Returns 0 when nothing is
    left to do, 1 when a child failed or its state is unknown, 3 with `EPIC:MERGE_WAITING:<urls>` while a
    child waits on an open PR or with `EPIC:BUDGET:<spent>` once `epic.max_usd` is reached,
    128 + the signal when stopped."""
    from . import lock as epic_lock

    assert epic_lock.held(epic_dir), "the coordinator runs under the epic lock its build holds"
    return await Coordinator(config, state, args, Path(epic_dir), epic).run()
