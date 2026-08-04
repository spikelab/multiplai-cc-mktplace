"""The expensive fleet collectors: git, gh, background jobs, backlog.

Every test here runs against a `tmp_path` fixture. None of them may touch the
real `.multiplai/` — an agent contaminated the live workspace that way on
2026-07-30, and these collectors walk directories by design.
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from lib.fleet_sources import backlog as backlog_mod
from lib.fleet_sources import git_repos, jobs, prs

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# git repos
# ---------------------------------------------------------------------------

def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A real one-commit checkout — `git` behaviour is the thing under test."""
    path = tmp_path / "proj"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    (path / "a.txt").write_text("hello\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-qm", "first")
    return path


def test_find_repos_includes_nested_checkouts(tmp_path, repo):
    """A repo inside a repo is a separate project, not an implementation detail."""
    nested = repo / "sub"
    nested.mkdir()
    _git(nested, "init", "-q")
    found = git_repos.find_repos(tmp_path)
    assert repo in found and nested in found


def test_find_repos_skips_worktrees_dir(tmp_path, repo):
    """`.worktrees/` is reported per-repo, never walked — else every branch double-counts."""
    wt = tmp_path / ".worktrees" / "feat"
    wt.mkdir(parents=True)
    _git(wt, "init", "-q")
    assert wt not in git_repos.find_repos(tmp_path)


def test_clean_repo_reports_clean(tmp_path, repo):
    (state,) = [r for r in git_repos.collect_repos(tmp_path) if r.path == "proj"]
    assert state.clean and state.dirty == 0 and state.branch == "main"


def test_dirty_and_untracked_are_counted(tmp_path, repo):
    (repo / "a.txt").write_text("changed\n")
    (repo / "new.txt").write_text("x\n")
    (state,) = [r for r in git_repos.collect_repos(tmp_path) if r.path == "proj"]
    assert state.dirty == 2 and state.untracked == 1 and not state.clean


def test_branch_without_upstream_is_never_pushed(tmp_path, repo):
    """Never-pushed and ahead-of-remote are different facts and render differently."""
    _git(repo, "remote", "add", "origin", "git@github.com:o/r.git")
    _git(repo, "checkout", "-qb", "feat")
    (state,) = [r for r in git_repos.collect_repos(tmp_path) if r.path == "proj"]
    assert "feat" in state.no_upstream and not state.unpushed


def test_repo_without_remote_reports_no_unpushed_work(tmp_path, repo):
    """Nowhere to push to means "never pushed" is noise, not news."""
    (state,) = [r for r in git_repos.collect_repos(tmp_path) if r.path == "proj"]
    assert state.slug == "" and state.error == ""
    assert state.no_upstream == [] and state.clean


def test_remote_url_parses_to_a_slug(tmp_path, repo):
    _git(repo, "remote", "add", "origin", "https://github.com/o/r.git")
    (state,) = [r for r in git_repos.collect_repos(tmp_path) if r.path == "proj"]
    assert state.slug == "o/r"


def test_broken_checkout_reports_one_error(tmp_path):
    """A directory with a bogus `.git` must yield an error record, not an exception."""
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nowhere\n")
    states = git_repos.collect_repos(tmp_path)
    assert [s for s in states if s.path == "broken" and s.error]


def test_worktree_is_listed_against_its_owner(tmp_path, repo):
    linked = tmp_path / ".worktrees" / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(linked))
    (state,) = [r for r in git_repos.collect_repos(tmp_path) if r.path == "proj"]
    assert any("wt" in w for w in state.worktrees)


# ---------------------------------------------------------------------------
# pull requests
# ---------------------------------------------------------------------------

def _pr(**kw):
    base = {"repo": "o/r", "number": 1, "title": "t", "head": "h", "base": "main"}
    base.update(kw)
    return prs.PullRequest(**base)


def test_bot_detection_covers_app_authored_prs():
    """dependabot arrives as `app/dependabot` — the login prefix is the fallback."""
    pr = prs._one({"number": 1, "author": {"login": "app/dependabot"}}, "o/r")
    assert pr.is_bot
    assert not prs._one({"number": 2, "author": {"login": "spikelab"}}, "o/r").is_bot


def test_rollup_is_pessimistic_about_failure():
    """One red check blocks the merge, however many green ones surround it."""
    checks = [{"conclusion": "SUCCESS"}] * 5 + [{"conclusion": "FAILURE"}]
    assert prs._rollup(checks) == "failing"
    assert prs._rollup([{"conclusion": "SUCCESS"}]) == "passing"
    assert prs._rollup([{"status": "IN_PROGRESS"}]) == "pending"
    assert prs._rollup([]) == "none"


def test_stacks_chain_by_base_branch():
    """B based on A, C based on B — one decision, returned in merge order."""
    a = _pr(number=1, head="a", base="main")
    b = _pr(number=2, head="b", base="a")
    c = _pr(number=3, head="c", base="b")
    (chain,) = prs.stacks([c, a, b])
    assert [p.number for p in chain] == [1, 2, 3]


def test_independent_prs_form_no_stack():
    assert prs.stacks([_pr(number=1, head="a"), _pr(number=2, head="b")]) == []


def test_stacks_survive_a_base_cycle():
    """Malformed input must not hang the digest."""
    a = _pr(number=1, head="a", base="b")
    b = _pr(number=2, head="b", base="a")
    assert len(prs.stacks([a, b])) <= 1


def test_no_access_is_not_an_error(monkeypatch):
    """A repo outside the token's scope is a standing fact, reported separately."""
    def fake_run(args, **kw):
        from lib.fleet_sources.common import Ran
        return Ran(False, err="GraphQL: Could not resolve to a Repository with the name 'x/y'.")

    monkeypatch.setattr(prs, "run", fake_run)
    monkeypatch.setattr(prs, "gh_available", lambda: True)
    scan = prs.collect_prs(["x/y"])
    assert scan.no_access == ["x/y"] and not scan.errors and scan.available


def test_total_auth_failure_reports_unavailable_not_zero(monkeypatch):
    """The failure mode that matters: never claim "no open PRs" over a bad token."""
    def fake_run(args, **kw):
        from lib.fleet_sources.common import Ran
        return Ran(False, err="gh: To use GitHub CLI in a GitHub Actions workflow, "
                              "authentication required. Run gh auth login")

    monkeypatch.setattr(prs, "run", fake_run)
    monkeypatch.setattr(prs, "gh_available", lambda: True)
    scan = prs.collect_prs(["x/y", "x/z"])
    assert scan.available is False and scan.prs == []


def test_missing_gh_reports_unavailable(monkeypatch):
    monkeypatch.setattr(prs, "gh_available", lambda: False)
    assert prs.collect_prs(["x/y"]).available is False


def test_scan_round_trips_through_the_cache():
    scan = prs.PRScan(prs=[_pr(created_at=NOW, updated_at=NOW)], no_access=["a/b"])
    back = prs.scan_from_dict(json.loads(json.dumps(prs.scan_to_dict(scan))))
    assert back is not None
    assert back.prs[0].number == 1 and back.prs[0].updated_at == NOW
    assert back.no_access == ["a/b"]


def test_corrupt_cache_falls_through_to_a_live_query():
    """None, not an empty scan — an unusable cache must never read as "none open"."""
    assert prs.scan_from_dict("garbage") is None
    assert prs.scan_from_dict(None) is None


# ---------------------------------------------------------------------------
# background jobs
# ---------------------------------------------------------------------------

def _job(cfg, short, state="running", mtime=None, detail="doing a thing"):
    d = cfg / "jobs" / short
    d.mkdir(parents=True)
    f = d / "state.json"
    f.write_text(json.dumps({
        "state": state, "detail": detail, "tokens": 10,
        "inFlight": {"tasks": 1, "queued": 0},
    }))
    if mtime is not None:
        import os
        os.utime(f, (mtime, mtime))
    return d


def test_recent_running_job_is_running(tmp_path):
    _job(tmp_path, "aaa", mtime=(NOW - timedelta(minutes=5)).timestamp())
    (job,) = jobs.collect_jobs(tmp_path, now=NOW)
    assert job.running and not job.stale


def test_old_job_is_stale_not_running(tmp_path):
    """The real roster on disk was three weeks old; it must not read as live."""
    _job(tmp_path, "bbb", mtime=(NOW - timedelta(days=23)).timestamp())
    (job,) = jobs.collect_jobs(tmp_path, now=NOW)
    assert job.stale and not job.running


def test_finished_job_is_never_running(tmp_path):
    _job(tmp_path, "ccc", state="done", mtime=NOW.timestamp())
    (job,) = jobs.collect_jobs(tmp_path, now=NOW)
    assert job.finished and not job.running


def test_liveness_never_probes_a_pid():
    """Roster pids live in another namespace; probing one would lie confidently."""
    source = (jobs.__file__)
    text = open(source, encoding="utf-8").read()
    for forbidden in ("os.kill", "psutil", "/proc/", "pid_exists"):
        assert forbidden not in text, f"{forbidden} would probe a foreign pid"


def test_roster_supplies_cwd_and_session(tmp_path):
    _job(tmp_path, "ddd", mtime=NOW.timestamp())
    (tmp_path / "daemon").mkdir()
    (tmp_path / "daemon" / "roster.json").write_text(json.dumps({
        "workers": {"ddd": {"sessionId": "sid-1", "cwd": "/w"}}
    }))
    (job,) = jobs.collect_jobs(tmp_path, now=NOW)
    assert job.session_id == "sid-1" and job.cwd == "/w" and job.in_roster


def test_unreadable_job_is_skipped_not_fatal(tmp_path):
    d = tmp_path / "jobs" / "eee"
    d.mkdir(parents=True)
    (d / "state.json").write_text("{not json")
    assert jobs.collect_jobs(tmp_path, now=NOW) == []


def test_absent_config_dir_is_empty(tmp_path):
    assert jobs.collect_jobs(tmp_path / "nope", now=NOW) == []


# ---------------------------------------------------------------------------
# backlog
# ---------------------------------------------------------------------------

def test_backlog_counts_lines_not_files(tmp_path):
    """One file can hold thirty learnings; the unit a dream run consumes is a line."""
    data = tmp_path / ".multiplai" / "data"
    data.mkdir(parents=True)
    learnings = tmp_path / ".multiplai" / "learnings"
    learnings.mkdir()
    (learnings / "2026-08-01.md").write_text("a\nb\n\nc\n")
    (learnings / "2026-08-02.md").write_text("d\n")
    got = backlog_mod.collect_backlog(data, now=NOW)
    assert got.learnings_lines == 4
    assert got.learnings_files == 2
    assert got.oldest_learning == "2026-08-01"


def test_empty_backlog_is_empty(tmp_path):
    data = tmp_path / ".multiplai" / "data"
    data.mkdir(parents=True)
    assert backlog_mod.collect_backlog(data, now=NOW).empty


def test_failed_extractions_are_counted_separately(tmp_path):
    data = tmp_path / ".multiplai" / "data"
    (data / "failed_extractions").mkdir(parents=True)
    (data / "failed_extractions" / "x.json").write_text("{}")
    got = backlog_mod.collect_backlog(data, now=NOW)
    assert got.failed_extractions == 1 and not got.empty
