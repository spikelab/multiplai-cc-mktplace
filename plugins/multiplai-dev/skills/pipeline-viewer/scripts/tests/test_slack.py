"""slack.py and `notify`: one post per revision, thread replies, no token."""

from __future__ import annotations

import slackstub
from conftest import people_map
from helpers import status_fn
from pipeline_viewer.__main__ import main
from pipeline_viewer.slack import Slack, stop_text

SECRET = "xoxp-test-not-a-real-token-123"


def _notify(fx, capsys, change="export-csv", stop=1):
    code = main(["notify", "--repo", str(fx["repo"]), "--box", str(fx["box"]),
                 "--worktrees-root", str(fx["wt"]), "--change", change, "--stop", str(stop)])  # fmt: skip
    return code, capsys.readouterr()


def test_one_post_per_revision(fx, capsys, monkeypatch):
    posts = slackstub.install(monkeypatch)
    monkeypatch.setenv("SLACK_TOKEN", SECRET)
    code, out = _notify(fx, capsys)
    assert code == 0 and out.out.startswith("SLACK:POSTED:")
    assert posts[0]["channel"] == "C0TEST"
    assert "<@U0PAT>" in posts[0]["text"] and "stop 1 (proposal)" in posts[0]["text"]
    code, out = _notify(fx, capsys)
    assert out.out.startswith("SLACK:ALREADY:") and len(posts) == 1
    assert SECRET not in out.out + out.err
    assert slackstub.TOKENS == [SECRET]


def test_new_revision_posts_again(fx, capsys, monkeypatch):
    posts = slackstub.install(monkeypatch)
    monkeypatch.setenv("SLACK_TOKEN", SECRET)
    _notify(fx, capsys)
    slack = Slack(fx["box"], "C0TEST")
    slack.notify("export-csv", 1, "another-revision", "again")
    assert len(posts) == 2


def test_replies_go_in_the_thread(fx, capsys, monkeypatch):
    posts = slackstub.install(monkeypatch)
    monkeypatch.setenv("SLACK_TOKEN", SECRET)
    _notify(fx, capsys)
    revision = status_fn(fx, "export-csv")[1]().stops[0].revision
    outcome, _ = Slack(fx["box"], "C0TEST").reply(
        "export-csv", 1, revision, "Pat is reviewing"
    )
    assert outcome == "posted"
    import json

    thread = next(iter(json.loads((fx["box"] / "notified.json").read_text()).values()))
    assert "thread_ts" not in posts[0]
    assert posts[1]["thread_ts"] == thread["ts"]


def test_no_token_writes_the_outbox(fx, capsys, monkeypatch):
    posts = slackstub.install(monkeypatch)
    code, out = _notify(fx, capsys)
    assert code == 0 and out.out.strip() == "SLACK:SKIPPED:no token"
    assert posts == []
    text = (fx["box"] / "slack-outbox.md").read_text()
    assert "export-csv" in text and "not posted: no token" in text
    assert not (fx["box"] / "notified.json").exists()


def test_a_failed_post_is_reported_without_the_token(fx, capsys, monkeypatch):
    slackstub.install(monkeypatch, slackstub.FailingClient)
    monkeypatch.setenv("SLACK_TOKEN", SECRET)
    code, out = _notify(fx, capsys)
    assert code == 1 and out.out.strip() == "SLACK:FAILED:channel_not_found"
    assert SECRET not in (fx["box"] / "slack-outbox.md").read_text()


def test_a_stop_not_waiting_posts_nothing(fx, capsys, monkeypatch):
    posts = slackstub.install(monkeypatch)
    monkeypatch.setenv("SLACK_TOKEN", SECRET)
    _code, out = _notify(fx, capsys, stop=2)
    assert out.out.strip() == "SLACK:SKIPPED:stop 2 is not_reached" and posts == []


def test_stop_text_mentions_and_links(shared_fx):
    st = status_fn(shared_fx, "dark-mode")[1]()
    text = stop_text(st, 3, people_map(), "https://x.example.org/")
    assert "<@U0PAT>, <@U0ELI>" in text
    assert "https://x.example.org/#change=dark-mode&stop=3" in text
