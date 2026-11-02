"""`git fetch origin` in an epic run, where other gits share the ref store."""

from __future__ import annotations

from pathlib import Path

import pytest

LOCK_LOST = (
    "error: cannot lock ref 'refs/remotes/origin/ENG-40-e': is at eb4f1369 "
    "but expected 35568247"
)


def _results(*outcomes):
    from build_pipeline.git_ops import GitResult

    calls = []

    def run(argv, cwd=None, timeout=None, **kw):
        calls.append(argv)
        ok, err = outcomes[min(len(calls), len(outcomes)) - 1]
        return GitResult(argv=argv, returncode=0 if ok else 1, stdout="", stderr=err)

    return calls, run


def test_a_fetch_that_lost_a_ref_lock_is_run_again(monkeypatch, tmp_path):
    """Two gits on one ref store (an epic's children push and fetch in their
    worktrees while the coordinator fetches): git reports the ref it could not
    lock, and the fetch, run again, gets it."""
    from build_pipeline import feature_branch

    calls, run = _results((False, LOCK_LOST), (True, ""))
    monkeypatch.setattr(feature_branch.git_ops, "_run", run)
    monkeypatch.setattr(feature_branch.time, "sleep", lambda s: None)
    assert feature_branch.fetch(tmp_path) is None
    assert len(calls) == 2


def test_another_fetch_failure_is_run_again_after_each_wait(monkeypatch, tmp_path):
    """A network error or an expired credential is run again after each of
    TRANSIENT_WAITS, then gives git's words."""
    from build_pipeline import feature_branch

    monkeypatch.setattr(feature_branch, "TRANSIENT_WAITS", (15, 30, 60))
    waits = []
    monkeypatch.setattr(feature_branch.time, "sleep", waits.append)
    calls, run = _results(*[(False, "fatal: could not read from remote")] * 4)
    monkeypatch.setattr(feature_branch.git_ops, "_run", run)
    assert "could not read" in feature_branch.fetch(tmp_path)
    assert len(calls) == 4
    assert waits == [15, 30, 60]


def test_a_fetch_that_fails_then_answers_is_not_a_failure(monkeypatch, tmp_path):
    from build_pipeline import feature_branch

    calls, run = _results(
        (False, "remote: Invalid username or token."), (False, "fatal: unable to access"), (True, "")
    )
    monkeypatch.setattr(feature_branch.git_ops, "_run", run)
    assert feature_branch.fetch(tmp_path) is None
    assert len(calls) == 3


def test_a_ref_lock_lost_every_time_gives_gits_words(monkeypatch, tmp_path):
    from build_pipeline import feature_branch

    calls, run = _results((False, LOCK_LOST))
    monkeypatch.setattr(feature_branch.git_ops, "_run", run)
    monkeypatch.setattr(feature_branch.time, "sleep", lambda s: None)
    assert "cannot lock ref" in feature_branch.fetch(tmp_path)
    assert len(calls) == feature_branch.FETCH_ATTEMPTS


@pytest.mark.asyncio
async def test_the_coordinators_fetch_runs_again_after_a_lost_ref_lock(monkeypatch):
    from build_pipeline.epic import coordinator

    answers = [LOCK_LOST, None]
    seen = []

    async def once(repo):
        seen.append(repo)
        return answers[len(seen) - 1]

    async def no_sleep(s):
        return None

    monkeypatch.setattr(coordinator, "_fetch_once", once)
    monkeypatch.setattr(coordinator.asyncio, "sleep", no_sleep)
    assert await coordinator._fetch_origin(Path("/r")) is None
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_the_coordinators_fetch_runs_again_after_an_expired_credential(monkeypatch):
    """Run 14 of DFACCMGMT-1 set two children aside on one `Invalid username or
    token`: the fetch is now run again after each wait."""
    from build_pipeline.epic import coordinator

    answers = ["remote: Invalid username or token.", "fatal: unable to access", None]
    seen = []

    async def once(repo):
        seen.append(repo)
        return answers[len(seen) - 1]

    monkeypatch.setattr(coordinator, "_fetch_once", once)
    assert await coordinator._fetch_origin(Path("/repo")) is None
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_the_coordinators_fetch_gives_up_after_the_last_wait(monkeypatch):
    from build_pipeline import feature_branch
    from build_pipeline.epic import coordinator

    seen = []

    async def once(repo):
        seen.append(repo)
        return "fatal: unable to access"

    monkeypatch.setattr(coordinator, "_fetch_once", once)
    assert await coordinator._fetch_origin(Path("/repo")) == "fatal: unable to access"
    assert len(seen) == len(feature_branch.TRANSIENT_WAITS) + 1


def test_a_gh_read_that_fails_once_is_read_again(monkeypatch):
    """Run 12 set om-account-identity aside on one failed `gh pr list`."""
    from build_pipeline.epic import coordinator
    from build_pipeline.git_ops import GitResult

    calls = []

    def run_gh(argv, cwd=None, timeout=None):
        calls.append(argv)
        if len(calls) == 1:
            return GitResult(argv=argv, returncode=1, stdout="", stderr="HTTP 502")
        return GitResult(argv=argv, returncode=0, stdout='[{"url": "u"}]', stderr="")

    monkeypatch.setattr(coordinator.git_ops, "_run_gh", run_gh)
    assert coordinator._gh(Path("/repo"), "pr", "list") == [{"url": "u"}]
    assert len(calls) == 2


def test_a_gh_read_that_always_fails_gives_none_after_the_waits(monkeypatch):
    from build_pipeline.epic import coordinator
    from build_pipeline.git_ops import GitResult

    monkeypatch.setattr(coordinator, "GH_WAITS", (2, 5, 10))
    waits = []
    monkeypatch.setattr(coordinator.time, "sleep", waits.append)
    calls = []

    def run_gh(argv, cwd=None, timeout=None):
        calls.append(argv)
        return GitResult(argv=argv, returncode=1, stdout="", stderr="HTTP 401: Bad credentials")

    monkeypatch.setattr(coordinator.git_ops, "_run_gh", run_gh)
    assert coordinator._gh(Path("/repo"), "pr", "list") is None
    assert len(calls) == 4
    assert waits == [2, 5, 10]
