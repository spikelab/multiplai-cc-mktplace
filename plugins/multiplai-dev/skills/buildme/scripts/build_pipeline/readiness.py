"""buildme builds only from ready specs (pm-spec's handoff).

pm-spec writes a change's "what" documents (proposal, use cases,
requirements) and marks them ready in `.change.yaml`
(`multiplai_core.specs.is_ready`). buildme reads them and never writes them.
This module holds what a build prints and returns when it cannot go on
because of them:

- `SPEC:NOT_READY:<reason>` and the line naming the pm-spec command, exit 2:
  the specs are not marked ready, changed after they were, or (for a build
  that branches from them) are not all committed.
- `SPEC:ISSUES:<path>`, exit 4: buildme found a problem in the requirements
  and wrote it to `spec-issues.md` for `pm-spec revise`; `.change.yaml`
  `spec_issues` names the file until pm-spec clears it.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from multiplai_core.specs import read_change_meta, set_spec_issues

from . import git_ops

MARKER_NOT_READY = "SPEC:NOT_READY"
MARKER_ISSUES = "SPEC:ISSUES"
EXIT_NOT_READY = 2
# Shared with `tdd_engine.EXIT_AGENT_TIMEOUT`: the marker on stdout tells the two apart.
EXIT_SPEC_ISSUES = 4
ISSUES_FILE = "spec-issues.md"


class SpecIssuesFound(Exception):
    """A build step found a problem in the requirements that only a change to
    them can fix. *issues* are `(title, body)` pairs, one `## <title>` section
    each in spec-issues.md; *step* names who found them."""

    def __init__(self, issues: list[tuple[str, str]], *, step: str):
        self.issues = list(issues)
        self.step = step
        super().__init__(f"{step}: {len(self.issues)} requirements issue(s)")


def write_issues(change_dir: Path, issues: list[tuple[str, str]], *, change: str) -> Path:
    """Append *issues* to the change's spec-issues.md, one `## <title>` section
    each (the form `pm-spec revise` reads: one issue per section), and set
    `.change.yaml` `spec_issues` to it. A new file gets a header first. A
    section already in the file, same title and body, is not written twice,
    so a step that stopped after writing and runs again adds nothing."""
    path = Path(change_dir) / ISSUES_FILE
    text = path.read_text() if path.is_file() else (
        f"# Spec issues: {change}\n\nRequirements problems buildme found. Fix them with "
        f"`/multiplai-pm:pm-spec revise {change}`, then run the build again.\n")
    for title, body in issues:
        section = f"## {' '.join(title.split())}\n\n{body.strip()}\n"
        if section not in text:
            text = text.rstrip("\n") + "\n\n" + section
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    tmp.replace(path)
    set_spec_issues(change_dir, path)
    return path


def stop_on_issues(change_dir: Path, found: SpecIssuesFound, *, change: str) -> int:
    """Write what *found* carries to spec-issues.md and refuse with
    `SPEC:ISSUES:<path>`; the exit code (4)."""
    return refuse_issues(write_issues(change_dir, found.issues, change=change))


def change_project(change_dir: Path) -> Path | None:
    """The repository *change_dir* belongs to when it is
    `<repo>/specs/changes/<change>`, else None."""
    change_dir = Path(change_dir)
    if change_dir.parent.name == "changes" and change_dir.parent.parent.name == "specs":
        return change_dir.parent.parent.parent
    return None


def pm_spec_line(name: str, *, resume: bool = False, project_dir: Path | None = None) -> str:
    where = f" --project-dir {project_dir}" if project_dir else ""
    return f"Run /multiplai-pm:pm-spec {'resume ' if resume else ''}{name}{where} first"


def refuse_not_ready(name: str, reason: str, *, resume: bool = False,
                     project_dir: Path | None = None, advice: str | None = None) -> int:
    """Print `SPEC:NOT_READY:<reason>` and the pm-spec line; the exit code (2).
    *resume* names `pm-spec resume <name>`, which checks specs already on disk
    and marks them ready without rewriting them. *project_dir* adds
    `--project-dir`: the specs the build reads are in that checkout (a
    worktree build's own worktree), not the one the user runs in. *advice*,
    when given, is printed in place of the pm-spec line: what to do is not a
    pm-spec run."""
    reason = " ".join(str(reason).split())
    print(f"{MARKER_NOT_READY}:{reason}", flush=True)
    print(advice or pm_spec_line(name, resume=resume, project_dir=project_dir), flush=True)
    return EXIT_NOT_READY


def open_issues(change_dir: Path) -> Path | None:
    """The open `spec-issues.md` that `.change.yaml` `spec_issues` names, or
    None when none is open. A relative record is relative to the change dir."""
    try:
        value = read_change_meta(change_dir).get("spec_issues")
    except (OSError, UnicodeError, ValueError):
        return None
    if not value:
        return None
    path = Path(str(value))
    return path if path.is_absolute() else Path(change_dir) / path


def refuse_issues(path: Path) -> int:
    """Print `SPEC:ISSUES:<path>` and the pm-spec revise line; the exit code (4).

    The file sits in the change dir the build reads, which for a worktree
    build is the build's worktree, so the line names that repository with
    `--project-dir` when the change dir is `<repo>/specs/changes/<change>`."""
    path = Path(path)
    print(f"{MARKER_ISSUES}:{path}", flush=True)
    change_dir = path.parent
    repo = change_project(change_dir)
    where = f" --project-dir {repo}" if repo else ""
    print(f"Run /multiplai-pm:pm-spec revise {change_dir.name}{where}, then run the build again",
          flush=True)
    return EXIT_SPEC_ISSUES


def uncommitted(project_dir: Path, path: Path) -> list[str]:
    """The paths under *path* that differ from HEAD (staged, unstaged or
    untracked), buildme's own bookkeeping files left out. Raises
    `git_ops.GitLifecycleError` when git cannot answer."""
    project_dir = Path(project_dir)
    try:
        rel = Path(path).resolve().relative_to(project_dir.resolve()).as_posix() or "."
    except ValueError:
        raise git_ops.GitLifecycleError(f"{path} is not inside {project_dir}") from None
    res = git_ops._run(["git", "status", "--porcelain", "--untracked-files=all", "--", rel,
                        *git_ops.BOOKKEEPING_EXCLUDES], cwd=project_dir)
    if not res.ok:
        raise git_ops.GitLifecycleError(f"{res.command} failed: {res.stderr.strip()[-300:]}")
    return [line[3:] for line in res.stdout.splitlines() if line.strip()]



def saved_revision(change_dir: Path) -> str | None:
    """The `spec_revision` `.change.yaml` records (what was marked ready), or
    None when there is none or it cannot be read."""
    try:
        value = read_change_meta(change_dir).get("spec_revision")
    except (OSError, UnicodeError, ValueError):
        return None
    return str(value) if value else None


def specs_at(repo: Path, rev: str, rel: str, dest: Path) -> Path | None:
    """Write the change dir *rel* (relative to *repo*, POSIX form) as commit
    *rev* holds it to `<dest>/<rel>`, byte for byte, and return that copy:
    what a worktree cut from *rev* would hold. None when *rev* does not hold
    *rel*. Raises `git_ops.GitLifecycleError` when git cannot read *rev*."""
    res = git_ops._run(["git", "ls-tree", "-r", "-z", rev, "--", f"{rel}/"], cwd=repo,
                       errors="surrogateescape")
    if not res.ok:
        raise git_ops.GitLifecycleError(f"{res.command} failed: {res.stderr.strip()[-300:]}")
    entries = [e for e in res.stdout.split("\0") if e]
    if not entries:
        return None
    for entry in entries:
        meta, _, path = entry.partition("\t")
        _mode, kind, oid = meta.split()
        if kind != "blob":
            continue
        blob = subprocess.run(["git", "cat-file", "blob", oid], cwd=repo, capture_output=True,
                              timeout=git_ops._GIT_TIMEOUT)
        if blob.returncode != 0:
            raise git_ops.GitLifecycleError(f"git cat-file blob {oid} failed in {repo}")
        target = Path(dest) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob.stdout)
    return Path(dest) / rel


def saved_revision_at(repo: Path, rev: str, rel: str) -> str | None:
    """The `spec_revision` the change dir *rel*'s `.change.yaml` records at
    commit *rev*; None when it has none or git cannot read it."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            copy = specs_at(repo, rev, rel, Path(tmp))
            return saved_revision(copy) if copy is not None else None
    except (git_ops.GitLifecycleError, OSError, subprocess.SubprocessError, ValueError):
        return None
