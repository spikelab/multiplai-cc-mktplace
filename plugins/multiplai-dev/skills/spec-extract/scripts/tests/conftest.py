"""Shared fixtures: real git repositories in temporary directories, a fixed clock,
and a stand-in for `gh pr view`. No test calls `gh` or a model."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from spec_extract_pipeline import __main__ as cli
from spec_extract_pipeline import target as target_mod

FIXED = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
SCRIPTS_DIR = Path(__file__).resolve().parent.parent

_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_AUTHOR_DATE": "1700000000 +0000", "GIT_COMMITTER_DATE": "1700000000 +0000",
}


class Killed(BaseException):
    """Stands in for a kill: a BaseException, so no `except Exception` in the code catches it."""


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env=dict(os.environ, **_GIT_ENV), check=True)
    return proc.stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_repo(path: Path, origin: str | None = "https://github.com/acme/widgets.git") -> Path:
    """A repository with one commit holding a few Python files, and `origin` set (not fetched)."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    write(path / "src" / "billing" / "charge.py", "def charge(amount):\n    return amount\n")
    write(path / "src" / "a" / "one.py", "def one():\n    return 1\n")
    write(path / "src_a" / "two.py", "def two():\n    return 2\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    if origin:
        git(path, "remote", "add", "origin", origin)
    return path


def commit_file(repo: Path, rel: str, text: str, message: str = "change") -> str:
    write(repo / rel, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def run(*argv: str | Path, now: datetime = FIXED) -> int:
    return cli.main([str(a) for a in argv], now=lambda: now)


def read_state(run_dir: Path) -> dict:
    return json.loads((run_dir / "state.json").read_text(encoding="utf-8"))


def fake_state(run_dir: Path, **target) -> None:
    """A `state.json` another run wrote, holding only the `target` fields given."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(json.dumps({"target": target, "steps_done": []}), encoding="utf-8")


def outputs(run_dir: Path) -> dict[str, bytes]:
    """Every file under *run_dir* a resumed run must reproduce byte for byte: all but
    `state.json`, `.state-*.tmp`, `snapshot/` and `source/.git/`."""
    out = {}
    for p in sorted(run_dir.rglob("*")):
        rel = p.relative_to(run_dir).as_posix()
        if not p.is_file() or rel == "state.json" or rel.startswith(("snapshot/", "source/.git/")):
            continue
        if "/" not in rel and rel.startswith(".state-") and rel.endswith(".tmp"):
            continue
        out[rel] = p.read_bytes()
    return out


def everything(root: Path) -> dict[str, bytes | None]:
    """Every path under *root*, files with their bytes: what "writes nothing" compares."""
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


def source_head(run_dir: Path) -> str | None:
    src = run_dir / "source"
    return git(src, "rev-parse", "HEAD") if (src / ".git").is_dir() else None


@pytest.fixture
def pr_view(monkeypatch):
    """Replace `gh pr view`: set `pr_view.info[n] = {"headRefOid": …, "baseRefOid": …}`."""
    class View:
        info: dict[int, dict] = {}
        calls: list[int] = []

    view = View()
    view.info, view.calls = {}, []

    def fake(repo, number):
        view.calls.append(number)
        return dict(view.info[number])

    monkeypatch.setattr(target_mod, "_pr_view", fake)
    return view
