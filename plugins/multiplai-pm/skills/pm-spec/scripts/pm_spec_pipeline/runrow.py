"""The run row: one line in `<data_dir>/runs.jsonl` per invocation that
targeted a change (`multiplai_core.runs`).

`state.json` lives in the change's git-ignored `.pm-spec/` folder, in whichever
checkout ran pm-spec, and goes when that checkout does. The row stays, and
names the pm-spec version that wrote it, so runs can be compared across
versions. `__main__.main` writes it once, after the command has ended; an
argument refused before the change's branch is looked up writes none.

`outcome`, by exit code:

- 0: `succeeded`, the change handed off (or a revise done).
- 3: `waiting`, at a review stop, the ticket wait or the interview's
  questions, for a person.
- 4: `waiting`, blocked on findings, open requirements issues or the
  consistency round cap, until a person edits, accepts or checks again.
- 1 with cause `budget` or `usage-limit`: `stopped`.
- 1 otherwise, 2 and 5: `failed`.

`cause`, a stable code (`CAUSES`): the one the `Stop` that ended the run
carries, else the one `__main__` gives the exception it caught.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from multiplai_core import is_usage_limit, runs
from multiplai_core.specs import normalize_change_name

from . import branch, git_ops, sdk
from . import state as state_mod
from .config import RUN_DIR, SpecConfig
from .run import EXIT_BLOCKED, EXIT_DONE, EXIT_WAITING

log = logging.getLogger(__name__)

KIND = "pm-spec"
SKILL = "pm-spec"

# Every cause a row can carry. `error-<ExceptionClass>` is any other exception.
CAUSES = {
    # waiting (exit 3)
    "review-proposal": "at the review stop after the proposal",
    "review-components": "at the review stop after the components",
    "review-specs": "at the review stop after the specs",
    "tickets": "waiting for the components' tickets",
    "interview-replies": "waiting for the replies to the ticket interview",
    # waiting (exit 4)
    "findings": "a checked step blocked on findings",
    "revise-blocked": "a revise blocked on findings",
    "open-issues": "open requirements issues, for `revise`",
    "round-cap": "the consistency check hit its round cap after the specs review",
    # stopped (exit 1)
    "budget": "the spend cap (--max-usd)",
    "usage-limit": "a Claude usage limit",
    # failed: refused (exit 2, 5)
    "not-a-repo": "--project-dir is not in a git work tree",
    "no-change": "no change of that name to resume or revise",
    "no-ticket": "no ticket ref, of the change's own or a parent's",
    "bad-ticket": "the ticket id cannot start a branch name",
    "bad-branch": "--branch is not the ticket's, or not a valid branch name",
    "no-origin": "the repository has no origin remote",
    "no-default-branch": "origin has neither the branch nor a default branch",
    "not-a-worktree": "pm-spec's worktree path holds something else",
    "diverged": "the checkout and origin's branch have diverged",
    "missing-input": "an input file given on the command line does not exist",
    "unreadable-state": "state.json cannot be read",
    "run-exists": "`new` on a change that has a run",
    "no-run": "`resume` or `revise` on a change with no run",
    "no-issues-file": "`revise` with no issues file",
    "no-issues": "`revise` with an issues file holding no issue",
    "unknown-component": "--ticket names no component of the change",
    "tickets-pending": "a component's ticket is missing at the ticket wait",
    "bad-accept": "--accept names no open blocking finding, or has no reason",
    "untrusted-repo": "a tool-holding call on a repository not marked trusted",
    "locked": "another run holds the change's lock",
    # failed (exit 1)
    "fetch-failed": "git fetch origin failed",
    "worktree-failed": "git worktree add failed",
    "fast-forward-failed": "the checkout could not be fast-forwarded",
    "adopt-commit-failed": "the commit of specs copied onto the branch failed",
    "commit-failed": "a commit of the change dir failed",
    "push-failed": "a push of the branch failed",
    "prototype-failed": "the prototype failed its gate",
    "files-kept-changing": "the files changed during every consistency check",
}
STOPPED_CAUSES = ("budget", "usage-limit")
# The review stops, numbered as the pipeline viewer numbers them.
STOPS = {"proposal": 1, "components": 2, "specs": 3}


def plugin_root() -> Path:
    """The multiplai-pm plugin's root: the nearest directory above this file
    holding `.claude-plugin/plugin.json`."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".claude-plugin" / "plugin.json").is_file():
            return parent
    return here.parents[4]


def error_cause(error: BaseException) -> str:
    if is_usage_limit(error):
        return "usage-limit"
    return f"error-{type(error).__name__}"


def outcome(code: int, cause: str) -> str:
    if code == EXIT_DONE:
        return "succeeded"
    if code in (EXIT_WAITING, EXIT_BLOCKED):
        return "waiting"
    if cause in STOPPED_CAUSES:
        return "stopped"
    return "failed"


@dataclass
class Invocation:
    """What the row needs, filled in as the command goes: `targeted` once
    the change's branch is looked up, `place` and `config` once known."""

    command: str
    change: str
    project_dir: Path
    ticket: str
    started_at: str = field(default_factory=runs.now)
    spend_at_start: float = field(default_factory=lambda: sdk._process.cost_usd)
    targeted: bool = False
    place: branch.Place | None = None
    config: SpecConfig | None = None

    def run_dir(self) -> Path | None:
        """The run folder the command used (`revise` may use an archived
        copy), else the one in the change's checkout, else None."""
        if self.config is not None:
            return self.config.run_dir
        if self.place is not None:
            name = normalize_change_name(self.change)
            return self.place.project_dir / "specs" / "changes" / name / RUN_DIR
        return None


def _origin(repo: Path) -> str | None:
    try:
        proc = git_ops._git(["remote", "get-url", "origin"], repo)
    except git_ops.GitError:
        return None
    url = proc.stdout.strip()
    return url if proc.returncode == 0 and url else None


def _repo_name(remote: str | None, repo: Path) -> str:
    """origin's `<repo>` part (the last path part, `.git` dropped), as the
    review skill names `reviews/<repo>/`; else the checkout's directory name."""
    if remote:
        parts = [
            p
            for p in remote.strip()
            .rstrip("/")
            .removesuffix(".git")
            .replace(":", "/")
            .split("/")
            if p
        ]
        if len(parts) >= 2:
            return runs.folder_name(parts[-1])
    return runs.folder_name(repo.name)


def _state(run_dir: Path | None) -> state_mod.RunState | None:
    if run_dir is None:
        return None
    try:
        return state_mod.load(run_dir)
    except (state_mod.Unreadable, OSError, ValueError):  # a row without steps
        return None


def make(inv: Invocation, code: int, cause: str) -> dict:
    repo = inv.place.repo if inv.place else inv.project_dir
    remote = _origin(repo)
    run_dir = inv.run_dir()
    saved = _state(run_dir)
    ticket = inv.ticket or (saved.options.ticket_ref if saved else "")
    if not ticket and run_dir is not None:
        ticket = next(iter(branch.tickets(run_dir.parent)), "")
    steps = {
        name: step.outcome for name, step in (saved.steps if saved else {}).items()
    }
    reached = [
        n for name, n in STOPS.items() if steps.get(name) in ("passed", "skipped")
    ]
    return runs.make_row(
        producer=runs.producer(plugin_root(), SKILL),
        kind=KIND,
        target={
            "repo": _repo_name(remote, repo),
            "remote": remote,
            "branch": inv.place.branch if inv.place else None,
            "change": inv.change,
            "ticket": ticket or None,
            "subcommand": inv.command,
        },
        outcome=outcome(code, cause),
        cause=cause or None,
        started_at=inv.started_at,
        spend_usd=round(max(sdk._process.cost_usd - inv.spend_at_start, 0.0), 6),
        run={"stop": max(reached, default=None), "steps": steps, "exit_code": code},
    )


def record(inv: Invocation, code: int, cause: str) -> None:
    """Append the invocation's row, when it targeted a change. Never raises."""
    if not inv.targeted:
        return
    try:
        runs.append_row(make(inv, code, cause))
    except Exception:  # a run never fails because of its row
        log.warning("could not write the run row", exc_info=True)
