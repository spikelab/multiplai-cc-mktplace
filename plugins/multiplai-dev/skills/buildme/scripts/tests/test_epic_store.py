"""Epic mode: the epic directory's name (the review skill's slug rule, pinned
here) and the epic lock (plan-buildme-epic-mode-2026-10-12.md, items 5 and 6)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic import store

from .epic_row_helpers import make_project, use_workspace


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


# --- item 5: the directory name ------------------------------------------------


class TestSlug:
    """Copied from the review skill's slug rule (review_pipeline/target.py,
    `origin_owner_repo` and `slug_repo_name`) and pinned here."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/spikelab/github-tests.git",
            "git@github.com:spikelab/github-tests.git",
            "ssh://git@github.com/spikelab/github-tests",
            "https://github.com/spikelab/github-tests/",
        ],
    )
    def test_owner_and_repo_from_any_remote_form(self, url):
        assert store.origin_owner_repo(url) == ("spikelab", "github-tests")

    def test_a_local_bare_origin_uses_its_last_two_path_parts(self):
        assert store.origin_owner_repo("/tmp/x/o/r.git") == ("o", "r")

    def test_no_url(self):
        assert store.origin_owner_repo(None) is None
        assert store.origin_owner_repo("r") is None

    def test_epic_dir_is_under_the_data_dir(self, tmp_path, monkeypatch):
        data = use_workspace(monkeypatch, tmp_path)
        project = make_project(tmp_path)
        assert store.epic_dir_for(project, "e") == data / "buildme" / "epics" / "o-r--e"

    def test_a_repo_without_origin_uses_its_directory_name(self, tmp_path, monkeypatch):
        data = use_workspace(monkeypatch, tmp_path)
        repo = tmp_path / "plain"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        assert store.epic_dir_for(repo, "e") == data / "buildme" / "epics" / "plain--e"

    def test_its_git_calls_go_through_git_ops_run_with_its_timeout(
        self, tmp_path, monkeypatch
    ):
        """The coordinator and the findings stage read the slug with this; a
        bare `subprocess.run` with no timeout could hang them (#332,
        6314d19ce3). `git_ops._run` gives each call `_GIT_TIMEOUT`."""
        from build_pipeline import git_ops

        repo = tmp_path / "plain"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "git@github.com:o/r.git"],
            cwd=repo,
            check=True,
        )
        real, seen = git_ops._run, []

        def run(argv, cwd=None, **kw):
            seen.append(argv[:3])
            return real(argv, cwd=cwd, **kw)

        monkeypatch.setattr(git_ops, "_run", run)
        assert store.slug_repo_name(repo) == "o-r"
        assert seen == [["git", "remote", "get-url"]]

    @pytest.mark.parametrize("code", [124, 127])
    def test_a_git_that_timed_out_or_did_not_start_is_not_read_as_no_origin(
        self, tmp_path, monkeypatch, code
    ):
        """`git_ops._run` gives 124 for a timeout and 127 for a git that did
        not start. Neither says the repo has no origin, so the store raises
        instead of answering with the directory name."""
        from build_pipeline import git_ops

        repo = tmp_path / "plain"
        repo.mkdir()
        monkeypatch.setattr(
            git_ops,
            "_run",
            lambda argv, cwd=None, **kw: git_ops.GitResult(argv, code, "", "x"),
        )
        with pytest.raises(store.GitUnreadable):
            store.slug_repo_name(repo)


# --- item 6: the lock ----------------------------------------------------------

HOLDER = r"""
import fcntl, json, os, socket, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.ftruncate(fd, 0)
os.pwrite(fd, json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "since": "then"}).encode(), 0)
print("locked", flush=True)
sys.stdin.read()
"""


def hold_in_other_process(lock_file: Path) -> subprocess.Popen:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock_file)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "locked"
    return proc


class TestLock:
    def test_a_second_hold_in_the_same_process_is_a_no_op(self, tmp_path):
        epic_lock.hold(tmp_path / "e", "e")
        epic_lock.hold(tmp_path / "e", "e")
        assert epic_lock.held(tmp_path / "e")

    def test_the_holder_writes_its_pid(self, tmp_path):
        import os

        epic_lock.hold(tmp_path / "e", "e")
        info = json.loads((tmp_path / "e" / ".lock").read_text())
        assert info["pid"] == os.getpid() and info["host"] and info["since"]

    def test_another_process_holding_it_is_refused_with_its_pid(self, tmp_path):
        proc = hold_in_other_process(tmp_path / "e" / ".lock")
        try:
            with pytest.raises(epic_lock.EpicLocked) as err:
                epic_lock.hold(tmp_path / "e", "e")
            assert str(err.value).startswith(
                f"epic e is running now (pid {proc.pid} on "
            )
        finally:
            proc.communicate("")

    def test_the_lock_is_not_inherited_by_a_child_process(self, tmp_path):
        import os

        epic_lock.hold(tmp_path / "e", "e")
        assert not os.get_inheritable(epic_lock._held[tmp_path / "e"])
        out = subprocess.run(
            [
                sys.executable,
                "-c",
                "import fcntl,os,sys\n"
                "fd=os.open(sys.argv[1],os.O_RDWR)\n"
                "try:\n fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);print('got')\n"
                "except OSError:\n print('held')\n",
                str(tmp_path / "e" / ".lock"),
            ],
            capture_output=True,
            text=True,
        )
        assert out.stdout.strip() == "held"
