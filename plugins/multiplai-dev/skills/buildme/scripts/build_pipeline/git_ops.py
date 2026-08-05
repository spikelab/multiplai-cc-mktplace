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
  ``discard_to``, which throws away a failed refactor — and it refuses unless
  the target sha is still an ancestor of HEAD, so it can only ever move
  backwards along the current history. ``remove_worktree`` exists as a
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

import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_GIT_TIMEOUT = 60
_PUSH_TIMEOUT = 300
_GH_TIMEOUT = 300


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


def _run(argv: list[str], cwd: Path | str | None = None, timeout: int = _GIT_TIMEOUT) -> GitResult:
    """Run a fixed argv list. shell=False (the default) — never a string."""
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return GitResult(argv, proc.returncode, proc.stdout or "", proc.stderr or "")
    except FileNotFoundError as e:
        return GitResult(argv, 127, "", str(e))
    except subprocess.TimeoutExpired:
        return GitResult(argv, 124, "", f"timed out after {timeout}s")


def _run_gh(argv: list[str], cwd: Path | str | None = None, timeout: int = _GH_TIMEOUT) -> GitResult:
    """Separate seam for ``gh`` so tests can mock it without mocking git."""
    return _run(argv, cwd=cwd, timeout=timeout)


# --- Inspection -----------------------------------------------------------


def repo_root(path: Path | str) -> Path | None:
    """Absolute root of the git repo containing ``path``, or None."""
    res = _run(["git", "rev-parse", "--show-toplevel"], cwd=path)
    if not res.ok:
        return None
    return Path(res.stdout.strip())


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


def resolve_new_branch(repo: Path, change_name: str, base: str) -> tuple[str, Path]:
    """Pick a free ``buildme/<change>`` branch and its matching worktree path.

    A colliding branch that carries commits not on ``base`` is somebody's
    work — refuse rather than guess. A colliding branch with nothing on it
    gets a ``-2``/``-3``/... suffix (branch and worktree stay in lockstep).
    """
    base_branch = branch_name_for(change_name)
    for suffix in range(1, 51):
        branch = base_branch if suffix == 1 else f"{base_branch}-{suffix}"
        dest = worktree_dest(repo, change_name, suffix)
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


def create_worktree(repo: Path, branch: str, dest: Path) -> GitResult:
    """``git worktree add -b <branch> <dest>``. Never forces, never reuses."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    res = _run(
        ["git", "worktree", "add", "-b", branch, str(dest)],
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


def remove_worktree(repo: Path, dest: Path) -> GitResult:
    """DOCUMENTED HELPER — `git worktree remove`, deliberately never called
    by the pipeline (test_git_ops.py asserts no other module references it).

    Worktree teardown is the calling session's decision, made from the
    workspace root (an agent must never delete the worktree it is standing
    in). Kept here so the one place that knows how to run git also documents
    the safe teardown command.
    """
    return _run(["git", "worktree", "remove", str(dest)], cwd=repo)


def commit_paths(worktree: Path, message: str, paths: list[str]) -> str | None:
    """Stage exactly ``paths`` and commit them. Returns the new SHA, or None
    when there was nothing to commit (or the commit failed — logged, never
    raised: a bookkeeping commit must not kill a build).

    Explicit pathspecs only. This function never stages the whole tree.
    """
    if not paths:
        return None
    add = _run(["git", "add", "--", *paths], cwd=worktree)
    if not add.ok:
        log.warning("git add failed in %s: %s", worktree, add.stderr.strip())
        return None
    staged = _run(["git", "diff", "--cached", "--quiet"], cwd=worktree)
    if staged.returncode == 0:
        log.info("Nothing staged to commit in %s (%s)", worktree, message)
        return None
    commit = _run(["git", "commit", "-m", message], cwd=worktree)
    if not commit.ok:
        log.warning("git commit failed in %s: %s", worktree, commit.stderr.strip())
        return None
    sha = _run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip()
    log.info("COMMIT sha=%s msg=%s", sha[:8], message)
    return sha or None


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
    ":(exclude,glob)**/.build-state.json",
    ":(exclude,glob)**/.board.json",
    ":(exclude,glob)**/build-progress.md",
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
    return commit_paths(Path(config.project_dir), message, [*paths, *BOOKKEEPING_EXCLUDES])


# --- Whole-tree operations (the TDD block loop) ---------------------------
#
# Everything above stages explicit paths. The three below are the pipeline's
# only whole-tree git operations, and they live here rather than in the TDD
# engine so that every `git` invocation the pipeline makes is in one module,
# behind one `_run`. They are pathspec-limited (`-- . :(exclude)…`), never a
# bare `git add -A`: a block's output is not fully enumerable from the agent's
# self-reported FILES: slot, so dropping a produced file would be worse than
# sweeping one in — and since the build runs inside its own worktree,
# "everything under ." IS this build's own work.


def commit_tree(repo: Path | str, message: str, label: str = "") -> str | None:
    """Commit everything in *repo* except buildme's bookkeeping.

    Returns the new commit's SHA, or None when there was nothing to commit or
    the commit failed (logged as a warning — never raises). *label* only names
    the operation in log lines.
    """
    add = _run(["git", "add", "-A", "--", ".", *BOOKKEEPING_EXCLUDES], cwd=repo)
    if not add.ok:
        log.warning("Failed to commit %s: %s", label, add.stderr.strip() or add.stdout.strip())
        return None
    staged = _run(["git", "diff", "--cached", "--quiet"], cwd=repo)
    if staged.returncode == 0:
        log.info("No changes to commit for %s", label)
        return None
    commit = _run(["git", "commit", "-m", message], cwd=repo)
    if not commit.ok:
        log.warning("Failed to commit %s: %s", label, commit.stderr.strip() or commit.stdout.strip())
        return None
    sha = _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    if not sha:
        log.warning("Committed %s but could not read HEAD", label)
        return None
    log.info("COMMIT %s sha=%s", label, sha[:8])
    return sha


def rev_parse_head(repo: Path | str) -> str | None:
    """*repo*'s HEAD SHA, or None (not a repo / no commits)."""
    res = _run(["git", "rev-parse", "HEAD"], cwd=repo)
    if not res.ok:
        log.warning("git rev-parse HEAD failed: %s", res.stderr.strip())
        return None
    return res.stdout.strip() or None


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


def tree_is_clean(repo: Path | str) -> bool:
    """True when *repo* holds nothing but buildme's own bookkeeping.

    Used only to decide whether HEAD is a faithful snapshot to discard back
    to. Returns False when git could not be asked — "unknown" must never be
    read as "safe to reset", because a reset over uncommitted work destroys it.
    """
    res = _run(["git", "status", "--porcelain", "--", ".", *BOOKKEEPING_EXCLUDES], cwd=repo)
    if not res.ok:
        log.warning("git status failed: %s", res.stderr.strip())
        return False
    return not res.stdout.strip()


def discard_to(repo: Path | str, sha: str, label: str = "") -> bool:
    """Throw away every change made since *sha*, keeping buildme's bookkeeping.

    Used to un-do a refactor that failed verification. `reset --hard` restores
    tracked files; the `clean` removes files the agent newly created, which
    `reset` leaves behind and the next whole-tree commit would otherwise sweep
    into the build as reverted-but-committed work. The bookkeeping pathspecs
    are what keep the clean from deleting .build-state.json out from under a
    running build. (File-level excludes cannot save a wholly *untracked*
    directory — `clean -d` removes those entire. By the time a block runs the
    spec stages have committed the change directory, so the exclude bites on
    the individual files, which is the case that matters.)

    **Only ever moves backwards along the current history.** `reset --hard`
    will happily jump to any commit, including one on unrelated history, and
    the refactorer holds `Bash` — so it can move HEAD, commit, or switch
    branches between the moment *sha* was captured and this call. Resetting
    without checking would then discard whatever HEAD had actually reached:
    earlier blocks' commits, or, under `--no-worktree`, the user's own. So
    *sha* must still be an ancestor of HEAD, and this refuses when it is not.

    Returns False (with a warning) when git refused — never raises. A caller
    that gets False must not treat the refactor as reverted.
    """
    if not is_ancestor_of_head(repo, sha):
        log.warning(
            "Refusing to discard %s: %s is not an ancestor of HEAD — resetting "
            "would throw away history this pass did not create",
            label, sha[:8],
        )
        return False
    reset = _run(["git", "reset", "--hard", sha], cwd=repo)
    if not reset.ok:
        log.warning("Could not discard %s (reset to %s): %s",
                    label, sha[:8], reset.stderr.strip() or reset.stdout.strip())
        return False
    clean = _run(["git", "clean", "-fdq", "--", ".", *BOOKKEEPING_EXCLUDES], cwd=repo)
    if not clean.ok:
        log.warning("Could not discard %s (clean after reset to %s): %s",
                    label, sha[:8], clean.stderr.strip() or clean.stdout.strip())
        return False
    log.info("DISCARD %s reset to %s", label, sha[:8])
    return True


def push_branch(worktree: Path, branch: str) -> GitResult:
    """``git push -u origin <branch>``. Never forces. Never raises."""
    return _run(["git", "push", "-u", "origin", branch], cwd=worktree, timeout=_PUSH_TIMEOUT)


def open_pr(worktree: Path, title: str, body: str, draft: bool = True) -> tuple[str | None, GitResult]:
    """``gh pr create``. Returns (pr_url, result). Never raises."""
    argv = ["gh", "pr", "create", "--title", title, "--body", body]
    if draft:
        argv.append("--draft")
    res = _run_gh(argv, cwd=worktree)
    if not res.ok:
        return None, res
    url = _extract_pr_url(res.stdout)
    return url, res


_PR_URL_RE = re.compile(r"https://\S*/pull/\d+")


def _extract_pr_url(text: str) -> str | None:
    m = _PR_URL_RE.search(text or "")
    return m.group(0) if m else None


# --- Diagnosis ------------------------------------------------------------


def manual_finish_commands(
    worktree: Path, branch: str, title: str, *, include_pr: bool = True, draft: bool = True
) -> str:
    """The exact commands a human runs to finish by hand after a push/PR
    failure. Printed into build-progress.md — never executed."""
    lines = [
        f"cd {shlex.quote(str(worktree))}",
        f"git push -u origin {shlex.quote(branch)}",
    ]
    if include_pr:
        pr = f"gh pr create --title {shlex.quote(title)} --body-file -"
        if draft:
            pr += " --draft"
        lines.append(pr)
    return "\n".join(lines)
