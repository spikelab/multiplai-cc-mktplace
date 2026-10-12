"""A two-commit git repo the citation checks re-read from. No test calls a model."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

BASE_APP = "def greet(name):\n    return 'hi ' + name\n"
HEAD_APP = "import logging\n\n\ndef greet(name):\n    logging.info('greeting')\n    return 'hello ' + name\n"


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """(repo path, base sha, head sha): app/main.py changes between the commits."""
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    (root / "app" / "main.py").write_text(BASE_APP)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "app" / "main.py").write_text(HEAD_APP)
    (root / "app" / "util.py").write_text("X = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "head")
    head = _git(root, "rev-parse", "HEAD")
    return str(root), base, head


@pytest.fixture
def resolve(repo):
    """Repo-relative paths; the side picks the commit."""
    path, base, head = repo

    def _resolve(p: str, side: str):
        if p.startswith("elsewhere/"):
            return None
        return path, (base if side == "base" else head), p

    return _resolve
