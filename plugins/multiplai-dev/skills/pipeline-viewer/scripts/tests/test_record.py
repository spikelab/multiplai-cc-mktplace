"""record.py: every write and every refusal."""

from __future__ import annotations

import fcntl
import json
import os
import socket

import pytest
from helpers import ENG, PM, git, roles, status_fn
from pipeline_viewer import record
from viewer_kit.people import Person


def _trailers(cwd, rev="HEAD") -> str:
    return git(cwd, "log", "-1", "--format=%(trailers)", rev)


def test_approve_is_an_empty_commit_named_for_the_person(fx):
    p, fresh = status_fn(fx, "export-csv")
    revision = fresh().stops[0].revision
    res = record.approve(p, fresh, PM, 1, roles(fx, 1))
    wt = fx["export-csv"]
    assert res.commit == git(wt, "rev-parse", "HEAD")
    assert git(wt, "log", "-1", "--format=%an <%ae>") == "Pat Planner <pat@example.org>"
    assert git(wt, "log", "-1", "--format=%cn") == "Fixture Runner"
    assert git(wt, "show", "--format=", "--name-only", "HEAD") == ""
    t = _trailers(wt)
    assert (
        "Stop: 1" in t
        and "Approved-by: Pat Planner" in t
        and f"Revision: {revision}" in t
    )
    assert (
        git(wt, "log", "-1", "--format=%s")
        == "review(export-csv): Pat Planner approved stop 1"
    )
    # Pushed to the change's branch on origin, from pm-spec's detached worktree.
    assert git(fx["origin"], "rev-parse", "TCK-1-export-csv") == res.commit
    assert res.pushed and not res.unpushed and not res.already


def test_stop_4_approval_matches_the_buildme_gate(fx):
    p, fresh = status_fn(fx, "rate-limit")
    record.approve(p, fresh, ENG, 4, roles(fx, 4))
    t = _trailers(fx["rate-limit"])
    assert (
        "Stop: 4" in t
        and "Revision: planrev4" in t
        and "Approved-by: Eli Engineer" in t
    )


def test_acknowledge_twice_commits_once(fx):
    p, fresh = status_fn(fx, "export-csv")
    first = record.acknowledge(p, fresh, PM, 1, roles(fx, 1))
    second = record.acknowledge(p, fresh, PM, 1, roles(fx, 1))
    assert second.already and second.commit == first.commit
    assert git(fx["export-csv"], "log", "-1", "--format=%s") == (
        "review(export-csv): Pat Planner is reviewing stop 1"
    )


def test_comment_commits_only_its_file(fx):
    p, fresh = status_fn(fx, "export-csv")
    wt = fx["export-csv"]
    (wt / "specs/changes/export-csv/proposal.md").write_text("an unsaved edit\n")
    res = record.comment(p, fresh, PM, 1, "proposal.md", "Why", "Who asked for this?")
    assert git(wt, "show", "--format=", "--name-only", "HEAD") == (
        "specs/changes/export-csv/review/stop-1.md"
    )
    assert (
        git(wt, "log", "-1", "--format=%s")
        == 'review(export-csv): comment on stop 1 section "Why"'
    )
    text = (wt / "specs/changes/export-csv/review/stop-1.md").read_text()
    assert (
        "## proposal.md — Why" in text
        and "Who asked for this?" in text
        and "Pat Planner" in text
    )
    assert res.path == "specs/changes/export-csv/review/stop-1.md"
    # The unsaved edit is still unsaved.
    assert "proposal.md" in git(wt, "status", "--porcelain")


def test_rework_stops_1_to_3_writes_spec_issues(fx):
    p, fresh = status_fn(fx, "search-box")
    record.rework(
        p, fresh, PM, 2, roles(fx, 2), "Merge the components", "One is enough."
    )
    d = fx["search-box"] / "specs/changes/search-box"
    issues = (d / "spec-issues.md").read_text()
    assert "## Merge the components" in issues and "One is enough." in issues
    names = git(
        fx["search-box"], "show", "--format=", "--name-only", "HEAD"
    ).splitlines()
    assert "specs/changes/search-box/spec-issues.md" in names
    assert "Rework-requested-by: Pat Planner" in _trailers(fx["search-box"])


def test_rework_stop_4_writes_the_review_file_not_spec_issues(fx):
    p, fresh = status_fn(fx, "rate-limit")
    record.rework(p, fresh, ENG, 4, roles(fx, 4), "Smaller blocks", "Split block 1.")
    d = fx["rate-limit"] / "specs/changes/rate-limit"
    assert not (d / "spec-issues.md").exists()
    assert "Rework requested: Smaller blocks" in (d / "review/stop-4.md").read_text()


def test_decided_by_the_agent(fx):
    p, fresh = status_fn(fx, "export-csv")
    res = record.decided(p, fresh, 1, "no reply in 240 minutes")
    t = _trailers(fx["export-csv"])
    assert "Decided-by: agent" in t and "Reason: no reply in 240 minutes" in t
    assert git(fx["export-csv"], "log", "-1", "--format=%an") == "Fixture Runner"
    assert not res.already


def test_refuses_a_person_who_is_not_a_reviewer(fx):
    p, fresh = status_fn(fx, "export-csv")
    with pytest.raises(record.Refused) as exc:
        record.approve(p, fresh, ENG, 1, roles(fx, 1))
    assert exc.value.status == 403
    assert exc.value.message == "Eli Engineer is not a reviewer of stop 1"


def test_refuses_a_stop_not_reached(fx):
    p, fresh = status_fn(fx, "export-csv")
    with pytest.raises(record.Refused, match="stop 2 of export-csv is not waiting"):
        record.approve(p, fresh, PM, 2, roles(fx, 2))


def test_refuses_approval_at_stop_5(fx):
    p, fresh = status_fn(fx, "help-page")
    with pytest.raises(record.Refused, match="stop 5 is approved on the pull request"):
        record.approve(p, fresh, ENG, 5, roles(fx, 5))


def test_refuses_with_no_checkout_on_the_branch(fx):
    p, fresh = status_fn(fx, "export-csv")
    p.write = None
    with pytest.raises(
        record.Refused, match="no checkout holds branch TCK-1-export-csv"
    ):
        record.comment(p, fresh, PM, 1, "proposal.md", "Why", "x")


def test_refuses_while_pm_spec_runs(fx):
    p, fresh = status_fn(fx, "export-csv")
    head = git(fx["export-csv"], "rev-parse", "HEAD")
    lock = fx["export-csv"] / "specs/changes/export-csv/.pm-spec/run.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(
            fd, json.dumps({"pid": os.getpid(), "host": socket.gethostname()}).encode()
        )
        with pytest.raises(record.Refused) as exc:
            record.comment(p, fresh, PM, 1, "proposal.md", "Why", "x")
        assert (
            exc.value.message
            == "pm-spec is running on export-csv; try again when it stops"
        )
    finally:
        os.close(fd)
    assert git(fx["export-csv"], "rev-parse", "HEAD") == head


def test_approve_refuses_uncommitted_edits_to_the_docs(fx):
    p, fresh = status_fn(fx, "export-csv")
    (fx["export-csv"] / "specs/changes/export-csv/proposal.md").write_text("edited\n")
    with pytest.raises(record.Refused) as exc:
        record.approve(p, fresh, PM, 1, roles(fx, 1))
    assert exc.value.message == (
        "specs/changes/export-csv/proposal.md have uncommitted edits; commit or discard them first"
    )


def test_refuses_a_diverged_branch(fx, tmp_path):
    other = tmp_path / "other"
    git(
        fx["root"],
        "clone",
        "-q",
        "-b",
        "TCK-1-export-csv",
        str(fx["origin"]),
        str(other),
    )
    git(other, "-c", "user.name=X", "-c", "user.email=x@example.org",
        "commit", "-q", "--allow-empty", "-m", "someone else")  # fmt: skip
    git(other, "push", "-q")
    wt = fx["export-csv"]
    git(wt, "commit", "-q", "--allow-empty", "-m", "local only")
    p, fresh = status_fn(fx, "export-csv")
    with pytest.raises(record.Refused) as exc:
        record.comment(p, fresh, PM, 1, "proposal.md", "Why", "x")
    assert exc.value.message == (
        "branch TCK-1-export-csv has diverged from origin; resolve it, then retry"
    )


def test_fast_forwards_before_writing(fx, tmp_path):
    other = tmp_path / "other"
    git(
        fx["root"],
        "clone",
        "-q",
        "-b",
        "TCK-1-export-csv",
        str(fx["origin"]),
        str(other),
    )
    git(other, "-c", "user.name=X", "-c", "user.email=x@example.org",
        "commit", "-q", "--allow-empty", "-m", "reviewer edit")  # fmt: skip
    git(other, "push", "-q")
    pushed = git(other, "rev-parse", "HEAD")
    p, fresh = status_fn(fx, "export-csv")
    res = record.comment(p, fresh, PM, 1, "proposal.md", "Why", "x")
    assert git(fx["export-csv"], "rev-parse", f"{res.commit}^") == pushed


def test_unknown_person_role_is_refused_by_name(fx):
    p, fresh = status_fn(fx, "export-csv")
    stranger = Person(name="Sam Stranger", email="s@example.org", role="pm")
    with pytest.raises(record.Refused, match="not a reviewer"):
        record.acknowledge(p, fresh, stranger, 1, roles(fx, 1))
