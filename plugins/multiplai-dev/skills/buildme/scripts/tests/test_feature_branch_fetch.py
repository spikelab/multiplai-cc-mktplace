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


def test_another_fetch_failure_is_not_run_again(monkeypatch, tmp_path):
    from build_pipeline import feature_branch

    calls, run = _results((False, "fatal: could not read from remote"))
    monkeypatch.setattr(feature_branch.git_ops, "_run", run)
    assert "could not read" in feature_branch.fetch(tmp_path)
    assert len(calls) == 1


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
