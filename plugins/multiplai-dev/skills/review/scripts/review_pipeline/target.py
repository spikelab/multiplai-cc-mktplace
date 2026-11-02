"""Resolve what is being reviewed: a branch, a PR, a commit range, or a whole tree.

A tree target (`--tree`, `--dir`) is a commit with no base: `base_sha` is git's
empty tree, so every file reads as added, and the files under review are the
files at head (under `--path`), less the ones `tree_files` skips.

Read-only over the reviewed repository. Every call is a fixed argv with
`shell=False`. Nothing here checks out, merges or writes to the repo, and
nothing fetches unless the caller passes `fetch=True`.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import git_targets
from multiplai_core.citations import GIT_ARGV, git_env

from .models import CLASSIC_UNREAD, GateResult, Need, TargetInfo

log = logging.getLogger(__name__)

_DIFF_FLAGS = ["--no-color", "--no-ext-diff", "--no-textconv"]


# `git hash-object -t tree /dev/null`: git accepts it wherever a base commit goes.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# A tree review leaves these out of the files under review, each with a reason
# in skipped.txt: they are large, machine-written, or not text.
TREE_MAX_CHARS = 200_000
LOCKFILES = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.lock",
        "Gemfile.lock",
        "go.sum",
    }
)
GENERATED_ATTRS = ("linguist-generated", "linguist-vendored")
# Directories `--dir` does not copy: installed dependencies and build output.
IMPORT_EXCLUDES = ("node_modules", ".venv", "venv", "__pycache__", "dist", "build")


class TargetError(Exception):
    """The target could not be resolved. The message says why."""


def git(
    repo: str | Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess:
    """Run one git command against *repo*; stdout/stderr as text."""
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
        raise TargetError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


# Shared with codebase-walkthrough (plugins/multiplai-dev/lib/git_targets).
rev_parse = git_targets.rev_parse
merge_base = git_targets.merge_base


def default_branch(repo: str | Path) -> str:
    """`origin/HEAD`'s branch name; `main` when origin/HEAD is not set."""
    proc = git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    ref = proc.stdout.strip()
    if proc.returncode == 0 and ref.startswith("refs/remotes/origin/"):
        return ref[len("refs/remotes/origin/") :]
    return "main"


def remote_url(repo: str | Path) -> str | None:
    proc = git(repo, "remote", "get-url", "origin", check=False)
    url = proc.stdout.strip()
    return url if proc.returncode == 0 and url else None


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


def origin_owner_repo(url: str | None) -> tuple[str, str] | None:
    """`(owner, repo)` from a remote URL (the last two path parts, `.git` dropped), else None.

    `https://github.com/spikelab/github-tests.git`,
    `git@github.com:spikelab/github-tests.git` and the `ssh://` form all give
    `("spikelab", "github-tests")`.
    """
    if not url:
        return None
    path = url.strip().rstrip("/")
    path = path.removesuffix(".git")
    parts = [p for p in re.split(r"[/:]", path) if p]
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


def origin_name(url: str | None) -> str | None:
    """`<owner>-<repo>` from a remote URL (`origin_owner_repo`), else None: `spikelab-github-tests`."""
    pair = origin_owner_repo(url)
    return "-".join(pair) if pair else None


def slug_repo_name(repo: Path) -> str:
    """The repository part of a slug: `<owner>-<repo>` from the `origin` remote.

    Every checkout and worktree of one repository has the same origin, so a
    PR reviewed from any of them gets one directory. Only a repository with
    no `origin` remote (or one that names no owner) falls back to its
    directory name, and that is logged.
    """
    name = origin_name(remote_url(repo))
    if name:
        return name
    log.info(
        "no origin remote in %s with an owner and a name: the slug uses the directory name %s",
        repo,
        repo.name,
    )
    return repo.name


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
    # Lookups that failed or came back empty, as asks for a person.
    needs: list[Need] = field(default_factory=list)


def _first_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[0][:300] if lines else "no error message"


PR_VIEW_FIELDS = git_targets.PR_VIEW_FIELDS


def _pr_view(repo: Path, number: int) -> dict:
    try:
        return git_targets.pr_view(repo, number)
    except git_targets.GitTargetError as e:
        raise TargetError(str(e)) from e


def branch_rules(
    repo: Path, branch: str, needs: list[Need] | None = None
) -> list[dict] | None:
    """GitHub's rules on *branch*: its rulesets, plus the checks classic protection requires.

    `gh api` fills `{owner}` and `{repo}` from the repository's origin. `[]`
    means GitHub reports no rules. A missing `gh`, a non-GitHub remote or an
    API error gives None and a warning: the review goes on without this
    context rather than failing. A failed call also appends a `Need` to
    *needs*, with the command a person would run to read the rules.
    """
    gh = shutil.which("gh")
    if gh is None or not branch or github_web_base(remote_url(repo)) is None:
        return None
    endpoint = f"repos/{{owner}}/{{repo}}/rules/branches/{branch}"
    proc = subprocess.run(
        [gh, "api", endpoint],
        cwd=repo,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        shell=False,
        check=False,
    )

    def need(error: str) -> None:
        if needs is not None:
            # The command is the call made above, placeholders included: gh fills
            # them the same way when it runs in the repository, so a fork clone or
            # `gh repo set-default` resolves to the repository the pipeline asked.
            needs.append(
                Need(
                    what=f"GitHub's rules on the base branch `{branch}`, so the review can tell whether CI "
                    f"blocks a merge. Run the command in `{repo}`. The review went on without them: {error}",
                    cause="lookup-failed",
                    command=f"gh api {endpoint}",
                    source="pipeline",
                )
            )

    if proc.returncode != 0:
        log.warning(
            "gh api rules/branches/%s failed; reviewing without branch rules: %s",
            branch,
            proc.stderr.strip()[:300],
        )
        need(_first_line(proc.stderr))
        return None
    try:
        rules = json.loads(proc.stdout)
    except ValueError:
        log.warning(
            "gh api rules/branches/%s returned no JSON; reviewing without branch rules",
            branch,
        )
        need("gh api returned no JSON")
        return None
    if not isinstance(rules, list):
        return None
    return [r for r in rules if isinstance(r, dict)] + _classic_checks(
        gh, repo, branch, needs
    )


def _classic_checks(
    gh: str, repo: Path, branch: str, needs: list[Need] | None
) -> list[dict]:
    """The checks classic branch protection requires, as one `required_status_checks` rule.

    `rules/branches` lists rulesets only, so a check required by classic protection is read
    from the branch itself. When that read fails, a `CLASSIC_UNREAD` rule says so.
    """
    endpoint = f"repos/{{owner}}/{{repo}}/branches/{branch}"
    proc = subprocess.run(
        [gh, "api", endpoint],
        cwd=repo,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        shell=False,
        check=False,
    )
    try:
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
    except ValueError:
        data = None
    if data is None:
        log.warning("gh api %s failed; classic branch protection not read", endpoint)
        if needs is not None:
            needs.append(
                Need(
                    what=f"Whether classic branch protection on `{branch}` requires CI checks. Run the "
                    f"command in `{repo}`. The review went on without it: "
                    f"{_first_line(proc.stderr) or 'gh api returned no JSON'}",
                    cause="lookup-failed",
                    command=f"gh api {endpoint}",
                    source="pipeline",
                )
            )
        return [{"type": CLASSIC_UNREAD}]
    required = (
        ((data.get("protection") or {}).get("required_status_checks") or {})
        if isinstance(data, dict)
        else {}
    )
    contexts = list(required.get("contexts") or []) + [
        c.get("context")
        for c in required.get("checks") or []
        if isinstance(c, dict) and c.get("context")
    ]
    names = list(dict.fromkeys(c for c in contexts if isinstance(c, str)))
    if not names:
        return []
    return [
        {
            "type": "required_status_checks",
            "source": "classic branch protection",
            "parameters": {"required_status_checks": [{"context": c} for c in names]},
        }
    ]


def normalise_path(path: str | None) -> str:
    """A repo-relative directory as git prints paths: no `./`, no trailing `/`; "" for the root."""
    p = (path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.strip("/")
    return "" if p in ("", ".") else p


def tree_root(repo: Path, path: str | None) -> tuple[Path, str]:
    """(the top of *repo*'s working tree, the directory under review relative to it).

    git lists names relative to the directory it runs in but `git show
    <sha>:<path>` reads them from the root, so a tree target always reads from
    the top. When *repo* is a subdirectory, the directory under review is that
    subdirectory, with *path* (relative to *repo*) joined below it. A
    repository with no working tree (bare) is read as given.
    """
    proc = git(repo, "rev-parse", "--show-toplevel", check=False)
    top_text = proc.stdout.strip() if proc.returncode == 0 else ""
    top = Path(top_text).resolve() if top_text else repo
    prefix = (
        repo.relative_to(top).as_posix() if top != repo and top in repo.parents else ""
    )
    joined = normalise_path("/".join(p for p in (prefix, normalise_path(path)) if p))
    sub = normalise_path(posixpath.normpath(joined)) if joined else ""
    if sub == ".." or sub.startswith("../"):
        raise TargetError(f"--path {path} is outside the repository {top}")
    return top, sub


def resolve(
    repo: str | Path,
    *,
    branch: str | None = None,
    pr: int | None = None,
    range_: str | None = None,
    tree: str | None = None,
    path: str | None = None,
    base_branch: str | None = None,
    fetch: bool = False,
) -> Resolved:
    """Turn exactly one of branch / pr / range / tree into base and head shas.

    A tree has no base: `base_sha` is `EMPTY_TREE`, `head_sha` the commit *tree*
    names, and `ref` the directory under review (*path*, or "." for all of it).
    A tree's `repo` is the top of the working tree; a *repo* below it becomes
    the directory under review, with *path* below that (`tree_root`).
    """
    repo = Path(repo).expanduser().resolve()
    chosen = [x for x in (branch, pr, range_, tree) if x not in (None, "")]
    if len(chosen) != 1:
        raise TargetError("pass exactly one of --branch, --pr, --range, --tree")
    if path and not tree:
        raise TargetError("--path goes with --tree")
    if (
        not (repo / ".git").exists()
        and git(repo, "rev-parse", "--git-dir", check=False).returncode != 0
    ):
        raise TargetError(f"{repo} is not a git repository")
    if fetch:
        try:
            git_targets.fetch_origin(repo)
        except git_targets.GitTargetError as e:
            raise TargetError(str(e)) from e

    if tree:
        repo, sub = tree_root(repo, path)
        head = rev_parse(repo, tree)
        problem = "" if head else f"{tree} does not resolve to a commit"
        return Resolved(
            repo, "tree", sub or ".", EMPTY_TREE, head, problem=problem, path=sub
        )

    if branch:
        head = rev_parse(repo, f"origin/{branch}")
        default = base_branch or default_branch(repo)
        base = merge_base(repo, f"origin/{default}", head) if head else None
        problem = (
            "" if head else f"origin/{branch} not found (pass --fetch to fetch it)"
        )
        if head and not base:
            problem = f"no merge-base between origin/{default} and origin/{branch}"
        return Resolved(
            repo, "branch", branch, base, head, problem=problem, base_ref=default
        )

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
        resolved = Resolved(
            repo,
            "pr",
            str(pr),
            base,
            head,
            pr=int(pr),
            problem=problem,
            title=(info.get("title") or "").strip(),
            description=(info.get("body") or "").strip(),
            base_ref=info.get("baseRefName") or "",
        )
        # A PR always has a title and a base branch; empty means gh returned less than it should.
        # (An empty body is normal and is not asked for.)
        missing = [
            name
            for name, value in (
                ("title", resolved.title),
                ("baseRefName", resolved.base_ref),
            )
            if not value
        ]
        if missing:
            # The same call `_pr_view` made, from the same directory, with no --repo: gh
            # picks the repository the same way it did for the pipeline.
            resolved.needs.append(
                Need(
                    what=f"PR #{pr}'s {' and '.join(missing)}: `gh pr view` returned "
                    f"{'them' if len(missing) > 1 else 'it'} empty, so the review went on without "
                    f"{'them' if len(missing) > 1 else 'it'}. Run the command in `{repo}`",
                    cause="lookup-failed",
                    command=f"gh pr view {pr} --json {','.join(missing)}",
                    source="pipeline",
                )
            )
        return resolved

    range_ = range_ or ""
    if ".." not in range_ or range_.count("..") != 1 or "..." in range_:
        raise TargetError(f"--range must be <base>..<head>, got {range_!r}")
    a, b = range_.split("..")
    base, head = rev_parse(repo, a), rev_parse(repo, b)
    problem = (
        "" if base and head else f"{a if not base else b} does not resolve to a commit"
    )
    return Resolved(repo, "range", range_, base, head, problem=problem)


def tree_paths(repo: str | Path, sha: str) -> frozenset[str]:
    """Every path in the tree of commit *sha* (`git ls-tree -r --name-only`), blobs and links alike."""
    return frozenset(
        p
        for p in git(repo, "ls-tree", "-r", "-z", "--name-only", sha).stdout.split("\0")
        if p
    )


def exclude_pathspec(exclude: list[str] | tuple[str, ...] = ()) -> list[str]:
    """The pathspec that leaves *exclude*'s globs out of a diff: none for no
    globs, else the whole tree from the top less each glob, read from the top
    of the repository whatever directory git runs in (`review --exclude`)."""
    if not exclude:
        return []
    return ["--", ":(top)", *(f":(top,exclude,glob){g}" for g in exclude)]


def diff_text(
    repo: Path, base: str, head: str, exclude: list[str] | tuple[str, ...] = ()
) -> str:
    return git(
        repo, "diff", *_DIFF_FLAGS, f"{base}..{head}", *exclude_pathspec(exclude)
    ).stdout


def target_gate(
    resolved: Resolved, diff: str | None, files: list[str] | None = None
) -> GateResult:
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
            return GateResult(
                passed=False,
                reason=f"no file {where} at {resolved.head_sha[:12]} is left to "
                f"review (see skipped.txt)",
                action="exit",
            )
        return GateResult(passed=True)
    if not diff or not diff.strip():
        return GateResult(
            passed=False,
            reason="the diff between base and head is empty",
            action="exit",
        )
    return GateResult(passed=True)


def _check_attrs(repo: Path, head: str, paths: list[str]) -> dict[str, str]:
    """{path: the attribute set} for paths with linguist-generated or -vendored set at *head*.

    `git check-attr --source` needs git 2.40; an older git skips this check
    with a warning rather than failing the review.
    """
    if not paths:
        return {}
    proc = subprocess.run(
        [
            *GIT_ARGV,
            "-C",
            str(repo),
            "check-attr",
            "-z",
            "--stdin",
            "--source",
            head,
            *GENERATED_ATTRS,
        ],
        input="\0".join(paths) + "\0",
        capture_output=True,
        text=True,
        env=git_env(),
        shell=False,
        check=False,
    )
    if proc.returncode != 0:
        log.warning(
            "git check-attr --source failed; not skipping generated files: %s",
            proc.stderr.strip()[:300],
        )
        return {}
    parts = proc.stdout.split("\0")
    out: dict[str, str] = {}
    for i in range(0, len(parts) - 2, 3):
        path, attr, value = parts[i], parts[i + 1], parts[i + 2]
        if value in ("set", "true") and path not in out:
            out[path] = attr
    return out


def _chars_at(repo: Path, head: str, path: str) -> int:
    """Characters in *path* at *head*, decoded like core `file_at` (bytes that are not UTF-8 count as one each)."""
    proc = subprocess.run(
        [*GIT_ARGV, "-C", str(repo), "show", f"{head}:{path}"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=git_env(),
        shell=False,
        check=False,
    )
    return len(proc.stdout.decode("utf-8", errors="replace"))


def tree_files(
    repo: Path, head: str, path: str = ""
) -> tuple[list[str], list[tuple[str, str]]]:
    """(files to review, [(file, reason) skipped]) at *head*, under *path*, in path order.

    Skipped: binary files, files over TREE_MAX_CHARS characters, lockfiles by
    name, files with linguist-generated or linguist-vendored set, and submodules.
    """
    spec = ["--", path] if path else []
    listed = [
        f
        for f in git(
            repo, "ls-tree", "-r", "-z", "--name-only", head, *spec
        ).stdout.split("\0")
        if f
    ]
    sizes: dict[str, int] = {}
    for rec in git(repo, "ls-tree", "-r", "-l", "-z", head, *spec).stdout.split("\0"):
        meta, _, name = rec.partition("\t")
        fields = meta.split()
        if name and len(fields) == 4 and fields[3].isdigit():
            sizes[name] = int(fields[3])
    binary = set()
    for rec in git(
        repo, "diff", *_DIFF_FLAGS, "--numstat", "-z", EMPTY_TREE, head, *spec
    ).stdout.split("\0"):
        added, _, rest = rec.partition("\t")
        if added == "-":
            binary.add(rest.partition("\t")[2])
    generated = _check_attrs(repo, head, listed)
    keep: list[str] = []
    skipped: list[tuple[str, str]] = []
    for f in sorted(listed):
        if f in binary:
            skipped.append((f, "binary"))
        elif (
            sizes.get(f, 0) > TREE_MAX_CHARS
            and _chars_at(repo, head, f) > TREE_MAX_CHARS
        ):
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


def build_target(
    resolved: Resolved,
    *,
    tickets: list[str] | None = None,
    deployed_in: str | None = None,
    name: str | None = None,
    label: str | None = None,
    needs: list[Need] | None = None,
    exclude: list[str] | tuple[str, ...] = (),
) -> TargetInfo:
    """Everything about the target except files written to disk.

    A change's *exclude* globs (`review --exclude`) leave matching paths out
    of `files`; the paths the change touches there go to `excluded_files`.

    The slug starts with `slug_repo_name()` (`<owner>-<repo>` from origin), the
    label with the directory name. *name* replaces both (a `--dir` review
    names the source directory, not its copy); *label* replaces
    the label outright. Every lookup that failed, here or in `resolve`, is
    appended to *needs*.
    """
    if needs is not None:
        needs.extend(resolved.needs)
    repo, base, head = resolved.repo, resolved.base_sha, resolved.head_sha
    if resolved.kind == "tree":
        files, skipped = tree_files(repo, head, resolved.path)
        repo_name = name or repo.name
        slug_name = name or slug_repo_name(repo)
        slug = sanitize_slug(
            f"{slug_name}--tree" + (f"-{resolved.path}" if resolved.path else "")
        )
        what = resolved.path or "whole tree"
        return TargetInfo(
            repo_path=str(repo),
            remote_url=remote_url(repo),
            base_sha=base,
            head_sha=head,
            commits=[],
            files=files,
            skipped=skipped,
            slug=slug,
            label=label or f"{repo_name}: {what} at {head[:8]}",
            kind="tree",
            ref=resolved.ref,
            tickets=list(tickets or []),
            deployed_in=deployed_in,
        )
    commits = []
    for line in git(
        repo, "log", "--format=%H%x00%s", f"{base}..{head}"
    ).stdout.splitlines():
        if "\0" in line:
            sha, subject = line.split("\0", 1)
            commits.append((sha, subject))
    files = [
        f
        for f in git(
            repo,
            "diff",
            *_DIFF_FLAGS,
            "--name-only",
            f"{base}..{head}",
            *exclude_pathspec(exclude),
        ).stdout.splitlines()
        if f
    ]
    excluded_files = []
    if exclude:
        kept = set(files)
        excluded_files = [
            f
            for f in git(
                repo, "diff", *_DIFF_FLAGS, "--name-only", f"{base}..{head}"
            ).stdout.splitlines()
            if f and f not in kept
        ]
    ref = f"pr-{resolved.pr}" if resolved.kind == "pr" else resolved.ref
    slug = sanitize_slug(f"{slug_repo_name(repo)}--{ref}")
    label = (
        f"{repo.name} {('PR #' + str(resolved.pr)) if resolved.pr else resolved.ref}"
    )
    return TargetInfo(
        repo_path=str(repo),
        remote_url=remote_url(repo),
        base_sha=base,
        head_sha=head,
        commits=commits,
        files=files,
        slug=slug,
        label=label,
        kind=resolved.kind,
        ref=resolved.ref,
        pr=resolved.pr,
        tickets=list(tickets or []),
        deployed_in=deployed_in,
        title=resolved.title,
        description=resolved.description,
        base_ref=resolved.base_ref,
        branch_rules=branch_rules(repo, resolved.base_ref, needs)
        if resolved.base_ref
        else None,
        excluded=list(exclude),
        excluded_files=excluded_files,
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
        "".join(f"{sha} {subject}\n" for sha, subject in target.commits),
        encoding="utf-8",
    )
    (target_dir / "files.txt").write_text(
        "".join(f"{f}\n" for f in target.files), encoding="utf-8"
    )
    if target.kind == "tree":
        (target_dir / "skipped.txt").write_text(
            "".join(f"{f}\t{why}\n" for f, why in target.skipped), encoding="utf-8"
        )
    return target.model_copy(update={"diff_path": str(diff_path)})


_IMPORT_ENV = {
    "GIT_AUTHOR_NAME": "review_pipeline",
    "GIT_AUTHOR_EMAIL": "review@localhost",
    "GIT_COMMITTER_NAME": "review_pipeline",
    "GIT_COMMITTER_EMAIL": "review@localhost",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def dir_name(src: Path) -> str:
    """The name a `--dir` review uses for *src* in its slug: `<basename>-<6 hex of its absolute path>`.

    Two directories with the same basename get different slugs, so one
    review never replaces the other's `source/` repository.
    """
    return f"{src.name}-{hashlib.sha256(str(src).encode()).hexdigest()[:6]}"


# --- where a review is written: <out>/<repo>/<object>/ ------------------------------
#
# The slug (`build_target`) is unchanged and still names the review inside its
# files; only the folder is built from these. `review_pipeline.orchestrator.prepare`
# chooses the folder, and review-viewer's `gitdata` restates `REVIEW_FILES`,
# `NOT_OBJECT_NAMES` and `NOT_NESTED_NAMES` (no import between the two packages).

# The names the review root itself uses (`legacy/` for migrate's copies, `.locks/`
# for run locks): a repository with one of them is renamed `<name>@<owner>`.
TAKEN_REPO_NAMES = frozenset({"legacy", ".locks"})
# A folder holding any of these is a review folder (`write_target_files` writes
# `files.txt` on every run, before any stage).
REVIEW_FILES = ("findings.json", "review-state.json", "files.txt")
# Children of a repo folder that are never object folders. `tree` is not here: it
# is the whole-tree object's name.
NOT_OBJECT_NAMES = frozenset(
    {"rounds", "viewer", "base-tree", "assess-tree", "source", ".git", ".locks"}
)
# A review folder's own children that never hold another review: its rounds,
# viewer state and snapshots.
NOT_NESTED_NAMES = NOT_OBJECT_NAMES | {"tree"}


def object_name(resolved: Resolved) -> str:
    """The folder of one reviewed thing under its repo folder: `pr-<n>`, `branch-<ref>`,
    `range-<a>..<b>`, `tree`, or `tree-<path>` for one directory."""
    if resolved.kind == "pr":
        return sanitize_slug(f"pr-{resolved.pr}")
    if resolved.kind == "tree":
        return sanitize_slug(f"tree-{resolved.path}") if resolved.path else "tree"
    return sanitize_slug(f"{resolved.kind}-{resolved.ref}")


def repo_part(name: str, owner: str | None) -> str:
    """The repo folder's name: *name* cleaned, `@<owner>` (or `@local`) added when it is
    `legacy` or `.locks`, the two names the review root already uses."""
    part = sanitize_slug(name)
    return f"{part}@{owner or 'local'}" if part in TAKEN_REPO_NAMES else part


def checkout_repo_part(repo: Path) -> tuple[str, str | None]:
    """`(repo folder name, owner)` from *repo*'s `origin`; with no origin (or one that
    names no owner), the directory name and None."""
    pair = origin_owner_repo(remote_url(repo))
    if pair:
        return repo_part(pair[1], pair[0]), pair[0]
    return repo_part(Path(repo).name, None), None


def dir_object(src: Path) -> tuple[str, str]:
    """`(repo folder name, object name)` of a `--dir` review of *src*:
    `(<basename>, dir-<basename>-<6 hex>)`. The hash of the absolute path keeps two
    directories with one basename apart."""
    return repo_part(src.name, None), "dir-" + sanitize_slug(dir_name(src))


def is_review_folder(path: Path) -> bool:
    """True when *path* holds a file only a review writes (`REVIEW_FILES`).
    A dot file such as `.DS_Store` never makes a folder a review folder."""
    return any((path / name).is_file() for name in REVIEW_FILES)


def review_children(out_dir: Path) -> list[Path]:
    """*out_dir*'s child folders that may hold a review, sorted. When *out_dir* is
    itself a review folder (a new-layout folder a 0.42 run was given as `--out`),
    its own `rounds/`, `viewer/` and snapshots (`NOT_NESTED_NAMES`) are left out."""
    if not out_dir.is_dir():
        return []
    own = is_review_folder(out_dir)
    return sorted(
        p
        for p in out_dir.iterdir()
        if p.is_dir() and not (own and p.name in NOT_NESTED_NAMES)
    )


def object_folders(out_dir: Path) -> list[Path]:
    """Every `<out>/<repo>/<object>/` folder under *out_dir*, sorted.

    Where a name sits decides what it is, not the name alone: a repo may be
    called `tree` or `rounds`, and the whole-tree object is `tree`. The first
    level skips `legacy/`, `.locks/` and partial copies, and any review folder
    (a 0.46 `<out>/<its slug>/`), and, when *out_dir* is itself a review folder,
    its own rounds, viewer state and snapshots (`review_children`). Under a repo
    folder, every child folder is an object except the `NOT_OBJECT_NAMES` and
    partial copies. A review an older version nested inside an object folder is
    not listed.
    """
    found: list[Path] = []
    for repo in review_children(out_dir):
        if (
            repo.name in TAKEN_REPO_NAMES
            or repo.name.startswith(".partial-")
            or is_review_folder(repo)
        ):
            continue
        found.extend(
            obj
            for obj in sorted(repo.iterdir())
            if obj.is_dir()
            and obj.name not in NOT_OBJECT_NAMES
            and not obj.name.startswith(".partial-")
        )
    return found


def _import_git(dest: Path, src: Path, *args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [*GIT_ARGV, "-C", str(dest), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=dict(git_env(), **_IMPORT_ENV),
        shell=False,
        check=False,
    )
    ok = proc.returncode == 0 or (
        args[0] == "diff" and proc.returncode == 1
    )  # diff --quiet: 1 = changed
    if not ok:
        raise TargetError(
            f"git {args[0]} in the copy of {src} failed: {proc.stderr.strip()}"
        )
    return proc


def check_out_dir(src: str | Path, out: Path) -> None:
    """Raise TargetError when *out* is *src* or inside it: a `--dir` review never writes to its source."""
    src, out = Path(src).expanduser().resolve(), Path(out).expanduser().resolve()
    if out == src or src in out.parents:
        raise TargetError(
            f"the output directory {out} is inside {src}; pass --out somewhere else"
        )


def import_dir(path: str | Path, target_dir: Path) -> Path:
    """Copy a directory that is not under git into `<target_dir>/source/` and commit it there.

    The copy leaves out IMPORT_EXCLUDES (and any `.git`), and `git add` honours
    a `.gitignore` copied with it. Commits have a fixed author and no user git
    config. Nothing is written to *path*. Returns the copy, which is then
    reviewed as `--tree HEAD`. The copy is not `<target_dir>/tree/`: that is
    the agents' snapshot, deleted when a run ends.

    An earlier import is kept, so the commit an earlier review's
    `findings.json` and `review-state.json` name stays readable: the working
    files of `source/` are replaced with a fresh copy, and a new commit is
    added on top of the old one only when the contents changed. When they did
    not, HEAD is reused and the review gets the same head sha.
    """
    src = Path(path).expanduser().resolve()
    if not src.is_dir():
        raise TargetError(f"{src} is not a directory")
    inside = git(src, "rev-parse", "--show-toplevel", check=False)
    if inside.returncode == 0 and inside.stdout.strip():
        top = Path(inside.stdout.strip()).resolve()
        rel = src.relative_to(top).as_posix() if src != top else "."
        raise TargetError(
            f"{src} is inside the git repository {top}; review it with "
            f"--repo {top} --tree --path {rel}"
        )
    check_out_dir(src, target_dir)
    dest = (target_dir / "source").resolve()
    kept = (dest / ".git").is_dir()
    try:
        if dest.exists() and not kept:
            shutil.rmtree(
                dest
            )  # a copy that was never committed holds nothing a review names
        for child in dest.iterdir() if kept else ():
            if child.name == ".git":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            src,
            dest,
            symlinks=True,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(*IMPORT_EXCLUDES, ".git"),
        )
    except OSError as e:  # shutil.Error is an OSError
        raise TargetError(f"cannot copy {src} into {dest}: {e}") from e
    if not kept:
        _import_git(dest, src, "init", "-q", "-b", "main")
    _import_git(dest, src, "add", "-A")
    if kept and _import_git(dest, src, "diff", "--cached", "--quiet").returncode == 0:
        return dest  # same contents as the last import: keep its commit
    _import_git(
        dest,
        src,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        f"Import {src.name} for review",
    )
    return dest


def deployed(target: TargetInfo) -> str | None:
    """ "yes"/"no" for `--deployed-in <name>`: is head an ancestor of origin/<name>?"""
    if not target.deployed_in:
        return None
    ref = rev_parse(target.repo_path, f"origin/{target.deployed_in}")
    if ref is None:
        return f"unknown (origin/{target.deployed_in} not found)"
    proc = git(
        target.repo_path,
        "merge-base",
        "--is-ancestor",
        target.head_sha,
        ref,
        check=False,
    )
    return "yes" if proc.returncode == 0 else "no"


def _init_snapshot_repo(dest: Path) -> None:
    """`git init` in *dest*, so search tools treat the snapshot as a repository of its own.

    The snapshot sits under the data directory, whose `.gitignore` is `*`,
    inside whatever repository holds that directory. ripgrep (and so the
    agents' Grep) follows the ignore files of parent directories up to the
    repository's root, so a directory search there listed no file. With its own
    `.git`, ripgrep stops at *dest*; the snapshot's own `.gitignore` still applies.
    """
    try:
        proc = subprocess.run(
            [*GIT_ARGV, "init", "-q", str(dest)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=git_env(),
            shell=False,
            check=False,
        )
    except OSError as e:
        raise TargetError(f"git init {dest} failed: {e}") from e
    if proc.returncode != 0:
        raise TargetError(
            f"git init {dest} failed: {proc.stderr.decode(errors='replace').strip()}"
        )


def _hidden_by(dest: Path) -> str:
    """Which ignore file hides *dest* from its parent's repository (`git check-ignore -v`), in words."""
    try:
        proc = subprocess.run(
            [*GIT_ARGV, "-C", str(dest.parent), "check-ignore", "-v", str(dest)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=git_env(),
            shell=False,
            check=False,
        )
    except OSError:
        return "git check-ignore could not run"
    rule = proc.stdout.decode(errors="replace").strip().split("\t")[0]
    if proc.returncode != 0 or not rule:
        return "git check-ignore names no ignore file that hides it"
    source, _, rest = rule.partition(":")
    line, _, pattern = rest.partition(":")
    return f"ignored by `{pattern}` at {source}:{line}"


def _check_search(
    target: TargetInfo, dest: Path, note: Callable[[str], None] | None
) -> None:
    """Raise TargetError when ripgrep lists no file in *dest* while the target has changed files.

    Without ripgrep on PATH it writes one line through *note* (else the log) and returns.
    """
    rg = shutil.which("rg")
    if rg is None:
        line = f"NOTICE ripgrep (rg) not found: not checked that search sees the snapshot {dest}"
        if note is not None:
            note(line)
        else:
            log.warning(line)
        return
    if not target.files:
        return
    try:
        proc = subprocess.run(
            [rg, "--files", str(dest)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            shell=False,
            check=False,
        )
    except OSError as e:
        raise TargetError(f"rg --files {dest} failed: {e}") from e
    if not proc.stdout.strip():
        raise TargetError(
            f"search sees no file in the snapshot {dest} (rg --files lists nothing): {_hidden_by(dest)}"
        )


def snapshot_head(
    target: TargetInfo, dest: Path, note: Callable[[str], None] | None = None
) -> Path:
    """Extract the tree at head_sha into *dest* for the agents to read.

    The agents' Read/Grep/Glob tools see a directory, not a commit, and the
    repo's working tree is whatever happens to be checked out. The gates check
    quotes against `git show <head_sha>:<path>`, so the agents must read the
    same bytes. `git archive` reads the object store and writes nothing to the
    repo. *dest* is then made a git work tree (`_init_snapshot_repo`) and
    ripgrep must list a file in it (`_check_search`) before the `.review-head`
    marker is written; a snapshot with the marker and no `.git` gets only the
    init. *note* takes the one line written when ripgrep is not installed.
    """
    marker = dest / ".review-head"
    if (
        marker.exists()
        and marker.read_text(encoding="utf-8").strip() == target.head_sha
    ):
        if not (dest / ".git").exists():
            marker.unlink()  # a failed init or check leaves no marker, as on a first extract
            _init_snapshot_repo(dest)
            _check_search(target, dest, note)
            marker.write_text(target.head_sha + "\n", encoding="utf-8")
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    proc = subprocess.run(
        [*GIT_ARGV, "-C", target.repo_path, "archive", "--format=tar", target.head_sha],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=git_env(),
        shell=False,
        check=False,
    )
    if proc.returncode != 0:
        raise TargetError(
            f"git archive {target.head_sha[:12]} failed: {proc.stderr.decode(errors='replace').strip()}"
        )
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tar:
        tar.extractall(dest, filter="data")
    _init_snapshot_repo(dest)
    _check_search(target, dest, note)
    (dest / ".review-head").write_text(target.head_sha + "\n", encoding="utf-8")
    return dest


# Bytes of paths one `git archive` is given. A PR that deletes many files would
# otherwise overflow the OS argv limit (E2BIG); 64 KiB is well inside Linux's
# and macOS's, environment included.
ARCHIVE_ARGV_BYTES = 64 * 1024


def _archive_batches(paths: list[str]) -> list[list[str]]:
    """Split *paths* into lists of at most ARCHIVE_ARGV_BYTES each; a longer path goes alone."""
    batches: list[list[str]] = []
    batch: list[str] = []
    size = 0
    for p in paths:
        n = len(os.fsencode(p)) + 1
        if batch and size + n > ARCHIVE_ARGV_BYTES:
            batches.append(batch)
            batch, size = [], 0
        batch.append(p)
        size += n
    if batch:
        batches.append(batch)
    return batches


def snapshot_base(target: TargetInfo, dest: Path, paths: list[str]) -> Path:
    """Extract *paths* as they are at base_sha into *dest*, replacing what is there.

    The agents' working directory is the head snapshot. The explain stage's
    agents also read lines a change removed, so those files are extracted at
    base, beside it, with `git archive` (which writes nothing to the repo), in
    batches that fit argv, and made a git work tree (`_init_snapshot_repo`), so
    search sees it. Any failure, an OSError included, is a TargetError.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for batch in _archive_batches(paths):
        try:
            proc = subprocess.run(
                [
                    *GIT_ARGV,
                    "-C",
                    target.repo_path,
                    "archive",
                    "--format=tar",
                    target.base_sha,
                    "--",
                    *batch,
                ],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                env=git_env(),
                shell=False,
                check=False,
            )
        except OSError as e:
            # E2BIG, or git missing
            raise TargetError(f"git archive {target.base_sha[:12]} failed: {e}") from e
        if proc.returncode != 0:
            raise TargetError(
                f"git archive {target.base_sha[:12]} failed: {proc.stderr.decode(errors='replace').strip()}"
            )
        try:
            with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tar:
                tar.extractall(dest, filter="data")
        except (tarfile.TarError, OSError) as e:
            # The "data" filter refuses, for one, a symlink that is absolute or points outside dest;
            # writing the files can fail with ENOSPC or ENAMETOOLONG.
            raise TargetError(f"extracting {target.base_sha[:12]} failed: {e}") from e
    _init_snapshot_repo(dest)
    return dest
