"""Every `gh` and `git` call this skill makes.

- No shell: each call is a fixed argv list, `shell=False`.
- `gh` goes through `_run_gh` (copied from buildme's `git_ops._run_gh`) so
  tests stub it without stubbing git.
- Writes: `git push -u origin refs/heads/<b>:refs/heads/<b>` (never `--force`), `gh pr create`,
  `gh pr edit <url> --body-file`, `gh pr comment <url> --body-file`. Nothing here merges,
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


def _run(
    argv: list[str], cwd: Path | str | None = None, timeout: int = _GIT_TIMEOUT
) -> Result:
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
        return Result(argv, proc.returncode, proc.stdout or "", proc.stderr or "")
    except FileNotFoundError as e:
        return Result(argv, 127, "", str(e))
    except subprocess.TimeoutExpired:
        return Result(argv, 124, "", f"timed out after {timeout}s")


def _run_gh(
    argv: list[str], cwd: Path | str | None = None, timeout: int = _GH_TIMEOUT
) -> Result:
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
    return _run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=repo
    ).ok


def local_branch_exists(repo: Path, name: str) -> bool:
    return _ref_exists(repo, f"refs/heads/{name}")


def valid_branch(repo: Path, name: str) -> bool:
    """A valid Git branch name that cannot be read as an option. `--branch` also expands
    `@{-N}`, so the name must come back unchanged."""
    if not name or name.startswith("-"):
        return False
    res = _run(["git", "check-ref-format", "--branch", name], cwd=repo)
    return res.ok and res.stdout.strip() == name


_REMOTE_RE = re.compile(
    r"(?:https://[^/\s]+/|git@[^:\s]+:)([^/\s]+)/([^/\s]+?)(?:\.git)?/?"
)
_URL_REPO_RE = re.compile(r"https://[^/\s]+/([^/\s]+)/([^/\s]+)/pull/\d+/?")


def origin_repo(repo: Path) -> str | None:
    """`owner/name` of the checkout's `origin` (an `https://` or `git@host:` URL); None otherwise."""
    res = _run(["git", "remote", "get-url", "origin"], cwd=repo)
    m = _REMOTE_RE.fullmatch(res.stdout.strip()) if res.ok else None
    return f"{m.group(1)}/{m.group(2)}" if m else None


def url_repo(url: str) -> str | None:
    """`owner/name` of a PR URL `https://<host>/<owner>/<name>/pull/<n>`; None otherwise."""
    m = _URL_REPO_RE.fullmatch((url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def git_default_branch(repo: Path) -> str | None:
    """origin's HEAD, else `main` or `master` when one exists. No `gh` call."""
    res = _run(["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd=repo)
    if res.ok and res.stdout.strip():
        return res.stdout.strip().removeprefix("refs/remotes/origin/")
    for name in ("main", "master"):
        if _ref_exists(repo, name) or _ref_exists(repo, f"origin/{name}"):
            return name
    return None


def diff_refs(repo: Path, base: str, head: str) -> tuple[str, str | None]:
    """The refs to diff: `origin/<base>` when it exists (what GitHub compares against), and *head*
    when it exists locally, else `origin/<head>` when only the remote-tracking ref exists.

    A named *head* that is in neither place gives None, never `HEAD`: the checked-out branch is
    some other branch, and its size and files would describe the wrong change. Only an empty
    *head* (no branch name to go on) means `HEAD`."""
    base_ref = f"origin/{base}" if _ref_exists(repo, f"origin/{base}") else base
    if not head:
        return base_ref, "HEAD"
    if _ref_exists(repo, head):
        return base_ref, head
    if _ref_exists(repo, f"origin/{head}"):
        return base_ref, f"origin/{head}"
    return base_ref, None


def _head_missing(head: str) -> str:
    return f"`{head}` is not in this checkout (no local ref and no `origin/{head}`); fetch it first"


_OID_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def is_oid(text: str) -> bool:
    """A full SHA-1 or SHA-256 commit id, safe to put in a git argv."""
    return bool(_OID_RE.fullmatch(text))


def fetch_pr_head(repo: Path, number: str, oid: str) -> tuple[bool, str]:
    """Make the PR's head commit *oid* available locally: already there, or fetched from
    origin's `refs/pull/<number>/head`, which GitHub keeps for every PR, a fork's included.
    (False, why) when it still is not there."""
    if _ref_exists(repo, oid):
        return True, ""
    res = _run(
        ["git", "fetch", "--quiet", "--no-tags", "origin", f"refs/pull/{number}/head"],
        cwd=repo,
    )
    if not res.ok:
        return False, f"`{res.command}` failed: {res.why()}"
    if not _ref_exists(repo, oid):
        return False, (
            f"origin's refs/pull/{number}/head is not {oid[:12]}: "
            "the PR was pushed to since `gh pr view` read it"
        )
    return True, ""


_SHORTSTAT_RE = {
    "files": re.compile(r"(\d+) files? changed"),
    "added": re.compile(r"(\d+) insertions?\(\+\)"),
    "removed": re.compile(r"(\d+) deletions?\(-\)"),
}


def shortstat(repo: Path, base: str, head: str) -> tuple[DiffSize | None, str]:
    """`git diff --shortstat <base>...<head>`, parsed; (None, why) when git fails."""
    base_ref, head_ref = diff_refs(repo, base, head)
    if head_ref is None:
        return None, _head_missing(head)
    res = _run(["git", "diff", "--shortstat", f"{base_ref}...{head_ref}"], cwd=repo)
    if not res.ok:
        return None, f"`{res.command}` failed: {res.why()}"
    nums = {
        k: int(m.group(1)) if (m := rx.search(res.stdout)) else 0
        for k, rx in _SHORTSTAT_RE.items()
    }
    return DiffSize(**nums), ""


def changed_files(repo: Path, base: str, head: str, limit: int = 300) -> list[str]:
    """`git diff --name-status <base>...<head>` lines, at most *limit*; [] when git fails."""
    base_ref, head_ref = diff_refs(repo, base, head)
    if head_ref is None:
        return []
    res = _run(["git", "diff", "--name-status", f"{base_ref}...{head_ref}"], cwd=repo)
    if not res.ok:
        return []
    return [line for line in res.stdout.splitlines() if line.strip()][:limit]


# --- gh (read) --------------------------------------------------------------


def gh_default_branch(repo: Path) -> str | None:
    res = _run_gh(
        [
            "gh",
            "repo",
            "view",
            "--json",
            "defaultBranchRef",
            "--jq",
            ".defaultBranchRef.name",
        ],
        cwd=repo,
    )
    name = res.stdout.strip() if res.ok else ""
    return name or None


def view_pr(repo: Path, pr: str) -> tuple[dict | None, Result]:
    res = _run_gh(
        [
            "gh",
            "pr",
            "view",
            pr,
            "--json",
            "body,author,number,url,baseRefName,headRefName,headRefOid,isCrossRepository",
        ],
        cwd=repo,
    )
    if not res.ok:
        return None, res
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None, Result(res.argv, 2, res.stdout, "gh pr view did not return JSON")
    return data, res


def viewer_permission(repo: Path) -> str:
    res = _run_gh(
        [
            "gh",
            "repo",
            "view",
            "--json",
            "viewerPermission",
            "--jq",
            ".viewerPermission",
        ],
        cwd=repo,
    )
    return res.stdout.strip() if res.ok else ""


def viewer_login(repo: Path) -> str:
    res = _run_gh(["gh", "api", "user", "--jq", ".login"], cwd=repo)
    return res.stdout.strip() if res.ok else ""


# --- writes -----------------------------------------------------------------


def push_argv(branch: str) -> list[str]:
    # A full refspec: the local branch and nothing else, to the branch of the same name.
    return ["git", "push", "-u", "origin", f"refs/heads/{branch}:refs/heads/{branch}"]


def push(repo: Path, branch: str) -> Result:
    """`git push -u origin refs/heads/<branch>:refs/heads/<branch>`. Never forces."""
    return _run(push_argv(branch), cwd=repo, timeout=_PUSH_TIMEOUT)


def create_argv(
    title: str, body_file: Path, head: str, base: str | None, draft: bool
) -> list[str]:
    argv = [
        "gh",
        "pr",
        "create",
        "--title",
        title,
        "--body-file",
        str(body_file),
        "--head",
        head,
    ]
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
