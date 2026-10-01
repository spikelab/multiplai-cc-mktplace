"""Resolve the module to explain, and take snapshots of commits.

Read-only over every repository. The only git commands run anywhere in the
pipeline are `rev-parse`, `show`, `archive` and `ls-files`, each a fixed argv
with `shell=False`. Nothing checks out, fetches or writes to a repo.

The working tree of the target is never read: its files are listed from a
`git archive` of HEAD, and agents read that snapshot.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

from .models import GateResult, RepoInfo, TargetInfo

log = logging.getLogger(__name__)

# User or repo config must not change what we parse.
_GIT = ["git", "-c", "color.ui=never", "-c", "core.quotepath=off"]

LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock",
             "Pipfile.lock", "Cargo.lock", "composer.lock", "Gemfile.lock", "go.sum"}
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff", ".svgz", ".heic",
    ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".tar", ".jar", ".whl",
    ".mp3", ".mp4", ".mov", ".m4a", ".wav", ".avi", ".webm", ".ogg",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a",
    ".sqlite", ".sqlite3", ".db", ".pkl", ".pickle", ".npy", ".npz", ".parquet",
    ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".key", ".numbers", ".psd", ".ai", ".sketch",
}
GENERATED_SUFFIXES = (".min.js", ".min.css", ".map", "_pb2.py", "_pb2_grpc.py", ".pb.go", ".generated.ts")
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".mypy_cache",
             ".pytest_cache", ".tox", "staticfiles"}
SNAPSHOT_MAX_BYTES = 2 * 1024 * 1024   # a single file larger than this is data, not code
SMALL_FILE_LINES = 30


class TargetError(Exception):
    """The target could not be resolved. The message says why."""


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("GIT_EXTERNAL_DIFF", None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def git(repo: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run one git command against *repo*; stdout/stderr as text."""
    if args and args[0] not in ("rev-parse", "show", "archive", "ls-files"):
        raise TargetError(f"git {args[0]} is not allowed here")
    proc = subprocess.run(
        [*_GIT, "-C", str(repo), *args],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, env=_env(), shell=False, check=False,
    )
    if check and proc.returncode != 0:
        raise TargetError(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc


def repo_root(path: str | Path) -> Path | None:
    proc = git(path, "rev-parse", "--show-toplevel", check=False)
    return Path(proc.stdout.strip()).resolve() if proc.returncode == 0 and proc.stdout.strip() else None


def head_sha(repo: str | Path) -> str | None:
    proc = git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False)
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def remote_url(repo: str | Path) -> str | None:
    # `git remote` is not on the allowed list; rev-parse cannot give the URL,
    # so read it from the repo's config file directly.
    cfg = Path(repo) / ".git" / "config"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', text, re.S)
    return m.group(1) if m else None


def github_web_base(url: str | None) -> str | None:
    """`https://github.com/<owner>/<repo>` for a GitHub remote, else None."""
    if not url:
        return None
    m = re.match(r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
                 r"([^/]+)/([^/]+?)(?:\.git)?/?$", url)
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
        [*_GIT, "-C", repo.path, "archive", "--format=tar", repo.head_sha],
        capture_output=True, stdin=subprocess.DEVNULL, env=_env(), shell=False, check=False,
    )
    if proc.returncode != 0:
        raise TargetError(f"git archive {repo.head_sha[:12]} failed in {repo.path}: "
                          f"{proc.stderr.decode(errors='replace').strip()}")
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
    while start > 0 and (snapshot / PurePosixPath(*parts[:start]) / "__init__.py").exists():
        start -= 1
    return ".".join(parts[start:])


def resolve_target(path: str | Path) -> tuple[Path, str, str]:
    """(repo root, repo-relative subpath, HEAD sha). Raises TargetError."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise TargetError(f"{p} does not exist")
    if not p.is_dir():
        raise TargetError(f"{p} is not a directory")
    root = repo_root(p)
    if root is None:
        raise TargetError(f"{p} is not inside a git repository")
    sha = head_sha(root)
    if sha is None:
        raise TargetError(f"HEAD does not resolve to a commit in {root}")
    sub = p.relative_to(root).as_posix()
    return root, ("" if sub == "." else sub), sha


def list_target_files(snapshot: Path, subpath: str, *, depth: str = "standard") -> tuple[
        list[str], dict[str, int], dict[str, str], int, str]:
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
        if depth == "overview" and ("tests" in parts[:-1] or f.name.startswith("test_") or f.name == "tests.py"):
            skipped[rel] = "test file (depth overview)"
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        files.append(rel)
        lines[rel] = len(text.split("\n")) - (1 if text.endswith("\n") else 0)
    latest = sorted(migrations, key=lambda m: PurePosixPath(m).name)[-1] if migrations else ""
    return files, lines, skipped, len(migrations), latest


def build_target(path: str | Path, run_dir: Path, *, name: str | None = None,
                 depth: str = "standard") -> tuple[TargetInfo, RepoInfo]:
    """Resolve, snapshot and list the target. Raises TargetError."""
    root, sub, sha = resolve_target(path)
    repo = RepoInfo(key=root.name, path=str(root), head_sha=sha, remote_url=remote_url(root))
    snap = snapshot_repo(repo, run_dir / "snap" / repo.key)
    files, lines, skipped, n_mig, latest = list_target_files(snap, sub, depth=depth)
    langs: dict[str, int] = {}
    for f in files:
        ext = PurePosixPath(f).suffix.lower() or PurePosixPath(f).name
        langs[ext] = langs.get(ext, 0) + 1
    module = PurePosixPath(sub).name if sub else root.name
    info = TargetInfo(
        repo_key=repo.key, repo_path=str(root), subpath=sub, head_sha=sha,
        name=name or module.lower(), package=_python_package(root, snap, sub),
        files=files, file_lines=lines, skipped=skipped, migrations=n_mig,
        latest_migration=latest, languages=langs,
    )
    return info, repo


def target_gate(info: TargetInfo | None, problem: str = "") -> GateResult:
    """The path is in a git repo, HEAD resolves, and there is at least one file to explain."""
    if problem:
        return GateResult(passed=False, reason=problem)
    if info is None:
        return GateResult(passed=False, reason="no target")
    if not re.fullmatch(r"[0-9a-f]{40}", info.head_sha or ""):
        return GateResult(passed=False, reason="HEAD does not resolve")
    if not info.files:
        return GateResult(passed=False, reason=f"no files to explain under {info.subpath or '.'} at HEAD")
    return GateResult(passed=True)
