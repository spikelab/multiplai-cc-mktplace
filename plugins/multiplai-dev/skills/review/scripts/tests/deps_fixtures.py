"""Repositories for the dependency tests: a reviewed repo whose uv.lock pins
git dependencies, and local bare repositories those pins point at."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from review_pipeline.models import TargetInfo

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
                          env=ENV).stdout.strip()


def commit_files(repo: Path, files: dict[str, str], message: str = "c") -> str:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def init(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    return repo


def dependency(root: Path, name: str, files: dict[str, str]) -> tuple[str, str, Path]:
    """A dependency repo with one commit of *files*, served from a bare clone.

    Returns (file:// URL of the bare repo, the commit, the work repo for later commits).
    """
    work = init(root / f"{name}-work")
    sha = commit_files(work, files, f"{name} v1")
    bare = root / f"{name}.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True, capture_output=True)
    return f"file://{bare}", sha, work


def push(work: Path, bare_url: str) -> None:
    git(work, "push", "-q", bare_url.removeprefix("file://"), "main")


def lock_entry(name: str, source: str) -> str:
    return f'[[package]]\nname = "{name}"\nversion = "1.0"\nsource = {source}\n\n'


def git_lock_entry(name: str, url: str, sha: str) -> str:
    return lock_entry(name, f'{{ git = "{url}?branch=main#{sha}" }}')


def reviewed_repo(root: Path, lock_files: dict[str, str], extra: dict[str, str] | None = None) -> TargetInfo:
    """A repo whose head commit holds *lock_files* (path -> uv.lock text)."""
    repo = init(root / "reviewed")
    base = commit_files(repo, {"README.md": "x\n"}, "base")
    head = commit_files(repo, {**lock_files, **(extra or {}), "src/app.py": "print(1)\n"}, "head")
    return TargetInfo(repo_path=str(repo), base_sha=base, head_sha=head, slug="reviewed", files=["src/app.py"])
