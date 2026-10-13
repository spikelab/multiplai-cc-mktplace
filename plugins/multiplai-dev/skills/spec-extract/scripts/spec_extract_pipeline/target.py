"""What spec-extract reads, and the run folder it writes.

A target is a commit (`--tree <sha> [--path <dir>]`), a PR's head (`--pr <n>`),
or a directory that is not under git (`--dir`), which is copied into
`<run>/source/` and committed there. The run folder is `<root>/<repo>/<object>/`,
named by the rule review gave its folders, plus the commit (plan, Decided 14).
Review's rule is restated here, not imported: skills do not import each other
in an installed plugin. Each restated piece names review's function.

Agents read a snapshot (`<run>/snapshot/`, the commit's blobs written out as git
stores them); gates read git (plan, Decided 15).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from multiplai_core.citations import GIT_ARGV, git_env
from pydantic import BaseModel, ConfigDict

from . import runlock

# Repository names the root itself could use (review's `TAKEN_REPO_NAMES`): a
# repository with one of them is renamed `<name>@<owner>`, or `<name>@local`.
TAKEN_REPO_NAMES = frozenset({"legacy", ".locks"})
# Directories `--dir` does not copy (review's `IMPORT_EXCLUDES`).
IMPORT_EXCLUDES = ("node_modules", ".venv", "venv", "__pycache__", "dist", "build")
SNAPSHOT_DIR = "snapshot"
SNAPSHOT_MARKER = ".spec-extract-head"
SOURCE_DIR = "source"
STATE_FILE = "state.json"


class TargetError(Exception):
    """The target or its folder was refused (exit 2). The message says why."""


class ImportLost(Exception):
    """A resumed `--dir` run's `source/` no longer holds the import its state names (exit 1,
    row R27). The message names `--restart`."""

    def __init__(self, dest: Path, head: str, src: str):
        super().__init__(f"{dest} no longer holds the import commit {head[:12]} this run's "
                         "state.json names; nothing was written. Run the same command with "
                         "--restart to move this run folder aside (it is renamed, never "
                         f"deleted) and import {src} again")


class Target(BaseModel):
    # As State: a field a newer version saved is refused (exit 1, R23), not dropped.
    model_config = ConfigDict(extra="forbid")

    kind: Literal["tree", "pr", "dir"]
    repo: str  # the top of the checkout; for --dir, <run>/source once imported
    head_sha: str | None = None  # None only for a --dir target before its import
    base_sha: str | None = None  # --pr only: the merge-base of the base tip and the head
    path: str = ""  # --tree --path: repo-relative, normalised; "" for the whole tree
    pr: int | None = None
    remote_url: str | None = None
    source: str | None = None  # --dir: the source's absolute path


# --- git ----------------------------------------------------------------------------

def git(repo: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run([*GIT_ARGV, "-C", str(repo), *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, env=git_env(), shell=False, check=False)
    if check and proc.returncode != 0:
        raise TargetError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def rev_parse(repo: str | Path, ref: str) -> str | None:
    """Full 40-hex sha for *ref*, or None when it does not resolve to a commit."""
    proc = git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def remote_url(repo: str | Path) -> str | None:
    proc = git(repo, "remote", "get-url", "origin", check=False)
    url = proc.stdout.strip()
    return url if proc.returncode == 0 and url else None


def _toplevel(repo: Path) -> Path | None:
    proc = git(repo, "rev-parse", "--show-toplevel", check=False)
    top = proc.stdout.strip() if proc.returncode == 0 else ""
    return Path(top).resolve() if top else None


# --- names (review's target.py) ----------------------------------------------------------

def sanitize_slug(text: str) -> str:
    """review's `sanitize_slug`: runs outside `[A-Za-z0-9._-]` become `_`, `_` stripped at
    both ends, empty becomes `target`. Never yields `@`."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
    return slug or "target"


def origin_owner_repo(url: str | None) -> tuple[str, str] | None:
    """review's `origin_owner_repo`: `(owner, repo)`, the last two path parts of a remote
    URL with `.git` dropped, else None."""
    if not url:
        return None
    path = url.strip().rstrip("/")
    if path.endswith(".git"):
        path = path[:-len(".git")]
    parts = [p for p in re.split(r"[/:]", path) if p]
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


def repo_part(name: str, owner: str | None) -> str:
    """review's `repo_part`: *name* cleaned; `@<owner>` (or `@local`) added when it is
    `legacy` or `.locks`."""
    part = sanitize_slug(name)
    return f"{part}@{owner or 'local'}" if part in TAKEN_REPO_NAMES else part


def checkout_repo_part(repo: Path, url: str | None) -> tuple[str, str | None]:
    """review's `checkout_repo_part`: `(repo folder name, owner)` from *url* (`origin`);
    with none, or one that names no owner, the directory name and None."""
    pair = origin_owner_repo(url)
    if pair:
        return repo_part(pair[1], pair[0]), pair[0]
    return repo_part(Path(repo).name, None), None


def normalise_path(path: str | None) -> str:
    """review's `normalise_path`: no `./`, no trailing `/`; "" for the root."""
    p = (path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.strip("/")
    return "" if p in ("", ".") else p


def tree_root(repo: Path, path: str | None) -> tuple[Path, str]:
    """review's `tree_root`: (the top of *repo*'s working tree, *path* relative to it).

    A *repo* below the top puts its own prefix in front of *path*. An absolute
    *path* is first made relative to *repo*.
    """
    top = _toplevel(repo) or repo
    if path and Path(path).is_absolute():
        path = os.path.relpath(Path(path).resolve(), repo)
    prefix = repo.relative_to(top).as_posix() if top != repo and top in repo.parents else ""
    joined = normalise_path("/".join(p for p in (prefix, normalise_path(path)) if p))
    sub = normalise_path(posixpath.normpath(joined)) if joined else ""
    if sub == ".." or sub.startswith("../"):
        raise TargetError(f"--path {path} is outside the repository {top}")
    return top, sub


def _hex6(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:6]


def dir_object(src: Path) -> tuple[str, str]:
    """review's `dir_object` and `dir_name`: `(<basename>, dir-<basename>-<6 hex of the
    absolute path>)`."""
    return repo_part(src.name, None), "dir-" + sanitize_slug(f"{src.name}-{_hex6(str(src))}")


def object_name(target: Target) -> str:
    """`tree@<sha12>`, `tree-<clean path>@<sha12>` or `pr-<n>@<head12>` (review's
    `object_name` plus the commit, so a moved head gets a new folder, R15)."""
    sha12 = (target.head_sha or "")[:12]
    if target.kind == "pr":
        return f"{sanitize_slug(f'pr-{target.pr}')}@{sha12}"
    if target.path:
        return f"{sanitize_slug(f'tree-{target.path}')}@{sha12}"
    return f"tree@{sha12}"


def hashed_object_name(target: Target) -> str:
    """The `--tree --path` name when another path holds the plain one:
    `tree-<clean path>-<6 hex of the normalised path>@<sha12>`."""
    return f"{sanitize_slug(f'tree-{target.path}')}-{_hex6(target.path)}@{(target.head_sha or '')[:12]}"


# --- resolve ------------------------------------------------------------------------

PR_VIEW_FIELDS = "headRefOid,baseRefOid,headRefName,baseRefName,url"


def github_owner_repo(url: str | None) -> tuple[str, str] | None:
    """`(owner, repo)` for a github.com remote URL (review's `github_web_base` forms), else None."""
    m = re.match(r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
                 r"([^/]+)/([^/]+?)(?:\.git)?/?$", url or "")
    return (m.group(1), m.group(2)) if m else None


def _pr_view(repo: Path, number: int) -> dict:
    """`gh pr view <n>`'s head and base commits, from origin's repository. The seam tests replace.

    The fetch, the refusal hints and the run folder all name `origin`, so gh is
    told origin's repository when origin is on github.com (in a fork clone gh
    would otherwise prefer `upstream`). For another host gh chooses, and a PR of
    another repository than origin's is refused.
    """
    gh = shutil.which("gh")
    if gh is None:
        raise TargetError("--pr needs the GitHub CLI (`gh`), which is not installed. Install it "
                          "from https://cli.github.com and run `gh auth login`, or pass "
                          "--tree <head sha>.")
    origin = remote_url(repo)
    argv = [gh, "pr", "view", str(number), "--json", PR_VIEW_FIELDS]
    on_github = github_owner_repo(origin)
    if on_github:
        argv += ["--repo", "/".join(on_github)]
    proc = subprocess.run(argv, cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          shell=False, check=False)
    if proc.returncode != 0:
        raise TargetError(f"gh pr view {number} failed: {proc.stderr.strip()}")
    try:
        info = json.loads(proc.stdout)
    except ValueError as e:
        raise TargetError(f"gh pr view {number} returned no JSON: {e}") from e
    if not isinstance(info, dict):
        raise TargetError(f"gh pr view {number} returned no JSON object")
    here = origin_owner_repo(origin)
    m = re.match(r"^https?://[^/]+/([^/]+)/([^/]+)/pull/\d+/?$", str(info.get("url") or ""))
    if here and m and not _same_remote(f"{m.group(1)}/{m.group(2)}", here):
        raise TargetError(f"gh pr view {number} read PR #{number} of {m.group(1)}/{m.group(2)}, but "
                          f"origin is {here[0]}/{here[1]}; run spec-extract in a clone whose origin "
                          f"is {m.group(1)}/{m.group(2)}")
    return info


def resolve(repo: str | Path, *, tree: str | None = None, path: str | None = None,
            pr: int | None = None, dir: str | Path | None = None, fetch: bool = False) -> Target:
    """The target of exactly one of *tree*, *pr*, *dir*.

    For `--dir`, `repo` and `head_sha` are filled in by `import_dir` once the run
    folder is chosen.
    """
    chosen = [x for x in (tree, pr, dir) if x not in (None, "")]
    if len(chosen) != 1:
        raise TargetError("pass exactly one of --tree, --pr, --dir")
    if path and not tree:
        raise TargetError("--path goes with --tree")

    if dir:
        src = Path(dir).expanduser().resolve()
        if not src.is_dir():
            raise TargetError(f"{src} is not a directory")
        top = _toplevel(src)
        if top is not None:
            rel = src.relative_to(top).as_posix() if src != top else "."
            raise TargetError(f"{src} is inside the git repository {top}; read it with "
                              f"--repo {top} --tree --path {rel}")
        return Target(kind="dir", repo="", source=str(src))

    start = Path(repo).expanduser().resolve()
    top = _toplevel(start)
    if top is None:
        raise TargetError(f"{start} is not a git repository")

    if tree:
        top, sub = tree_root(start, path)
        head = rev_parse(top, tree)
        if head is None:
            raise TargetError(f"{tree} does not resolve to a commit in {top}")
        return Target(kind="tree", repo=str(top), head_sha=head, path=sub, remote_url=remote_url(top))

    number = int(pr)
    if fetch:
        git(top, "fetch", "--quiet", "origin")
        git(top, "fetch", "--quiet", "origin", f"pull/{number}/head", check=False)
    info = _pr_view(top, number)
    head_oid, base_oid = str(info.get("headRefOid") or ""), str(info.get("baseRefOid") or "")
    head = rev_parse(top, head_oid) if head_oid else None
    if head is None:
        raise TargetError(f"PR #{number} head {head_oid[:12]} is not in this clone: run "
                          f"`git -C {top} fetch origin pull/{number}/head`, or pass --fetch")
    base_tip = rev_parse(top, base_oid) if base_oid else None
    if base_tip is None:
        raise TargetError(f"PR #{number} base {base_oid[:12]} is not in this clone: run "
                          f"`git -C {top} fetch origin`, or pass --fetch")
    proc = git(top, "merge-base", base_tip, head, check=False)
    base = proc.stdout.strip()
    if proc.returncode != 0 or not base:
        raise TargetError(f"PR #{number}: no merge-base between {base_tip[:12]} and {head[:12]}")
    return Target(kind="pr", repo=str(top), head_sha=head, base_sha=base, pr=number,
                  remote_url=remote_url(top))


# --- the run folder (plan, Decided 14) ------------------------------------------------

def folder_target(folder: Path) -> dict | None:
    """The `target` object of *folder*'s `state.json`, read without validating: None when
    there is no `state.json`, {} when it cannot be read (review's `rounds.folder_target`)."""
    path = folder / STATE_FILE
    if not path.exists() and not path.is_symlink():
        return None
    try:
        t = json.loads(path.read_text(encoding="utf-8")).get("target")
    except (OSError, ValueError, AttributeError):
        return {}
    return t if isinstance(t, dict) else {}


def _same_remote(url: str | None, here: tuple[str, str] | None) -> bool:
    """review's `_same_remote`: *url* names the repository *here* is, compared without case."""
    pair = origin_owner_repo(url)
    return (tuple(x.lower() for x in pair) if pair else None) == (
        tuple(x.lower() for x in here) if here else None)


def _try_folder(folder: Path, passed_over: Callable[[dict], bool],
                create: bool) -> tuple[contextlib.ExitStack, dict | None]:
    """Take *folder*'s lock in a stack of its own, then read whose run it holds.

    When another process holds the lock, the folder's `state.json` is read
    without it: a folder *passed_over* (another target's) comes back unlocked so
    the caller moves on; this target's, or one with no `state.json` yet, raises
    Locked (review's `_try_folder`). *create* makes the repo folder first.
    """
    held = contextlib.ExitStack()
    if create:
        folder.parent.mkdir(parents=True, exist_ok=True)
    try:
        held.enter_context(runlock.hold(folder))
    except runlock.Locked:
        held.close()
        t = folder_target(folder)
        if t is not None and passed_over(t):
            return contextlib.ExitStack(), t
        raise
    except BaseException:
        held.close()
        raise
    return held, folder_target(folder)


def check_out_dir(src: Path, folder: Path) -> None:
    """review's `check_out_dir`, on the folder the run writes: a `--dir` run never writes
    inside its source. The run writes `<root>/<repo>/` (its folder and `.locks/`), so
    that must not be at or under the source (`--out` the source's parent with the
    same basename is), and the source must not be under the run folder."""
    repo_dir = folder.parent
    if repo_dir == src or src in repo_dir.parents:
        raise TargetError(f"the run folder {folder} and its lock would be inside {src}; "
                          "pass --out somewhere else")
    if folder in src.parents:
        raise TargetError(f"{src} is inside the run folder {folder}; pass --out somewhere else")


def run_folder(root: Path, target: Target, lock: contextlib.ExitStack) -> Path:
    """Choose `<run>` for *target* under *root* and keep its lock on *lock*.

    First, this target's own folder: of the candidates the walk could reach,
    only those whose folder or lock file already exists are looked at, creating
    no folder, and the first whose readable `state.json` names this target is
    taken (R19); with none, the first whose `state.json` cannot be read (R23).
    Otherwise the walk, in review's `_choose_folder` order: `<repo>/<object>`;
    `<base>@<owner>/<object>` when that names another remote; for `--tree
    --path`, the hashed name in the folder's repo folder when it names another
    path. Raises Locked (exit 5) when another process holds this target's
    folder, TargetError (exit 2) when every folder tried holds another target's.
    """
    if target.kind == "dir":
        src = Path(target.source or "")
        repo_name, obj = dir_object(src)
        folder = root / repo_name / obj
        check_out_dir(src, folder)
        held, _ = _try_folder(folder, lambda t: False, create=True)
        lock.enter_context(held.pop_all())
        return folder

    url = target.remote_url
    repo_name, owner = checkout_repo_part(Path(target.repo), url)
    here = origin_owner_repo(url)
    obj = object_name(target)
    by_path = target.kind == "tree" and bool(target.path)

    def other_remote(t: dict) -> bool:
        return bool(t.get("remote_url")) and not _same_remote(t.get("remote_url"), here)

    def other_path(t: dict) -> bool:
        return by_path and bool(t.get("path")) and str(t.get("path")) != target.path

    def passed_over(t: dict) -> bool:
        return other_remote(t) or other_path(t)

    first = root / repo_name / obj
    owned = root / f"{repo_name.split('@')[0]}@{owner or 'local'}" / obj
    candidates = [first] + ([owned] if owned != first else [])
    if by_path:
        candidates += [c.parent / hashed_object_name(target) for c in list(candidates)]
    unreadable = None
    for folder in candidates:
        if not (folder.exists() or runlock.lock_path(folder).exists()):
            continue
        held, t = _try_folder(folder, passed_over, create=False)
        # An unreadable state.json ({}) names no target: it may be another target's,
        # so a later folder whose state names this target is taken before it.
        if t and not passed_over(t):
            lock.enter_context(held.pop_all())
            return folder
        if t == {} and unreadable is None:
            unreadable = folder
        held.close()
    if unreadable is not None:
        # No readable state names this target, and one candidate's cannot be read: it
        # may be this target's, so the run stops there (R23, exit 1) rather than
        # start a second folder.
        held, t = _try_folder(unreadable, passed_over, create=False)
        if t is not None and not passed_over(t):
            lock.enter_context(held.pop_all())
            return unreadable
        held.close()

    tried = [first]
    held, t = _try_folder(first, passed_over, create=True)
    folder = first
    if other_remote(t or {}) and owned != first:
        held.close()
        folder = owned
        tried.append(folder)
        held, t = _try_folder(folder, passed_over, create=True)
    if by_path and not other_remote(t or {}) and other_path(t or {}):
        held.close()
        folder = folder.parent / hashed_object_name(target)
        tried.append(folder)
        held, t = _try_folder(folder, passed_over, create=True)
    if passed_over(t or {}):
        held.close()
        raise TargetError("every folder this run could use holds another target's run: "
                          + ", ".join(str(f) for f in tried)
                          + "; pass --out to write this run somewhere else")
    lock.enter_context(held.pop_all())
    return folder


# --- --dir import, snapshot, --restart --------------------------------------------------

_IMPORT_NAME, _IMPORT_EMAIL = "spec-extract", "spec-extract@localhost"


def _import_env(now: datetime) -> dict[str, str]:
    """No user or system git config, a fixed author and the run's clock: the same files at
    the same clock give the same commit."""
    date = f"{int(now.timestamp())} +0000"
    return dict(git_env(), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                GIT_AUTHOR_NAME=_IMPORT_NAME, GIT_AUTHOR_EMAIL=_IMPORT_EMAIL,
                GIT_COMMITTER_NAME=_IMPORT_NAME, GIT_COMMITTER_EMAIL=_IMPORT_EMAIL,
                GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)


def _import_git(dest: Path, *args: str, env: dict[str, str]) -> None:
    proc = subprocess.run([*GIT_ARGV, "-c", f"user.name={_IMPORT_NAME}", "-c", f"user.email={_IMPORT_EMAIL}",
                           "-C", str(dest), *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, env=env, shell=False, check=False)
    if proc.returncode != 0:
        raise TargetError(f"git {args[0]} in {dest} failed: {proc.stderr.strip()}")


def import_dir(target: Target, run_dir: Path, now: datetime, saved_head: str | None = None) -> Target:
    """Copy the `--dir` source into `<run>/source/` and commit it there; return the target
    with `repo` and `head_sha` set.

    A `source/` that already holds a commit is reused as it is, whatever the
    source holds now (R14; `--restart` re-imports). With no saved state, one
    without a commit holds nothing a run named, and is copied again (R1). With
    *saved_head* (the saved state's import), `source/` must still be at it, or
    ImportLost: a new import would contradict the state (R27). Nothing is
    written to the source.
    """
    src = Path(target.source or "")
    dest = run_dir / SOURCE_DIR
    head = rev_parse(dest, "HEAD") if (dest / ".git").is_dir() else None
    if saved_head is not None and head != saved_head:
        raise ImportLost(dest, saved_head, str(src))
    if head is None:
        try:
            if dest.is_symlink() or dest.is_file():
                dest.unlink()
            elif dest.exists():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dest, symlinks=True,
                            ignore=shutil.ignore_patterns(*IMPORT_EXCLUDES, ".git"))
        except OSError as e:  # shutil.Error is an OSError
            raise TargetError(f"cannot copy {src} into {dest}: {e}") from e
        env = _import_env(now)
        _import_git(dest, "init", "-q", "-b", "main", env=env)
        # -f: a `.gitignore` copied from the source (GitHub's Python one ignores `lib/`)
        # must not keep copied code out of the commit; IMPORT_EXCLUDES already left out
        # what is not the project's.
        _import_git(dest, "add", "-A", "-f", env=env)
        _import_git(dest, "commit", "-q", "--allow-empty", "-m", f"Import {src.name} for spec-extract",
                    env=env)
        head = rev_parse(dest, "HEAD")
        if head is None:
            raise TargetError(f"the import of {src} into {dest} left no commit")
    return target.model_copy(update={"repo": str(dest), "head_sha": head})


def _git_bytes(repo: str | Path, *args: str, stdin: bytes | None = None) -> bytes:
    proc = subprocess.run([*GIT_ARGV, "-C", str(repo), *args], input=stdin, capture_output=True,
                          env=git_env(), shell=False, check=False,
                          **({} if stdin is not None else {"stdin": subprocess.DEVNULL}))
    if proc.returncode != 0:
        raise TargetError(f"git {args[0]} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def _tree_entries(repo: str | Path, sha: str) -> list[tuple[str, str, str, str]]:
    """`(mode, type, oid, path)` for every entry of the tree at *sha*, recursively."""
    out = []
    for raw in _git_bytes(repo, "ls-tree", "-r", "-z", "--full-tree", sha).split(b"\0"):
        if not raw:
            continue
        meta, _, path = raw.partition(b"\t")
        mode, kind, oid = meta.decode().split()
        out.append((mode, kind, oid, os.fsdecode(path)))
    return out


def _blob_bytes(repo: str | Path, oids: list[str]) -> dict[str, bytes]:
    """Each blob's bytes as git stores them (`git cat-file --batch`): no filter, no eol
    conversion, no export rule, as `git show <sha>:<path>` prints them for the gates."""
    uniq = list(dict.fromkeys(oids))
    if not uniq:
        return {}
    out = _git_bytes(repo, "cat-file", "--batch", stdin="".join(f"{o}\n" for o in uniq).encode())
    blobs, pos = {}, 0
    for oid in uniq:
        nl = out.index(b"\n", pos)
        header = out[pos:nl].split()
        if len(header) != 3 or header[1] != b"blob":
            raise TargetError(f"git cat-file: {oid} is not a blob in {repo}")
        size = int(header[2])
        blobs[oid] = out[nl + 1:nl + 1 + size]
        pos = nl + 1 + size + 1
    return blobs


def _safe_rel(path: str) -> bool:
    parts = path.split("/")
    return not path.startswith("/") and all(p not in ("", ".", "..", ".git") for p in parts)


def _inside(path: str, top: str) -> bool:
    return path == top or path.startswith(top + os.sep)


def _write_tree(repo: str | Path, sha: str, dest: Path) -> list[str]:
    """Write the tree at *sha* into *dest*; return what was left out, with why.

    Regular files get their blob's bytes and git's executable bit; a submodule is an
    empty directory, as `git archive` makes it. A link is kept only while it resolves
    inside *dest*: one that is absolute or leaves the tree is left out, and the
    check is repeated over every kept link until none leaves, since one link can
    lead through another.
    """
    entries = _tree_entries(repo, sha)
    blobs = _blob_bytes(repo, [oid for _, kind, oid, _ in entries if kind == "blob"])
    top = os.path.realpath(dest)
    left_out: list[str] = []
    links: list[tuple[Path, str]] = []
    # Files and directories first, while *dest* holds no link a parent could pass through.
    for phase in ("files", "links"):
        for mode, kind, oid, rel in entries:
            if (mode == "120000") != (phase == "links"):
                continue
            if not _safe_rel(rel):
                left_out.append(f"{rel}: not a path inside the tree")
                continue
            out = dest / rel
            if not _inside(os.path.realpath(out.parent), top):
                left_out.append(f"{rel}: its directory leads outside the tree")
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            if kind == "commit":
                out.mkdir(exist_ok=True)
            elif mode == "120000":
                to = os.fsdecode(blobs[oid])
                if os.path.isabs(to):
                    left_out.append(f"{rel} -> {to}: a link to an absolute path")
                    continue
                os.symlink(to, out)
                links.append((out, to))
            elif kind == "blob":
                out.write_bytes(blobs[oid])
                out.chmod(0o755 if mode == "100755" else 0o644)
    changed = True
    while changed:
        changed = False
        for out, to in links:
            if out.is_symlink() and not _inside(os.path.realpath(out), top):
                out.unlink()
                left_out.append(f"{out.relative_to(dest).as_posix()} -> {to}: a link outside the tree")
                changed = True
    return left_out


def snapshot(target: Target, run_dir: Path) -> Path:
    """Write the tree at `head_sha` into `<run>/snapshot/` for the agents to read.

    The files hold the bytes git stores, which the gates read with git: not
    `git archive`, whose `.gitattributes` export rules leave files out and rewrite
    others (plan, Decided 15). Links that leave the tree are left out and named on
    stderr; the run goes on. Reused when its marker names `head_sha`; otherwise (a
    snapshot stopped part-way has no marker, R2) deleted and written again. The
    marker is written last. Nothing is written to the repository.
    """
    dest = run_dir / SNAPSHOT_DIR
    marker = dest / SNAPSHOT_MARKER
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == target.head_sha:
        return dest
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for why in _write_tree(target.repo, str(target.head_sha), dest):
        print(f"spec-extract: snapshot: left out {why}", file=sys.stderr)
    marker.write_text(f"{target.head_sha}\n", encoding="utf-8")
    return dest


def restart(run_dir: Path, now: datetime) -> Path | None:
    """Rename *run_dir* to `<object>.old-<YYYYmmddHHMMSS>` beside it, adding `-2`, `-3` …
    when that name is taken, with one rename. Never deletes. None when there is no
    run folder. The lock file is outside the run folder, so it stays put."""
    if not (run_dir.exists() or run_dir.is_symlink()):
        return None
    base = f"{run_dir.name}.old-{now.strftime('%Y%m%d%H%M%S')}"
    n = 1
    while True:
        old = run_dir.parent / (base if n == 1 else f"{base}-{n}")
        if not (old.exists() or old.is_symlink()):
            break
        n += 1
    os.rename(run_dir, old)
    return old
