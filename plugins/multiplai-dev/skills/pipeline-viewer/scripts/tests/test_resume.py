"""The plan's Stop and resume rows S1-S7, S11 and S13. Each case stops the
work at the row's point, resumes it, and compares the result with a run that
never stopped."""

from __future__ import annotations

import fcntl
import json
import os
import socket
from datetime import timedelta

import pytest
import slackstub
from helpers import PM, git, roles, status_fn
from pipeline_viewer import config, deadlines, record
from pipeline_viewer.__main__ import main
from pipeline_viewer.slack import Slack
from pipeline_viewer.status import parse_utc
from viewer_kit.mailbox import Mailbox


def _review_log(cwd) -> list[tuple[str, str]]:
    """(subject, trailers) of the review commits, oldest first, for comparing runs."""
    out = git(cwd, "log", "--reverse", "--format=%s%x1f%an%x1f%(trailers)%x1e")
    rows = []
    for rec in out.split("\x1e"):
        if "\x1f" in rec and rec.strip().startswith("review("):
            subject, author, trailers = rec.strip().split("\x1f")
            rows.append((subject, author, trailers.strip()))
    return rows


def _box(tmp_path, name) -> Mailbox:
    box = Mailbox(tmp_path / name)
    box.create()
    return box


# --- S1: the server was down when a deadline passed -------------------------------


def test_s1_deadline_missed_while_down_fires_once(shared_fx, tmp_path):
    st = status_fn(shared_fx, "export-csv")[1]()
    cfg = config.load(shared_fx["repo"])
    since = parse_utc(st.stops[0].waiting_since)

    never = _box(tmp_path, "never")
    for minute in range(301):
        deadlines.check([st], cfg, never, None, now=since + timedelta(minutes=minute))

    stopped = _box(tmp_path, "stopped")
    deadlines.check([st], cfg, stopped, None, now=since + timedelta(minutes=10))
    # Killed here; restarted at minute 300: a fresh Mailbox, deadlines.json reloaded.
    restarted = Mailbox(stopped.dir)
    fired = deadlines.check(
        [st], cfg, restarted, None, now=since + timedelta(minutes=300)
    )
    assert sorted(k for k, _ in fired) == ["reminder", "timeout"]
    deadlines.check([st], cfg, restarted, None, now=since + timedelta(minutes=301))

    def kinds(box):
        return [
            (r["kind"], r["change"], r["stop"], r["revision"]) for r in box.read_inbox()
        ]

    assert kinds(restarted) == kinds(never)
    assert deadlines.load(restarted.dir).keys() == deadlines.load(never.dir).keys()
    # The page's cursor restarts at 0 and reads the same rows.
    assert restarted.read_outbox(0) == never.read_outbox(0)


# --- S2: a second serve while the first lives ------------------------------------


def test_s2_restart_while_alive_refuses(fx, capsys):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    record_ = {
        "url": f"http://127.0.0.1:{port}/",
        "public_url": None,
        "port": port,
        "pid": os.getpid(),
    }
    server_json = fx["box"] / "server.json"
    server_json.write_text(json.dumps(record_))
    try:
        code = main(["serve", "--repo", str(fx["repo"]), "--box", str(fx["box"])])
    finally:
        sock.close()
    assert code == 3
    assert capsys.readouterr().out.strip() == (
        f"pipeline-viewer already serving http://127.0.0.1:{port}/ (pid {os.getpid()})"
    )
    # The running server's record is untouched, as if nothing had been asked.
    assert json.loads(server_json.read_text()) == record_


# --- S3: committed, push failed --------------------------------------------------


def test_s3_failed_push_is_pushed_later_never_recommitted(fx, tmp_path):
    from fixture_repo import build

    never = build(tmp_path / "never")
    p, fresh = status_fn(never, "export-csv")
    record.approve(p, fresh, PM, 1, roles(never, 1))

    origin = fx["origin"]
    hidden = origin.with_name("origin.hidden")
    origin.rename(hidden)
    p, fresh = status_fn(fx, "export-csv")
    try:
        first = record.approve(p, fresh, PM, 1, roles(fx, 1))
    finally:
        hidden.rename(origin)
    assert first.commit and not first.pushed and first.unpushed and first.push_error
    assert fresh().unpushed is True

    again = record.approve(p, fresh, PM, 1, roles(fx, 1))
    assert (
        again.already
        and again.commit == first.commit
        and again.pushed
        and not again.unpushed
    )
    assert fresh().unpushed is False
    assert git(origin, "rev-parse", "TCK-1-export-csv") == first.commit
    strip = [
        (s, a, t.split("Revision:")[0]) for s, a, t in _review_log(fx["export-csv"])
    ]
    expect = [
        (s, a, t.split("Revision:")[0]) for s, a, t in _review_log(never["export-csv"])
    ]
    assert strip == expect


def test_s3_retry_push_route_pushes_only(fx):
    origin = fx["origin"]
    hidden = origin.with_name("origin.hidden")
    origin.rename(hidden)
    p, fresh = status_fn(fx, "export-csv")
    try:
        first = record.acknowledge(p, fresh, PM, 1, roles(fx, 1))
    finally:
        hidden.rename(origin)
    head = git(fx["export-csv"], "rev-parse", "HEAD")
    res = record.push(p)
    assert res.pushed and not res.unpushed and res.commit is None
    assert git(fx["export-csv"], "rev-parse", "HEAD") == head == first.commit


# --- S4: pm-spec running ---------------------------------------------------------


def test_s4_refused_while_pm_spec_runs_then_written(fx, tmp_path):
    from fixture_repo import build

    never = build(tmp_path / "never")
    p, fresh = status_fn(never, "export-csv")
    record.comment(p, fresh, PM, 1, "proposal.md", "Why", "Who asked?")

    p, fresh = status_fn(fx, "export-csv")
    head = git(fx["export-csv"], "rev-parse", "HEAD")
    lock = fx["export-csv"] / "specs/changes/export-csv/.pm-spec/run.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(
            fd, json.dumps({"pid": os.getpid(), "host": socket.gethostname()}).encode()
        )
        with pytest.raises(record.Refused, match="pm-spec is running on export-csv"):
            record.comment(p, fresh, PM, 1, "proposal.md", "Why", "Who asked?")
        assert git(fx["export-csv"], "rev-parse", "HEAD") == head
        assert not (fx["export-csv"] / "specs/changes/export-csv/review").exists()
    finally:
        os.close(fd)
    record.comment(p, fresh, PM, 1, "proposal.md", "Why", "Who asked?")
    assert [(s, a) for s, a, _ in _review_log(fx["export-csv"])] == [
        (s, a) for s, a, _ in _review_log(never["export-csv"])
    ]


# --- S5: uncommitted edits to the stop's documents --------------------------------


def test_s5_comment_goes_through_approve_waits_for_the_commit(fx, tmp_path):
    from fixture_repo import build

    edit = "\n## Risks\n\nNone known.\n"
    never = build(tmp_path / "never")
    nwt = never["export-csv"]
    (nwt / "specs/changes/export-csv/proposal.md").open("a").write(edit)
    git(nwt, "commit", "-qam", "specs(export-csv): reviewer edit")
    p, fresh = status_fn(never, "export-csv")
    record.approve(p, fresh, PM, 1, roles(never, 1))

    wt = fx["export-csv"]
    (wt / "specs/changes/export-csv/proposal.md").open("a").write(edit)
    p, fresh = status_fn(fx, "export-csv")
    c = record.comment(p, fresh, PM, 1, "proposal.md", "Risks", "Good.")
    assert (
        git(wt, "show", "--format=", "--name-only", c.commit)
        == "specs/changes/export-csv/review/stop-1.md"
    )
    with pytest.raises(
        record.Refused, match="have uncommitted edits; commit or discard them first"
    ):
        record.approve(p, fresh, PM, 1, roles(fx, 1))
    git(wt, "commit", "-qam", "specs(export-csv): reviewer edit")
    record.approve(p, fresh, PM, 1, roles(fx, 1))
    revision = [t for _, _, t in _review_log(wt)][-1]
    assert revision == _review_log(nwt)[-1][2]  # same Revision: the committed docs


# --- S6: the branch diverged from origin ------------------------------------------


def test_s6_diverged_refuses_then_retries_after_resolving(fx, tmp_path):
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
        "commit", "-q", "--allow-empty", "-m", "elsewhere")  # fmt: skip
    git(other, "push", "-q")
    wt = fx["export-csv"]
    git(wt, "commit", "-q", "--allow-empty", "-m", "local")
    head = git(wt, "rev-parse", "HEAD")
    p, fresh = status_fn(fx, "export-csv")
    with pytest.raises(
        record.Refused, match="branch TCK-1-export-csv has diverged from origin"
    ):
        record.acknowledge(p, fresh, PM, 1, roles(fx, 1))
    assert git(wt, "rev-parse", "HEAD") == head
    git(wt, "reset", "-q", "--hard", "origin/TCK-1-export-csv")
    res = record.acknowledge(p, fresh, PM, 1, roles(fx, 1))
    assert (
        res.pushed and git(fx["origin"], "rev-parse", "TCK-1-export-csv") == res.commit
    )


# --- S7: the same stop reached again at a new revision ------------------------------


def test_s7_new_revision_fires_afresh(fx):
    cfg = config.load(fx["repo"])
    box = Mailbox(fx["box"])
    _p, fresh = status_fn(fx, "export-csv")
    first = fresh()
    since = parse_utc(first.stops[0].waiting_since)
    deadlines.check([first], cfg, box, None, now=since + timedelta(minutes=300))
    # pm-spec revise: new proposal, stop 1 reached again.
    wt = fx["export-csv"]
    (wt / "specs/changes/export-csv/proposal.md").open("a").write(
        "\n## Revised\n\nYes.\n"
    )
    git(wt, "commit", "-qam", "specs(export-csv): revised proposal")
    second = fresh()
    assert second.stops[0].revision != first.stops[0].revision
    since2 = parse_utc(second.stops[0].waiting_since)
    fired = deadlines.check(
        [second], cfg, box, None, now=since2 + timedelta(minutes=300)
    )
    assert sorted(k for k, _ in fired) == ["reminder", "timeout"]
    keys = deadlines.load(box.dir).keys()
    assert {k.rsplit("|", 1)[1] for k in keys} == {
        first.stops[0].revision,
        second.stops[0].revision,
    }
    assert [r["revision"] for r in box.read_inbox()] == [
        first.stops[0].revision,
        second.stops[0].revision,
    ]


# --- S11: posted, crashed before notified.json --------------------------------------


def test_s11_crash_after_post_posts_one_duplicate(fx, monkeypatch, tmp_path):
    posts = slackstub.install(monkeypatch)
    monkeypatch.setenv("SLACK_TOKEN", "xoxp-x")
    revision = status_fn(fx, "export-csv")[1]().stops[0].revision

    never = Slack(tmp_path / "never", "C0TEST")
    (tmp_path / "never").mkdir()
    never.notify("export-csv", 1, revision, "m")
    never.notify("export-csv", 1, revision, "m")
    assert len(posts) == 1

    posts.clear()
    slack = Slack(fx["box"], "C0TEST")
    real_save = Slack._save

    def crash(self, k, entry):
        raise KeyboardInterrupt("killed")

    monkeypatch.setattr(Slack, "_save", crash)
    with pytest.raises(KeyboardInterrupt):
        slack.notify("export-csv", 1, revision, "m")
    monkeypatch.setattr(Slack, "_save", real_save)
    assert slack.notify("export-csv", 1, revision, "m")[0] == "posted"
    assert slack.notify("export-csv", 1, revision, "m")[0] == "already"
    assert len(posts) == 2  # the accepted duplicate, then once only


# --- S13: a timeout row after someone approved ---------------------------------------


def test_s13_record_decided_after_approval_refuses(fx, capsys, tmp_path):
    from fixture_repo import build

    never = build(tmp_path / "never")
    p, fresh = status_fn(never, "export-csv")
    record.approve(p, fresh, PM, 1, roles(never, 1))

    p, fresh = status_fn(fx, "export-csv")
    record.approve(p, fresh, PM, 1, roles(fx, 1))
    head = git(fx["export-csv"], "rev-parse", "HEAD")
    code = main(["record", "decided", "--repo", str(fx["repo"]), "--box", str(fx["box"]),
                 "--worktrees-root", str(fx["wt"]), "--change", "export-csv", "--stop", "1",
                 "--reason", "no reply in 240 minutes"])  # fmt: skip
    assert code == 3
    assert capsys.readouterr().err.strip() == "stop 1 already approved by Pat Planner"
    assert git(fx["export-csv"], "rev-parse", "HEAD") == head
    assert [(s, a) for s, a, _ in _review_log(fx["export-csv"])] == [
        (s, a) for s, a, _ in _review_log(never["export-csv"])
    ]
