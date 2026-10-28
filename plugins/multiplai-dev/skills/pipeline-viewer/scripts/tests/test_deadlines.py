"""deadlines.py: each deadline fires once per (change, stop, revision)."""

from __future__ import annotations

from datetime import timedelta

import slackstub
from helpers import status_fn
from pipeline_viewer import config, deadlines
from pipeline_viewer.slack import Slack
from pipeline_viewer.status import parse_utc
from viewer_kit.mailbox import Mailbox


def _check(fx, now, slack=None):
    st = status_fn(fx, "export-csv")[1]()
    return deadlines.check(
        [st], config.load(fx["repo"]), Mailbox(fx["box"]), slack, now=now
    ), st


def test_nothing_before_the_deadlines(shared_fx, tmp_path):
    st = status_fn(shared_fx, "export-csv")[1]()
    box = Mailbox(tmp_path / "box")
    box.create()
    now = parse_utc(st.stops[0].waiting_since) + timedelta(minutes=5)
    assert (
        deadlines.check([st], config.load(shared_fx["repo"]), box, None, now=now) == []
    )


def test_reminder_then_timeout_each_once(fx, monkeypatch):
    posts = slackstub.install(monkeypatch)
    monkeypatch.setenv("SLACK_TOKEN", "xoxp-x")
    slack = Slack(fx["box"], "C0TEST")
    st = status_fn(fx, "export-csv")[1]()
    s = st.stops[0]
    slack.notify("export-csv", 1, s.revision, "first")
    since = parse_utc(s.waiting_since)
    fired, _ = _check(fx, since + timedelta(minutes=61), slack)
    assert [k for k, _ in fired] == ["reminder"]
    assert posts[-1]["thread_ts"] and "Reminder" in posts[-1]["text"]
    fired, _ = _check(fx, since + timedelta(minutes=241), slack)
    assert [k for k, _ in fired] == ["timeout"]
    rows = Mailbox(fx["box"]).read_inbox()
    assert [(r["kind"], r["change"], r["stop"], r["revision"]) for r in rows] == [
        ("timeout", "export-csv", 1, s.revision)
    ]
    assert "author" not in rows[0]
    assert _check(fx, since + timedelta(minutes=500), slack)[0] == []
    assert len(posts) == 2


def test_an_acknowledged_stop_never_times_out(fx):
    from helpers import PM, roles
    from pipeline_viewer import record

    p, fresh = status_fn(fx, "export-csv")
    record.acknowledge(p, fresh, PM, 1, roles(fx, 1))
    since = parse_utc(fresh().stops[0].waiting_since)
    assert _check(fx, since + timedelta(days=3))[0] == []
