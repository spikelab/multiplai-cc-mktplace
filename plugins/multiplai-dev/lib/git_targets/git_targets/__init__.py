"""Resolve a PR, a commit range or a commit to shas, and list what changed between two commits.

Read-only over the clone: every call is a fixed argv with `shell=False` and
stdin closed. Nothing here checks out, merges or commits. `fetch_origin` is the
one call that writes (remote-tracking refs and objects), and only a caller that
was asked to fetch makes it.

`rev_parse`, `merge_base`, `PR_VIEW_FIELDS`, `pr_view` and `fetch_origin` came
from review's `target.py`, unchanged; review and codebase-walkthrough both
import them from here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from multiplai_core.citations import GIT_ARGV, git_env

_DIFF_FLAGS = ["--no-color", "--no-ext-diff", "--no-textconv"]
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# Bytes of paths one `git archive` is given: a change that deletes many files would
# otherwise overflow the OS argv limit (E2BIG). Review's figure.
ARCHIVE_ARGV_BYTES = 64 * 1024


class GitTargetError(Exception):
    """A target could not be resolved. The message says why."""


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
        raise GitTargetError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def rev_parse(repo: str | Path, ref: str) -> str | None:
    """Full 40-hex sha for *ref*, or None when it does not resolve to a commit."""
    proc = git(
        repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False
    )
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def merge_base(repo: str | Path, a: str, b: str) -> str | None:
    proc = git(repo, "merge-base", a, b, check=False)
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def fetch_origin(repo: str | Path) -> None:
    """`git fetch --quiet origin`: the only call here that writes to the clone."""
    git(repo, "fetch", "--quiet", "origin")


PR_VIEW_FIELDS = "headRefOid,baseRefOid,headRefName,baseRefName,title,body"


def pr_view(repo: Path, number: int) -> dict:
    gh = shutil.which("gh")
    if gh is None:
        raise GitTargetError(
            "--pr needs the GitHub CLI (`gh`), which is not installed. Install it from "
            "https://cli.github.com and run `gh auth login`, or pass --range <base>..<head>."
        )
    proc = subprocess.run(
        [gh, "pr", "view", str(number), "--json", PR_VIEW_FIELDS],
        cwd=repo,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        shell=False,
        check=False,
    )
    if proc.returncode != 0:
        raise GitTargetError(f"gh pr view {number} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


# --- what changed ---------------------------------------------------------------------------


@dataclass
class ChangedFile:
    """One file of `git diff -M base head`. Line ranges are 1-based and inclusive."""

    path: str  # at head; the old path for a deleted file
    status: str  # A, M, D, R, C, T (the letter of --name-status, without the score)
    old_path: str = ""  # at base: differs from path only for R and C
    added: list[tuple[int, int]] = field(default_factory=list)  # head lines
    removed: list[tuple[int, int]] = field(default_factory=list)  # base lines

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "status": self.status,
            "old_path": self.old_path,
            "added": [list(r) for r in self.added],
            "removed": [list(r) for r in self.removed],
        }


def _name_status(repo: str | Path, base: str, head: str) -> list[ChangedFile]:
    out = git(
        repo, "diff", "--name-status", "-z", "-M", *_DIFF_FLAGS, base, head
    ).stdout
    parts = out.split("\0")
    files: list[ChangedFile] = []
    i = 0
    while i < len(parts) and parts[i]:
        letter = parts[i][0]
        if letter in "RC":
            old, new = parts[i + 1], parts[i + 2]
            files.append(ChangedFile(path=new, status=letter, old_path=old))
            i += 3
        else:
            p = parts[i + 1]
            files.append(ChangedFile(path=p, status=letter, old_path=p))
            i += 2
    return files


def _hunks(patch: str) -> list[tuple[list[tuple[int, int]], list[tuple[int, int]]]]:
    """(added, removed) per `diff --git` section of a `-U0` patch, in order."""
    sections: list[tuple[list, list]] = []
    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            sections.append(([], []))
            continue
        m = _HUNK.match(line)
        if m and sections:
            a, b, c, d = m.groups()
            removed_n = 1 if b is None else int(b)
            added_n = 1 if d is None else int(d)
            if removed_n:
                sections[-1][1].append((int(a), int(a) + removed_n - 1))
            if added_n:
                sections[-1][0].append((int(c), int(c) + added_n - 1))
    return sections


def _patch(repo: str | Path, base: str, head: str, *paths: str) -> str:
    spec = ["--", *paths] if paths else []
    return git(repo, "diff", "-U0", "-M", *_DIFF_FLAGS, base, head, *spec).stdout


def changed_files(repo: str | Path, base: str, head: str) -> list[ChangedFile]:
    """Every file `git diff -M base head` lists, with the head lines it adds and the base lines it removes.

    The patch's sections come in --name-status order. When their counts differ (a
    file name git quotes in one listing and not the other), each file is diffed
    on its own instead.
    """
    files = _name_status(repo, base, head)
    sections = _hunks(_patch(repo, base, head))
    if len(sections) != len(files):
        sections = []
        for f in files:
            paths = (f.old_path, f.path) if f.old_path != f.path else (f.path,)
            one = _hunks(_patch(repo, base, head, *paths))
            sections.append(one[0] if one else ([], []))
    for f, (added, removed) in zip(files, sections, strict=True):
        f.added, f.removed = added, removed
    return files


def commit_subjects(repo: str | Path, base: str, head: str) -> list[tuple[str, str]]:
    """(sha, subject) of each commit in `base..head`, oldest first."""
    out = git(repo, "log", "--reverse", "--format=%H%x00%s", f"{base}..{head}").stdout
    return [
        (sha, subject)
        for line in out.splitlines()
        if "\0" in line
        for sha, subject in [line.split("\0", 1)]
    ]


def archive_batches(paths: list[str]) -> list[list[str]]:
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
