"""Every `gh` and `git` call this skill makes.

- No shell: each call is a fixed argv list, `shell=False`.
- `gh` goes through `_run_gh` (copied from buildme's `git_ops._run_gh`) so
  tests stub it without stubbing git.
- Writes: `git push -u origin <branch>` (never `--force`), `gh pr create`,
  `gh pr edit --body-file`, `gh pr comment --body-file`. Nothing here merges,
  closes a PR, deletes a branch or edits a title or labels.
- A failure returns a `Result`; callers turn it into `PR_DIAGNOSIS:` and exit 2.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

_GIT_TIMEOUT = 60
_PUSH_TIMEOUT = 300
_GH_TIMEOUT = 300

# The roles `gh repo view --json viewerPermission` returns that can edit any PR's body.
EDIT_ROLES = ("ADMIN", "MAINTAIN", "WRITE")


@dataclass(frozen=True)
class Result:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def command(self) -> str:
        return " ".join(shlex.quote(a) for a in self.argv)

    def why(self) -> str:
        return (self.stderr or self.stdout).strip()[:800] or f"exit {self.returncode}"


@dataclass(frozen=True)
class DiffSize:
    files: int
    added: int
    removed: int

    def line(self) -> str:
        return f"DIFF:{self.files} files, +{self.added} -{self.removed}"


def _run(argv: list[str], cwd: Path | str | None = None, timeout: int = _GIT_TIMEOUT) -> Result:
    try:
        proc = subprocess.run(argv, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                              timeout=timeout, shell=False, check=False)
        return Result(argv, proc.returncode, proc.stdout or "", proc.stderr or "")
    except FileNotFoundError as e:
        return Result(argv, 127, "", str(e))
    except subprocess.TimeoutExpired:
        return Result(argv, 124, "", f"timed out after {timeout}s")


def _run_gh(argv: list[str], cwd: Path | str | None = None, timeout: int = _GH_TIMEOUT) -> Result:
    """Separate seam for `gh` so tests can stub it without stubbing git."""
    return _run(argv, cwd=cwd, timeout=timeout)


# --- git (read) -------------------------------------------------------------


def repo_root(path: Path) -> Path | None:
    res = _run(["git", "rev-parse", "--show-toplevel"], cwd=path)
    return Path(res.stdout.strip()) if res.ok else None


def current_branch(repo: Path) -> str:
    res = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    return res.stdout.strip() if res.ok else ""


def _ref_exists(repo: Path, ref: str) -> bool:
    return _run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=repo).ok


def git_default_branch(repo: Path) -> str | None:
    """origin's HEAD, else `main` or `master` when one exists. No `gh` call."""
    res = _run(["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd=repo)
    if res.ok and res.stdout.strip():
        return res.stdout.strip().removeprefix("refs/remotes/origin/")
    for name in ("main", "master"):
        if _ref_exists(repo, name) or _ref_exists(repo, f"origin/{name}"):
            return name
    return None


def diff_refs(repo: Path, base: str, head: str) -> tuple[str, str]:
    """The refs to diff: `origin/<base>` when it exists (what GitHub compares against), and *head*
    when it exists locally, else `origin/<head>` when only the remote-tracking ref exists, else `HEAD`."""
    base_ref = f"origin/{base}" if _ref_exists(repo, f"origin/{base}") else base
    if head and _ref_exists(repo, head):
        head_ref = head
    elif head and _ref_exists(repo, f"origin/{head}"):
        head_ref = f"origin/{head}"
    else:
        head_ref = "HEAD"
    return base_ref, head_ref


_SHORTSTAT_RE = {
    "files": re.compile(r"(\d+) files? changed"),
    "added": re.compile(r"(\d+) insertions?\(\+\)"),
    "removed": re.compile(r"(\d+) deletions?\(-\)"),
}


def shortstat(repo: Path, base: str, head: str) -> tuple[DiffSize | None, str]:
    """`git diff --shortstat <base>...<head>`, parsed; (None, why) when git fails."""
    base_ref, head_ref = diff_refs(repo, base, head)
    res = _run(["git", "diff", "--shortstat", f"{base_ref}...{head_ref}"], cwd=repo)
    if not res.ok:
        return None, f"`{res.command}` failed: {res.why()}"
    nums = {k: int(m.group(1)) if (m := rx.search(res.stdout)) else 0 for k, rx in _SHORTSTAT_RE.items()}
    return DiffSize(**nums), ""


def changed_files(repo: Path, base: str, head: str, limit: int = 300) -> list[str]:
    """`git diff --name-status <base>...<head>` lines, at most *limit*; [] when git fails."""
    base_ref, head_ref = diff_refs(repo, base, head)
    res = _run(["git", "diff", "--name-status", f"{base_ref}...{head_ref}"], cwd=repo)
    if not res.ok:
        return []
    return [line for line in res.stdout.splitlines() if line.strip()][:limit]


# --- gh (read) --------------------------------------------------------------


def gh_default_branch(repo: Path) -> str | None:
    res = _run_gh(["gh", "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"],
                  cwd=repo)
    name = res.stdout.strip() if res.ok else ""
    return name or None


def view_pr(repo: Path, pr: str) -> tuple[dict | None, Result]:
    res = _run_gh(["gh", "pr", "view", pr, "--json",
                   "body,author,number,url,baseRefName,headRefName"], cwd=repo)
    if not res.ok:
        return None, res
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None, Result(res.argv, 2, res.stdout, "gh pr view did not return JSON")
    return data, res


def viewer_permission(repo: Path) -> str:
    res = _run_gh(["gh", "repo", "view", "--json", "viewerPermission", "--jq", ".viewerPermission"], cwd=repo)
    return res.stdout.strip() if res.ok else ""


def viewer_login(repo: Path) -> str:
    res = _run_gh(["gh", "api", "user", "--jq", ".login"], cwd=repo)
    return res.stdout.strip() if res.ok else ""


# --- writes -----------------------------------------------------------------


def push_argv(branch: str) -> list[str]:
    return ["git", "push", "-u", "origin", branch]


def push(repo: Path, branch: str) -> Result:
    """`git push -u origin <branch>`. Never forces."""
    return _run(push_argv(branch), cwd=repo, timeout=_PUSH_TIMEOUT)


def create_argv(title: str, body_file: Path, head: str, base: str | None, draft: bool) -> list[str]:
    argv = ["gh", "pr", "create", "--title", title, "--body-file", str(body_file), "--head", head]
    if base:
        argv += ["--base", base]
    if draft:
        argv.append("--draft")
    return argv


def edit_argv(pr: str, body_file: Path) -> list[str]:
    return ["gh", "pr", "edit", pr, "--body-file", str(body_file)]


def comment_argv(pr: str, body_file: Path) -> list[str]:
    return ["gh", "pr", "comment", pr, "--body-file", str(body_file)]


def run_write(repo: Path, argv: list[str]) -> Result:
    """Run one of the `gh` write argvs built above."""
    return _run_gh(argv, cwd=repo)


_PR_URL_RE = re.compile(r"https://\S*/pull/\d+")


def pr_url(text: str) -> str | None:
    m = _PR_URL_RE.search(text or "")
    return m.group(0) if m else None
