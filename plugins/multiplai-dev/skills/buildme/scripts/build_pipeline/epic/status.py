"""`build_pipeline epic status`: where each child of an epic stands, read
from disk (plan-factory-merged-2026-10-29.md, item 69).

It prints one JSON object per child, one per line: `state`, `head`,
`target`, `pr`, `blocker`, `next`, `spend` (`known_usd`, and `unknown`: the
parts of its spend no record holds), the board moves not sent yet
(`board_unsent`), the live process if any, and `epic_running`. It launches
nothing and writes nothing. It never takes the epic lock: it reads the
holder `lock.hold` wrote into `.lock` and asks `ps` whether that pid is a
live `build_pipeline` on this host, so a running epic, or one starting while
status reads, is never refused or delayed (row S51).

What it reads: `epic.json` (each child's status as the coordinator last
saved it), the records under `agents/` and their logs, the review ledger,
the children's build checkpoints (`spend.state_spend`), the board outbox,
`git rev-parse HEAD` in each child's worktree, and `ps` for whether a
stopped attempt's process is still alive (`coordinator.build_pid` and
`fix_pid`, as `_recover` reads them). No `gh` call and no network.

A child's state is the coordinator's status, except for an attempt with no
result: while the epic runs, or while its build or fixer process is alive,
the child is `running` (a build) or keeps the saved status (a review, post,
fix or merge, which the running epic drives); with neither, it is
`interrupted`, and the next epic run writes its result from its log.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

from multiplai_core.paths import get_paths

from .. import git_ops
from . import coordinator, outbox, records, spend, store

# What the next epic run, or a person, does for a child in each state.
_NEXT = {
    "merged": "nothing: its PR is merged",
    "running": "wait: its build is running",
    "reviewing": "wait: its PR's review is running",
    "interrupted": "the next epic run writes its result from its log, then "
    "takes its PR as built or launches it again",
    "built": "the next epic run reviews its PR, fixes the findings and merges "
    "it (under epic.merge: manual, a person merges it)",
    "failed": "the next epic run launches it again",
    "unknown": "the next epic run reads it again: a git, gh or ps read failed",
}


class StatusError(Exception):
    """No epic, or its epic.json cannot be read."""


def epic_live(epic_dir: Path) -> dict | None:
    """The holder `lock.hold` wrote into `.lock` when that pid is a live
    `build_pipeline` on this host; None otherwise. Never takes the lock."""
    try:
        info = json.loads((Path(epic_dir) / ".lock").read_text() or "{}")
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or not isinstance(info.get("pid"), int):
        return None
    if info.get("host") != socket.gethostname():
        return None
    try:
        os.kill(info["pid"], 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # alive, another user's
    return info if _runs_build_pipeline(info["pid"]) else None


def _runs_build_pipeline(pid: int) -> bool:
    """Whether *pid*'s command line names `build_pipeline`: a pid reused by
    another program after the epic ended is not the epic. True when `ps`
    cannot say, since the pid is alive."""
    try:
        ps = subprocess.run(
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    return ps.returncode != 0 or "build_pipeline" in ps.stdout


def _worktree(tries: list[records.Attempt]) -> Path | None:
    """The last `WORKTREE:` line of the child's builds, when it exists."""
    found = [m for a in tries if a.kind == "build" for m in records.markers(a.log)]
    trees = records.marker(found, "WORKTREE")
    return Path(trees[-1]) if trees and Path(trees[-1]).is_dir() else None


def _head(tries: list[records.Attempt]) -> str | None:
    tree = _worktree(tries)
    if tree is not None:
        res = git_ops._run(["git", "rev-parse", "HEAD"], cwd=tree)
        if res.ok and res.stdout.strip():
            return res.stdout.strip()
    for a in reversed(tries):
        if a.started.get("head_sha"):
            return a.started["head_sha"]
    return None


def _target(tries: list[records.Attempt]) -> str | None:
    """The `--branch` of the child's last build: the branch its PR targets."""
    for a in reversed(tries):
        argv = a.started.get("argv") or []
        if a.kind == "build" and "--branch" in argv[:-1]:
            return argv[argv.index("--branch") + 1]
    return None


def _change(tries: list[records.Attempt], epic: str, cid: str) -> str:
    for a in reversed(tries):
        argv = a.started.get("argv") or []
        if a.kind == "build" and "--change" in argv[:-1]:
            return argv[argv.index("--change") + 1]
    return f"{epic}-{cid}"


def _state_files(
    tries: list[records.Attempt], repo: Path, project: Path, change: str, name: str
) -> list[Path]:
    """The child's `.build-state.json` and completed checkpoint in its
    worktree, as `Coordinator._state_files` places them."""
    from ..change_manager import change_path
    from ..state import completed_state_path

    try:
        offset = project.resolve().relative_to(repo.resolve())
    except ValueError:
        offset = Path()
    root = (_worktree(tries) or repo) / offset
    state = change_path(root / "specs", change) / name
    return [state, completed_state_path(state)]


def _spend(
    tries: list[records.Attempt],
    pr: str | None,
    slug: str,
    state_files: list[Path],
    cid: str,
) -> dict:
    unknown = []
    for a in tries:
        if a.kind not in spend.SPEND_KINDS:
            continue
        name = f"{a.n:03d}-{a.kind}"
        value = (a.result or {}).get("spend")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            unknown.append(
                f"{name}: no spend recorded (still running, or stopped before it printed SPEND:)"
            )
        elif records.spend_incomplete(a.result.get("markers") or []):
            unknown.append(f"{name}: a skill it ran printed no spend")
    usd, _ = spend.attempts_spend(tries)
    reviews, reviews_complete = spend.review_spend(
        Path(get_paths().data_dir) / spend.LEDGER, spend.review_slugs(slug, [pr])
    )
    if not reviews_complete:
        unknown.append(
            "a review of its PR has not finished, has no readable cost, or was "
            "run again fresh after a stop"
        )
    built, built_complete = spend.state_spend(
        {cid: state_files}, spend.build_spend(tries)
    )
    if not built_complete:
        unknown.append(
            "its build checkpoint cannot be read, or holds calls of unknown spend"
        )
    return {"known_usd": round(usd + reviews + built, 6), "unknown": unknown}


def _live(a: records.Attempt, pr: str | None, repo: Path) -> bool | str:
    """True when the open attempt's process is alive, False when gone, or
    the error when `ps` failed. Only builds and fixers outlive their epic."""
    try:
        if a.kind == "build":
            return coordinator._argv_pid(a.started.get("argv") or []) is not None
        if a.kind == "fix" and pr:
            m = spend._PR_NUMBER.search(pr)
            return bool(m) and coordinator.fix_pid(int(m.group(1)), repo) is not None
    except coordinator.ReadFailed as e:
        return str(e)
    return False


def _next(state: str, reason: str, waits_on: list[str]) -> str:
    if state in ("pending", "waiting") and waits_on:
        return f"launches once {', '.join(waits_on)} merged"
    if state == "pending":
        return "launches on the next epic run, or when a slot frees in the running one"
    if state == "waiting" and reason.startswith(coordinator.PLAN_WAITING):
        return "an engineer approves its plan (stop 4); the next epic run looks for the approval"
    if state == "waiting" and reason.startswith(coordinator.WAITING_ON):
        return f"fix the specs of {reason[len(coordinator.WAITING_ON) :]}; it launches after"
    if state == "parked":
        if reason.startswith(coordinator.SPEC_REFUSALS):
            return (
                "run /multiplai-pm:pm-spec revise on its specs, then run the epic again"
            )
        if reason.startswith("SPLIT:REQUIRED:"):
            return (
                "a person decides the split: build it as one PR with "
                "--accept-size, or split the spec"
            )
        return "a person decides: " + (reason or "it is parked")
    if state == "skipped":
        return "fix the dependency named in the blocker; it launches after"
    return _NEXT.get(state, "")


def child_status(
    cid: str,
    deps: list[str],
    saved: store.ChildStatus,
    statuses: dict[str, str],
    tries: list[records.Attempt],
    *,
    epic: store.Epic,
    live_epic: bool,
    repo: Path,
    project: Path,
    slug: str,
    unsent: list[str],
    state_name: str,
) -> dict:
    state, reason = saved.status, saved.reason or saved.merge_park
    pr = saved.pr or next(
        (a.result.get("pr") for a in reversed(tries) if (a.result or {}).get("pr")),
        None,
    )
    process = None
    if tries and tries[-1].result is None:
        a = tries[-1]
        alive = _live(a, pr, repo)
        process = {"kind": a.kind, "record": f"{a.n:03d}-{a.kind}-{cid}"}
        if isinstance(alive, str):
            state, reason = (
                "unknown",
                f"cannot tell whether its {a.kind} is running: {alive}",
            )
        elif a.kind == "build" and (live_epic or alive):
            state, reason = "running", ""
        elif not (live_epic or alive):
            state, reason = "interrupted", f"its {a.kind} stopped with no result"
        process["alive"] = alive if isinstance(alive, bool) else None
    waits_on = (
        [d for d in deps if statuses.get(d) != "merged"]
        if state in ("pending", "waiting")
        else []
    )
    named = [f"{d} ({statuses.get(d, '?')})" for d in waits_on]
    blocker = reason or (f"depends on {', '.join(named)}" if named else "")
    change = _change(tries, epic.name, cid)
    return {
        "child": cid,
        "state": state,
        "head": _head(tries),
        "target": _target(tries),
        "pr": pr,
        "blocker": blocker,
        "next": _next(state, reason, waits_on),
        "spend": _spend(
            tries,
            pr,
            slug,
            _state_files(tries, repo, project, change, state_name),
            cid,
        ),
        "board_unsent": unsent,
        "process": process,
        "epic_running": live_epic,
    }


def epic_status(
    project_dir: Path, change: str, state_file: str = ".build-state.json"
) -> list[dict]:
    """One dict per child of the epic *change* of *project_dir*, in the
    order `epic.json` lists them. Raises StatusError with no epic."""
    from ..change_manager import normalize_change_name

    project = Path(project_dir).absolute()
    repo = git_ops.repo_root(project) or project
    name = normalize_change_name(change)
    epic_dir = store.epic_dir_for(repo, name)
    try:
        epic = store.load_epic(epic_dir)
    except store.EpicFileError as e:
        raise StatusError(f"no epic {name} for {repo}: {e}") from None
    live = epic_live(epic_dir) is not None
    tries = records.attempts(epic_dir)
    slug = store.slug_repo_name(repo)
    unsent = outbox.unsent(epic_dir)
    statuses = {
        c.id: epic.child_status.get(c.id, store.ChildStatus()).status
        for c in epic.children
    }
    return [
        child_status(
            c.id,
            list(c.depends_on),
            epic.child_status.get(c.id, store.ChildStatus()),
            statuses,
            [a for a in tries if a.child == c.id],
            epic=epic,
            live_epic=live,
            repo=repo,
            project=project,
            slug=slug,
            unsent=unsent.get(c.id, []),
            state_name=Path(state_file).name,
        )
        for c in epic.children
    ]


def run_cli(args) -> int:
    """`epic status`: print one JSON object per child; 2 with no epic."""
    import sys

    try:
        rows = epic_status(Path(args.project_dir), args.change)
    except StatusError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    for row in rows:
        print(json.dumps(row, sort_keys=True), flush=True)
    return 0
