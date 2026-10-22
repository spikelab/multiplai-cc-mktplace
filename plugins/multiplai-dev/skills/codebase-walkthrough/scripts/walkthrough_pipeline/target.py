"""Resolve the module to explain, and take snapshots of commits.

Read-only over every repository. The only git commands run anywhere in the
pipeline are `rev-parse`, `show`, `archive`, `ls-files`, `ls-tree`, `diff`,
`merge-base`, `log` and `grep`, each a fixed argv with `shell=False`. Nothing checks out
or writes to a repo; `--fetch` (a change target only, when the user passes it)
runs `git fetch --quiet origin` through `git_targets.fetch_origin`.

The working tree of the target is never read: its files are listed from a
`git archive` of the commit (HEAD, `--commit`, or a change's head), and agents
read that snapshot.

A change target (`--scope pr`, `--pr` or `--range`) is read at two commits: its
head, under the repository's key, and the merge-base, under `<key>@base`, a
second key for the same repository. Only the files the change touches are
snapshotted at the base.
"""

from __future__ import annotations

import io
import logging
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import git_targets
from multiplai_core.citations import GIT_ARGV, git_env

from .models import ChangeInfo, GateResult, RepoInfo, TargetInfo

log = logging.getLogger(__name__)

LOCKFILES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
    "go.sum",
}
BINARY_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".bmp",
    ".tiff",
    ".svgz",
    ".heic",
    ".pdf",
    ".zip",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    ".tar",
    ".jar",
    ".whl",
    ".mp3",
    ".mp4",
    ".mov",
    ".m4a",
    ".wav",
    ".avi",
    ".webm",
    ".ogg",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".bin",
    ".o",
    ".a",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".pkl",
    ".pickle",
    ".npy",
    ".npz",
    ".parquet",
    ".xlsx",
    ".xls",
    ".docx",
    ".doc",
    ".pptx",
    ".key",
    ".numbers",
    ".psd",
    ".ai",
    ".sketch",
}
GENERATED_SUFFIXES = (
    ".min.js",
    ".min.css",
    ".map",
    "_pb2.py",
    "_pb2_grpc.py",
    ".pb.go",
    ".generated.ts",
)
SKIP_DIRS = {
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    "staticfiles",
}
SNAPSHOT_MAX_BYTES = 2 * 1024 * 1024  # a single file larger than this is data, not code
SMALL_FILE_LINES = 30


class TargetError(Exception):
    """The target could not be resolved. The message says why."""


class MissingToolError(Exception):
    """A command-line tool the run cannot do without is not on PATH."""


RG_MISSING = (
    "ripgrep (`rg`) is not on PATH. The walkthrough uses it to find references in every repository "
    "and to check that written text occurs in the code. Install it (macOS: `brew install ripgrep`; "
    "Debian/Ubuntu: `apt install ripgrep`; see https://github.com/BurntSushi/ripgrep#installation) "
    "and run again."
)


def require_rg() -> None:
    if not shutil.which("rg"):
        raise MissingToolError(RG_MISSING)


def git(
    repo: str | Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess:
    """Run one git command against *repo*; stdout/stderr as text."""
    if args and args[0] not in (
        "rev-parse",
        "show",
        "archive",
        "ls-files",
        "ls-tree",
        "diff",
        "merge-base",
        "log",
        "grep",
    ):
        raise TargetError(f"git {args[0]} is not allowed here")
    proc = subprocess.run(
        [*GIT_ARGV, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=git_env(),
        shell=False,
        check=False,
    )
    if check and proc.returncode != 0:
        raise TargetError(
            f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}"
        )
    return proc


def repo_root(path: str | Path) -> Path | None:
    proc = git(path, "rev-parse", "--show-toplevel", check=False)
    return (
        Path(proc.stdout.strip()).resolve()
        if proc.returncode == 0 and proc.stdout.strip()
        else None
    )


def head_sha(repo: str | Path) -> str | None:
    proc = git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False)
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def tree_files(repo: str | Path, sha: str, subpath: str) -> list[str]:
    """Every path git lists at *sha* under *subpath*, repo-relative.

    The same `git ls-tree -r --name-only` that `review-viewer serve --tree` runs,
    so this is the list of files the viewer counts as changed in a tree target.
    """
    spec = ["--", subpath] if subpath else []
    out = git(repo, "ls-tree", "-r", "-z", "--name-only", sha, *spec).stdout
    return [f for f in out.split("\0") if f]


def remote_url(repo: str | Path) -> str | None:
    # `git remote` is not on the allowed list; rev-parse cannot give the URL,
    # so read it from the repo's config file directly.
    cfg = Path(repo) / ".git" / "config"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', text, re.DOTALL)
    return m.group(1) if m else None


def github_web_base(url: str | None) -> str | None:
    """`https://github.com/<owner>/<repo>` for a GitHub remote, else None."""
    if not url:
        return None
    m = re.match(
        r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"([^/]+)/([^/]+?)(?:\.git)?/?$",
        url,
    )
    return f"https://github.com/{m.group(1)}/{m.group(2)}" if m else None


def sanitize_slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
    return slug or "target"


# --- snapshots -----------------------------------------------------------------


def is_secret_path(path: str) -> bool:
    """`.env`, `.env.prod`, `x/.env.local`: never read, never snapshotted."""
    return PurePosixPath(path).name.startswith(".env")


def _keep_member(member: tarfile.TarInfo) -> bool:
    if not (member.isfile() or member.isdir()):
        return False
    if member.isdir():
        return True
    p = PurePosixPath(member.name)
    if is_secret_path(member.name):
        return False
    if any(part in SKIP_DIRS for part in p.parts[:-1]):
        return False
    if p.suffix.lower() in BINARY_EXTS:
        return False
    return member.size <= SNAPSHOT_MAX_BYTES


def snapshot_repo(repo: RepoInfo, dest: Path) -> Path:
    """Extract the tree at repo.head_sha into *dest*, leaving out secrets, binaries and big files.

    The agents' Read/Grep/Glob tools see a directory, not a commit; the gates
    check quotes with `git show <sha>:<path>`. `git archive` reads the object
    store and writes nothing to the repo.
    """
    marker = dest / ".walkthrough-head"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == repo.head_sha:
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    proc = subprocess.run(
        [*GIT_ARGV, "-C", repo.path, "archive", "--format=tar", repo.head_sha],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=git_env(),
        shell=False,
        check=False,
    )
    if proc.returncode != 0:
        raise TargetError(
            f"git archive {repo.head_sha[:12]} failed in {repo.path}: "
            f"{proc.stderr.decode(errors='replace').strip()}"
        )
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tar:
        members = [m for m in tar.getmembers() if _keep_member(m)]
        tar.extractall(dest, members=members, filter="data")
    marker.write_text(repo.head_sha + "\n", encoding="utf-8")
    return dest


# --- the target ------------------------------------------------------------------


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return b"\0" in f.read(8192)
    except OSError:
        return True


def _python_package(repo: Path, snapshot: Path, subpath: str) -> str:
    """Dotted import path of *subpath*: climb while the parent is still a package."""
    parts = list(PurePosixPath(subpath).parts) if subpath else []
    if not parts:
        return repo.name
    start = len(parts) - 1
    while (
        start > 0
        and (snapshot / PurePosixPath(*parts[:start]) / "__init__.py").exists()
    ):
        start -= 1
    return ".".join(parts[start:])


def _existing_ancestor(p: Path) -> Path:
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def resolve_target(
    path: str | Path, commit: str | None = None
) -> tuple[Path, str, str]:
    """(repo root, repo-relative subpath, sha). Raises TargetError.

    The sha is HEAD's, or *commit*'s when given. With *commit* the directory has to
    exist at that commit, not in the working tree.
    """
    p = Path(path).expanduser().resolve()
    if commit is None:
        if not p.exists():
            raise TargetError(f"{p} does not exist")
        if not p.is_dir():
            raise TargetError(f"{p} is not a directory")
    elif p.exists() and not p.is_dir():
        raise TargetError(f"{p} is not a directory")
    root = repo_root(_existing_ancestor(p))
    if root is None:
        raise TargetError(f"{p} is not inside a git repository")
    if commit is None:
        sha = head_sha(root)
        if sha is None:
            raise TargetError(f"HEAD does not resolve to a commit in {root}")
    else:
        sha = git_targets.rev_parse(root, commit)
        if sha is None:
            raise TargetError(f"{commit} does not resolve to a commit in {root}")
    sub = p.relative_to(root).as_posix()
    sub = "" if sub == "." else sub
    if commit is not None and sub:
        out = git(root, "ls-tree", "-d", "--name-only", sha, "--", sub).stdout
        if sub not in out.splitlines():
            raise TargetError(f"{sub} is not a directory in {sha[:12]}")
    return root, sub, sha


def list_target_files(
    snapshot: Path, subpath: str, *, depth: str = "standard"
) -> tuple[list[str], dict[str, int], dict[str, str], int, str]:
    """(files, line counts, skipped, migration count, latest migration), repo-relative, sorted."""
    base = snapshot / subpath if subpath else snapshot
    files: list[str] = []
    lines: dict[str, int] = {}
    skipped: dict[str, str] = {}
    migrations: list[str] = []
    if not base.is_dir():
        return files, lines, skipped, 0, ""
    for f in sorted(base.rglob("*")):
        if not f.is_file() or f.name == ".walkthrough-head":
            continue
        rel = f.relative_to(snapshot).as_posix()
        parts = PurePosixPath(rel).parts
        if any(part in SKIP_DIRS for part in parts[:-1]):
            continue
        if "migrations" in parts[:-1]:
            if f.suffix == ".py" and f.name != "__init__.py":
                migrations.append(rel)
            continue
        if f.name in LOCKFILES:
            skipped[rel] = "lockfile"
            continue
        if f.name.endswith(GENERATED_SUFFIXES):
            skipped[rel] = "generated"
            continue
        if _is_binary(f):
            skipped[rel] = "binary"
            continue
        if depth == "overview" and (
            "tests" in parts[:-1] or f.name.startswith("test_") or f.name == "tests.py"
        ):
            skipped[rel] = "test file (depth overview)"
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        files.append(rel)
        lines[rel] = len(text.split("\n")) - (1 if text.endswith("\n") else 0)
    latest = (
        sorted(migrations, key=lambda m: PurePosixPath(m).name)[-1]
        if migrations
        else ""
    )
    return files, lines, skipped, len(migrations), latest


def build_target(
    path: str | Path,
    run_dir: Path,
    *,
    name: str | None = None,
    depth: str = "standard",
    key: str | None = None,
    commit: str | None = None,
) -> tuple[TargetInfo, RepoInfo]:
    """Resolve, snapshot and list the target. Raises TargetError. *key* names the
    target's repository (default: its directory's name); *commit* the commit read
    (default: HEAD)."""
    root, sub, sha = resolve_target(path, commit)
    repo = RepoInfo(
        key=key or root.name, path=str(root), head_sha=sha, remote_url=remote_url(root)
    )
    snap = snapshot_repo(repo, run_dir / "snap" / repo.key)
    files, lines, skipped, n_mig, latest = list_target_files(snap, sub, depth=depth)
    langs: dict[str, int] = {}
    for f in files:
        ext = PurePosixPath(f).suffix.lower() or PurePosixPath(f).name
        langs[ext] = langs.get(ext, 0) + 1
    module = PurePosixPath(sub).name if sub else root.name
    info = TargetInfo(
        repo_key=repo.key,
        repo_path=str(root),
        subpath=sub,
        head_sha=sha,
        name=name or module.lower(),
        package=_python_package(root, snap, sub),
        files=files,
        file_lines=lines,
        skipped=skipped,
        migrations=n_mig,
        latest_migration=latest,
        languages=langs,
    )
    return info, repo


# --- a change: a PR or a commit range ----------------------------------------------------------

# A change's files at the base side that are snapshotted: every one whose old lines the change touches.
BASE_STATUSES = ("M", "D", "R", "C", "T")


@dataclass
class ResolvedChange:
    """A change resolved to two commits, before anything is written."""

    root: Path
    kind: str  # "pr" or "range"
    ref: str
    head: str
    base: str
    pr: int | None = None
    title: str = ""
    body: str = ""
    base_ref: str = ""
    head_ref: str = ""

    def slug_part(self) -> str:
        return (
            f"pr-{self.pr}--{self.head[:12]}"
            if self.kind == "pr"
            else f"range-{self.base[:12]}-{self.head[:12]}"
        )


def resolve_change(
    repo: str | Path,
    *,
    pr: int | None = None,
    range_: str | None = None,
    fetch: bool = False,
    expect_head: str | None = None,
) -> ResolvedChange:
    """Turn `--pr N` or `--range A..B` into (head, merge-base). Writes nothing unless *fetch*. Raises TargetError."""
    p = Path(repo).expanduser().resolve()
    root = repo_root(p) if p.is_dir() else None
    if root is None:
        raise TargetError(f"{p} is not inside a git repository")
    if (pr is None) == (not range_):
        raise TargetError("pass exactly one of --pr and --range")
    try:
        if fetch:
            git_targets.fetch_origin(root)
        if pr is not None:
            info = git_targets.pr_view(root, int(pr))
            head = git_targets.rev_parse(root, info.get("headRefOid") or "")
            if head is None:
                raise TargetError(
                    f"PR #{pr} head {str(info.get('headRefOid'))[:12]} is not in this clone (pass --fetch)"
                )
            tip = git_targets.rev_parse(root, info.get("baseRefOid") or "")
            base = git_targets.merge_base(root, tip, head) if tip else None
            if base is None:
                raise TargetError(
                    f"PR #{pr} base {str(info.get('baseRefOid'))[:12]} is not in this clone (pass --fetch)"
                )
            found = ResolvedChange(
                root,
                "pr",
                str(pr),
                head,
                base,
                pr=int(pr),
                title=(info.get("title") or "").strip(),
                body=(info.get("body") or "").strip(),
                base_ref=info.get("baseRefName") or "",
                head_ref=info.get("headRefName") or "",
            )
        else:
            text = range_ or ""
            if text.count("..") != 1 or "..." in text:
                raise TargetError(f"--range must be <base>..<head>, got {text!r}")
            a, b = text.split("..")
            tip, head = git_targets.rev_parse(root, a), git_targets.rev_parse(root, b)
            if tip is None or head is None:
                raise TargetError(
                    f"{a if tip is None else b} does not resolve to a commit in {root}"
                )
            base = git_targets.merge_base(root, tip, head)
            if base is None:
                raise TargetError(f"{a} and {b} have no merge-base")
            found = ResolvedChange(
                root, "range", text, head, base, base_ref=a, head_ref=b
            )
    except git_targets.GitTargetError as e:
        raise TargetError(str(e)) from e
    if expect_head and found.head != git_targets.rev_parse(root, expect_head):
        raise TargetError(
            f"the change's head is {found.head[:12]}, not {expect_head[:12]} (--expect-head)"
        )
    return found


def snapshot_paths(repo: RepoInfo, dest: Path, paths: list[str]) -> Path:
    """Extract only *paths* at repo.head_sha into *dest*, with the same filters as `snapshot_repo`."""
    marker = dest / ".walkthrough-head"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == repo.head_sha:
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for batch in git_targets.archive_batches(paths):
        try:
            proc = subprocess.run(
                [
                    *GIT_ARGV,
                    "-C",
                    repo.path,
                    "archive",
                    "--format=tar",
                    repo.head_sha,
                    "--",
                    *batch,
                ],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                env=git_env(),
                shell=False,
                check=False,
            )
            if proc.returncode != 0:
                raise TargetError(
                    f"git archive {repo.head_sha[:12]} failed in {repo.path}: "
                    f"{proc.stderr.decode(errors='replace').strip()}"
                )
            with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tar:
                members = [m for m in tar.getmembers() if _keep_member(m)]
                tar.extractall(dest, members=members, filter="data")
        except OSError as e:
            raise TargetError(f"base snapshot in {dest}: {e}") from e
    marker.write_text(repo.head_sha + "\n", encoding="utf-8")
    return dest


def _change_files(
    snap: Path, changed: list[dict], depth: str
) -> tuple[list[str], dict[str, int], dict[str, str]]:
    """The changed files present at head that are explained, their line counts, and the ones left out."""
    files: list[str] = []
    lines: dict[str, int] = {}
    skipped: dict[str, str] = {}
    for f in changed:
        rel = f["path"]
        if f["status"] == "D":
            continue
        p = PurePosixPath(rel)
        disk = snap / rel
        if any(part in SKIP_DIRS for part in p.parts[:-1]):
            skipped[rel] = "dependency or build directory"
        elif p.name in LOCKFILES:
            skipped[rel] = "lockfile"
        elif p.name.endswith(GENERATED_SUFFIXES):
            skipped[rel] = "generated"
        elif not disk.is_file():
            skipped[rel] = "not snapshotted (a secret, binary or file over 2 MB)"
        elif _is_binary(disk):
            skipped[rel] = "binary"
        elif depth == "overview" and (
            "tests" in p.parts[:-1]
            or p.name.startswith("test_")
            or p.name == "tests.py"
        ):
            skipped[rel] = "test file (depth overview)"
        else:
            text = disk.read_text(encoding="utf-8", errors="replace")
            files.append(rel)
            lines[rel] = len(text.split("\n")) - (1 if text.endswith("\n") else 0)
    return sorted(files), lines, skipped


def build_change_target(
    found: ResolvedChange,
    run_dir: Path,
    *,
    name: str | None = None,
    depth: str = "standard",
    key: str | None = None,
) -> tuple[TargetInfo, RepoInfo, RepoInfo]:
    """Snapshot both commits of a resolved change and list its files: (target, head repo, base repo)."""
    root = found.root
    k = key or root.name
    url = remote_url(root)
    repo = RepoInfo(key=k, path=str(root), head_sha=found.head, remote_url=url)
    base = RepoInfo(
        key=f"{k}@base", path=str(root), head_sha=found.base, remote_url=url
    )
    try:
        changed = [
            f.to_dict() for f in git_targets.changed_files(root, found.base, found.head)
        ]
        commits = [
            [sha, subject]
            for sha, subject in git_targets.commit_subjects(
                root, found.base, found.head
            )
        ]
    except git_targets.GitTargetError as e:
        raise TargetError(str(e)) from e
    snap = snapshot_repo(repo, run_dir / "snap" / repo.key)
    snapshot_paths(
        base,
        run_dir / "snap" / base.key,
        sorted(
            {
                f["old_path"] or f["path"]
                for f in changed
                if f["status"] in BASE_STATUSES
            }
        ),
    )
    files, lines, skipped = _change_files(snap, changed, depth)
    langs: dict[str, int] = {}
    for f in files:
        ext = PurePosixPath(f).suffix.lower() or PurePosixPath(f).name
        langs[ext] = langs.get(ext, 0) + 1
    label = (
        f"PR #{found.pr}"
        if found.kind == "pr"
        else f"{found.base[:8]}..{found.head[:8]}"
    )
    default_name = (
        f"pr-{found.pr}"
        if found.kind == "pr"
        else f"range-{found.base[:8]}-{found.head[:8]}"
    )
    info = TargetInfo(
        scope="change",
        change=ChangeInfo(
            kind=found.kind,
            ref=found.ref,
            pr=found.pr,
            title=found.title,
            body=found.body,
            base_ref=found.base_ref,
            head_ref=found.head_ref,
            base_sha=found.base,
            base_key=base.key,
            commits=commits,
            files=changed,
        ),
        repo_key=repo.key,
        repo_path=str(root),
        subpath="",
        head_sha=found.head,
        name=name or default_name,
        package=label,
        files=files,
        file_lines=lines,
        skipped=skipped,
        languages=langs,
    )
    return info, repo, base


def target_gate(info: TargetInfo | None, problem: str = "") -> GateResult:
    """The path is in a git repo, HEAD resolves, and there is at least one file to explain."""
    if problem:
        return GateResult(passed=False, reason=problem)
    if info is None:
        return GateResult(passed=False, reason="no target")
    if not re.fullmatch(r"[0-9a-f]{40}", info.head_sha or ""):
        return GateResult(passed=False, reason="HEAD does not resolve")
    if not info.files:
        return GateResult(
            passed=False,
            reason=(
                "the change touches no file that can be explained"
                if info.scope == "change"
                else f"no files to explain under {info.subpath or '.'} at HEAD"
            ),
        )
    return GateResult(passed=True)
