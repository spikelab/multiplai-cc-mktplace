"""The source of the reviewed repository's git dependencies, inside the snapshot.

A finding often turns on what a dependency does with a value the change
passes it. The agents read the snapshot only, so `materialize` extracts each
git-sourced package that a `uv.lock` at head pins, at that pinned commit, into
`<snapshot>/.review-deps/<name>@<commit[:12]>/`. The gates re-read a
dependency citation from the clone cache's git objects, never from the
extracted files.

Registry, `path` and `editable` sources, and other ecosystems' lock files, are
not read.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from multiplai_core.citations import GIT_ARGV, git_env
from multiplai_core.paths import get_paths
from pydantic import BaseModel, Field

from .config import ReviewConfig
from .models import DEPS_DIR, DepEntry, Need, TargetInfo
from .target import git

log = logging.getLogger(__name__)

LOCK_FILE = "uv.lock"
MANIFEST_FILE = ".manifest.json"
_COMMIT = re.compile(r"[0-9a-f]{40}")

# The URL schemes a dependency may be cloned over; a `git@host:path` URL is ssh.
# Tests add "file" by monkeypatching this set; nothing else enables it.
ALLOWED_SCHEMES: set[str] = {"https", "ssh"}
GIT_TIMEOUT_S = 300
LOCK_WAIT_S = 300
DEFAULT_MAX_MB = 50


@dataclass(frozen=True)
class Dep:
    name: str
    url: str  # without the `?branch=…` query and the `#<commit>` fragment
    commit: str  # 40 hex


def _parse_source(value: str) -> tuple[str, str]:
    """(url, commit) from a uv.lock git source, `<url>[?<query>]#<commit>`."""
    url, _, commit = value.partition("#")
    return url.split("?", 1)[0], commit


def locked_git_deps(target: TargetInfo) -> list[Dep]:
    """One `Dep` per git-sourced package in every uv.lock at head, by (url, commit) once."""
    listing = git(target.repo_path, "ls-tree", "-r", "--name-only", "-z", target.head_sha, check=False)
    paths = [p for p in listing.stdout.split("\0") if p.rsplit("/", 1)[-1] == LOCK_FILE]
    seen: set[tuple[str, str]] = set()
    out: list[Dep] = []
    for path in sorted(paths):
        shown = git(target.repo_path, "show", f"{target.head_sha}:{path}", check=False)
        if shown.returncode != 0:
            continue
        try:
            data = tomllib.loads(shown.stdout)
        except tomllib.TOMLDecodeError as e:
            log.info("%s is not TOML; its dependencies are not read: %s", path, e)
            continue
        for package in data.get("package") or []:
            source = package.get("source") if isinstance(package, dict) else None
            if not isinstance(source, dict) or not isinstance(source.get("git"), str):
                continue
            url, commit = _parse_source(source["git"])
            name = str(package.get("name") or "")
            if not _COMMIT.fullmatch(commit):
                log.info("%s: %s pins %r, not a 40-hex commit; skipped", path, name, commit)
                continue
            if not name or (url, commit) in seen:
                continue
            seen.add((url, commit))
            out.append(Dep(name, url, commit))
    return out


# --- fetch and extract ----------------------------------------------------------


class Manifest(BaseModel):
    """`<snapshot>/.review-deps/.manifest.json`, written after every dependency is done."""
    head_sha: str
    deps: list[DepEntry] = Field(default_factory=list)

    def ok(self) -> list[DepEntry]:
        return [e for e in self.deps if e.status == "ok"]


def cache_root() -> Path:
    """The clone cache: bare partial clones, kept between reviews in the skill's git-ignored state folder."""
    return get_paths().skill_state_dir("review") / "deps"


def url_scheme(url: str) -> str | None:
    """The URL's scheme, "ssh" for `user@host:path`, or None when it is neither form.

    A value with whitespace or a leading `-` has no scheme: git could read it
    as an option or a transport helper.
    """
    if not url or url.startswith("-") or any(c.isspace() for c in url):
        return None
    if m := re.match(r"([A-Za-z][A-Za-z0-9+.-]*)://", url):
        return m.group(1).lower()
    if re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9][A-Za-z0-9.-]*:[^:/][^:]*", url):
        return "ssh"
    return None


def cache_path(url: str) -> Path:
    """`<cache root>/<host>/<path>.git` for *url*, without its credentials, inside the cache root."""
    scheme = url_scheme(url)
    if scheme is not None and "://" in url:
        rest = url.split("://", 1)[1]
    else:
        rest = url.replace(":", "/", 1)
    host, _, path = rest.partition("/")
    host = host.rsplit("@", 1)[-1]  # drop `user:token@`
    parts = [host or "local", *path.split("/")]
    parts = [re.sub(r"[^A-Za-z0-9._-]", "_", p) for p in parts if p not in ("", ".", "..")]
    name = "/".join(parts) or "local"
    return cache_root() / (name if name.endswith(".git") else name + ".git")


def _protocol_argv() -> list[str]:
    argv = ["-c", "protocol.allow=never"]
    for scheme in sorted(ALLOWED_SCHEMES):
        argv += ["-c", f"protocol.{scheme}.allow=always"]
    return argv


def _git(*args: str, lazy_fetch: bool = True) -> subprocess.CompletedProcess | None:
    """One git command, argv only, never prompting, under the timeout; None when it timed out or could not start."""
    env = git_env()
    if not lazy_fetch:
        env["GIT_NO_LAZY_FETCH"] = "1"
    try:
        return subprocess.run([*GIT_ARGV, *_protocol_argv(), *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, env=env, shell=False, check=False,
                              timeout=GIT_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("git %s did not finish: %s", args[2] if args[:1] == ("-C",) else args[0], e)
        return None


def _ok(proc: subprocess.CompletedProcess | None) -> bool:
    return proc is not None and proc.returncode == 0


def _error(proc: subprocess.CompletedProcess | None) -> str:
    if proc is None:
        return f"timed out after {GIT_TIMEOUT_S} s"
    return (proc.stderr or "").strip().splitlines()[-1:][0][:200] if (proc.stderr or "").strip() else \
        f"exit {proc.returncode}"


def _has_commit(entry: Path, commit: str) -> bool:
    """Whether the cache holds *commit*. In a partial clone `cat-file` would otherwise fetch a
    missing object from origin by itself, one object at a time."""
    return _ok(_git("-C", str(entry), "cat-file", "-e", f"{commit}^{{commit}}", lazy_fetch=False))


def _clone(url: str, entry: Path) -> str:
    """Clone *url* bare into *entry*; "" on success, else why (and no half-written entry)."""
    entry.parent.mkdir(parents=True, exist_ok=True)
    proc = _git("clone", "--bare", "--quiet", "--filter=blob:none", "--", url, str(entry))
    if _ok(proc):
        return ""
    shutil.rmtree(entry, ignore_errors=True)
    return f"git clone failed: {_error(proc)}"


def _fetched(entry: Path, commit: str) -> str:
    """Fetch *commit* into *entry*; "" when the cache then holds it, else why."""
    proc = _git("-C", str(entry), "fetch", "--quiet", "origin", commit)
    if _ok(proc) and _has_commit(entry, commit):
        return ""
    return f"git fetch of {commit[:12]} failed: {_error(proc)}"


def _ensure_commit(dep: Dep, entry: Path, recloned: set[Path]) -> str:
    """Make the cache entry hold *dep*'s commit; "" on success, else why.

    An entry that cannot fetch the commit may be a clone or fetch killed part
    way, or corrupt: it is deleted and cloned again, once per run.
    """
    cloned = False
    if not entry.is_dir():
        if why := _clone(dep.url, entry):
            return why
        cloned = True
    if _has_commit(entry, dep.commit):
        return ""
    why = _fetched(entry, dep.commit)
    if not why or cloned or entry in recloned:
        return why
    log.info("cache entry %s cannot fetch %s; cloning it again", entry, dep.commit[:12])
    recloned.add(entry)
    shutil.rmtree(entry, ignore_errors=True)
    if why := _clone(dep.url, entry):
        return why
    return "" if _has_commit(entry, dep.commit) else _fetched(entry, dep.commit)


@contextlib.contextmanager
def _locked(entry: Path) -> Iterator[bool]:
    """Hold `<entry>.lock` exclusively, waiting up to LOCK_WAIT_S; yields False when it never came free."""
    path = entry.with_name(entry.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + LOCK_WAIT_S
    held = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(1.0, left))
        yield held
    finally:
        if held:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _inside_only(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
    """tarfile's "data" filter, but a member it refuses (a link leaving *dest*) is left out, not fatal."""
    try:
        return tarfile.data_filter(member, dest)
    except tarfile.FilterError as e:
        log.info("dependency archive: %s not extracted: %s", member.name, e)
        return None


def _extract(dep: Dep, entry: Path, root: Path, max_mb: int) -> tuple[str, str]:
    """(status, reason) after extracting *dep*'s commit into `<root>/<name>@<commit12>/`."""
    folder = f"{dep.name}@{dep.commit[:12]}"
    tmp = Path(tempfile.mkdtemp(prefix=f".{folder}.partial-", dir=root))
    try:
        archive = tmp.with_suffix(".tar")
        proc = _git("-C", str(entry), "archive", "--format=tar", f"--output={archive}", dep.commit)
        if not _ok(proc):
            return "failed", f"git archive failed: {_error(proc)}"
        size = archive.stat().st_size
        if size > max_mb * 1024 * 1024:
            return "skipped", f"its source is {size / 1024 / 1024:.0f} MB, over the {max_mb} MB limit (deps_max_mb)"
        try:
            with tarfile.open(archive, mode="r:") as tar:
                tar.extractall(tmp, filter=_inside_only)
        except (tarfile.TarError, OSError) as e:
            return "failed", f"extracting failed: {e}"
        dest = root / folder
        if dest.exists():
            shutil.rmtree(dest)
        tmp.rename(dest)
        return "ok", ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.with_suffix(".tar").unlink(missing_ok=True)


def _materialize_one(dep: Dep, root: Path, max_mb: int, recloned: set[Path]) -> DepEntry:
    def entry(status: str, reason: str = "", cache: str = "") -> DepEntry:
        return DepEntry(name=dep.name, url=dep.url, commit=dep.commit, cache=cache, status=status, reason=reason)

    scheme = url_scheme(dep.url)
    if scheme not in ALLOWED_SCHEMES:
        return entry("failed", f"its URL scheme ({scheme or 'unknown'}) is not allowed; "
                               f"only {', '.join(sorted(ALLOWED_SCHEMES))} URLs are cloned")
    cache = cache_path(dep.url)
    with _locked(cache) as held:
        if not held:
            return entry("skipped", f"another process held the clone cache lock for {LOCK_WAIT_S} s",
                         str(cache))
        if why := _ensure_commit(dep, cache, recloned):
            return entry("failed", why, str(cache))
        status, reason = _extract(dep, cache, root, max_mb)
        return entry(status, reason, str(cache))


def write_manifest(root: Path, manifest: Manifest) -> None:
    tmp = root / (MANIFEST_FILE + ".tmp")
    tmp.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(root / MANIFEST_FILE)


def materialize(deps: list[Dep], snapshot: Path, *, head_sha: str, max_mb: int = DEFAULT_MAX_MB) -> Manifest:
    """Extract each of *deps* at its commit into `<snapshot>/.review-deps/`, then write the manifest.

    A dependency that cannot be fetched or is too large is recorded as
    `failed` or `skipped`; the others still extract.
    """
    root = snapshot / DEPS_DIR
    root.mkdir(parents=True, exist_ok=True)
    recloned: set[Path] = set()
    manifest = Manifest(head_sha=head_sha, deps=[_materialize_one(d, root, max_mb, recloned) for d in deps])
    write_manifest(root, manifest)
    return manifest


def needs(manifest: Manifest) -> list[Need]:
    """One `Need` per dependency whose source is not in the snapshot."""
    out = []
    for e in manifest.deps:
        if e.status == "ok":
            continue
        allowed = url_scheme(e.url) in ALLOWED_SCHEMES
        out.append(Need(
            what=f"The source of {e.folder}, which uv.lock pins, so the review could follow calls into it: "
                 f"{e.reason}. The review went on without it",
            blocks="review", cause="unreachable" if e.reason.startswith("git ") else "lookup-failed",
            # need_gate allows read-only commands only; a clone goes in `where`.
            command=f"git ls-remote {e.url}" if allowed else "",
            where=f"a clone of {e.url} checked out at {e.commit}" if allowed else "",
            source="pipeline"))
    return out


def read_manifest(root: Path) -> Manifest | None:
    try:
        return Manifest.model_validate_json((root / MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ensure(target: TargetInfo, snapshot: Path, config: ReviewConfig) -> Manifest:
    """The snapshot's dependency source for *target*, extracted unless it is already there.

    The saved manifest is reused when it names the same head and the same
    dependencies and every `ok` folder still exists. Otherwise `.review-deps/`
    is removed, partial folders included, and every dependency is extracted
    again from the clone cache. With `deps` off it does nothing.
    """
    if not config.deps:
        return Manifest(head_sha=target.head_sha)
    wanted = locked_git_deps(target)
    root = snapshot / DEPS_DIR
    saved = read_manifest(root)
    if (saved is not None and saved.head_sha == target.head_sha
            and [(e.name, e.url, e.commit) for e in saved.deps] == [(d.name, d.url, d.commit) for d in wanted]
            and all((root / e.folder).is_dir() for e in saved.ok())):
        return saved
    if root.exists():
        shutil.rmtree(root)
    if not wanted:
        return Manifest(head_sha=target.head_sha)
    return materialize(wanted, snapshot, head_sha=target.head_sha, max_mb=config.deps_max_mb)
