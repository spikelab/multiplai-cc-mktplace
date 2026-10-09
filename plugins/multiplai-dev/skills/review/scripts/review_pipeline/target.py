"""Resolve what is being reviewed: a branch, a PR, a commit range, or a whole tree.

A tree target (`--tree`, `--dir`) is a commit with no base: `base_sha` is git's
empty tree, so every file reads as added, and the files under review are the
files at head (under `--path`), less the ones `tree_files` skips.

Read-only over the reviewed repository. Every call is a fixed argv with
`shell=False`. Nothing here checks out, merges or writes to the repo, and
nothing fetches unless the caller passes `fetch=True`.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

from .models import GateResult, TargetInfo

log = logging.getLogger(__name__)

# User or repo config must not change what we parse.
_GIT = ["git", "-c", "color.ui=never", "-c", "core.quotepath=off"]
_DIFF_FLAGS = ["--no-color", "--no-ext-diff", "--no-textconv"]


# `git hash-object -t tree /dev/null`: git accepts it wherever a base commit goes.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# A tree review leaves these out of the files under review, each with a reason
# in skipped.txt: they are large, machine-written, or not text.
TREE_MAX_CHARS = 200_000
LOCKFILES = frozenset({
    "uv.lock", "poetry.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock",
    "Gemfile.lock", "go.sum",
})
GENERATED_ATTRS = ("linguist-generated", "linguist-vendored")
# Directories `--dir` does not copy: installed dependencies and build output.
IMPORT_EXCLUDES = ("node_modules", ".venv", "venv", "__pycache__", "dist", "build")


class TargetError(Exception):
    """The target could not be resolved. The message says why."""


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("GIT_EXTERNAL_DIFF", None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def git(repo: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run one git command against *repo*; stdout/stderr as text."""
    proc = subprocess.run(
        [*_GIT, "-C", str(repo), *args],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, env=_env(),
        shell=False, check=False,
    )
    if check and proc.returncode != 0:
        raise TargetError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def rev_parse(repo: str | Path, ref: str) -> str | None:
    """Full 40-hex sha for *ref*, or None when it does not resolve to a commit."""
    proc = git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def merge_base(repo: str | Path, a: str, b: str) -> str | None:
    proc = git(repo, "merge-base", a, b, check=False)
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def default_branch(repo: str | Path) -> str:
    """`origin/HEAD`'s branch name; `main` when origin/HEAD is not set."""
    proc = git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    ref = proc.stdout.strip()
    if proc.returncode == 0 and ref.startswith("refs/remotes/origin/"):
        return ref[len("refs/remotes/origin/"):]
    return "main"


def remote_url(repo: str | Path) -> str | None:
    proc = git(repo, "remote", "get-url", "origin", check=False)
    url = proc.stdout.strip()
    return url if proc.returncode == 0 and url else None


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


@dataclass
class Resolved:
    """A resolved target before the gate: shas may be None."""
    repo: Path
    kind: str
    ref: str
    base_sha: str | None
    head_sha: str | None
    pr: int | None = None
    problem: str = ""
    title: str = ""
    description: str = ""
    base_ref: str = ""
    path: str = ""  # tree targets: the directory under review, "" for the whole tree


PR_VIEW_FIELDS = "headRefOid,baseRefOid,headRefName,baseRefName,title,body"


def _pr_view(repo: Path, number: int) -> dict:
    gh = shutil.which("gh")
    if gh is None:
        raise TargetError(
            "--pr needs the GitHub CLI (`gh`), which is not installed. Install it from "
            "https://cli.github.com and run `gh auth login`, or pass --range <base>..<head>.")
    proc = subprocess.run(
        [gh, "pr", "view", str(number), "--json", PR_VIEW_FIELDS],
        cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL, shell=False, check=False,
    )
    if proc.returncode != 0:
        raise TargetError(f"gh pr view {number} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def branch_rules(repo: Path, branch: str) -> list[dict] | None:
    """GitHub's rules on *branch* (rulesets and classic protection, merged by GitHub).

    `gh api` fills `{owner}` and `{repo}` from the repository's origin. `[]`
    means GitHub reports no rules. A missing `gh`, a non-GitHub remote or an
    API error gives None and a warning: the review goes on without this
    context rather than failing.
    """
    gh = shutil.which("gh")
    if gh is None or not branch or github_web_base(remote_url(repo)) is None:
        return None
    proc = subprocess.run(
        [gh, "api", f"repos/{{owner}}/{{repo}}/rules/branches/{branch}"],
        cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL, shell=False, check=False,
    )
    if proc.returncode != 0:
        log.warning("gh api rules/branches/%s failed; reviewing without branch rules: %s",
                    branch, proc.stderr.strip()[:300])
        return None
    try:
        rules = json.loads(proc.stdout)
    except ValueError:
        log.warning("gh api rules/branches/%s returned no JSON; reviewing without branch rules", branch)
        return None
    return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else None


def normalise_path(path: str | None) -> str:
    """A repo-relative directory as git prints paths: no `./`, no trailing `/`; "" for the root."""
    p = (path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.strip("/")
    return "" if p in ("", ".") else p


def resolve(repo: str | Path, *, branch: str | None = None, pr: int | None = None,
            range_: str | None = None, tree: str | None = None, path: str | None = None,
            base_branch: str | None = None, fetch: bool = False) -> Resolved:
    """Turn exactly one of branch / pr / range / tree into base and head shas.

    A tree has no base: `base_sha` is `EMPTY_TREE`, `head_sha` the commit *tree*
    names, and `ref` the directory under review (*path*, or "." for all of it).
    """
    repo = Path(repo).expanduser().resolve()
    chosen = [x for x in (branch, pr, range_, tree) if x not in (None, "")]
    if len(chosen) != 1:
        raise TargetError("pass exactly one of --branch, --pr, --range, --tree")
    if path and not tree:
        raise TargetError("--path goes with --tree")
    if not (repo / ".git").exists() and git(repo, "rev-parse", "--git-dir", check=False).returncode != 0:
        raise TargetError(f"{repo} is not a git repository")
    if fetch:
        git(repo, "fetch", "--quiet", "origin")

    if tree:
        head = rev_parse(repo, tree)
        sub = normalise_path(path)
        problem = "" if head else f"{tree} does not resolve to a commit"
        return Resolved(repo, "tree", sub or ".", EMPTY_TREE, head, problem=problem, path=sub)

    if branch:
        head = rev_parse(repo, f"origin/{branch}")
        default = base_branch or default_branch(repo)
        base = merge_base(repo, f"origin/{default}", head) if head else None
        problem = "" if head else f"origin/{branch} not found (pass --fetch to fetch it)"
        if head and not base:
            problem = f"no merge-base between origin/{default} and origin/{branch}"
        return Resolved(repo, "branch", branch, base, head, problem=problem, base_ref=default)

    if pr is not None and pr != "":
        info = _pr_view(repo, int(pr))
        head = rev_parse(repo, info["headRefOid"])
        base_tip = rev_parse(repo, info["baseRefOid"])
        base = merge_base(repo, base_tip, head) if head and base_tip else None
        problem = ""
        if not head:
            problem = f"PR #{pr} head {info['headRefOid'][:12]} is not in this clone (pass --fetch)"
        elif not base:
            problem = f"PR #{pr} base {info['baseRefOid'][:12]} is not in this clone (pass --fetch)"
        return Resolved(repo, "pr", str(pr), base, head, pr=int(pr), problem=problem,
                        title=(info.get("title") or "").strip(), description=(info.get("body") or "").strip(),
                        base_ref=info.get("baseRefName") or "")

    range_ = range_ or ""
    if ".." not in range_ or range_.count("..") != 1 or "..." in range_:
        raise TargetError(f"--range must be <base>..<head>, got {range_!r}")
    a, b = range_.split("..")
    base, head = rev_parse(repo, a), rev_parse(repo, b)
    problem = "" if base and head else f"{a if not base else b} does not resolve to a commit"
    return Resolved(repo, "range", range_, base, head, problem=problem)


def diff_text(repo: Path, base: str, head: str) -> str:
    return git(repo, "diff", *_DIFF_FLAGS, f"{base}..{head}").stdout


def target_gate(resolved: Resolved, diff: str | None, files: list[str] | None = None) -> GateResult:
    """Base found, head found, diff non-empty. A tree target needs one file left to review instead of a diff."""
    if resolved.problem:
        return GateResult(passed=False, reason=resolved.problem, action="exit")
    if not resolved.base_sha:
        return GateResult(passed=False, reason="base commit not found", action="exit")
    if not resolved.head_sha:
        return GateResult(passed=False, reason="head commit not found", action="exit")
    if resolved.kind == "tree":
        if not files:
            where = f"under {resolved.path}" if resolved.path else "in the tree"
            return GateResult(passed=False, reason=f"no file {where} at {resolved.head_sha[:12]} is left to "
                                                   f"review (see skipped.txt)", action="exit")
        return GateResult(passed=True)
    if not diff or not diff.strip():
        return GateResult(passed=False, reason="the diff between base and head is empty", action="exit")
    return GateResult(passed=True)


def _check_attrs(repo: Path, head: str, paths: list[str]) -> dict[str, str]:
    """{path: the attribute set} for paths with linguist-generated or -vendored set at *head*.

    `git check-attr --source` needs git 2.40; an older git skips this check
    with a warning rather than failing the review.
    """
    if not paths:
        return {}
    proc = subprocess.run(
        [*_GIT, "-C", str(repo), "check-attr", "-z", "--stdin", "--source", head, *GENERATED_ATTRS],
        input="\0".join(paths) + "\0", capture_output=True, text=True, env=_env(), shell=False, check=False,
    )
    if proc.returncode != 0:
        log.warning("git check-attr --source failed; not skipping generated files: %s", proc.stderr.strip()[:300])
        return {}
    parts = proc.stdout.split("\0")
    out: dict[str, str] = {}
    for i in range(0, len(parts) - 2, 3):
        path, attr, value = parts[i], parts[i + 1], parts[i + 2]
        if value in ("set", "true") and path not in out:
            out[path] = attr
    return out


def tree_files(repo: Path, head: str, path: str = "") -> tuple[list[str], list[tuple[str, str]]]:
    """(files to review, [(file, reason) skipped]) at *head*, under *path*, in path order.

    Skipped: binary files, files over TREE_MAX_CHARS characters, lockfiles by
    name, files with linguist-generated or linguist-vendored set, and submodules.
    """
    spec = ["--", path] if path else []
    listed = [f for f in git(repo, "ls-tree", "-r", "-z", "--name-only", head, *spec).stdout.split("\0") if f]
    sizes: dict[str, int] = {}
    for rec in git(repo, "ls-tree", "-r", "-l", "-z", head, *spec).stdout.split("\0"):
        meta, _, name = rec.partition("\t")
        fields = meta.split()
        if name and len(fields) == 4 and fields[3].isdigit():
            sizes[name] = int(fields[3])
    binary = set()
    for rec in git(repo, "diff", *_DIFF_FLAGS, "--numstat", "-z", EMPTY_TREE, head, *spec).stdout.split("\0"):
        added, _, rest = rec.partition("\t")
        if added == "-":
            binary.add(rest.partition("\t")[2])
    generated = _check_attrs(repo, head, listed)
    keep: list[str] = []
    skipped: list[tuple[str, str]] = []
    for f in sorted(listed):
        if f in binary:
            skipped.append((f, "binary"))
        elif sizes.get(f, 0) > TREE_MAX_CHARS and len(git(repo, "show", f"{head}:{f}").stdout) > TREE_MAX_CHARS:
            skipped.append((f, f"over {TREE_MAX_CHARS} characters"))
        elif f.rsplit("/", 1)[-1] in LOCKFILES:
            skipped.append((f, "lockfile"))
        elif f in generated:
            skipped.append((f, f"{generated[f]} is set"))
        elif f not in sizes:
            skipped.append((f, "submodule"))  # ls-tree prints no size for a gitlink
        else:
            keep.append(f)
    return keep, skipped


def build_target(resolved: Resolved, *, tickets: list[str] | None = None,
                 deployed_in: str | None = None, name: str | None = None,
                 label: str | None = None) -> TargetInfo:
    """Everything about the target except files written to disk.

    *name* replaces the repository's directory name in the slug and label (a
    `--dir` review names the source directory, not its copy); *label* replaces
    the label outright.
    """
    repo, base, head = resolved.repo, resolved.base_sha, resolved.head_sha
    if resolved.kind == "tree":
        files, skipped = tree_files(repo, head, resolved.path)
        repo_name = name or repo.name
        slug = sanitize_slug(f"{repo_name}--tree" + (f"-{resolved.path}" if resolved.path else ""))
        what = resolved.path or "whole tree"
        return TargetInfo(
            repo_path=str(repo), remote_url=remote_url(repo), base_sha=base, head_sha=head,
            commits=[], files=files, skipped=skipped, slug=slug,
            label=label or f"{repo_name}: {what} at {head[:8]}", kind="tree", ref=resolved.ref,
            tickets=list(tickets or []), deployed_in=deployed_in,
        )
    commits = []
    for line in git(repo, "log", "--format=%H%x00%s", f"{base}..{head}").stdout.splitlines():
        if "\0" in line:
            sha, subject = line.split("\0", 1)
            commits.append((sha, subject))
    files = [f for f in git(repo, "diff", *_DIFF_FLAGS, "--name-only", f"{base}..{head}").stdout.splitlines() if f]
    ref = f"pr-{resolved.pr}" if resolved.kind == "pr" else resolved.ref
    slug = sanitize_slug(f"{repo.name}--{ref}")
    label = f"{repo.name} {('PR #' + str(resolved.pr)) if resolved.pr else resolved.ref}"
    return TargetInfo(
        repo_path=str(repo), remote_url=remote_url(repo), base_sha=base, head_sha=head,
        commits=commits, files=files, slug=slug, label=label, kind=resolved.kind,
        ref=resolved.ref, pr=resolved.pr, tickets=list(tickets or []), deployed_in=deployed_in,
        title=resolved.title, description=resolved.description, base_ref=resolved.base_ref,
        branch_rules=branch_rules(repo, resolved.base_ref) if resolved.base_ref else None,
    )


def write_target_files(target: TargetInfo, diff: str, target_dir: Path) -> TargetInfo:
    """Write diff.patch (and the commit and file lists) under *target_dir*.

    A tree target has no diff: diff.patch is empty, and skipped.txt lists each
    file left out with its reason.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    diff_path = target_dir / "diff.patch"
    diff_path.write_text(diff, encoding="utf-8")
    (target_dir / "commits.txt").write_text(
        "".join(f"{sha} {subject}\n" for sha, subject in target.commits), encoding="utf-8")
    (target_dir / "files.txt").write_text("".join(f"{f}\n" for f in target.files), encoding="utf-8")
    if target.kind == "tree":
        (target_dir / "skipped.txt").write_text(
            "".join(f"{f}\t{why}\n" for f, why in target.skipped), encoding="utf-8")
    return target.model_copy(update={"diff_path": str(diff_path)})


_IMPORT_ENV = {
    "GIT_AUTHOR_NAME": "review_pipeline", "GIT_AUTHOR_EMAIL": "review@localhost",
    "GIT_COMMITTER_NAME": "review_pipeline", "GIT_COMMITTER_EMAIL": "review@localhost",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
}


def import_dir(path: str | Path, target_dir: Path) -> Path:
    """Copy a directory that is not under git into `<target_dir>/source/` and commit it there.

    The copy leaves out IMPORT_EXCLUDES (and any `.git`), and `git add` honours
    a `.gitignore` copied with it. One commit, with a fixed author and no user
    git config. Nothing is written to *path*. Returns the copy, which is then
    reviewed as `--tree HEAD`. The copy is not `<target_dir>/tree/`: that is
    the agents' snapshot, deleted when a run ends.
    """
    src = Path(path).expanduser().resolve()
    if not src.is_dir():
        raise TargetError(f"{src} is not a directory")
    inside = git(src, "rev-parse", "--show-toplevel", check=False)
    if inside.returncode == 0 and inside.stdout.strip():
        top = Path(inside.stdout.strip()).resolve()
        rel = src.relative_to(top).as_posix() if src != top else "."
        raise TargetError(f"{src} is inside the git repository {top}; review it with "
                          f"--repo {top} --tree --path {rel}")
    dest = (target_dir / "source").resolve()
    if dest == src or src in dest.parents:
        raise TargetError(f"the output directory {target_dir} is inside {src}; pass --out somewhere else")
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, symlinks=True, ignore=shutil.ignore_patterns(*IMPORT_EXCLUDES, ".git"))
    env = dict(_env(), **_IMPORT_ENV)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["commit", "-q", "--allow-empty", "-m", f"Import {src.name} for review"]):
        proc = subprocess.run([*_GIT, "-C", str(dest), *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, env=env, shell=False, check=False)
        if proc.returncode != 0:
            raise TargetError(f"git {args[0]} in the copy of {src} failed: {proc.stderr.strip()}")
    return dest


def deployed(target: TargetInfo) -> str | None:
    """"yes"/"no" for `--deployed-in <name>`: is head an ancestor of origin/<name>?"""
    if not target.deployed_in:
        return None
    ref = rev_parse(target.repo_path, f"origin/{target.deployed_in}")
    if ref is None:
        return f"unknown (origin/{target.deployed_in} not found)"
    proc = git(target.repo_path, "merge-base", "--is-ancestor", target.head_sha, ref, check=False)
    return "yes" if proc.returncode == 0 else "no"


def snapshot_head(target: TargetInfo, dest: Path) -> Path:
    """Extract the tree at head_sha into *dest* for the agents to read.

    The agents' Read/Grep/Glob tools see a directory, not a commit, and the
    repo's working tree is whatever happens to be checked out. The gates check
    quotes against `git show <head_sha>:<path>`, so the agents must read the
    same bytes. `git archive` reads the object store and writes nothing to the
    repo.
    """
    marker = dest / ".review-head"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == target.head_sha:
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    proc = subprocess.run(
        [*_GIT, "-C", target.repo_path, "archive", "--format=tar", target.head_sha],
        capture_output=True, stdin=subprocess.DEVNULL, env=_env(), shell=False, check=False,
    )
    if proc.returncode != 0:
        raise TargetError(f"git archive {target.head_sha[:12]} failed: {proc.stderr.decode(errors='replace').strip()}")
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tar:
        tar.extractall(dest, filter="data")
    (dest / ".review-head").write_text(target.head_sha + "\n", encoding="utf-8")
    return dest
