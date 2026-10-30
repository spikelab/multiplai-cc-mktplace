"""Every ``git`` / ``gh`` invocation the build pipeline makes, in one place.

Design rules (these are the trust boundary — do not relax them):

- **No shell.** Every call is a fixed ``argv`` list passed to
  ``subprocess.run`` with the default ``shell=False``. No user-supplied
  string is ever interpolated into a shell string.
- **Names are normalized first.** Branch and worktree names are derived from
  ``change_manager.normalize_change_name``, so a hostile ``--change`` value
  cannot become a path traversal or an option-looking argument.
- **Never destructive except for one guarded discard.** Nothing here merges,
  rebases, force-pushes, or deletes a branch. The single exception is
  ``discard_to``, which throws away a failed step's work — and it refuses
  unless the target sha is still an ancestor of HEAD, so it can only ever move
  backwards along the current history. It touches only files under the
  project directory, and refuses when a commit it would drop changes a file
  outside it: the project may be a subdirectory of a larger repo, and nothing
  outside the project is buildme's to revert. ``remove_worktree`` exists as a
  documented helper for a *calling session* to use from the workspace root;
  the pipeline itself never calls it (worktree-safety rule: never self-cleanup
  from inside the worktree).
- **Explicit pathspecs.** ``commit_paths`` stages the paths it is given and
  nothing else.
- **Push / PR failures are non-fatal.** They return a result the caller turns
  into a diagnosis with the exact manual commands; they never raise.

No LLM calls live in this module.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_GIT_TIMEOUT = 60
_PUSH_TIMEOUT = 300
_GH_TIMEOUT = 300
# The pr skill may make one model call and re-ask it once.
_PR_SKILL_TIMEOUT = 1500
# A whole /multiplai-dev:review run on a PR (finders, verify, explain).
_REVIEW_SKILL_TIMEOUT = 4 * 3600
# A codebase-walkthrough `check` is git only; a `run` (regenerate_walkthroughs)
# is bounded by its own --max-usd, and this only stops one that hangs.
_WALKTHROUGH_SKILL_TIMEOUT = 4 * 3600
# A spec-extract run before the plan is written: bounded by its own --max-usd;
# this only stops one that hangs.
_SPEC_EXTRACT_SKILL_TIMEOUT = 4 * 3600


class GitLifecycleError(RuntimeError):
    """A refusal to start: the repository is not in a state the pipeline can
    safely take over (not a repo, dirty tree, colliding branch with work on
    it). Always carries a diagnosis of what the caller must do first."""


@dataclass(frozen=True)
class GitResult:
    """Typed result of one git/gh invocation."""

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def command(self) -> str:
        """The invocation, shell-quoted — for diagnoses only, never executed."""
        return " ".join(shlex.quote(a) for a in self.argv)


def _run(
    argv: list[str],
    cwd: Path | str | None = None,
    timeout: int = _GIT_TIMEOUT,
    errors: str | None = None,
    env: dict[str, str] | None = None,
) -> GitResult:
    """Run a fixed argv list. shell=False (the default) — never a string.
    *errors* is how undecodable output bytes are decoded (`subprocess.run`);
    None raises `UnicodeDecodeError`."""
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            errors=errors,
            env=env,
            timeout=timeout,
        )
        return GitResult(argv, proc.returncode, proc.stdout or "", proc.stderr or "")
    except FileNotFoundError as e:
        return GitResult(argv, 127, "", str(e))
    except subprocess.TimeoutExpired:
        return GitResult(argv, 124, "", f"timed out after {timeout}s")


def _run_gh(
    argv: list[str], cwd: Path | str | None = None, timeout: int = _GH_TIMEOUT
) -> GitResult:
    """Separate seam for ``gh`` so tests can mock it without mocking git."""
    return _run(argv, cwd=cwd, timeout=timeout)


def stop_signal_name(exc: BaseException) -> str:
    """The signal behind a stop: ``SystemExit(128 + n)`` from
    `_exit_on_termination` names signal n; anything else (KeyboardInterrupt,
    the task cancel `asyncio.run` makes of Ctrl-C) is SIGINT."""
    if isinstance(exc, SystemExit) and isinstance(exc.code, int) and exc.code > 128:
        try:
            return signal.Signals(exc.code - 128).name
        except ValueError:
            pass
    return "SIGINT"


@contextlib.contextmanager
def _exit_on_termination(*, interrupt: bool = True):
    """Make SIGINT, SIGTERM and SIGHUP raise for the length of the block.

    A child started with `start_new_session=True` (a skill run here,
    cosmic-ray in `mutation`) does not get the signals sent to buildme's
    process group, so buildme has to pass them on by killing the child's
    group in its own cleanup. This makes that cleanup run.

    SIGTERM and SIGHUP become SystemExit: their default action ends the
    process at once, with no `finally` run, so a detached review would keep
    spending model budget and hold its target's run lock, and a detached
    cosmic-ray would keep rewriting source files. A handler someone else
    installed for them is left alone.

    SIGINT becomes KeyboardInterrupt unless it is ignored. buildme runs under
    `asyncio.run`, whose SIGINT handler only cancels the main task on the
    first Ctrl-C; a wait in `communicate` blocks the event loop, so that
    cancel is not seen until the child finishes on its own. Raising in the
    wait is what lets the caller kill the group at once. The previous
    handler is put back afterwards.

    With `interrupt=False`, SIGINT is left as it is: `build` and `findings`
    hold SIGTERM and SIGHUP this way for their whole run, so their `SPEND:`
    line is printed on those stops too (`__main__.main`), and `asyncio.run`
    keeps its own Ctrl-C handling.

    Outside the main thread (where Python cannot set a handler) this does
    nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _raise(signum, _frame):
        raise SystemExit(128 + signum)

    def _interrupt(_signum, _frame):
        raise KeyboardInterrupt

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(sig) == signal.SIG_DFL:
            previous[sig] = signal.signal(sig, _raise)
    if interrupt and signal.getsignal(signal.SIGINT) not in (signal.SIG_IGN, None):
        previous[signal.SIGINT] = signal.signal(signal.SIGINT, _interrupt)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _run_skill(argv: list[str], cwd: Path | str | None, timeout: int) -> GitResult:
    """Run a skill launcher (`uv run ... python -m ...`) in a process group of its own.

    `uv run` starts the skill as its own child. `subprocess.run` kills only
    `uv` on a timeout, which left the skill running with its output pipes
    closed: a review kept its target's run lock and later died on a closed
    pipe, recorded as a failure instead of an interrupted run. So whatever
    ends the wait (the timeout, a KeyboardInterrupt, a SystemExit) kills the
    whole group, the way a killed review is meant to end.

    The group of its own also keeps the skill from getting the signals sent
    to buildme's group, so `_exit_on_termination` turns a SIGTERM, a SIGHUP
    or a Ctrl-C to buildme into an exception here, and the group is killed
    the same way.
    """
    with _exit_on_termination():
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            return GitResult(argv, 127, "", str(e))
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            _count_skill_spend(argv, None)
            return GitResult(argv, 124, "", f"timed out after {timeout}s")
        except BaseException:
            _kill_group(proc)
            _count_skill_spend(argv, None)
            raise
    _count_skill_spend(argv, out)
    return GitResult(argv, proc.returncode, out or "", err or "")


# The skill commands that call a model and print their own
# `SPEND:<usd>:<tokens>` line on exit. The review skill prints none: its
# spend is in its ledger, which an epic reads.
SPEND_REPORTING = {
    "pr_pipeline": ("open", "edit"),
    "walkthrough_pipeline": ("run", "resume"),
    "spec_extract_pipeline": ("run",),
}


def _count_skill_spend(argv: list[str], stdout: str | None) -> None:
    """Add a model-calling skill's `SPEND:` line to buildme's process total
    (`budget.spend_marker`), or mark that total incomplete when the skill
    printed none (killed, timed out, or failed before printing it)."""
    if not any(
        a in SPEND_REPORTING and b in SPEND_REPORTING[a] for a, b in zip(argv, argv[1:])
    ):
        return
    from . import budget

    for line in reversed((stdout or "").splitlines()):
        if line.startswith("SPEND:"):
            parts = line.strip().split(":")
            try:
                usd, tokens = float(parts[1]), int(parts[2])
            except (IndexError, ValueError):
                break
            budget.add_process_spend(usd, tokens)
            if parts[3:4] == ["incomplete"]:
                budget.process_spend_unknown(
                    "a skill's own spend is incomplete: " + " ".join(argv[:12])
                )
            return
    budget.process_spend_unknown(
        "a skill that calls a model printed no SPEND line: " + " ".join(argv[:12])
    )


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL *proc*'s process group, then reap *proc* and close its pipes."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        log.warning("pid %s did not exit after SIGKILL", proc.pid)
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()


def _run_pr_skill(
    argv: list[str], cwd: Path | str | None = None, timeout: int = _PR_SKILL_TIMEOUT
) -> GitResult:
    """Separate seam for the pr skill's subprocess so tests can stub it."""
    return _run_skill(argv, cwd, timeout)


def _run_review_skill(
    argv: list[str], cwd: Path | str | None = None, timeout: int = _REVIEW_SKILL_TIMEOUT
) -> GitResult:
    """Separate seam for the review skill's subprocess (the findings stage)."""
    return _run_skill(argv, cwd, timeout)


def _run_walkthrough_skill(
    argv: list[str],
    cwd: Path | str | None = None,
    timeout: int = _WALKTHROUGH_SKILL_TIMEOUT,
) -> GitResult:
    """Separate seam for the codebase-walkthrough skill's subprocess
    (``walkthrough_pipeline check`` and ``run``, after the build)."""
    return _run_skill(argv, cwd, timeout)


def _run_spec_extract_skill(
    argv: list[str],
    cwd: Path | str | None = None,
    timeout: int = _SPEC_EXTRACT_SKILL_TIMEOUT,
) -> GitResult:
    """Separate seam for the spec-extract skill's subprocess
    (``spec_extract_pipeline run ... --apply``, in CODEBASE_ANALYSIS)."""
    return _run_skill(argv, cwd, timeout)


# --- Inspection -----------------------------------------------------------


def repo_root(path: Path | str) -> Path | None:
    """Absolute root of the git repo containing ``path``, or None."""
    res = _run(["git", "rev-parse", "--show-toplevel"], cwd=path)
    if not res.ok:
        return None
    return Path(res.stdout.strip())


def path_prefix(path: Path | str) -> str | None:
    """``path`` relative to its repo's root, as git writes it (``"packages/api/"``,
    ``""`` at the root), or None when it is not in a repo."""
    res = _run(["git", "rev-parse", "--show-prefix"], cwd=path)
    if not res.ok:
        return None
    return res.stdout.strip()


def files_at(path: Path | str, sha: str) -> list[str] | None:
    """Every file under ``path`` in commit ``sha``, relative to ``path``
    (``git ls-tree`` lists from the working directory down), or None."""
    res = _run(
        ["git", "-c", "core.quotepath=off", "ls-tree", "-r", "-z", "--name-only", sha],
        cwd=path,
    )
    if not res.ok:
        log.warning("git ls-tree %s failed: %s", sha, res.stderr.strip())
        return None
    return [p for p in res.stdout.split("\0") if p]


def text_at(path: Path | str, sha: str, rel: str) -> str | None:
    """The text of *rel* (relative to *path*) in commit *sha*, or None when it is not there."""
    res = _run(["git", "show", f"{sha}:./{rel}"], cwd=path)
    return res.stdout if res.ok else None


def is_git_repo(path: Path | str) -> bool:
    return repo_root(path) is not None


def has_commits(repo: Path | str) -> bool:
    """False for a freshly ``git init``ed repo with no HEAD (a worktree cannot
    be created from it)."""
    return _run(["git", "rev-parse", "--verify", "HEAD"], cwd=repo).ok


def current_branch(repo: Path | str) -> str:
    res = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    return res.stdout.strip() if res.ok else ""


def default_branch(repo: Path | str) -> str:
    """Best-effort default branch: origin's HEAD, else main/master, else the
    currently checked-out branch."""
    res = _run(["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd=repo)
    if res.ok and res.stdout.strip():
        return res.stdout.strip().rsplit("/", 1)[-1]
    for candidate in ("main", "master"):
        if branch_exists(repo, candidate):
            return candidate
    return current_branch(repo)


def is_dirty(repo: Path | str) -> bool:
    """True when tracked files have uncommitted changes.

    Untracked files are deliberately ignored — a scratch project routinely
    has untracked noise, and refusing on it would make the pipeline unusable.
    """
    res = _run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo)
    return bool(res.stdout.strip())


def branch_exists(repo: Path | str, branch: str) -> bool:
    return _run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo
    ).ok


def unmerged_commit_count(repo: Path | str, branch: str, base: str) -> int | None:
    """Commits on ``branch`` that are not on ``base``. None when undeterminable —
    the caller must treat "unknown" as a refusal, never as "empty branch"
    (classifying a branch we could not inspect as safe-to-shadow would silently
    weaken the collision refusal)."""
    res = _run(["git", "rev-list", "--count", f"{base}..{branch}"], cwd=repo)
    if not res.ok:
        return None
    try:
        return int(res.stdout.strip() or "0")
    except ValueError:
        return None


def has_remote(repo: Path | str, name: str = "origin") -> bool:
    res = _run(["git", "remote"], cwd=repo)
    return name in res.stdout.split()


def worktree_paths(repo: Path | str) -> list[Path]:
    """Every worktree registered on the repo (including the main checkout)."""
    res = _run(["git", "worktree", "list", "--porcelain"], cwd=repo)
    if not res.ok:
        return []
    return [
        Path(line.split(" ", 1)[1].strip())
        for line in res.stdout.splitlines()
        if line.startswith("worktree ")
    ]


# --- Naming ---------------------------------------------------------------


def branch_name_for(change_name: str) -> str:
    """``buildme/<normalized-change-name>``."""
    from .change_manager import normalize_change_name

    return f"buildme/{normalize_change_name(change_name)}"


def worktree_dir_name(change_name: str) -> str:
    from .change_manager import normalize_change_name

    return f"buildme-{normalize_change_name(change_name)}"


def worktrees_root(repo: Path) -> Path:
    """``$WORKSPACE/.worktrees`` when WORKSPACE is set (Spike's standing rule
    that every worktree lives there), else ``<repo>/../.worktrees``."""
    workspace = os.environ.get("WORKSPACE", "").strip()
    if workspace:
        return Path(workspace).expanduser() / ".worktrees"
    return repo.parent / ".worktrees"


def worktree_dest(repo: Path, change_name: str, suffix: int = 1) -> Path:
    name = worktree_dir_name(change_name)
    if suffix > 1:
        name = f"{name}-{suffix}"
    return worktrees_root(repo) / name


def resolve_new_branch(
    repo: Path,
    change_name: str,
    base: str,
    *,
    taken: tuple[str, ...] = (),
    name: str | None = None,
) -> tuple[str, Path]:
    """Pick a free branch, *name* (``<TICKET>-<change>``, which the caller
    passes; ``buildme/<change>`` without one), and its matching worktree path.

    A colliding branch that carries commits not on ``base`` is somebody's
    work — refuse rather than guess. A colliding branch with nothing on it
    gets a ``-2``/``-3``/... suffix (branch and worktree stay in lockstep).
    A branch in *taken* (a published build's, when the user asked for a new
    build of that change) is skipped the same way, whatever it carries, and
    so is one origin has (``refs/remotes/origin/<branch>``): a branch cut
    here under that name could not be pushed there.
    """
    base_branch = name or branch_name_for(change_name)
    for suffix in range(1, 51):
        branch = base_branch if suffix == 1 else f"{base_branch}-{suffix}"
        dest = worktree_dest(repo, change_name, suffix)
        if branch in taken:
            continue
        if branch_exists(repo, branch):
            ahead = unmerged_commit_count(repo, branch, base)
            if ahead is None:
                raise GitLifecycleError(
                    f"Branch '{branch}' already exists and `git rev-list "
                    f"{base}..{branch}` failed, so it cannot be proven empty. "
                    f"Refusing to guess — inspect it (or fix '{base}') and re-run."
                )
            if ahead > 0:
                raise GitLifecycleError(
                    f"Branch '{branch}' already exists with {ahead} commit(s) not on "
                    f"'{base}'. That looks like a previous build. Resume it, rename it, "
                    f"or run with a different --change name. Refusing to reuse or "
                    f"overwrite it."
                )
            continue
        if _run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
            cwd=repo,
        ).ok:
            continue
        if dest.exists():
            continue
        return branch, dest
    raise GitLifecycleError(
        f"Could not find a free branch name for '{base_branch}' after 50 attempts."
    )


# --- Mutation -------------------------------------------------------------


def preflight(repo_path: Path, *, worktree_requested: bool) -> Path:
    """Validate the source repo before taking it over. Returns the repo root.

    Raises GitLifecycleError with a diagnosis — never falls back silently.
    """
    root = repo_root(repo_path)
    if root is None:
        raise GitLifecycleError(
            f"{repo_path} is not a git repository, so no worktree can be created "
            f"from it. Either `git init` it and make one commit first, or re-run "
            f"with --no-worktree (which builds in place and will `git init` for you)."
        )
    if worktree_requested and not has_commits(root):
        raise GitLifecycleError(
            f"{root} is a git repository with no commits yet — `git worktree add` "
            f"needs a HEAD to branch from. Make an initial commit "
            f"(`git -C {root} commit --allow-empty -m 'chore: init'`), or re-run "
            f"with --no-worktree."
        )
    if is_dirty(root):
        raise GitLifecycleError(
            f"{root} has uncommitted changes in tracked files. Commit or stash them "
            f"first — the build must start from a clean tree so its branch shows only "
            f"what it produced. (`git -C {root} status --short`)"
        )
    return root


def create_worktree(
    repo: Path, branch: str, dest: Path, start_point: str | None = None
) -> GitResult:
    """``git worktree add -b <branch> <dest> [<start_point>]``. Never forces,
    never reuses. Without a start point the branch starts at the repo's HEAD."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    res = _run(
        [
            "git",
            "worktree",
            "add",
            "-b",
            branch,
            str(dest),
            *([start_point] if start_point else []),
        ],
        cwd=repo,
        timeout=_GIT_TIMEOUT,
    )
    if not res.ok:
        raise GitLifecycleError(
            f"Failed to create worktree at {dest} on branch {branch}: "
            f"{(res.stderr or res.stdout).strip()}"
        )
    log.info("WORKTREE created path=%s branch=%s repo=%s", dest, branch, repo)
    return res


def worktree_for_branch(repo: Path | str, branch: str) -> Path | None:
    """The worktree that has *branch* checked out, or None."""
    res = _run(["git", "worktree", "list", "--porcelain"], cwd=repo)
    if not res.ok:
        return None
    path: Path | None = None
    for line in res.stdout.splitlines():
        if line.startswith("worktree "):
            path = Path(line.split(" ", 1)[1].strip())
        elif line.strip() == f"branch refs/heads/{branch}" and path is not None:
            return path
    return None


def add_worktree_for_branch(repo: Path, branch: str, dest: Path) -> GitResult:
    """Check an existing branch out in a new worktree at *dest*: the local
    branch when there is one, else a local branch tracking
    ``origin/<branch>`` (fetched first). Never forces, never resets a branch.
    Raises GitLifecycleError with the git error."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(repo, branch):
        argv = ["git", "worktree", "add", str(dest), branch]
    else:
        fetch = _run(
            [
                "git",
                "fetch",
                "origin",
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            ],
            cwd=repo,
            timeout=_PUSH_TIMEOUT,
        )
        if not fetch.ok:
            raise GitLifecycleError(
                f"Branch '{branch}' is not local and `git fetch origin {branch}` failed: "
                f"{(fetch.stderr or fetch.stdout).strip()}"
            )
        argv = [
            "git",
            "worktree",
            "add",
            "--track",
            "-b",
            branch,
            str(dest),
            f"origin/{branch}",
        ]
    res = _run(argv, cwd=repo, timeout=_GIT_TIMEOUT)
    if not res.ok:
        raise GitLifecycleError(
            f"Failed to check out {branch} in a worktree at {dest}: "
            f"{(res.stderr or res.stdout).strip()}"
        )
    log.info("WORKTREE added path=%s branch=%s repo=%s", dest, branch, repo)
    return res


def fast_forward_from_origin(worktree: Path, branch: str) -> GitResult:
    """Bring *branch* up to ``origin/<branch>`` when that is a fast-forward
    (someone pushed to the PR). Never merges, never resets: a diverged branch
    is left alone and the later push reports it."""
    fetch = _run(
        [
            "git",
            "fetch",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        ],
        cwd=worktree,
        timeout=_PUSH_TIMEOUT,
    )
    if not fetch.ok:
        return fetch
    return _run(["git", "merge", "--ff-only", f"origin/{branch}"], cwd=worktree)


def remove_worktree(repo: Path, dest: Path) -> GitResult:
    """DOCUMENTED HELPER — `git worktree remove`, deliberately never called
    by the pipeline (test_git_ops.py asserts no other module references it).

    Worktree teardown is the calling session's decision, made from the
    workspace root (an agent must never delete the worktree it is standing
    in). Kept here so the one place that knows how to run git also documents
    the safe teardown command.
    """
    return _run(["git", "worktree", "remove", str(dest)], cwd=repo)


def _positive_pathspec(exclude: str) -> str:
    """``:(exclude,glob)**/x`` → ``:(glob)**/x``: the same match, selecting."""
    magic, _, rest = exclude[2:].partition(")")
    kept = [m for m in magic.split(",") if m and m != "exclude"]
    return f":({','.join(kept)}){rest}" if kept else rest


def _working_identity(path: Path) -> str:
    """Fingerprint the working version independently from its staged blob."""
    if path.is_symlink():
        return "link:" + os.readlink(path)
    if path.is_file():
        return (
            f"mode:{path.stat().st_mode & 0o111}:sha256:"
            + hashlib.sha256(path.read_bytes()).hexdigest()
        )
    return "other" if path.exists() else "deleted"


def _agent_index_evidence(repo: Path | str) -> tuple[Path | None, dict]:
    result = _run(
        ["git", "rev-parse", "--git-path", "buildme-agent-index.json"], cwd=repo
    )
    if not result.ok:
        return None, {}
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = Path(repo) / path
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    return path, data if isinstance(data, dict) else {}


@contextlib.contextmanager
def agent_write_window(repo: Path | str | None):
    """Remember staging introduced by this agent, never pre-existing user staging.

    Evidence binds HEAD, project, index blobs and final working contents.
    A hard kill before the exit record fails closed; normal cancellation records
    the agent's index in finally. Callers must serialize writers to a project.
    """
    if repo is None or not Path(repo).is_dir():
        yield
        return
    path, evidence = _agent_index_evidence(repo)
    before = index_entries(repo) if path else None
    staged = (
        _run(
            ["git", "diff", "--cached", "--name-only", "--relative", "-z", "--", "."],
            cwd=repo,
        )
        if path
        else None
    )
    head = rev_parse_head(repo) if path else None
    key = str(Path(repo).resolve())
    prior = evidence.get(key, {})
    entries = prior.get("entries", {}) if prior.get("head") == head else {}
    owned = {
        p: item
        for p, item in entries.items()
        if isinstance(item, dict)
        and item.get("index") == (before or {}).get(p)
        and item.get("working") == _working_identity(Path(repo) / p)
    }
    protected = {
        p
        for p in (staged.stdout.split("\0") if staged and staged.ok else [])
        if p and p not in owned
    }
    try:
        yield
    finally:
        if (
            path
            and before is not None
            and staged
            and staged.ok
            and head == rev_parse_head(repo)
        ):
            after = index_entries(repo)
            if after is not None:
                names = set(owned) | {
                    p
                    for p in before.keys() | after.keys()
                    if p not in protected and before.get(p) != after.get(p)
                }
                owned = {
                    p: {
                        "index": after.get(p),
                        "working": _working_identity(Path(repo) / p),
                    }
                    for p in names
                }
                if owned:
                    evidence[key] = {"head": head, "entries": owned}
                    from .io_utils import atomic_write

                    atomic_write(path, json.dumps(evidence, sort_keys=True))


def commit_paths(worktree: Path | str, message: str, paths: list[str]) -> str | None:
    """Commit explicit paths using an isolated index, preserving other staging.

    Divergent staged/working versions require exact agent ownership evidence. A failed commit leaves
    the active index intact; successful commits reconcile only selected entries
    if staging has not changed while hooks ran. Returns SHA, or None only for no changes. Refusals/failures raise
    GitLifecycleError so callers retain their checkpoint without advancing.
    """
    excludes = [
        p for p in paths if p.startswith(":(") and "exclude" in p[2 : p.find(")")]
    ]
    positives = [p for p in paths if p not in excludes]
    if not positives:
        return None
    staged_paths = _run(
        ["git", "diff", "--cached", "--name-only", "-z", "--relative", "--", *paths],
        cwd=worktree,
    )
    working_paths = _run(
        ["git", "diff", "--name-only", "-z", "--relative", "--", *paths], cwd=worktree
    )
    if not staged_paths.ok or not working_paths.ok:
        raise GitLifecycleError("Cannot inspect commit paths")
    # A staged deletion (including the old side of a rename) can still have
    # a working file. It is now untracked, so plain `git diff` omits it.
    deleted = _run(
        [
            "git",
            "diff",
            "--cached",
            "--no-renames",
            "--diff-filter=D",
            "--name-only",
            "-z",
            "--relative",
            "--",
            *paths,
        ],
        cwd=worktree,
    )
    if not deleted.ok:
        raise GitLifecycleError("Cannot inspect staged deletions")
    recreated = {
        name
        for name in deleted.stdout.split("\0")
        if name and os.path.lexists(Path(worktree) / name)
    }
    divergent = (
        set(staged_paths.stdout.split("\0")) & set(working_paths.stdout.split("\0"))
    ) - {""}
    before_index = index_entries(worktree)
    _, evidence = _agent_index_evidence(worktree)
    record = evidence.get(str(Path(worktree).resolve()), {})
    owned = (
        record.get("entries", {})
        if record.get("head") == rev_parse_head(worktree)
        else {}
    )
    conflicts = divergent | recreated
    if any(
        not isinstance(owned.get(p), dict)
        or owned[p].get("index") != (before_index or {}).get(p)
        or owned[p].get("working") != _working_identity(Path(worktree) / p)
        for p in conflicts
    ):
        raise GitLifecycleError(
            f"Cannot commit {message}: staged and working versions differ: {', '.join(sorted(conflicts))}. "
            "Inspect git diff and git diff --cached for these paths. Save both versions, then "
            "git add the paths whose working version should be committed, or git restore --staged "
            "the paths whose working version should remain unstaged. Missing agent ownership "
            "after an interruption cannot be inferred safely."
        )
    index_path = _run(["git", "rev-parse", "--git-path", "index"], cwd=worktree)
    if before_index is None or not index_path.ok:
        raise GitLifecycleError("Cannot inspect active index")
    original = Path(index_path.stdout.strip())
    if not original.is_absolute():
        original = Path(worktree) / original
    with tempfile.TemporaryDirectory(prefix="buildme-commit-index-") as directory:
        temporary = Path(directory) / "index"
        if original.exists():
            shutil.copy2(original, temporary)
        env = dict(os.environ, GIT_INDEX_FILE=str(temporary))
        # Stage positive paths first: Git can skip a wholly untracked directory
        # when its add pathspec also excludes a bookkeeping file inside it.
        add = _run(["git", "add", "-A", "--", *positives], cwd=worktree, env=env)
        if not add.ok:
            raise GitLifecycleError("Cannot stage commit paths: " + add.stderr)
        if (
            excludes
            and not _run(
                [
                    "git",
                    "reset",
                    "-q",
                    "--",
                    *(_positive_pathspec(p) for p in excludes),
                ],
                cwd=worktree,
                env=env,
            ).ok
        ):
            raise GitLifecycleError("Cannot exclude bookkeeping from commit")
        staged = _run(
            ["git", "diff", "--cached", "--quiet", "--", *paths], cwd=worktree, env=env
        )
        if staged.returncode == 0:
            return None
        if staged.returncode != 1:
            raise GitLifecycleError("Cannot compare commit index")
        commit = _run(
            ["git", "commit", "-m", message, "--", *paths], cwd=worktree, env=env
        )
        if not commit.ok:
            log.warning(
                "Failed to commit %s: %s",
                message,
                commit.stderr.strip() or commit.stdout.strip(),
            )
            raise GitLifecycleError(
                "Commit failed: " + (commit.stderr or commit.stdout)
            )
    # Only reconcile entries we just committed, and only if no one changed
    # their staging while hooks ran. Other project/sibling entries stay intact.
    if index_entries(worktree) == before_index:
        _run(["git", "reset", "-q", "HEAD", "--", *paths], cwd=worktree)
    else:
        log.warning(
            "Index changed during commit; retained the caller's index unchanged"
        )
    sha = _run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip()
    if not sha:
        log.warning("Committed %s but could not read HEAD", message)
        raise GitLifecycleError("Commit succeeded but HEAD is unreadable")
    log.info("COMMIT %s sha=%s", message, sha[:8])
    return sha


# Buildme's own bookkeeping files must never be committed, from ANY commit
# path, and — much worse if it goes wrong — must never be *deleted* by the
# discard below. Glob magic so the rule holds at every depth:
# specs/changes/<name>/ while active, specs/archive/<date>-<name>/ after the
# --auto archive move, and build-progress.md at the project root. A leading
# `**/` matches at the root too, so one list covers every path.
#
# This is the ONE list. The TDD engine used to derive its own exact-path
# variant from BuildConfig, which meant two mechanisms disagreeing about which
# files were bookkeeping (that one covered build-progress.md, this one did
# not) — with a `git clean` on the other side of the disagreement.
BOOKKEEPING_EXCLUDES = [
    ":(exclude,glob)**/.plan-*.json",
    ":(exclude,glob)**/.plan-*.tmp",
    ":(exclude,glob)**/.build-state.json",
    # A succeeded run's kept checkpoint (state.BuildState.complete).
    ":(exclude,glob)**/.build-state.complete.json",
    # The findings stage's checkpoints: its round loop, and each round's TDD engine.
    ":(exclude,glob)**/.findings-state.json",
    ":(exclude,glob)**/.findings-r*-state.json",
    ":(exclude,glob)**/.board.json",
    ":(exclude,glob)**/build-progress.md",
    # Section files publish hands the pr skill (orchestrator._pre_publish_sections).
    ":(exclude,glob)**/pr-sections/**",
    # A failed whole test run's output (gates.write_failure_log).
    ":(exclude,glob)**/.buildme-test-runs/**",
    # Planning's working files, only inside a change dir under specs/ (active,
    # or archived): the codebase analysis and its facts, and each plan
    # rewrite's diff. They stay on disk for the build that reads them and off
    # the product branch (plan-factory-merged-2026-10-29.md, item 30). An
    # application file of the same name elsewhere is committed as usual, and
    # one a branch already tracks keeps its committed version.
    *(
        f":(exclude,glob)**/specs/{where}/**/{name}"
        for where in ("changes", "archive")
        for name in ("facts.json", "codebase-analysis.md", "plan-edits-*.diff")
    ),
]


def commit_stage(config, message: str, paths: list[str]) -> str | None:
    """Commit a spec-stage change, but only when the pipeline owns the branch.

    When the build is running in place (``--no-worktree``) there is no
    pipeline branch and this is a no-op — that is what keeps
    ``--no-worktree`` byte-identical to the pre-git-lifecycle pipeline.

    Bookkeeping files (``.build-state.json``, ``.board.json``) are excluded
    unconditionally — staging the change directory must never sweep them into
    the pushed PR.
    """
    if not getattr(config, "pipeline_branch", None):
        return None
    if not paths:
        return None
    return commit_paths(
        Path(config.project_dir), message, [*paths, *BOOKKEEPING_EXCLUDES]
    )


# --- Whole-tree operations (the TDD block loop) ---------------------------
#
# Everything above stages explicit paths. The ones below are the pipeline's
# only whole-tree git operations, and they live here rather than in the TDD
# engine so that every `git` invocation the pipeline makes is in one module,
# behind one `_run`. They are pathspec-limited (`-- . :(exclude)…`), never a
# bare `git add -A`: a block's output is not fully enumerable from the agent's
# self-reported FILES: slot, so dropping a produced file would be worse than
# sweeping one in — and "everything under ." is this build's own work.
#
# Every one of them is limited to the project directory, the commits and the
# discard included. The project may be a subdirectory of a larger repo
# (`--no-worktree` in a monorepo, or the findings stage's project inside its
# worktree), and the rest of that repo is the user's: a commit must not take
# a file the user staged there, and a discard must not revert an edit or drop
# a commit there. So the commits name their pathspec (`git commit -- …`, which
# leaves every other staged entry staged), and the discard resets only the
# branch pointer and restores files under `.`.


def commit_tree(repo: Path | str, message: str, label: str = "") -> str | None:
    """Commit everything under *repo* except buildme's bookkeeping.

    Returns the new SHA or None for no changes. Commit refusals/failures raise
    GitLifecycleError. *label* is retained for caller compatibility.
    """
    return commit_paths(repo, message, [".", *BOOKKEEPING_EXCLUDES])


def save_wip_ref(repo: Path | str, ref: str, message: str) -> str | None:
    """Save working files and staging independently without changing HEAD/index.

    The ref's tree holds working files; its second parent's tree holds the
    index. The first parent is the original HEAD. Existing refs are reused
    only for identical snapshots, otherwise a numbered ref preserves both
    attempts. Conflicts or any failed object/ref write refuse recovery.
    """
    if (
        not ref.startswith("refs/buildme/wip/")
        or not _run(["git", "check-ref-format", ref], cwd=repo).ok
    ):
        log.warning("Refusing to write wip ref %r", ref)
        return None
    if _run(["git", "rev-parse", "--verify", "-q", "MERGE_HEAD"], cwd=repo).ok:
        return None
    base = rev_parse_head(repo)
    if not base:
        return None
    # A stash-shaped commit: parent 1 is HEAD, parent 2 preserves the index,
    # and the commit's own tree preserves working files (including untracked).
    # Never stage into the caller's index, move HEAD, or run commit hooks.
    index_tree = _run(["git", "write-tree"], cwd=repo)
    if not index_tree.ok:  # An unmerged index cannot be represented by a tree.
        log.warning(
            "Cannot save WIP with an unmerged/unreadable index; preserve it manually"
        )
        return None
    index_commit = _run(
        [
            "git",
            "commit-tree",
            index_tree.stdout.strip(),
            "-p",
            base,
            "-m",
            "buildme: saved index",
        ],
        cwd=repo,
    )
    if not index_commit.ok:
        return None
    with tempfile.TemporaryDirectory(prefix="buildme-index-") as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"))
        for argv in (
            ["git", "read-tree", base],
            ["git", "add", "-A", "--", ".", *BOOKKEEPING_EXCLUDES],
        ):
            if not _run(argv, cwd=repo, env=env).ok:
                return None
        tree = _run(["git", "write-tree"], cwd=repo, env=env)
        if not tree.ok:
            return None
    saved = _run(
        [
            "git",
            "commit-tree",
            tree.stdout.strip(),
            "-p",
            base,
            "-p",
            index_commit.stdout.strip(),
            "-m",
            message,
        ],
        cwd=repo,
    )
    if not saved.ok:
        return None
    head = saved.stdout.strip()
    for attempt in range(1, 1000):
        name = ref if attempt == 1 else f"{ref}-{attempt}"
        held = _run(["git", "rev-parse", "--verify", "-q", name], cwd=repo)
        if held.ok:
            identical = all(
                _run(["git", "rev-parse", name + suffix], cwd=repo).stdout.strip()
                == expected
                for suffix, expected in (
                    ("^{tree}", tree.stdout.strip()),
                    ("^1", base),
                    ("^2^{tree}", index_tree.stdout.strip()),
                )
            )
            if identical:
                log.info(
                    "WIP %s already preserves this HEAD, index and working tree", name
                )
                return name
            continue
        # An empty old value makes update-ref create the ref only if it
        # does not exist, so a ref written meanwhile is never moved.
        res = _run(["git", "update-ref", name, head, ""], cwd=repo)
        if res.ok:
            log.info("WIP %s sha=%s", name, head[:8])
            return name
        log.warning("Failed to write %s: %s", name, res.stderr.strip())
        return None
    log.warning("Refusing to write wip ref %r: every attempt name is taken", ref)
    return None


def rev_parse_head(repo: Path | str) -> str | None:
    """*repo*'s HEAD SHA, or None (not a repo / no commits)."""
    res = _run(["git", "rev-parse", "HEAD"], cwd=repo)
    if not res.ok:
        log.warning("git rev-parse HEAD failed: %s", res.stderr.strip())
        return None
    return res.stdout.strip() or None


def merge_base(repo: Path | str, a: str, b: str) -> str | None:
    """The best common ancestor of *a* and *b*, or None (no common history,
    an unknown ref, or git could not be asked)."""
    res = _run(["git", "merge-base", a, b], cwd=repo)
    if not res.ok:
        return None
    return res.stdout.strip() or None


def diff_range(
    repo: Path | str, base: str, head: str, exclude: Path | str | None = None
) -> str | None:
    """`git diff <base>..<head>` over the project directory, leaving out
    *exclude* (a directory relative to *repo*). None when git could not be
    asked. Bytes that are not UTF-8 (a Latin-1 source file) are shown as
    U+FFFD: the diff is for a model to read."""
    spec = ["--", "."]
    if exclude is not None:
        spec.append(f":(exclude,literal){Path(exclude).as_posix()}")
    res = _run(["git", "diff", f"{base}..{head}", *spec], cwd=repo, errors="replace")
    if not res.ok:
        log.warning(
            "git diff %s..%s failed: %s", base[:12], head[:12], res.stderr.strip()
        )
        return None
    return res.stdout


def status_of(repo: Path | str, paths: list[Path | str]) -> str | None:
    """`git status --porcelain -- <paths>`: what git lists for exactly those
    paths, "" when nothing. None when git could not be asked."""
    res = _run(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            *(str(p) for p in paths),
        ],
        cwd=repo,
    )
    if not res.ok:
        log.warning("git status failed: %s", res.stderr.strip())
        return None
    return res.stdout


def is_ancestor_of_head(repo: Path | str, sha: str) -> bool:
    """Whether *sha* is reachable from HEAD (or is HEAD itself).

    Returns False when git could not be asked. Same reading as everywhere
    else: "unknown" is never allowed to mean "safe to destroy".
    """
    if not sha:
        return False
    res = _run(["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd=repo)
    if res.returncode not in (0, 1):
        log.warning("Could not check ancestry of %s: %s", sha[:8], res.stderr.strip())
        return False
    return res.returncode == 0


def commit_trailers(repo: Path | str) -> list[tuple[str, dict[str, str]]] | None:
    """Every commit reachable from *repo*'s HEAD, newest first, with its
    trailers (keys lower-cased; a key given twice keeps its last value).
    None when git could not be asked; [] for a repo with no commit."""
    res = _run(
        ["git", "log", "HEAD", "--format=%H%x1f%(trailers:only,unfold)%x1e"],
        cwd=repo,
        errors="replace",
    )
    if not res.ok:
        if not has_commits(repo) and is_git_repo(repo):
            return []
        log.warning("git log of trailers failed: %s", res.stderr.strip())
        return None
    out: list[tuple[str, dict[str, str]]] = []
    for record in res.stdout.split("\x1e"):
        sha, sep, body = record.strip("\n").partition("\x1f")
        if not sep or not sha.strip():
            continue
        trailers: dict[str, str] = {}
        for line in body.splitlines():
            key, colon, value = line.partition(":")
            if colon and key.strip():
                trailers[key.strip().lower()] = value.strip()
        out.append((sha.strip(), trailers))
    return out


def user_name(repo: Path | str) -> str:
    """git's `user.name` in *repo*, or "" when unset."""
    res = _run(["git", "config", "user.name"], cwd=repo)
    return res.stdout.strip() if res.ok else ""


def commit_empty(repo: Path | str, message: str) -> str:
    """Commit nothing but *message* on *repo*'s HEAD and return the new SHA.

    `--only --allow-empty` with no path: whatever the index holds stays staged
    and out of the commit. The repo's hooks run. Raises GitLifecycleError when
    git refuses."""
    res = _run(["git", "commit", "--only", "--allow-empty", "-m", message], cwd=repo)
    if not res.ok:
        raise GitLifecycleError(
            f"git commit failed: {(res.stderr or res.stdout).strip()[-400:]}"
        )
    sha = rev_parse_head(repo)
    if sha is None:
        raise GitLifecycleError("git commit succeeded but HEAD cannot be read")
    return sha


def tree_is_clean(
    repo: Path | str, ignore: Path | str | None = None, every_submodule: bool = False
) -> bool:
    """True when *repo* holds nothing but buildme's own bookkeeping.

    Used only to decide whether HEAD is a faithful snapshot to discard back
    to, and (`tdd_engine._reviewed_revision`) whether it is the tree a review
    read. Returns False when git could not be asked — "unknown" must never be
    read as "safe to reset", because a reset over uncommitted work destroys it.
    *ignore*, a directory relative to *repo*, is left out of the check too.
    With *every_submodule*, a submodule whose `ignore` setting hides it from
    `git status` is checked too.
    """
    excludes = list(BOOKKEEPING_EXCLUDES)
    if ignore is not None:
        excludes.append(f":(exclude,literal){Path(ignore).as_posix()}")
    submodules = ["--ignore-submodules=none"] if every_submodule else []
    res = _run(
        ["git", "status", "--porcelain", *submodules, "--", ".", *excludes], cwd=repo
    )
    if not res.ok:
        log.warning("git status failed: %s", res.stderr.strip())
        return False
    return not res.stdout.strip()


def unchecked_files(
    repo: Path | str, ignore: Path | str | None = None
) -> list[str] | None:
    """The entries under *repo* whose index tells git not to look at them
    (`assume-unchanged` or `skip-worktree`), so `git status` would not show an
    edit to them: files and gitlink entries, in *repo*'s index and in the
    index of every initialized submodule below it, nested ones included
    (`vendor/inner/a.py`, `vendor/inner`). Bookkeeping and *ignore* are left
    out of *repo*'s own index, as in `tree_is_clean`. None when an index could
    not be read, a submodule directory holds files but no checkout git can
    read, or the filesystem refused a look (an unreadable submodule directory,
    say). Read-only: it only lists."""
    excludes = list(BOOKKEEPING_EXCLUDES)
    if ignore is not None:
        excludes.append(f":(exclude,literal){Path(ignore).as_posix()}")
    try:
        return _unchecked_in(Path(repo), excludes, "")
    except OSError as exc:
        log.info("Could not inspect the index under %s: %s", repo, exc)
        return None


def _unchecked_in(repo: Path, excludes: list[str], prefix: str) -> list[str] | None:
    result = _run(["git", "ls-files", "-v", "-s", "-z", "--", ".", *excludes], cwd=repo)
    if not result.ok:
        return None
    found: list[str] = []
    for entry in filter(None, result.stdout.split("\0")):
        # `<tag> <mode> <sha> <stage>\t<path>`; `-v` writes a lowercase tag for
        # assume-unchanged and S for skip-worktree.
        identity, name = entry.split("\t", 1)
        tag, mode = identity.split(" ", 2)[:2]
        if (tag.islower() or tag == "S") and prefix + name not in found:
            found.append(prefix + name)
        path = repo / name
        if mode != "160000" or path.is_symlink() or not path.is_dir():
            continue
        top = _run(["git", "rev-parse", "--show-toplevel"], cwd=path)
        if not (top.ok and Path(top.stdout.strip()).resolve() == path.resolve()):
            if any(path.iterdir()):
                return None
            continue  # an uninitialized submodule: nothing checked out to hide
        below = _unchecked_in(path, list(BOOKKEEPING_EXCLUDES), f"{prefix}{name}/")
        if below is None:
            return None
        found += [n for n in below if n not in found]
    return found


def index_entries(
    repo: Path | str, ignore: Path | str | None = None
) -> dict[str, str] | None:
    """Project-relative index modes, blob IDs and conflict stages, read only."""
    excludes = list(BOOKKEEPING_EXCLUDES)
    if ignore is not None:
        excludes.append(f":(exclude,literal){Path(ignore).as_posix()}")
    result = _run(["git", "ls-files", "--stage", "-z", "--", ".", *excludes], cwd=repo)
    if not result.ok:
        return None
    entries: dict[str, str] = {}
    for entry in filter(None, result.stdout.split("\0")):
        identity, name = entry.split("\t", 1)
        entries[name] = entries.get(name, "") + identity + "\n"
    return entries


def dirty_files(
    repo: Path | str, ignore: Path | str | None = None
) -> dict[str, str] | None:
    """Every path under *repo* whose content differs from HEAD (an edit, a
    deletion, a new file git does not ignore), relative to *repo*, mapped to
    a fingerprint of its content now: so two calls give equal maps only when
    the same paths hold the same content. Buildme's bookkeeping and *ignore*
    (a directory relative to *repo*) are left out, as in `tree_is_clean`.
    None when git could not be asked."""
    excludes = list(BOOKKEEPING_EXCLUDES)
    if ignore is not None:
        excludes.append(f":(exclude,literal){Path(ignore).as_posix()}")
    changed = _run(
        [
            "git",
            "diff",
            "HEAD",
            "--name-only",
            "-z",
            "--relative",
            "--no-renames",
            "--",
            ".",
            *excludes,
        ],
        cwd=repo,
    )
    staged = _run(
        [
            "git",
            "diff",
            "--cached",
            "HEAD",
            "--name-only",
            "-z",
            "--relative",
            "--no-renames",
            "--",
            ".",
            *excludes,
        ],
        cwd=repo,
    )
    untracked = _run(
        [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            ".",
            *excludes,
        ],
        cwd=repo,
    )
    if not (changed.ok and staged.ok and untracked.ok):
        log.warning(
            "git could not list the changed files: %s",
            (changed.stderr or untracked.stderr).strip(),
        )
        return None
    out: dict[str, str] = {}
    for rel in filter(
        None, (changed.stdout + staged.stdout + untracked.stdout).split("\0")
    ):
        path = Path(repo) / rel
        try:
            out[rel] = _working_identity(path)
        except OSError as e:
            log.warning("Could not read %s: %s", path, e)
            return None
    return out


def discard_to(repo: Path | str, sha: str, label: str = "") -> bool:
    """Throw away every change made under *repo* since *sha*, keeping
    buildme's bookkeeping and everything outside *repo*.

    Used to un-do a failed step: a refactor that failed verification, a fix
    not kept, a mutant left on disk, a findings step being rewound. In order:

    1. *sha* must still be an ancestor of HEAD (below).
    2. When *repo* is a subdirectory of its repo, no commit in
       ``<sha>..HEAD`` may change a path outside *repo*: dropping it would
       drop someone else's work, so this refuses instead.
    3. ``reset --soft <sha>`` moves the branch back and keeps every file.
    4. ``restore --source=<sha> --staged --worktree -- .`` puts the index and
       the files under *repo* back to *sha*; files outside *repo* keep their
       content and their staged state.
    5. ``clean`` removes files the step newly created, which the next
       whole-tree commit would otherwise sweep into the build as
       reverted-but-committed work. The bookkeeping pathspecs keep the clean
       from deleting .build-state.json out from under a running build.
       (File-level excludes cannot save a wholly *untracked* directory —
       `clean -d` removes those entire. By the time a block runs the spec
       stages have committed the change directory, so the exclude bites on
       the individual files, which is the case that matters.)

    At the repo root this gives the same HEAD, index and files as
    ``reset --hard <sha>`` plus the same clean. The soft reset goes first so
    that a stop between steps 3 and 4 leaves HEAD at *sha* with the dropped
    changes staged under *repo*, which a resumed step's stop record can tell
    apart from someone else's work.

    **Only ever moves backwards along the current history.** A reset will
    happily jump to any commit, including one on unrelated history, and the
    refactorer holds `Bash` — so it can move HEAD, commit, or switch
    branches between the moment *sha* was captured and this call. Resetting
    without checking would then discard whatever HEAD had actually reached:
    earlier blocks' commits, or, under `--no-worktree`, the user's own. So
    *sha* must still be an ancestor of HEAD, and this refuses when it is not.

    Returns False (with a warning) when git refused or a check failed —
    never raises. A caller that gets False must not treat the work as
    discarded.
    """
    if not is_ancestor_of_head(repo, sha):
        log.warning(
            "Refusing to discard %s: %s is not an ancestor of HEAD — resetting "
            "would throw away history this pass did not create",
            label,
            sha[:8],
        )
        return False
    prefix = path_prefix(repo)
    if prefix is None:
        log.warning(
            "Refusing to discard %s: git could not say where %s is in its repo",
            label,
            repo,
        )
        return False
    if prefix:
        outside = _run(
            [
                "git",
                "diff",
                "--name-only",
                sha,
                "HEAD",
                "--",
                ":/",
                f":(top,exclude){prefix}",
            ],
            cwd=repo,
        )
        if not outside.ok:
            log.warning(
                "Refusing to discard %s: git could not list the files changed "
                "since %s: %s",
                label,
                sha[:8],
                outside.stderr.strip(),
            )
            return False
        paths = outside.stdout.splitlines()
        if paths:
            shown = ", ".join(paths[:5]) + (
                f" and {len(paths) - 5} more" if len(paths) > 5 else ""
            )
            log.warning(
                "Refusing to discard %s: the commits after %s change files outside "
                "the project (%s) — dropping them would drop work that is not "
                "this step's",
                label,
                sha[:8],
                shown,
            )
            return False
    reset = _run(["git", "reset", "--soft", sha], cwd=repo)
    if not reset.ok:
        log.warning(
            "Could not discard %s (soft reset to %s): %s",
            label,
            sha[:8],
            reset.stderr.strip() or reset.stdout.strip(),
        )
        return False
    restore = _run(
        ["git", "restore", f"--source={sha}", "--staged", "--worktree", "--", "."],
        cwd=repo,
    )
    if not restore.ok:
        log.warning(
            "Could not discard %s (restore to %s after the soft reset): %s",
            label,
            sha[:8],
            restore.stderr.strip() or restore.stdout.strip(),
        )
        return False
    clean = _run(["git", "clean", "-fdq", "--", ".", *BOOKKEEPING_EXCLUDES], cwd=repo)
    if not clean.ok:
        log.warning(
            "Could not discard %s (clean after restoring %s): %s",
            label,
            sha[:8],
            clean.stderr.strip() or clean.stdout.strip(),
        )
        return False
    log.info("DISCARD %s reset to %s", label, sha[:8])
    return True


def unwind_commits_to(repo: Path | str, sha: str, label: str = "") -> bool:
    """Move the current branch back to *sha* and keep every change made since.

    For an agent that holds `Bash` and committed its own work: `reset --soft`
    puts its commits' changes back into the index and tree, so buildme can
    check them and commit them itself (or discard them). Same guard as
    `discard_to`: *sha* must still be an ancestor of HEAD. Returns False (with
    a warning) when git refused — never raises.
    """
    if not is_ancestor_of_head(repo, sha):
        log.warning(
            "Refusing to unwind %s: %s is not an ancestor of HEAD", label, sha[:8]
        )
        return False
    reset = _run(["git", "reset", "--soft", sha], cwd=repo)
    if not reset.ok:
        log.warning(
            "Could not unwind %s (soft reset to %s): %s",
            label,
            sha[:8],
            reset.stderr.strip() or reset.stdout.strip(),
        )
        return False
    log.info("UNWIND %s soft reset to %s", label, sha[:8])
    return True


def push_branch(worktree: Path, branch: str) -> GitResult:
    """``git push -u origin <branch>``. Never forces. Never raises."""
    return _run(
        ["git", "push", "-u", "origin", branch], cwd=worktree, timeout=_PUSH_TIMEOUT
    )


def open_pr_with_skill(
    worktree: Path, argv: list[str]
) -> tuple[str | None, list[str], GitResult]:
    """Run the pr skill's ``pr_pipeline open`` (argv from
    ``orchestrator._pr_open_argv``). Returns (pr_url, its PR_DIAGNOSIS lines,
    result). Never raises."""
    res = _run_pr_skill(argv, cwd=worktree)
    url = None
    diagnoses: list[str] = []
    for line in (res.stdout or "").splitlines():
        if line.startswith("PR:"):
            url = line[3:].strip() or None
        elif line.startswith("PR_DIAGNOSIS:"):
            diagnoses.append(line[len("PR_DIAGNOSIS:") :].strip())
    return url, diagnoses, res


# --- Diagnosis ------------------------------------------------------------


def manual_finish_commands(
    worktree: Path,
    branch: str,
    title: str,
    *,
    include_pr: bool = True,
    draft: bool = True,
    pr_skill_argv: list[str] | None = None,
    base: str | None = None,
) -> str:
    """The exact commands a human runs to finish by hand after a push/PR
    failure. Printed into build-progress.md — never executed. With
    ``pr_skill_argv``, the PR is opened by the pr skill from the repo's PR
    template, and ``gh pr create`` is listed after it as the fallback.
    ``base`` is the PR's base branch when it is not the default branch."""
    lines = [
        f"cd {shlex.quote(str(worktree))}",
        f"git push -u origin {shlex.quote(branch)}",
    ]
    if include_pr and pr_skill_argv:
        lines.append(" ".join(shlex.quote(a) for a in pr_skill_argv))
        lines.append("# or, without the repo's PR template:")
    if include_pr:
        pr = f"gh pr create --title {shlex.quote(title)} --body-file -"
        if base:
            pr += f" --base {shlex.quote(base)}"
        if draft:
            pr += " --draft"
        lines.append(pr)
    return "\n".join(lines)
