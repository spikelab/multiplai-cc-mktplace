"""The row-test helpers compare what they should and nothing else."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from build_pipeline import git_ops
from tests.row_helpers import assert_same_as_unstopped, run_killed_at


def _repo(path: Path, date: str) -> Path:
    path.mkdir(parents=True)
    env = {**os.environ, "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date}

    def git(*argv: str) -> None:
        subprocess.run(["git", *argv], cwd=path, check=True, capture_output=True, env=env)

    git("-c", "init.defaultBranch=main", "init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    (path / "app.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    (path / "app.py").write_text("x = 2\n")
    git("commit", "-qam", "step")
    (path / "notes.txt").write_text("uncommitted\n")
    return path


def test_commits_made_at_different_times_compare_equal(tmp_path):
    a = _repo(tmp_path / "a", "2026-10-13T10:00:00")
    b = _repo(tmp_path / "b", "2026-10-13T11:30:05")
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=a, capture_output=True, text=True).stdout \
        != subprocess.run(["git", "rev-parse", "HEAD"], cwd=b, capture_output=True, text=True).stdout
    assert_same_as_unstopped(a, b)


def test_a_changed_byte_in_a_project_file_fails(tmp_path):
    a = _repo(tmp_path / "a", "2026-10-13T10:00:00")
    b = _repo(tmp_path / "b", "2026-10-13T10:00:00")
    (b / "notes.txt").write_text("uncommitted!\n")
    with pytest.raises(AssertionError):
        assert_same_as_unstopped(a, b)


def test_run_killed_at_stops_the_child_at_the_named_git_call(tmp_path):
    repo = _repo(tmp_path / "a", "2026-10-13T10:00:00")
    run_killed_at("status", lambda: git_ops.tree_is_clean(repo))
