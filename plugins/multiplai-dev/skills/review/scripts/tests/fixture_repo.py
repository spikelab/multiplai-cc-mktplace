"""Build the DB2038 regression repository (fixtures/settings_consumer_repo/) in a temp dir.

Two commits: `base/` then `head/`. Author, committer and dates are fixed, so
the shas are the same on every machine. `origin/main` points at base and
`origin/feature` at head, as if fetched; there is no real remote.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

TREES = Path(__file__).resolve().parent / "fixtures" / "settings_consumer_repo"

# Line numbers the tests cite, at head.
SETTINGS_DEF_LINE = 3        # settings.py: CHANNEX_OC_OTA_NAME = config(...)
DIRECT_BOOKING_USE_LINE = 6  # direct_booking.py: payload['ota_name'] = getattr(settings, ...)
KEYWORD_LINE = 1             # rateplan_service.py: KEYWORD = 'dolcebot'
KEYWORD_USE_LINE = 6         # rateplan_service.py: return [rp for rp ... KEYWORD in ...]

_ENV = {
    "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.com",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
}


def _git(repo: Path, *args: str, date: str = "2026-09-17T10:00:00Z") -> str:
    env = dict(os.environ, **_ENV, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def _commit(repo: Path, tree: Path, message: str, date: str) -> str:
    for child in repo.iterdir():
        if child.name != ".git":
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    shutil.copytree(tree, repo, dirs_exist_ok=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message, date=date)
    return _git(repo, "rev-parse", "HEAD")


def build(repo: Path) -> tuple[str, str]:
    """Create the repository at *repo*; return (base_sha, head_sha)."""
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    base = _commit(repo, TREES / "base", "Initial import", "2026-09-17T10:00:00Z")
    head = _commit(repo, TREES / "head", "DB-2038: direct booking from the modal", "2026-09-17T11:00:00Z")
    _git(repo, "update-ref", "refs/remotes/origin/main", base)
    _git(repo, "update-ref", "refs/remotes/origin/feature/db-2038", head)
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _git(repo, "remote", "add", "origin", "https://github.com/example/booking-engine.git")
    return base, head
