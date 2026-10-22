"""The coordinator: an epic pm-spec split builds its children
(plan-buildme-epic-mode-2026-10-12.md, items 11-16a; plan-pm-spec-split-2026-10-21.md,
Decision 11).

`run_epic` runs in the parent's build process, under the epic lock that
process already holds (item 6), and never takes it again. The children and
their dependencies are the change dirs' `.change.yaml` records
(`phase.run_epic_from_specs`). Each child is its own
`python -m build_pipeline build --change <epic>-<id>` subprocess, in the
epic's own project, in its own process group, with its
stdout and stderr going to the attempt's log file (never a pipe), so a child
outlives a killed coordinator. A child is ready once every child it depends on
has merged; at most `epic.max_parallel` build at once, and one starts only
after the last one printed `WORKTREE:` or exited. Every child starts from the
epic's feature branch, `origin/<branch>` after a `git fetch origin` (the branch
pm-spec pushed the specs to), and its PR targets that branch: a dependent child
starts after its dependencies merged into it. At the start of every run, and
before the first launch of each later wave (a dependency level) in the run,
origin's default branch is merged into the feature branch and pushed
(`feature_branch.merge_default`); a conflict launches nothing more and the run
exits 3 with `FEATURE_BRANCH:CONFLICT:<branch>:<files>`. The pull request from
the feature branch to the release branch is the user's.

Every run starts from disk: the records under `agents/` (`records.py`) say
what each child did, and a started record with no result is an attempt that
was stopped; `_recover` writes its result once its process is gone, then
looks for the PR it may have opened. What each stop does on resume is PR B1's
table in the plan. Every wait goes through `CLOCK`.

Each child whose PR is open is reviewed by the review skill (PR C, items
21-24; `_review`), at most once per head: a head the review ledger already
holds a complete review of is never reviewed again. A review counts against
`epic.max_parallel` like a build. Its stop and resume rows are PR C's table.

A reviewed head with blockers runs the fix loop (PR D2, items 32-34;
`_fix`, `fix.py`): the review's chosen findings are posted, then
`python -m build_pipeline findings` fixes them in the child's worktree, and
the review step reads the PR again and reuses the fixer's last review. A
fixer an earlier run left is waited for, then run again (`_fix_pending`).
Its stop and resume rows are PR D2's table.

A child whose review left no blocker is merged (PR E, items 35-42;
`_merge`, `merge.py`). Under `epic.merge: auto` the merge step merges
`origin/<feature branch>` into the child's branch in its worktree, has an
agent resolve version, CHANGELOG and lockfile conflicts, runs the tests,
pushes, waits for the PR's required checks and runs `gh pr merge --merge`,
one merge at a time. A PR with no checks at all merges once none appeared
for `CHECKS_APPEAR` seconds after the step's own push (at once when it
pushed nothing): buildme's own review is then the only gate. Under `manual`
nothing is merged: the PR is waited on, and the run exits 3 with
`EPIC:MERGE_WAITING` while it is open. With `epic.merge` unset, it is `auto`
when the feature branch is not origin's default branch, else `manual`
(`Coordinator.__post_init__`). Its stop and resume rows are PR E's table.

Spend (PR B2): before each launch, when `epic.max_usd` is set and the epic's
spend (`spend.py`: the records' `spend` plus the review ledger) has reached
it, nothing more is launched; the run waits for the running children and
exits 3 with `EPIC:BUDGET:<spent>`. A fixer runs several rounds in one
launch, so it is given what is left as `findings --max-usd`: the cap less
the spend and less what fixers still running were given and have not yet
spent on their PRs' reviews. While those hold all that is left, a fixer
waits for them and nothing new is launched, by `_loop` or inside a step
(`_unheld`). One that stopped at its share (`FINDINGS:BUDGET`) has an
`interrupted` result: with the spend at `epic.max_usd` it is a budget stop,
and a re-run after `epic.max_usd` is raised runs it again; under it, it runs
again with its share recomputed once the others end.
`epic.json` and each summary hold `spent_usd` and `spend_complete`.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from build_pipeline.budget import BudgetExceededError

from .. import budget, feature_branch, git_ops, intake, stop_record
from ..config import git_toggles
from ..models import AgentResult
from . import CHILD_ENV, fix, merge, records, review, spend, store
from .fix import write_decisions

log = logging.getLogger(__name__)

SCRIPTS_DIR = Path(__file__).resolve().parents[2]
POLL = 1.0  # seconds between looks at a running child
TICK = 30.0  # seconds between reads of the waited-on PRs
STOP_WAIT = 30.0  # seconds the children get to exit on SIGINT or SIGTERM
FETCH_TIMEOUT = 300  # seconds `git fetch origin` gets before a launch
TESTS_TIMEOUT = (
    300  # seconds the merge tests get, as `gates.run_test_suite` gives (40.4)
)
CHECKS_WAIT = 30.0  # seconds between reads of a PR's pending required checks (40.5)
CHECKS_APPEAR = 120.0  # seconds a head the step just pushed gets for its checks to appear (40.5; judgment)
CHECKS_LIMIT = 3600.0  # seconds 40.5 waits on pending checks before leaving the PR open (E.S8; judgment)
NO_CHECKS = re.compile(
    r"no (required )?checks reported", re.IGNORECASE
)  # gh 2.102's words

MARKER_SUMMARY = "EPIC:SUMMARY"
MARKER_MERGE_WAITING = "EPIC:MERGE_WAITING"
MARKER_BUDGET = "EPIC:BUDGET"
PR_CLOSED = "PR closed without a merge"
PR_UNREAD = "cannot read its PR's state: gh pr view failed"  # B1.S24: read again before the run ends
MERGE_UNKNOWN = "cannot merge: "  # the merge step's fetch or push failed: `unknown` for the rest of the run
NO_PR = "no PR opened"
REVIEW_KINDS = ("review", "resume")
FIX_KINDS = ("post", "fix")
PR_FIELDS = "number,state,headRefOid,mergeable,mergeStateStatus"


class ReadFailed(Exception):
    """A `git`, `gh` or `ps` read failed, so what it would have said is not known."""


class StartFailed(Exception):
    """A review step's process could not start (`uv` not on PATH): its record
    has its result, and the child is `unknown` until a run starts it."""


class Clock:
    """The one sleep-and-clock seam: every wait the coordinator makes."""

    def now(self) -> float:
        return asyncio.get_running_loop().time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


CLOCK = Clock()


def _tick(coord: Coordinator) -> None:
    """Called on every look at the running children. A seam for the row tests."""


def _merge_point(coord: Coordinator, c: Child, point: str) -> None:
    """Called at each named point of the merge step (`start`, `merged`,
    `push`, `checks`). A seam for the row tests."""


def _spawn(argv: list[str], log_path: Path) -> subprocess.Popen:
    """Start one child in its own process group, its output in *log_path*, with
    `CHILD_ENV` set so it does not run spec-extract (see `epic.CHILD_ENV`)."""
    with log_path.open("ab") as out:
        return subprocess.Popen(
            argv,
            cwd=SCRIPTS_DIR,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, CHILD_ENV: "1"},
        )


def _spawn_review(
    argv: list[str], log_path: Path, cwd: Path, env: dict | None = None
) -> subprocess.Popen:
    """Start one review or resume in its own process group, its output in
    *log_path*, so it outlives a killed coordinator (C.S2)."""
    with log_path.open("ab") as out:
        return subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )


# --- where a child builds -------------------------------------------------------


def _git(
    repo: Path, *args: str, timeout: int = git_ops._GIT_TIMEOUT
) -> git_ops.GitResult:
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
            "git",
            "fetch",
            "origin",
            cwd=str(repo),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        return str(e)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), FETCH_TIMEOUT)
    except TimeoutError:
        return f"timed out after {FETCH_TIMEOUT}s"
    finally:
        if proc.returncode is None:  # timed out, or the run was stopped
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        return err.decode(errors="replace").strip()[-400:] or f"exit {proc.returncode}"
    return None


def in_place_reason(project_dir: Path) -> str | None:
    """Why a child would not build in its own worktree and open a PR: the
    epic's project's specs/config.yaml, which every child reads, turns off
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
    return (
        f"{path} sets {' and '.join(off)}, so each child would build in place or open no PR; "
        "an epic needs each child in its own worktree with a pushed PR. Remove the setting and re-run."
    )


# --- is a stopped child still running --------------------------------------------


def _has(line: str, text: str) -> bool:
    return re.search(re.escape(text) + r"(?=\s|$)", line) is not None


def build_pid(change: str, project_dir: str) -> int | None:
    """The pid of a live build of the child change *change* in *project_dir*:
    one `ps` line holding `build_pipeline`, the word `build`, `--change
    <change>` and `--project-dir <project_dir>`, each followed by a space or
    the line's end, and not the word `findings` (item 14): a fixer of the same
    change (`fix.fix_argv`) carries `--change` too, and is not its build. The
    project dir is what the child was launched with: a change of the same
    name in another project is not this child. Raises ReadFailed when `ps`
    fails: a live child must never be taken for a gone one."""

    def match(line: str) -> bool:
        words = line.split()
        return (
            _has(line, "build_pipeline")
            and "build" in words
            and "findings" not in words
            and _has(line, f"--change {change}")
            and _has(line, f"--project-dir {project_dir}")
        )

    return _ps_find(match)


def fix_pid(number: int, repo: Path) -> int | None:
    """The pid of a live `findings` on PR *number* of *repo* (D2.S4, D2.S9):
    one `ps` line holding `build_pipeline`, `findings` and `--pr <number>` (or
    `--pr=<number>`). A `findings` started by hand, as buildme-findings'
    SKILL.md shows it, has no `--change` and may name the source repo, yet it
    runs in the worktree that has the PR's branch, the child's. So the line's
    `--project-dir` is compared only by repository: when it is an absolute
    path git reads, it must share *repo*'s git common directory. Any other
    line on the PR counts, as waiting on a fixer of another repo's PR with the
    same number only waits, and a second fixer here would not. Raises
    ReadFailed when `ps` fails."""
    ours = _common_dir(repo)

    def match(line: str) -> bool:
        if not (
            _has(line, "build_pipeline")
            and _has(line, "findings")
            and (_has(line, f"--pr {number}") or _has(line, f"--pr={number}"))
        ):
            return False
        named = re.search(r"--project-dir[ =](\S+)", line)
        if ours is None or named is None or not Path(named.group(1)).is_absolute():
            return True
        theirs = _common_dir(Path(named.group(1)))
        return theirs is None or theirs == ours

    return _ps_find(match)


def _common_dir(path: Path) -> Path | None:
    """*path*'s git common directory, the same for a repo and each of its
    worktrees; None when git cannot read it."""
    res = git_ops._run(["git", "-C", str(path), "rev-parse", "--git-common-dir"])
    out = res.stdout.strip()
    return (path / out).resolve() if res.ok and out else None


def post_pid(review_dir: str) -> int | None:
    """The pid of a live `review_pipeline post <review_dir>` (D2.S2): an
    earlier run's post runs in its own session, so it can outlive its
    coordinator. Raises ReadFailed when `ps` fails."""
    return _ps_pid("review_pipeline", f"post {review_dir}")


def _ps_pid(*needles: str) -> int | None:
    """The pid on the first `ps` line holding every needle, each followed by a
    space or the line's end."""
    return _ps_find(lambda line: all(_has(line, n) for n in needles))


def _ps_find(match: Callable[[str], bool]) -> int | None:
    """The pid on the first `ps` line *match* accepts."""
    try:
        ps = subprocess.run(
            ["ps", "-ax", "-ww", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ReadFailed(f"ps failed: {e}") from None
    if ps.returncode != 0 or not ps.stdout.strip():
        raise ReadFailed(
            f"ps failed: exit {ps.returncode} {ps.stderr.strip()[-200:]}".rstrip()
        )
    for line in ps.stdout.splitlines():
        if match(line):
            pid, _, _ = line.strip().partition(" ")
            if pid.isdigit():
                return int(pid)
    return None


def _opt(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv[:-1] else None


def _argv_pid(argv: list[str]) -> int | None:
    """`build_pid` for the build launched with *argv*; None when *argv* names
    no change or project dir. A command buildme's own epic split saved
    (`--mode brief --plan-file ...`) names both, so a build it started that
    still runs is found too."""
    change, project = _opt(argv, "--change"), _opt(argv, "--project-dir")
    return build_pid(change, project) if change and project else None


def _parses(argv: list[str]) -> bool:
    """Whether this version's `build_pipeline` command line accepts the saved
    command *argv* (`python -m build_pipeline ...`)."""
    from ..__main__ import build_parser

    rest = (
        argv[argv.index("build_pipeline") + 1 :] if "build_pipeline" in argv else argv
    )
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            build_parser().parse_args(rest)
    except SystemExit:
        return False
    return True


# --- the coordinator ---------------------------------------------------------------


@dataclass
class Share:
    """What a running fixer was given of `epic.max_usd` (its `--max-usd`), and
    its PR's review spend in the ledger when it was launched. Kept in memory
    only, for the run that launched it."""

    usd: float
    reviews: float


@dataclass
class Child:
    id: str
    depends_on: list[str]
    status: str = "pending"
    pr: str | None = None
    reason: str = ""
    argv: list[str] | None = None  # the command to relaunch with, when set
    proc: subprocess.Popen | None = None
    attempt: records.Attempt | None = None
    relaunched: bool = False  # a failure was relaunched in this invocation
    pr_round: int | None = None  # item 23, cached in epic.json
    conflicts: bool | None = None
    reviewed: bool = False  # its review step ran in this invocation
    task: asyncio.Task | None = None  # the review step, while it runs
    rproc: subprocess.Popen | None = None  # the review or resume process
    rattempt: records.Attempt | None = None
    merge_stop: stop_record.StepStop | None = None  # PR E, item 39
    merge_park: str = ""  # why the merge step parked it; kept until <W> is clean
    head: str | None = None  # its PR's head at the last read in this run


@dataclass
class Coordinator:
    config: object
    args: object
    epic_dir: Path
    epic: store.Epic
    branch: str  # the epic's feature branch: every child's base and PR target
    children: dict[str, Child] = field(default_factory=dict)
    signal_sent: int | None = None
    budget_stop: float | None = None  # the spend that stopped launches, once reached
    shares: dict[str, Share] = field(
        default_factory=dict
    )  # running fixers' --max-usd, per child
    merge_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock
    )  # item 35: one merge at a time
    waves: set[int] = field(
        default_factory=set
    )  # the dependency levels this run launched a child of
    conflict: list[str] | None = (
        None  # the files a merge of main into the branch conflicted in
    )

    def __post_init__(self) -> None:
        self.project_dir = Path(self.config.project_dir)
        self.repo = git_ops.repo_root(self.project_dir) or self.project_dir
        self.settings = self.config.epic
        # Decision 4: the children's PRs target the feature branch, so unless
        # config.yaml says otherwise they merge on buildme's own review.
        if not getattr(self.settings, "merge_set", True) and self.branch != (
            feature_branch.default_name(self.repo)
        ):
            self.settings = dataclasses.replace(self.settings, merge="auto")
        self.slug = store.slug_repo_name(self.repo)
        for c in self.epic.children:
            self.children[c.id] = Child(c.id, list(c.depends_on))
            cached = self.epic.child_status.get(c.id)
            if cached is not None:
                self.children[c.id].pr_round, self.children[c.id].conflicts = (
                    cached.pr_round,
                    cached.conflicts,
                )
                self.children[c.id].merge_stop, self.children[c.id].merge_park = (
                    cached.merge_stop,
                    cached.merge_park,
                )

    # -- argv -----------------------------------------------------------------

    def new_argv(self, cid: str) -> list[str]:
        """Item 11's command for a first launch: the child's own change, whose
        ready specs pm-spec wrote, started from `origin/<feature branch>`
        (`--base-ref`) with its PR against the branch (`--branch`). Each child
        runs its own codebase analysis and research (`--skip-research` only
        when the parent run had it), and no spec-extract (`--epic-child`, see
        `epic.CHILD_ENV`). The path is absolute."""
        argv = [
            "python",
            "-m",
            "build_pipeline",
            "--session-id",
            getattr(self.args, "session_id", "") or "",
            "build",
            "--change",
            f"{self.epic.name}-{cid}",
            "--auto",
            "--trust-repo",
            "--epic-child",
            "--pr-ready",
            "--project-dir",
            str(self.project_dir.absolute()),
            "--base-ref",
            feature_branch.ref(self.branch),
            "--branch",
            self.branch,
        ]
        if getattr(self.config, "skip_research", False):
            argv.append("--skip-research")
        if getattr(self.config, "accept_unknown_spend", False):
            argv.append("--accept-unknown-spend")
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
            raise ReadFailed(
                f"git worktree list failed: {porcelain.stderr.strip()[-200:]}"
            )
        wanted = {Path(w).resolve() for w in worktrees}
        for block in porcelain.stdout.split("\n\n"):
            lines = block.splitlines()
            path = next(
                (ln[len("worktree ") :] for ln in lines if ln.startswith("worktree ")),
                None,
            )
            ref = next(
                (
                    ln[len("branch refs/heads/") :]
                    for ln in lines
                    if ln.startswith("branch refs/heads/")
                ),
                None,
            )
            if path and Path(path).resolve() in wanted and ref and ref not in branches:
                branches.append(ref)
        for branch in branches:
            listed = _gh(
                self.repo,
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "url",
            )
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
            except ReadFailed as e:  # B1.S20: no launch until a read answers
                c.status, c.reason = (
                    "unknown",
                    f"cannot tell whether it opened a PR: {e}",
                )
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
        elif code == 3 and (
            records.wait_marker(last.get("markers", [])) or ""
        ).startswith("BUILD:COMPLETE:"):
            # The build already published on an earlier attempt, which then
            # exited non-zero (an error or a stop after the publish) and was
            # relaunched; the relaunch refuses to build it again. It is built
            # when its PR can be found.
            try:
                c.pr = c.pr or self._find_pr(tries)
            except ReadFailed as e:
                c.status, c.reason = (
                    "unknown",
                    f"cannot tell whether it opened a PR: {e}",
                )
                return
            if c.pr:
                c.status, c.reason = "built", ""
            else:
                c.status, c.reason = (
                    "parked",
                    (
                        "already published (BUILD:COMPLETE), but no PR found: "
                        + records.wait_marker(last.get("markers", []))[
                            len("BUILD:COMPLETE:") :
                        ]
                    ),
                )
        elif code == 3:
            c.status, c.reason = (
                "parked",
                records.wait_marker(last.get("markers", [])) or "exit 3",
            )
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
                try:
                    live = (
                        a is tries[-1]
                        and _argv_pid(a.started.get("argv", [])) is not None
                    )
                except ReadFailed as e:  # B1.S21: never relaunch what may still run
                    c.status, c.reason = (
                        "unknown",
                        f"cannot tell whether its build is still running: {e}",
                    )
                    break
                if live:
                    c.status, c.attempt = "running", a
                    log.info(
                        "EPIC child %s: its build from an earlier run is alive; waiting for it",
                        cid,
                    )
                    break
                records.finish(a, None, interrupted=True)
            else:
                self._classify(c, tries)
        for a in records.attempts(
            self.epic_dir
        ):  # a review's or fix's park stays (C.S6, C.S12, D2.S6)
            c = self.children.get(a.child)
            if (
                c is not None
                and a.kind in (*REVIEW_KINDS, *FIX_KINDS)
                and (a.result or {}).get("parked")
            ):
                c.status, c.reason = "parked", a.result["parked"]
        for c in (
            self.children.values()
        ):  # and a merge step's (PR E), until <W> is clean (E.S26)
            if c.merge_park and not self._merge_retry(c):
                c.status, c.reason = "parked", c.merge_park

    # -- dependencies and merges -------------------------------------------------

    def _waited_on(self) -> list[Child]:
        """Children with a PR that a child not yet done depends on, or that
        passed review (item 41; under `auto`, one the merge step left open): open, or
        `unknown` because the last read of it failed. A child the merge step
        left `unknown` (`MERGE_UNKNOWN`), or one only reviewed whose review
        failed, stays so: a read of its PR never makes it `built` again."""
        deps = {
            d
            for c in self.children.values()
            if c.status in ("pending", "waiting", "failed")
            for d in c.depends_on
        }

        def waits(c: Child) -> bool:
            if c.status == "built":
                return c.id in deps or self._passed(c)
            if c.status != "unknown" or not c.pr or c.reason.startswith(MERGE_UNKNOWN):
                return False
            return c.id in deps or (
                c.reason == PR_UNREAD and self._clean_head(c) is not None
            )

        return [c for c in self.children.values() if waits(c)]

    def _read_prs(self, which: list[Child]) -> None:
        """`gh pr view` each PR. A failed read is not "still open": the child is
        `unknown` until a read answers (B1.S24)."""
        for c in which:
            got = _gh(self.repo, "pr", "view", c.pr, "--json", "state,headRefOid")
            state = got.get("state") if isinstance(got, dict) else None
            c.head = (got.get("headRefOid") or None) if state is not None else None
            if state == "MERGED":
                c.status, c.reason = "merged", ""
            elif state == "CLOSED":
                c.status, c.reason = "parked", PR_CLOSED
            elif state is None:
                c.status, c.reason = "unknown", PR_UNREAD
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
                stuck = [
                    d
                    for d in c.depends_on
                    if self.children[d].status in ("parked", "skipped")
                ]
                if stuck:
                    c.status, c.reason, changed = (
                        "skipped",
                        f"dependency {stuck[0]} {self.children[stuck[0]].status}",
                        True,
                    )
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

    def _level(self, cid: str, seen: frozenset = frozenset()) -> int:
        """*cid*'s wave: 0 with no dependency, else one more than its deepest dependency's."""
        deps = [
            d
            for d in self.children[cid].depends_on
            if d in self.children and d not in seen
        ]
        return 1 + max((self._level(d, seen | {cid}) for d in deps), default=-1)

    async def _merge_main(self) -> str | None:
        """Merge origin's default branch into the feature branch and push
        (`feature_branch.merge_default`). None when done or nothing was new;
        a conflict sets `conflict` and returns its reason; another failure
        returns git's words."""
        merged = await asyncio.to_thread(
            feature_branch.merge_default, self.repo, self.branch
        )
        if merged.outcome == "conflict":
            self.conflict = merged.files
            log.error(
                "EPIC %s: merging origin's default branch conflicts in %s",
                self.branch,
                merged.files,
            )
            return f"merging origin's default branch into {self.branch} conflicts in {', '.join(merged.files)}"
        if merged.outcome == "failed":
            return f"cannot merge origin's default branch into {self.branch}: {merged.error}"
        return None

    async def _launch(self, c: Child) -> bool:
        """Start *c* from `origin/<feature branch>`. Before the first launch
        of a wave (`_level`) that is not the run's first, origin's default
        branch is merged into the feature branch (`_merge_main`); then `git
        fetch origin` must work. False when it was not started, with nothing
        written for it but its status: the merge conflicted (*c* stays
        pending and nothing more is launched) or failed, the fetch failed, the
        branch is gone from origin, or its specs/config.yaml is not the
        branch's (B1.S22). The next run then fetches again."""
        if self.conflict is not None:
            return False
        if c.status == "failed":
            c.relaunched = True
        argv = c.argv
        if argv is not None and not _parses(argv):
            # Saved by buildme's own epic split (`--mode brief --plan-file ...`),
            # whose flags this version removed: argparse would refuse it (exit 2).
            log.info(
                "EPIC child %s: its saved command does not parse any more; building a new one",
                c.id,
            )
            argv = None
        base = feature_branch.ref(self.branch)
        if argv is not None and (
            _opt(argv, "--base-ref") != base or _opt(argv, "--branch") != self.branch
        ):
            # Saved before the children started from the feature branch.
            log.info(
                "EPIC child %s: its saved command does not start from %s; building a new one",
                c.id,
                base,
            )
            argv = None
        level = self._level(c.id)
        if level not in self.waves:
            why = await self._merge_main() if self.waves else None
            if why is not None:
                if self.conflict is None:
                    c.status, c.reason = "unknown", why
                    self._save()
                return False
            self.waves.add(level)
        failed = await _fetch_origin(self.repo)
        sha = (
            feature_branch._remote_sha(self.repo, self.branch)
            if failed is None
            else None
        )
        if sha is None:
            c.status, c.reason = (
                "unknown",
                f"cannot start from {base}: "
                + (
                    f"git fetch origin failed: {failed}"
                    if failed
                    else f"origin has no {self.branch}"
                ),
            )
            self._save()
            return False
        if intake.config_yaml_off_ref(self.project_dir, base):
            c.status, c.relaunched = "failed", True
            c.reason = f"in {self.project_dir}: {intake.CONFIG_NOT_ON_BASE}"
            self._save()
            return False
        if argv is None:
            argv = self.new_argv(c.id)
        # A saved command must use this invocation's explicit acceptance, not
        # a stale flag from a previous epic run.
        argv = [arg for arg in argv if arg != "--accept-unknown-spend"]
        if getattr(self.config, "accept_unknown_spend", False):
            argv.append("--accept-unknown-spend")
        c.attempt = records.start(self.epic_dir, "build", c.id, argv, head_sha=sha)
        c.status, c.argv, c.reason = "running", argv, ""
        self._save()
        c.proc = _spawn(argv, c.attempt.log)
        log.info(
            "EPIC child %s: launched pid %d (change %s) from %s at %s",
            c.id,
            c.proc.pid,
            _opt(argv, "--change"),
            base,
            sha[:12],
        )
        # One start at a time: worktree creation in one repo is not run concurrently.
        while c.proc.poll() is None and not records.marker(
            records.markers(c.attempt.log), "WORKTREE"
        ):
            await CLOCK.sleep(POLL)
        return True

    def _done(self, c: Child, exit_code: int | None, *, interrupted: bool) -> None:
        records.finish(c.attempt, exit_code, interrupted=interrupted)
        tries = [
            a
            for a in records.attempts(self.epic_dir)
            if a.kind == "build" and a.child == c.id
        ]
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
                    gone = _argv_pid(c.attempt.started["argv"]) is None
                except ReadFailed as e:  # B1.S21
                    c.status, c.reason, c.attempt = (
                        "unknown",
                        f"cannot tell whether its build is still running: {e}",
                        None,
                    )
                    self._save()
                    continue
                if gone:
                    self._done(c, None, interrupted=True)

    # -- reviewing a child's PR (PR C) ----------------------------------------------

    def _review_due(self) -> list[Child]:
        """Children with an open PR whose review step has not run in this invocation."""
        return [
            c for c in self.children.values() if c.status == "built" and not c.reviewed
        ]

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
        source repo's root when there is none or it is gone (removed by hand)."""
        found = [
            m
            for a in records.attempts(self.epic_dir)
            if a.kind == "build" and a.child == c.id
            for m in records.markers(a.log)
        ]
        trees = records.marker(found, "WORKTREE")
        return Path(trees[-1]) if trees and Path(trees[-1]).is_dir() else self.repo

    async def _run_review(
        self, c: Child, kind: str, argv: list[str], head: str
    ) -> tuple[int, str]:
        """Run one `review` or `resume` to its end, as a pair of records; its
        exit code and output. The process is in its own group with its output
        in the attempt's log, so it outlives a killed coordinator (C.S2).
        Raises StartFailed when it cannot start."""
        c.rattempt = records.start(
            self.epic_dir, kind, c.id, argv, head_sha=head, pr_round=c.pr_round
        )
        self._save()
        try:
            c.rproc = _spawn_review(argv, c.rattempt.log, self._worktree(c))
        except OSError as e:
            records.finish(c.rattempt, None, interrupted=False)
            raise StartFailed(f"its {kind} could not start: {e}") from None
        while c.rproc.poll() is None:
            await CLOCK.sleep(POLL)
        code, c.rproc = c.rproc.returncode, None
        return code, review.log_text(c.rattempt.log)

    def _fields(
        self, c: Child, line: dict, review_dir: str, number: int, *, reused: bool
    ) -> dict:
        """Item 22's fields of a review result, from its ledger line; with
        blockers, `next` and `next_reason`, what follows it (`review.decide`);
        with `parked` when that is `park` (an open HIGH at the round cap) or
        its findings cannot be read."""
        c.pr_round = review.pr_round(review.ledger_lines(), self.slug, number)
        blocking = review.blockers(review_dir)
        fields = {
            "head_sha": line.get("head_sha"),
            "base_sha": line.get("base_sha"),
            "review_dir": review_dir,
            "cost_usd": (line.get("run") or {}).get("cost_usd"),
            "pr_round": c.pr_round,
            "reused": reused,
            "conflicts": c.conflicts,
            "blocking": blocking or [],
            "reused_pipeline_run": (
                {"target_dir": line.get("target_dir"), "round": line.get("round")}
                if reused
                else None
            ),
        }
        cap = self.settings.max_review_rounds
        if blocking is None:
            fields["parked"] = f"cannot read the review's findings in {review_dir}"
        elif blocking:
            fields["next"], fields["next_reason"] = review.decide(
                review.blocking_findings(review_dir) or [],
                c.pr_round,
                cap,
                self.settings.round_worth,
            )
            if fields["next"] == "park":
                fields["parked"] = (
                    f"review round {c.pr_round} of {cap} left {len(blocking)} blocker(s): "
                    + "; ".join(blocking)
                )
        return fields

    def _reviewed(self, c: Child, fields: dict) -> None:
        if fields.get("parked"):
            c.status, c.reason = "parked", fields["parked"]
        else:
            c.status, c.reason = "built", ""
        log.info(
            "EPIC child %s: reviewed at %s, round %s, %d blocker(s)%s",
            c.id,
            fields.get("head_sha"),
            fields.get("pr_round"),
            len(fields.get("blocking") or []),
            f"; parked: {c.reason}" if c.status == "parked" else "",
        )

    def _reuse(self, c: Child, line: dict, head: str, number: int) -> None:
        """Item 24, found: no review. A started record for this head with no
        result gets its result now (C.S2, C.S4); when no record of this head
        holds a review's fields (none at all, or only the exit-2 `is running
        now` asks), one pair is written; a head whose review is recorded
        writes nothing."""
        tries = [
            a
            for a in records.attempts(self.epic_dir)
            if a.child == c.id
            and a.kind in REVIEW_KINDS
            and a.started.get("head_sha") == head
        ]
        fields = self._fields(c, line, review.review_folder(line), number, reused=True)
        open_ = [a for a in tries if a.result is None]
        for a in open_[:-1]:
            records.finish(a, None, interrupted=True)
        if open_:
            records.finish(open_[-1], None, interrupted=True, extra=fields)
        elif not any("blocking" in a.result for a in tries):
            argv = review.review_argv(self._worktree(c), number)
            a = records.start(
                self.epic_dir, "review", c.id, argv, head_sha=head, pr_round=c.pr_round
            )
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
                if (
                    not isinstance(view, dict)
                    or not view.get("headRefOid")
                    or not isinstance(view.get("number"), int)
                ):
                    c.status, c.reason = (
                        "unknown",
                        "cannot read its PR: gh pr view failed",
                    )
                    return
                if view.get("state") in ("MERGED", "CLOSED"):
                    c.status, c.reason = (
                        ("merged", "")
                        if view["state"] == "MERGED"
                        else ("parked", PR_CLOSED)
                    )
                    return
                number, head = view["number"], view["headRefOid"]
                c.head = head
                c.conflicts = review.conflicts(view)
                pending = await self._fix_pending(c, number, head)
                if (
                    pending is not None
                ):  # a fixer an earlier run left (D2.S4, S5, S7, S11)
                    if pending:
                        continue
                    return
                if head == self._final_head(
                    c
                ):  # R3: the final fix's head is never reviewed
                    c.status, c.reason = "built", ""
                    await self._after_review(c, head)
                    return
                if self._merge_head(
                    c, head
                ):  # E.S8, E.S13: its own pushed merge, not reviewed again
                    await self._merge(c)
                    return
                lines = review.ledger_lines()
                running = review.running_dir(lines, self.slug, head)
                if running is not None:
                    if review.lock_held(
                        running
                    ):  # C.S2: it runs now; never two at once
                        await CLOCK.sleep(review.REVIEW_WAIT)
                        continue
                    # Its lock is free, so it ended: read again, as its `finished`
                    # line may have come after the read above.
                    lines = review.ledger_lines()
                c.pr_round = review.pr_round(lines, self.slug, number)
                line = review.finished_line(lines, self.slug, number, head)
                if line is not None:
                    self._reuse(c, line, head, number)
                    if await self._fix(c, head, number):
                        continue
                    await self._after_review(c, head)
                    return
                if not await self._unheld():  # Decided 6, D2.S18: before each launch
                    c.status, c.reason = "built", ""
                    return
                stale = [
                    a
                    for a in records.attempts(self.epic_dir)
                    if a.child == c.id and a.kind in REVIEW_KINDS and a.result is None
                ]
                code, out = await self._run_review(
                    c, "review", review.review_argv(self._worktree(c), number), head
                )
                if code == 2 and review.RUNNING_NOW in out:  # C.S2: never two at once
                    records.finish(c.rattempt, code, interrupted=False)
                    await CLOCK.sleep(review.REVIEW_WAIT)
                    continue
                for a in stale:  # `review` answered, so an earlier one is gone
                    records.finish(a, None, interrupted=True)
                hint = review.INTERRUPTED.search(out) if code == 4 else None
                if hint:  # C.S3
                    records.finish(c.rattempt, code, interrupted=False)
                    if not await self._unheld():
                        c.status, c.reason = "built", ""
                        return
                    code, out = await self._run_review(
                        c, "resume", review.resume_argv(hint.group(1).strip()), head
                    )
                    if code == 2 and review.RUNNING_NOW in out:
                        records.finish(c.rattempt, code, interrupted=False)
                        await CLOCK.sleep(review.REVIEW_WAIT)
                        continue
                found = review.findings_path(out)
                line = review.finished_line(
                    review.ledger_lines(), self.slug, number, head
                )
                if code == 0 and found and line is not None:
                    fields = self._fields(
                        c, line, str(Path(found).parent), number, reused=False
                    )
                    records.finish(c.rattempt, code, interrupted=False, extra=fields)
                    self._reviewed(c, fields)
                    if await self._fix(c, head, number):
                        continue
                    await self._after_review(c, head)
                    return
                if code == 0 and found:
                    # The review reads the PR's head itself, after its fetch: a
                    # commit pushed since the read above makes it review and
                    # record that newer head (C.S8, C.S19). Read the PR again.
                    again = _gh(self.repo, "pr", "view", c.pr, "--json", PR_FIELDS)
                    if not isinstance(again, dict) or not again.get("headRefOid"):
                        records.finish(c.rattempt, code, interrupted=False)
                        c.status, c.reason = (
                            "unknown",
                            "cannot read its PR: gh pr view failed",
                        )
                        return
                    if again["headRefOid"] != head:
                        c.head = again["headRefOid"]
                        records.finish(c.rattempt, code, interrupted=False)
                        continue
                if fix.stopped(
                    code
                ):  # C.S3: killed, or 128 + signal as `uv run` reports it
                    records.finish(c.rattempt, code, interrupted=True)
                    c.status, c.reason = "built", ""
                    log.warning(
                        "EPIC child %s: its review was stopped (exit %d); the next run asks it again",
                        c.id,
                        code,
                    )
                    return
                if code == 2 and review.READ_FAILED.search(
                    out
                ):  # C.S20: a failed read, not a park
                    records.finish(c.rattempt, code, interrupted=False)
                    c.status, c.reason = (
                        "unknown",
                        f"its review could not read git or GitHub: {review.tail(out)}",
                    )
                    log.warning("EPIC child %s: %s", c.id, c.reason)
                    return
                why = (
                    f"review exited {code}: {review.tail(out)}"
                    if code != 0
                    else f"review exited 0 without a findings line and a finished ledger line: {review.tail(out)}"
                )
                records.finish(
                    c.rattempt, code, interrupted=False, extra={"parked": why}
                )
                c.status, c.reason = "parked", why
                log.warning("EPIC child %s: parked: %s", c.id, why)
                return
        except StartFailed as e:  # not the PR's fault: the next run starts it again
            c.status, c.reason = "unknown", str(e)
            log.warning("EPIC child %s: %s", c.id, c.reason)
        finally:
            if c.rproc is None:
                c.rattempt = None
            self._save()

    # -- fixing a reviewed PR (PR D2) ---------------------------------------------

    def _child_project(self, c: Child) -> Path:
        """`<P>`: the child's worktree joined with the project's offset in its repo, absolute."""
        try:
            offset = self.project_dir.resolve().relative_to(self.repo.resolve())
        except ValueError:
            offset = Path()
        return (self._worktree(c) / offset).absolute()

    def _park(self, c: Child, why: str) -> None:
        c.status, c.reason = "parked", why
        log.warning("EPIC child %s: parked: %s", c.id, why)

    async def _run_step(
        self,
        c: Child,
        attempt: records.Attempt,
        cwd: Path,
        *,
        env: dict | None = None,
        timeout: float | None = None,
    ) -> tuple[int | None, str]:
        """Run a started `post`, `fix` or `merge-tests` to its end, in its own
        session with its output in the attempt's log, as `_run_review` runs a
        review; its exit code and output. None when it ran past *timeout*
        seconds and was killed. A process that cannot start raises `OSError`."""
        c.rattempt = attempt
        self._save()
        c.rproc = _spawn_review(attempt.started["argv"], attempt.log, cwd, env)
        until = None if timeout is None else CLOCK.now() + timeout
        while c.rproc.poll() is None:
            if until is not None and CLOCK.now() >= until:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(c.rproc.pid, signal.SIGKILL)
                c.rproc.wait()
                c.rproc = None
                return None, f"timed out after {timeout:.0f}s"
            await CLOCK.sleep(POLL)
        code, c.rproc = c.rproc.returncode, None
        return code, review.log_text(attempt.log)

    async def _gone(self, c: Child, what: str, find: Callable[[], int | None]) -> bool:
        """Wait while *find* sees a live process of this child: never two at
        once (D2.S2, D2.S4, D2.S9). False, with the child `unknown`, when `ps`
        fails."""
        while True:
            try:
                if find() is None:
                    return True
            except ReadFailed as e:
                c.status, c.reason = (
                    "unknown",
                    f"cannot tell whether its {what} is still running: {e}",
                )
                log.warning("EPIC child %s: %s", c.id, c.reason)
                return False
            await CLOCK.sleep(POLL)

    async def _fixer_gone(self, c: Child, number: int) -> bool:
        return await self._gone(c, "fixer", lambda: fix_pid(number, self.repo))

    def _rounds_left(self, c: Child) -> int:
        return self.settings.max_review_rounds - (c.pr_round or 0)

    def _rounds_for_fixer(self, c: Child, project: Path) -> int:
        """`<M>` (32.2): one round per fixer launch, so a decision follows
        every review (Spike, 2026-10-15). Every round in the change's findings
        state, finished or not, plus one: `findings` resumes an open round
        first, and stops at the count."""
        return fix.rounds_started(project, f"{self.epic.name}-{c.id}") + 1

    async def _run_fix(
        self, c: Child, head: str, number: int, rounds: int, *, final: bool = False
    ) -> bool:
        """32.2: run `findings` on *c*'s PR. True when it exited 0, so the review
        step reads the PR again; else the child is parked (`FINDINGS:FAILED`, or
        any other exit), `built` when the fixer was stopped by a signal (killed,
        or exit 128 + SIGTERM or SIGHUP; its result `interrupted`, so the next
        run runs it again, D2.S5), or `unknown`. It is given what is left of
        `epic.max_usd` (`_left`) as `--max-usd`, after waiting while other
        running fixers hold all of it (D2.S18). When it stopped there
        (`FINDINGS:BUDGET`) its result is `interrupted`. With the epic's spend
        at `epic.max_usd`, the budget is reached: the child stays `built`, a
        run with a higher `epic.max_usd` runs it again (D2.S16), and nothing
        more is launched. Under it, the fixer used up only its share while
        others held the rest: True, so the review step runs it again with
        its share recomputed (D2.S19). *final*: the final fix (`--no-review`),
        whose result keeps `final_head`, the HEAD it left in `<P>`."""
        project = self._child_project(c)
        change = f"{self.epic.name}-{c.id}"
        if not await self._fixer_gone(c, number):
            return False
        ceiling = self.settings.max_usd
        c.status = "reviewing"
        left = None if ceiling is None else self._left(c.id)
        while left is not None and left <= 0:
            if self._over_budget():
                c.status, c.reason = "built", ""
                return False
            await CLOCK.sleep(POLL)  # D2.S18: wait for the fixers that hold it
            left = self._left(c.id)
        argv = fix.fix_argv(
            getattr(self.args, "session_id", "") or "",
            number,
            change,
            project,
            rounds,
            left,
            final=final,
            accept_unknown_spend=getattr(self.config, "accept_unknown_spend", False),
        )
        attempt = records.start(
            self.epic_dir, "fix", c.id, argv, head_sha=head, pr_round=c.pr_round
        )
        if left is not None:
            self.shares[c.id] = Share(max(left, 0.0), self._pr_reviews(c))
        try:
            code, out = await self._run_step(c, attempt, project)
        finally:
            self.shares.pop(c.id, None)
        if code == 0 and fix.budget_stopped(out):
            records.finish(attempt, code, interrupted=True)
            if (
                not self._over_budget()
            ):  # D2.S19: only its share; the others' real spend decides
                log.info(
                    "EPIC child %s: its fixer stopped at its $%.2f share of epic.max_usd $%.2f; "
                    "it runs again once the other fixers end",
                    c.id,
                    left or 0.0,
                    ceiling or 0.0,
                )
                return True
            c.status, c.reason = "built", ""
            log.warning(
                "EPIC child %s: its fixer stopped at the $%.2f left of epic.max_usd $%.2f: "
                "launching nothing more",
                c.id,
                left or 0.0,
                ceiling or 0.0,
            )
            return False
        if code == 0 and final:
            left_head = git_ops.rev_parse_head(project)
            if left_head is None:
                why = f"cannot read HEAD in {project} after the final fix"
                records.finish(attempt, code, interrupted=False, extra={"parked": why})
                self._park(c, why)
                return False
            records.finish(
                attempt, code, interrupted=False, extra={"final_head": left_head}
            )
            if fix.acted_on_nothing(out):
                log.warning(
                    "EPIC child %s: its final fix found no new comment to act on (%s), so no fix ran; "
                    "its blockers go to the merge step and to its issue as they are",
                    c.id,
                    fix.ended(out),
                )
            return True
        if code == 0:
            records.finish(attempt, code, interrupted=False)
            return True
        if fix.stopped(code):
            records.finish(attempt, code, interrupted=True)
            c.status, c.reason = "built", ""
            log.warning(
                "EPIC child %s: its fixer was stopped (exit %d); the next run runs it again",
                c.id,
                code,
            )
            return False
        why = fix.failed_reason(out) or f"the fixer exited {code}: {review.tail(out)}"
        records.finish(attempt, code, interrupted=False, extra={"parked": why})
        self._park(c, why)
        return False

    async def _post(self, c: Child, rec: records.Attempt, head: str) -> bool:
        """32.1: choose what to post from the review's `findings.json`, then post
        it. True when `post` exited 0; else the child is parked, or `built`
        when `post` was stopped by a signal (killed, or 128 + SIGKILL, SIGTERM
        or SIGHUP as `uv run` reports it; the next run posts again, D2.S2)."""
        review_dir = str(rec.result.get("review_dir"))
        c.status = "reviewing"
        attempt = records.start(
            self.epic_dir,
            "post",
            c.id,
            fix.post_argv(review_dir),
            head_sha=head,
            pr_round=c.pr_round,
        )
        try:
            write_decisions(review_dir)
        except (OSError, ValueError) as e:
            why = f"cannot choose what to post: {e}"
            records.finish(attempt, None, interrupted=False, extra={"parked": why})
            self._park(c, why)
            return False
        try:
            code, out = await self._run_step(c, attempt, self._child_project(c))
        except (
            OSError
        ) as e:  # it could not start (`uv` not on PATH): the next run posts again
            records.finish(attempt, None, interrupted=False)
            c.status, c.reason = "unknown", f"its post could not start: {e}"
            log.warning("EPIC child %s: %s", c.id, c.reason)
            return False
        if code == 0:
            records.finish(attempt, code, interrupted=False)
            return True
        if fix.stopped(code):  # killed, or 128 + signal as `uv run` reports it
            records.finish(attempt, code, interrupted=True)
            c.status, c.reason = "built", ""
            log.warning(
                "EPIC child %s: its post was stopped (exit %d); the next run posts again",
                c.id,
                code,
            )
            return False
        why = f"posting the review exited {code}: {review.tail(out)}"
        records.finish(attempt, code, interrupted=False, extra={"parked": why})
        self._park(c, why)
        return False

    async def _fix(self, c: Child, head: str, number: int) -> bool:
        """Items 32-33, after *head*'s review is recorded: when it left blockers
        and no `fix` record follows it, post (unless a `post` exited 0 after
        it) and run the fixer. True when the fixer ran to its end. When a `fix`
        follows it that exited 0 saying it is done (`FINDINGS:DONE` or
        `FINDINGS:MAX_ROUNDS`), the head it left is this one and nothing will
        fix its blockers: the child parks (D2.S15). A review whose `next` is
        `final_fix` runs the final fix: one fresh round, `--no-review`."""
        if c.status != "built":
            return False
        tries = [a for a in records.attempts(self.epic_dir) if a.child == c.id]
        reviewed = [
            a
            for a in tries
            if a.kind in REVIEW_KINDS
            and a.started.get("head_sha") == head
            and a.result is not None
            and "blocking" in a.result
        ]
        if not reviewed or not reviewed[-1].result["blocking"]:
            return False
        rec = reviewed[-1]
        later = [a for a in tries if a.n > rec.n and a.kind in FIX_KINDS]
        fixed = [a for a in later if a.kind == "fix"]
        if fixed:  # this head's fixer ran
            last = fixed[-1]
            end = (
                fix.ended(review.log_text(last.log))
                if last.result is not None and last.result.get("exit_code") == 0
                else None
            )
            if end:  # D2.S15: it is done, and the head it left still has blockers
                self._park(
                    c,
                    f"the fixer ended ({end}) and left {head[:12]} with "
                    f"{len(rec.result['blocking'])} blocker(s): "
                    + "; ".join(rec.result["blocking"]),
                )
            return False
        if self._over_budget():  # Decided 6: before each launch
            return False
        if not any(
            a.kind == "post" and (a.result or {}).get("exit_code") == 0 for a in later
        ):
            if not await self._post(c, rec, head):
                return False
            if self._over_budget():
                c.status = "built"
                return False
        rounds = self._rounds_for_fixer(c, self._child_project(c))
        return await self._run_fix(
            c, head, number, rounds, final=rec.result.get("next") == "final_fix"
        )

    async def _fix_pending(self, c: Child, number: int, head: str) -> bool | None:
        """Item 33's resume. A `post` an earlier run left is waited for while it
        runs, then gets its result (D2.S2: the next post skips what it posted).
        When the last `fix` has no result (its coordinator was stopped) or an
        `interrupted` one, wait while it runs and write its result; when its
        log ends in `FINDINGS:FAILED`, park with the reason (D2.S6), else run
        it again with the `--max-rounds` it was launched with (D2.S4, S5,
        S7, S11), so it stops where a fixer that never stopped would: True
        when it exited 0, False when the step ends, None when no fixer is to
        be run again."""
        tries = [
            a
            for a in records.attempts(self.epic_dir)
            if a.child == c.id and a.kind in FIX_KINDS
        ]
        for a in tries:
            if a.kind == "post" and a.result is None:
                argv = a.started.get("argv") or []
                target = argv[argv.index("post") + 1] if "post" in argv[:-1] else None
                if target is not None and not await self._gone(
                    c, "post", lambda t=target: post_pid(t)
                ):
                    return False
                records.finish(a, None, interrupted=True)
        fixes = [a for a in tries if a.kind == "fix"]
        if not fixes or (
            fixes[-1].result is not None and not fixes[-1].result.get("interrupted")
        ):
            return None
        last = fixes[-1]
        if last.result is None and not await self._fixer_gone(c, number):
            return False
        why = fix.failed_reason(review.log_text(last.log))
        if why:  # it ended refusing or failing: running it again repeats that step
            code = (last.result or {}).get("exit_code")
            records.finish(last, code, interrupted=False, extra={"parked": why})
            self._park(c, why)
            return False
        if last.result is None:
            records.finish(last, None, interrupted=True)
        argv = last.started.get("argv") or []
        final = fix.is_final(argv)
        if not final:
            c.pr_round = review.pr_round(review.ledger_lines(), self.slug, number)
            if (
                self._rounds_left(c) < 1
            ):  # no review round left: the review step decides
                return None
        if self._over_budget():
            c.status = "built"
            return False
        # R2: once more, as launched; recounted, a finished round would run again.
        return await self._run_fix(c, head, number, fix.saved_rounds(argv), final=final)

    # -- merging a reviewed PR (PR E) ---------------------------------------------

    def _last_review(self, c: Child) -> records.Attempt | None:
        """*c*'s last recorded review that holds a review's fields."""
        done = [
            a
            for a in records.attempts(self.epic_dir)
            if a.child == c.id
            and a.kind in REVIEW_KINDS
            and a.result is not None
            and "blocking" in a.result
        ]
        return done[-1] if done else None

    def _clean_head(self, c: Child) -> str | None:
        """The head of *c*'s last recorded review when it left no blocker and
        did not park the child: the last clean reviewed head; or, when that
        review chose `final_fix`, the head its final fix left (`_final_head`)."""
        last = self._last_review(c)
        if last is None or last.result.get("parked"):
            return None
        if last.result["blocking"]:
            return self._final_head(c)
        return last.started.get("head_sha")

    def _final_fix(self, c: Child) -> records.Attempt | None:
        """The final fix that ended after *c*'s last recorded review, when that
        review chose `final_fix`; else None."""
        last = self._last_review(c)
        if last is None or last.result.get("next") != "final_fix":
            return None
        ended = [
            a
            for a in records.attempts(self.epic_dir)
            if a.child == c.id
            and a.kind == "fix"
            and a.n > last.n
            and (a.result or {}).get("final_head")
        ]
        return ended[-1] if ended else None

    def _final_head(self, c: Child) -> str | None:
        """The HEAD *c*'s final fix left (`_final_fix`); else None."""
        done = self._final_fix(c)
        return done.result["final_head"] if done is not None else None

    def _left_issue(self, c: Child) -> None:
        """R4: one issue for what *c*'s final fix left, on its repo: each
        blocker of the review that chose it, and that the last fixes had no
        review. A final fix that ended `FINDINGS:DONE` found no new comment
        to act on (the blockers were already on the PR, so `post` skipped
        them), and the issue says no fix ran on them. Filed unless an issue
        with its title exists, open or closed, so a re-run files none. A
        failed `gh` call is logged; the next run tries again."""
        done, last = self._final_fix(c), self._last_review(c)
        if done is None or last is None:
            return
        title = f"buildme epic {self.epic.name}: what the final fix of #{fix.pr_of(done.started['argv'])} left"
        found = _gh(
            self.repo,
            "issue",
            "list",
            "--state",
            "all",
            "--search",
            f'"{title}" in:title',
            "--json",
            "number,title,url",
        )
        if not isinstance(found, list):
            log.warning(
                "EPIC child %s: cannot search for its final-fix issue; the next run tries again",
                c.id,
            )
            return
        if any(isinstance(i, dict) and i.get("title") == title for i in found):
            return
        body = "\n".join(
            [
                f"Epic `{self.epic.name}`, child `{c.id}`: {c.pr}",
                "",
                f"Review round {last.result.get('pr_round')} (head {str(last.started.get('head_sha'))[:12]}) "
                f"left these Code findings. {last.result.get('next_reason') or ''}".rstrip(),
                (
                    "The final fix found no new comment on the PR to act on, so no fix ran on them; "
                    "the last fixes had no review."
                    if fix.acted_on_nothing(review.log_text(done.log))
                    else "One final fix acted on them; the last fixes had no review."
                ),
                "",
                *(f"- {b}" for b in last.result["blocking"]),
            ]
        )
        res = git_ops._run_gh(
            ["gh", "issue", "create", "--title", title, "--body", body],
            cwd=self.repo,
            timeout=60,
        )
        if res.ok:
            log.info("EPIC child %s: filed %s", c.id, res.stdout.strip())
        else:
            log.warning(
                "EPIC child %s: gh issue create failed: %s; the next run tries again",
                c.id,
                review.tail(res.stderr),
            )

    def _passed(self, c: Child) -> bool:
        """Item 41: *c*'s PR, at the head last read, passed review: that head is
        the last clean reviewed head, or it plus the merge step's own merges
        (E.S8). A review of a newer head that stopped leaves an older clean
        head; that PR has not passed review at its current head."""
        clean = self._clean_head(c)
        return (
            clean is not None
            and c.head is not None
            and (c.head == clean or self._merge_head(c, c.head))
        )

    def _merge_worktree(self, c: Child) -> Path | None:
        """`<W>`: the child's last `WORKTREE:` path, when it is still there."""
        w = self._worktree(c)
        return None if w == self.repo else w.absolute()

    def _merge_head(self, c: Child, head: str) -> bool:
        """Under `auto`: *head* is the last clean reviewed head with only the
        merge step's merges of `origin/<feature branch>` on top."""
        reviewed, w = self._clean_head(c), self._merge_worktree(c)
        return (
            self.settings.merge == "auto"
            and reviewed is not None
            and w is not None
            and head != reviewed
            and merge.merges_onto(w, head, reviewed, self.branch)
        )

    async def _after_review(self, c: Child, head: str) -> None:
        """Item 40: under `auto`, a PR whose review at *head* left no blocker is merged."""
        if (
            self.settings.merge == "auto"
            and c.status == "built"
            and self._clean_head(c) == head
        ):
            await self._merge(c)

    def _merge_retry(self, c: Child) -> bool:
        """E.S26 (Spike, 2026-10-14): a child the merge step parked is tried
        again once `<W>` has no merge in progress and no uncommitted change,
        as the park's hint `merge --abort, then re-run` promises. Every other
        park stays. Clears `merge_park` when it retries."""
        w = self._merge_worktree(c)
        if (
            w is None
            or not w.is_dir()
            or merge.merging(w)
            or stop_record.others_work(w, None)
        ):
            return False
        log.info(
            "EPIC child %s: its worktree is clean; retrying the merge step it parked in: %s",
            c.id,
            c.merge_park,
        )
        c.merge_park = ""
        return True

    def _merge_park(self, c: Child, why: str) -> None:
        c.merge_park = why
        self._park(c, why)
        self._save()

    async def _merge(self, c: Child) -> None:
        """Items 35-40 under `auto`: merge `origin/<feature branch>` into *c*'s
        branch, push it and merge its PR, one merge at a time. Every git command, check and stop record runs on `<W>` (item 36).
        A stop by an exception or a signal first saves `merge_stop` (item 39)."""
        w = self._merge_worktree(c)
        if w is None:
            self._merge_park(
                c, "its worktree is gone, so the merge step has nowhere to merge"
            )
            return
        c.status = "reviewing"
        async with self.merge_lock:
            try:
                await self._merge_steps(c, w)
            except BaseException:
                c.merge_stop = stop_record.record(w)
                self._save()
                raise
        if (
            c.status == "reviewing"
        ):  # neither merged, parked nor unknown: its PR is open
            c.status = "built"

    async def _merge_steps(self, c: Child, w: Path) -> None:
        reviewed, name = self._clean_head(c), merge.branch(w)
        if name is None:
            return self._merge_park(c, f"cannot read the branch checked out in {w}")
        # The PR's target, which `merge.py` calls `default`: the feature branch.
        default, origin_ref = self.branch, f"refs/remotes/origin/{name}"
        _merge_point(self, c, "start")
        if not merge.merging(w) and await asyncio.to_thread(
            merge.unpushed_merge, w, reviewed, name, default
        ):  # 37
            head = git_ops.rev_parse_head(w)
            why = stop_record.others_work(w, None, head=head)
            if why:
                return self._merge_park(c, why)
            if not await self._merge_tests(c, w, head, hint="") or not self._push(
                c, w, name
            ):
                return
            pushed = True
        else:
            aborted = merge.merging(w)
            if aborted and not self._abort(
                c, w, reviewed, origin_ref, default
            ):  # item 38
                return
            done = await self._merge_default(
                c, w, reviewed, name, default, origin_ref, check=not aborted
            )
            if done is None:
                return
            pushed = done == "merged"
        while True:
            head = git_ops.rev_parse_head(
                w
            )  # what 40.1 checked and the step pushed: the only head 40.6 merges
            if head is None:
                return self._merge_park(c, f"cannot read HEAD in {w}")
            if not await self._checks_pass(c, head, pushed=pushed):  # 40.5
                return
            refused = self._merge_pr(c, head)  # 40.6
            if refused is None:
                return
            done = await self._merge_default(
                c, w, reviewed, name, default, origin_ref
            )  # BEHIND: 40.1
            if done is None:
                return
            if (
                done == "up to date"
            ):  # nothing new to merge: never retry a refused merge in one run
                return self._merge_park(c, refused)
            pushed = True

    def _abort(
        self, c: Child, w: Path, reviewed: str | None, origin_ref: str, default: str
    ) -> bool:
        """Item 38: a merge left in progress is aborted only when the shared
        check finds nothing in `<W>` its stop did not leave; with no
        `merge_stop` (a kill), any change parks. A refused abort parks with
        git's own words and changes nothing else."""
        why = (
            stop_record.others_work(w, c.merge_stop)
            or merge.not_reviewed(w, reviewed, default)
            or stop_record.others_work(
                w, c.merge_stop, head=reviewed, origin_ref=origin_ref
            )
        )
        if why:
            self._merge_park(c, f"{why}; {merge.abort_hint(w)}")
            return False
        res = merge.git(w, "merge", "--abort")
        if res.ok:
            return True
        files = " ".join(shlex.quote(f) for f in merge.abort_names(res.stderr))
        self._merge_park(
            c,
            f"git merge --abort failed: {res.stderr.strip()[-400:]}; "
            f"git -C {w} checkout -- {files}, then git -C {w} merge --abort",
        )
        return False

    async def _merge_default(
        self,
        c: Child,
        w: Path,
        reviewed: str | None,
        name: str,
        default: str,
        origin_ref: str,
        *,
        check: bool = True,
    ) -> str | None:
        """40.1-40.4: check, merge `origin/<default>` (the feature branch),
        resolve, test, commit and push. "merged" or "up to date"; None when the child parked or is
        `unknown`. *check* False: item 38 aborted a merge, so restart at 40.2."""
        why = merge.ready(w, reviewed, origin_ref, default) if check else None  # 40.1
        if why:
            return self._merge_park(c, why)
        c.merge_stop = None  # 40.2
        self._save()
        failed = await _fetch_origin(w)
        if (
            failed is None
            and await asyncio.to_thread(merge.agent_ahead, w, default)
            and not await self._unheld()
        ):
            return None  # Decided 6: the agent would launch past the ceiling; nothing merged (E.S27)
        if failed is not None:
            c.status, c.reason = (
                "unknown",
                f"{MERGE_UNKNOWN}git fetch origin failed: {failed}",
            )
            log.warning("EPIC child %s: %s", c.id, c.reason)
            return None
        res = merge.git(w, "merge", "--no-ff", "--no-commit", f"origin/{default}")
        if not merge.merging(w):
            if res.ok:
                return "up to date"
            return self._merge_park(
                c, f"git merge origin/{default} failed: {review.tail(res.stderr)}"
            )
        _merge_point(self, c, "merged")
        hint = f"; {merge.abort_hint(w)}"
        files = merge.conflicted(w)
        if files is None:
            return self._merge_park(c, f"git cannot list the conflicts in {w}{hint}")
        left = merge.outside_merge(
            w, files
        )  # kept by item 38's abort: tested, never committed (E.S28)
        if left is None or left:
            c.merge_stop = stop_record.record(w)
            return self._merge_park(
                c,
                f"git cannot list the changes in {w}{hint}"
                if left is None
                else f"{w} holds changes the merge commit would leave out ({', '.join(left)}); "
                f"remove them, then {merge.abort_hint(w)}",
            )
        todo = await asyncio.to_thread(merge.plan, w, "HEAD", "MERGE_HEAD")
        if todo is None:
            c.merge_stop = stop_record.record(w)
            return self._merge_park(c, f"git cannot list the conflicts in {w}{hint}")
        if (
            files or todo.files or todo.refused
        ) and not await self._resolve_conflicts(  # 40.3
            c, w, todo, files, reviewed, name, default, hint
        ):
            return None
        if not await self._merge_tests(c, w, reviewed, hint=hint):  # 40.4
            return None
        res = merge.git(w, "commit", "--no-edit")
        if not res.ok:
            c.merge_stop = stop_record.record(w)
            return self._merge_park(
                c, f"git commit failed: {review.tail(res.stderr)}{hint}"
            )
        return "merged" if self._push(c, w, name) else None

    async def _resolve_conflicts(
        self,
        c: Child,
        w: Path,
        todo: merge.Plan,
        conflicted: list[str],
        head: str | None,
        name: str,
        default: str,
        hint: str,
    ) -> bool:
        """40.3: one agent call resolves version fields, CHANGELOGs and
        lockfiles (`merge.plan`: the conflicted ones, and a version both sides
        raised, which git merges cleanly when both wrote the same one). A
        conflicted file it may not resolve parks before the call; a failed
        call, a marker left, another file changed, or a resolution
        `merge.not_resolutions` names, after it. The files are staged for the
        commit. *conflicted*: the files git left conflicted."""
        bad = list(
            dict.fromkeys(
                [*todo.refused, *(f for f in conflicted if f not in todo.files)]
            )
        )
        if bad:
            c.merge_stop = stop_record.record(w)
            self._merge_park(
                c,
                f"merging origin/{default} conflicts in {', '.join(bad)}, which the "
                f"merge step may not resolve{hint}",
            )
            return False
        files = todo.files
        attempt = records.start(
            self.epic_dir,
            "merge",
            c.id,
            ["merge", *files],
            head_sha=head,
            pr_round=c.pr_round,
        )
        self._save()
        before, staged = budget.process_spend().cost_usd, merge.index(w)
        try:
            result = await merge.run_merge_agent(self.config, w, files, name, default)
        except BudgetExceededError:
            raise
        except Exception as exc:  # an ordinary error, not a stop: the call failed, and its spend is known
            result = AgentResult(success=False, error=f"{type(exc).__name__}: {exc}")
        except BaseException:  # a cancelled call records no usage: its spend is unknown
            records.finish(attempt, None, interrupted=True)
            raise
        changed = None if staged is None else merge.changed_since(w, staged)
        left = merge.unresolved(w, files) if result.success else []
        others = None if changed is None else [p for p in changed if p not in files]
        why = (
            f"the merge agent failed: {review.tail(result.error or result.output or '')}"
            if not result.success
            else f"the merge agent left conflicts in {', '.join(left)}"
            if left
            else f"git cannot list what the merge agent changed in {w}"
            if others is None
            else f"the merge agent changed files it may not: {', '.join(others)}"
            if others
            else None
        )
        records.finish(
            attempt,
            0 if result.success else 1,
            interrupted=False,
            extra={"spend": round(budget.process_spend().cost_usd - before, 6)},
        )
        # Unstaged edits to files git did not leave conflicted would block the abort, or outlive it (E.S26).
        stray = [p for p in changed or [] if p not in conflicted]
        hint = f"; {merge.undo_hint(w, stray)}" if why and stray else hint
        if (
            why is None
            and not merge.git(w, "add", "--", *dict.fromkeys([*files, *changed])).ok
        ):
            why = f"git add failed in {w}"
        if (
            why is None
        ):  # by content, as `merges_onto` checks a pushed merge; staged, so the abort works
            tree = merge.git(w, "write-tree")
            beyond = (
                await asyncio.to_thread(
                    merge.not_resolutions, w, "HEAD", "MERGE_HEAD", tree.stdout.strip()
                )
                if tree.ok
                else None
            )
            why = (
                f"git cannot compare the merge agent's resolution in {w}"
                if beyond is None
                else f"the merge agent changed more than 40.3 allows in {', '.join(beyond)}"
                if beyond
                else None
            )
        if why:
            c.merge_stop = stop_record.record(w)
            self._merge_park(c, why + hint)
            return False
        return True

    async def _merge_tests(
        self, c: Child, w: Path, head: str | None, *, hint: str
    ) -> bool:
        """40.4: the project's test command (`merge.suite_command`) in `<P>`, with `BUILDME_TRUST_REPO=1`,
        as a `merge-tests` pair. True when it passed or there is none."""
        project = self._child_project(c)
        command = await asyncio.to_thread(merge.suite_command, project)
        if command is None:
            log.info("EPIC child %s: merge tests skipped: no test command", c.id)
            return True
        try:
            from ..gates import _test_invocation

            argv, directory = _test_invocation(command, project)
            bad = None
        except ValueError as e:
            argv, bad = [command], f"cannot split the test command: {e}"
        attempt = records.start(
            self.epic_dir, "merge-tests", c.id, argv, head_sha=head, pr_round=c.pr_round
        )
        try:
            code, out = (
                (None, bad)
                if bad
                else await self._run_step(
                    c,
                    attempt,
                    directory,
                    env={**os.environ, "BUILDME_TRUST_REPO": "1"},
                    timeout=TESTS_TIMEOUT,
                )
            )
        except OSError as e:  # it could not start (a missing binary): a failed run
            code, out, c.rattempt = None, str(e), None
        except (
            BaseException
        ):  # stopped, SIGHUP included: the tests end with the step (E.S23)
            proc, c.rproc, c.rattempt = c.rproc, None, None
            if proc is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
            records.finish(
                attempt, proc.wait() if proc is not None else None, interrupted=True
            )
            raise
        records.finish(attempt, code, interrupted=False)
        if code == 0:
            return True
        c.merge_stop = stop_record.record(w)
        first = (out.strip().splitlines() or ["no output"])[0]
        why = (
            f"merge tests could not run: {first}"
            if code is None
            else f"merge tests failed: {command} exited {code}, see {attempt.log}"
        )
        self._merge_park(c, why + hint)
        return False

    def _push(self, c: Child, w: Path, name: str) -> bool:
        """`git push`, never forced. Rejected as origin's branch moved: parks (E.S7).
        Any other failure: `unknown`, as a failed fetch; the next run is E.S6."""
        _merge_point(self, c, "push")
        res = git_ops.push_branch(w, name)
        if res.ok:
            return True
        remote = merge.git(w, "ls-remote", "origin", f"refs/heads/{name}")
        moved = remote.stdout.split()[:1] if remote.ok else []
        if (
            moved
            and merge.git(w, "merge-base", "--is-ancestor", moved[0], "HEAD").returncode
            != 0
        ):
            self._merge_park(
                c,
                f"push rejected: origin/{name} moved to {moved[0]}; {merge.move_hint(w, moved[0])}",
            )
        else:
            c.status, c.reason = (
                "unknown",
                f"{MERGE_UNKNOWN}git push failed: {review.tail(res.stderr)}",
            )
            log.warning("EPIC child %s: %s", c.id, c.reason)
        return False

    async def _checks_pass(self, c: Child, head: str, *, pushed: bool) -> bool:
        """40.5: True once no check that gates *c*'s PR at *head* is pending:
        its required checks or, when none is required, every check it has,
        so a repo with CI and no branch rule still waits for its CI
        (judgment). A failed or cancelled one parks, naming it. Checks are
        read only while `gh pr view` shows *head* as the PR's head: for about
        1.5 s after a push GitHub still shows the previous head, whose checks
        `gh pr checks` reads (measured on spikelab/github-tests), so another
        head counts as pending for `CHECKS_APPEAR` seconds and then parks.
        Right after the step's own push (*pushed*) gh reports no checks until
        GitHub registers them, so none counts as pending for `CHECKS_APPEAR`
        seconds. Still pending after `CHECKS_LIMIT` seconds: False with the
        PR left open, which the run waits on (item 41) and a re-run reads
        again (E.S8). A read that fails leaves the child `unknown`, as a
        failed fetch does."""
        start = CLOCK.now()
        while True:
            _merge_point(self, c, "checks")
            waited = CLOCK.now() - start
            appearing = pushed and waited < CHECKS_APPEAR
            view = _gh(self.repo, "pr", "view", c.pr, "--json", "headRefOid")
            shown = view.get("headRefOid") if isinstance(view, dict) else None
            if not shown:
                c.status, c.reason = "unknown", f"{MERGE_UNKNOWN}gh pr view failed"
                log.warning("EPIC child %s: %s", c.id, c.reason)
                return False
            if shown != head:
                if waited >= CHECKS_APPEAR:
                    self._merge_park(
                        c,
                        f"its PR's head on GitHub is {shown}, not {head}, the head the "
                        f"merge step checked",
                    )
                    return False
                await CLOCK.sleep(CHECKS_WAIT)
                continue
            checks, which = self._read_checks(c, required=True), "required check"
            if checks == [] and not appearing:
                checks, which = self._read_checks(c, required=False), "check"
            if checks is None:
                return False
            failed = [
                str(x.get("name"))
                for x in checks
                if x.get("bucket") in ("fail", "cancel")
            ]
            if failed:
                self._merge_park(c, f"{which} {', '.join(failed)} failed")
                return False
            if not (checks == [] and appearing) and not any(
                x.get("bucket") == "pending" for x in checks
            ):
                return True
            if waited >= CHECKS_LIMIT:
                log.warning(
                    "EPIC child %s: its PR's checks are still pending after %.0f s; leaving %s open",
                    c.id,
                    waited,
                    c.pr,
                )
                return False
            await CLOCK.sleep(CHECKS_WAIT)

    def _read_checks(self, c: Child, *, required: bool) -> list[dict] | None:
        """One `gh pr checks`: the checks it lists, [] when gh reports none, or
        None when its answer cannot be read, which leaves *c* `unknown`."""
        argv = [
            "gh",
            "pr",
            "checks",
            c.pr,
            *(["--required"] if required else []),
            "--json",
            "name,bucket",
        ]
        res = git_ops._run_gh(argv, cwd=self.repo, timeout=60)
        try:  # printed whatever the exit: 8 while pending, 1 when one failed
            checks = json.loads(res.stdout) if res.stdout.strip() else None
        except ValueError:
            checks = None
        if isinstance(checks, list) and all(isinstance(x, dict) for x in checks):
            return checks
        if NO_CHECKS.search(res.stderr):
            return []
        c.status, c.reason = (
            "unknown",
            f"{MERGE_UNKNOWN}gh pr checks failed: {review.tail(res.stderr)}",
        )
        log.warning("EPIC child %s: %s", c.id, c.reason)
        return None

    def _merge_pr(self, c: Child, head: str) -> str | None:
        """40.6: `gh pr merge --merge --match-head-commit <head>`, never
        `--admin`: GitHub refuses it when the PR's head is no longer *head*,
        the head 40.1 checked and the step pushed. None when the PR merged,
        is left open (exit 0 on a branch with a merge queue only queues it:
        it is `merged` once `gh pr view` says `MERGED`), or the child parked
        or is `unknown`; refused `BEHIND`, the refusal, and the merge step
        starts again at 40.1."""
        res = git_ops._run_gh(
            ["gh", "pr", "merge", c.pr, "--merge", "--match-head-commit", head],
            cwd=self.repo,
            timeout=120,
        )
        view = _gh(self.repo, "pr", "view", c.pr, "--json", "state,mergeStateStatus")
        if isinstance(view, dict) and view.get("state") == "MERGED":
            c.status, c.reason, c.merge_stop = "merged", "", None
            log.info("EPIC child %s: merged %s", c.id, c.pr)
            self._save()
            return None
        if (
            res.ok
        ):  # queued, or auto-merge turned on: waited on until it merges (item 41)
            if isinstance(view, dict):
                log.info(
                    "EPIC child %s: gh pr merge left %s %s; waiting for it to merge",
                    c.id,
                    c.pr,
                    view.get("state"),
                )
            else:
                c.status, c.reason = "unknown", PR_UNREAD
                log.warning("EPIC child %s: %s", c.id, c.reason)
            return None
        if not isinstance(view, dict):
            c.status, c.reason = (
                "unknown",
                (
                    f"{MERGE_UNKNOWN}gh pr merge failed and its PR cannot be read: "
                    f"{review.tail(res.stderr)}"
                ),
            )
            log.warning("EPIC child %s: %s", c.id, c.reason)
            return None
        why = f"gh pr merge refused: {res.stderr.strip()[-400:]} (mergeStateStatus {view.get('mergeStateStatus')})"
        if view.get("mergeStateStatus") == "BEHIND":
            log.info(
                "EPIC child %s: %s; merging origin's default branch again", c.id, why
            )
            return why
        self._merge_park(c, why)
        return None

    def _save(self) -> None:
        self.epic.child_status = {
            cid: store.ChildStatus(
                status=c.status,
                pr=c.pr,
                reason=c.reason,
                pr_round=c.pr_round,
                conflicts=c.conflicts,
                merge_stop=c.merge_stop,
                merge_park=c.merge_park,
            )
            for cid, c in self.children.items()
        }
        self.epic.spent_usd, self.epic.spend_complete = self.spent()
        store.save_epic(self.epic_dir, self.epic)

    # -- spend (PR B2) ---------------------------------------------------------------

    def _review_slugs(self) -> set[str]:
        return spend.review_slugs(self.slug, [c.pr for c in self.children.values()])

    def spent(self) -> tuple[float, bool]:
        """(USD, complete): every record's spend plus the child PRs' reviews (item 19)."""
        from multiplai_core.paths import get_paths

        usd, complete = spend.attempts_spend(records.attempts(self.epic_dir))
        reviews, reviews_complete = spend.review_spend(
            Path(get_paths().data_dir) / spend.LEDGER, self._review_slugs()
        )
        return round(usd + reviews, 6), complete and reviews_complete

    def _pr_reviews(self, c: Child) -> float:
        """The USD of *c*'s PR's reviews in the ledger, as `spent` counts them."""
        from multiplai_core.paths import get_paths

        usd, _ = spend.review_spend(
            Path(get_paths().data_dir) / spend.LEDGER,
            spend.review_slugs(self.slug, [c.pr]),
        )
        return usd

    def _held(self, but: str | None = None) -> float:
        """What the running fixers (but *but*'s) were given and have not yet
        spent on their PRs' reviews. `spent` does not count a running fixer's
        own spend until it ends, and counts its PR's reviews as they finish,
        so this is the part of the cap they may still spend. Rounded to 6
        decimals, as `spent` is, so adding ledger costs leaves no float noise."""
        return round(
            sum(
                max(s.usd - (self._pr_reviews(self.children[cid]) - s.reviews), 0.0)
                for cid, s in self.shares.items()
                if cid != but
            ),
            6,
        )

    def _left(self, cid: str | None = None) -> float:
        """What a fixer for *cid* may be given: `epic.max_usd` less the spend
        and less what the other running fixers hold (`_held`; with no *cid*,
        all of them). Rounded to the 6 decimals `--max-usd` is passed with, so
        a share under half a millionth of a dollar is 0 and the fixer waits
        rather than being launched with `--max-usd 0.000000`."""
        return round(self.settings.max_usd - self.spent()[0] - self._held(cid), 6)

    def _held_back(self) -> bool:
        """True while running fixers hold all that is left of `epic.max_usd`:
        nothing paid is launched then (a build, a review or its resume, the
        merge agent), as it would spend their share. Not a budget stop: once
        they end, their real spend decides."""
        ceiling = self.settings.max_usd
        return ceiling is not None and bool(self.shares) and self._left() <= 0

    async def _unheld(self) -> bool:
        """Before a launch from inside a step (a review, a resume, the merge
        agent): wait while running fixers hold all that is left (`_held_back`),
        as `_loop` does for builds and reviews. False once `epic.max_usd` is
        reached."""
        while self._held_back() and not self._over_budget():
            await CLOCK.sleep(POLL)
        return not self._over_budget()

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
        log.warning(
            "EPIC spend $%.2f has reached epic.max_usd $%.2f: launching nothing more",
            usd,
            ceiling,
        )
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
                return _argv_pid(c.attempt.started["argv"])
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
        while (
            any(alive(c) for c in running)
            or any(c.rproc.poll() is None for c in reviewing)
        ) and CLOCK.now() < until:
            await CLOCK.sleep(0.5)
        for (
            c
        ) in reviewing:  # a review still running keeps no result: the next run asks it
            if c.rproc.poll() is None:
                log.warning(
                    "EPIC child %s: its review is still running after %ss",
                    c.id,
                    STOP_WAIT,
                )
                continue
            records.finish(c.rattempt, c.rproc.poll(), interrupted=True)
            c.rproc = c.rattempt = None
        for c in self.children.values():
            if c.status == "reviewing":
                c.status = "built"
        for c in running:
            if alive(c):
                log.warning(
                    "EPIC child %s is still running after %ss; the next run waits for it",
                    c.id,
                    STOP_WAIT,
                )
                continue
            records.finish(
                c.attempt,
                c.proc.poll() if c.proc is not None else None,
                interrupted=True,
            )
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
        refused = in_place_reason(self.project_dir)  # B1.S23
        if refused:
            log.error("EPIC %s", refused)
            print(f"ERROR:{refused}", file=sys.stderr, flush=True)
            return 2
        why = await self._merge_main()  # Decision 5, before anything is written
        if self.conflict is not None:
            return feature_branch.conflict_exit(self.branch, self.conflict)
        if why is not None:
            log.error("EPIC %s", why)
            print(f"ERROR:{why}", file=sys.stderr, flush=True)
            return 1
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
        previous = {
            sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)
        }
        previous = {sig: h for sig, h in previous.items() if h != signal.SIG_IGN}
        for sig in previous:
            try:
                loop.add_signal_handler(sig, on_signal, sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        try:
            await self._loop()
        except (KeyboardInterrupt, asyncio.CancelledError):
            sig = self.signal_sent or signal.SIGINT
            await self._cancel_reviews()
            if sig == signal.SIGHUP:
                log.warning(
                    "EPIC stopped by SIGHUP: leaving the children running; the next run waits for them"
                )
            else:
                log.warning(
                    "EPIC stopped by %s: stopping the children",
                    signal.Signals(sig).name,
                )
                await self._stop_children(sig)
            self._summary(run_dir)
            return 128 + sig
        except Exception:
            await self._cancel_reviews()
            self._summary(run_dir)  # where each child stood when the run broke
            raise
        finally:
            for sig, handler in previous.items():  # asyncio would leave SIG_DFL
                try:
                    if loop.remove_signal_handler(sig) and handler is not None:
                        signal.signal(sig, handler)
                except (NotImplementedError, RuntimeError, ValueError):
                    pass
        for c in (
            self.children.values()
        ):  # R4: a final-fixed PR, merged or waiting for a human merge
            waits = (
                self.settings.merge == "manual"
                and c.status == "built"
                and self._passed(c)
            )
            if (c.status == "merged" or waits) and self._final_fix(c) is not None:
                self._left_issue(c)
        self._summary(run_dir)
        bad = [c for c in self.children.values() if c.status in ("failed", "unknown")]
        for c in bad:  # beside EPIC:MERGE_WAITING too, not only in the summary
            print(
                f"EPIC: child {c.id} {c.status}: {c.reason}",
                file=sys.stderr,
                flush=True,
            )
        unreviewed = [c for c in self.children.values() if self._unreviewed(c)]
        for c in unreviewed:
            print(
                f"EPIC: child {c.id} built: its PR's head {(c.head or '')[:12]} has no finished review; "
                f"the next run reviews it",
                file=sys.stderr,
                flush=True,
            )
        if self.conflict is not None:  # nothing more was launched
            return feature_branch.conflict_exit(self.branch, self.conflict)
        if (
            self.budget_stop is not None
        ):  # the spend at exit, the running children's included
            print(f"{MARKER_BUDGET}:{self.spent()[0]:.2f}", flush=True)
            return 3
        waited = [c for c in self._waited_on() if c.status == "built"]
        deps = {
            d
            for c in self.children.values()
            if c.status in ("pending", "waiting", "failed")
            for d in c.depends_on
        }
        if waited and (
            not bad or any(c.id in deps for c in waited)
        ):  # a human merge (41) hides no failure
            print(
                f"{MARKER_MERGE_WAITING}:{','.join(c.pr for c in waited)}", flush=True
            )
            return 3
        return 1 if bad or unreviewed else 0

    def _unreviewed(self, c: Child) -> bool:
        """*c*'s review step ran in this invocation, yet its PR's head, last
        read, has no finished review: its review was stopped (C.S3), or the
        PR got a new head after a clean review. It is not waited on, as it
        has not passed review (`_passed`), and the next run reviews it."""
        last = self._last_review(c)
        return (
            c.status == "built"
            and c.reviewed
            and c.head is not None
            and not self._passed(c)
            and (last is None or last.started.get("head_sha") != c.head)
        )

    async def _loop(self) -> int:
        last_read = CLOCK.now()
        while True:
            self._reap_reviews()
            ready = self._resolve()
            if (
                self.conflict is not None
            ):  # a merge of main conflicted: launch nothing more
                ready = []
            running = sum(
                c.status in ("running", "reviewing") for c in self.children.values()
            )
            for c in (
                self._review_due()
            ):  # a review before a new build: it unblocks dependants
                if (
                    running >= self.settings.max_parallel
                    or self._over_budget()
                    or self._held_back()
                ):
                    break
                self._start_review(c)
                running += 1
            for c in ready:
                if (
                    running >= self.settings.max_parallel
                    or self._over_budget()
                    or self._held_back()
                ):
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
            if (
                self.budget_stop is not None
            ):  # nothing runs: the budget stop ends the run
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
        children = [
            {
                "id": cid,
                "status": c.status,
                "pr": c.pr,
                "rounds": c.pr_round or 0,
                "reason": c.reason,
            }
            for cid, c in self.children.items()
        ]
        totals: dict[str, int] = {}
        for c in children:
            totals[c["status"]] = totals.get(c["status"], 0) + 1
        usd, complete = self.spent()
        path = run_dir / "summary.json"
        store._write_atomic(
            path,
            json.dumps(
                {
                    "children": children,
                    "totals": totals,
                    "spent_usd": usd,
                    "spend_complete": complete,
                },
                indent=2,
            )
            + "\n",
        )
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


async def run_epic(config, args, epic_dir: Path, epic: store.Epic, branch: str) -> int:
    """Build an epic's children (item 11) from its feature branch *branch*,
    which holds their ready specs. Returns 0 when nothing is
    left to do, 1 when a child failed or its state is unknown, or a child's PR
    head has no finished review (`Coordinator._unreviewed`), 3 with `EPIC:MERGE_WAITING:<urls>` while a
    child waits on an open PR, with `EPIC:BUDGET:<spent>` once `epic.max_usd` is reached
    or with `FEATURE_BRANCH:CONFLICT:<branch>:<files>` when merging origin's default
    branch into *branch* conflicts, 128 + the signal when stopped."""
    from . import lock as epic_lock

    assert epic_lock.held(epic_dir), (
        "the coordinator runs under the epic lock its build holds"
    )
    return await Coordinator(config, args, Path(epic_dir), epic, branch).run()
