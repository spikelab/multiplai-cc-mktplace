"""Questions asked on the page, their answers, and their record in git."""

from __future__ import annotations

import fcntl
import json
import os
import socket

import pytest
from helpers import PM, git, status_fn
from pipeline_viewer import record
from pipeline_viewer.__main__ import main
from pipeline_viewer.models import InboxRow
from pipeline_viewer.questions import questions, unrecorded
from viewer_kit.mailbox import Mailbox

PAT = {
    "name": "Pat Planner",
    "email": "pat@example.org",
    "role": "pm",
    "slack_id": None,
}


def _ask(qid, change="export-csv", stop=1, section="Why", text="Why now?"):
    return {
        "v": 1, "id": qid, "ts": "2026-01-02T10:00:00Z", "kind": "ask", "change": change,
        "stop": stop, "doc": "proposal.md", "section": section, "text": text, "author": PAT,
    }  # fmt: skip


def test_questions_join_answers_and_filter():
    inbox = [
        _ask("q1"),
        {"id": "c1", "kind": "comment", "change": "export-csv", "stop": 1},
        _ask("q2", stop=2),
        _ask("q3", change="other"),
    ]
    outbox = [
        {"reply_to": "q1", "ts": "t1", "text": "Part one.", "done": False},
        {"reply_to": "q1", "ts": "t2", "text": "Part two.", "done": True, "commit": "abc1234"},
        {"reply_to": "zz", "ts": "t3", "text": "stray", "done": True},
    ]  # fmt: skip
    qs = questions(inbox, outbox, "export-csv")
    assert [q["id"] for q in qs] == ["q1", "q2"]
    q1 = qs[0]
    assert q1["answer"] == "Part one.\n\nPart two." and q1["done"] is True
    assert q1["answered_at"] == "t2" and q1["commit"] == "abc1234"
    assert q1["author"] == {"name": "Pat Planner", "role": "pm"}
    assert q1["asked_at"] == "2026-01-02T10:00:00Z" and q1["section"] == "Why"
    assert qs[1]["answer"] is None and qs[1]["done"] is False
    assert [q["id"] for q in questions(inbox, outbox, "export-csv", 2)] == ["q2"]
    assert unrecorded(qs, set()) == [q1]
    assert unrecorded(qs, {"q1"}) == []
    no_stop = questions(
        [_ask("q9", stop=None)], [{"reply_to": "q9", "text": "x", "done": True}]
    )
    assert unrecorded(no_stop, set()) == []


def _answered(qid="q1", text="Why now?", answer="Because.\n\n## Not a heading\n"):
    q = questions(
        [_ask(qid, text=text)],
        [{"reply_to": qid, "ts": "2026-01-02T11:00:00Z", "text": answer, "done": True}],
    )
    return q[0]


def test_answer_commits_the_review_file_with_trailers(fx):
    p, fresh = status_fn(fx, "export-csv")
    wt = fx["export-csv"]
    res = record.answer(p, fresh, _answered(), "Claude")
    assert res.commit and not res.already and res.pushed
    assert git(wt, "show", "--name-only", "--format=", "HEAD") == (
        "specs/changes/export-csv/review/stop-1.md"
    )
    trailers = git(wt, "log", "-1", "--format=%(trailers)")
    assert "Stop: 1" in trailers and "Answered-for: Pat Planner" in trailers
    assert "Question: q1" in trailers and "Revision: " in trailers
    # Committer only: the author is whoever runs the session, not the asker.
    assert git(wt, "log", "-1", "--format=%an") == "Fixture Runner"
    text = (wt / "specs/changes/export-csv/review/stop-1.md").read_text()
    assert "## Question: proposal.md — Why" in text
    assert "Pat Planner, 2026-01-02T10:00:00Z, asked:\n\n> Why now?" in text
    assert "Claude, 2026-01-02T11:00:00Z, answered:" in text
    assert "> ## Not a heading" in text  # quoted: cannot start a new block
    recs = fresh().stops[0].records
    assert [(r.kind, r.by) for r in recs] == [("answer", "Pat Planner")]
    assert fresh().stops[0].state == "waiting"  # an answer decides nothing

    again = record.answer(p, fresh, _answered(), "Claude")
    assert again.already and again.commit == res.commit
    assert record.recorded_questions(p) == {"q1"}


def test_answer_refused_while_pm_spec_runs(fx):
    p, fresh = status_fn(fx, "export-csv")
    head = git(fx["export-csv"], "rev-parse", "HEAD")
    lock = fx["export-csv"] / "specs/changes/export-csv/.pm-spec/run.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(
            fd, json.dumps({"pid": os.getpid(), "host": socket.gethostname()}).encode()
        )
        with pytest.raises(record.Refused, match="pm-spec is running"):
            record.answer(p, fresh, _answered(), "Claude")
        done = record.record_answers(p, fresh, [_answered()], "Claude")
        assert done == [
            ("q1", None, "pm-spec is running on export-csv; try again when it stops")
        ]
    finally:
        os.close(fd)
    assert git(fx["export-csv"], "rev-parse", "HEAD") == head


def test_reply_records_the_answer_in_git(fx, capsys, tmp_path):
    box = Mailbox(fx["box"])
    box.append_inbox(InboxRow(**{k: v for k, v in _ask("q-7").items() if k != "v"}))
    answer = tmp_path / "a.md"
    answer.write_text("Because people asked.\n")
    args = ["reply", "--repo", str(fx["repo"]), "--box", str(fx["box"]),
            "--worktrees-root", str(fx["wt"]), "--to", "q-7", "--file", str(answer)]  # fmt: skip
    assert main(args) == 0
    out = capsys.readouterr().out
    sha = git(fx["export-csv"], "rev-parse", "HEAD")
    assert f"RECORD:q-7:{sha}" in out
    assert "Question: q-7" in git(fx["export-csv"], "log", "-1", "--format=%(trailers)")
    assert box.read_outbox(0)[0][0]["done"] is True


def test_reply_with_more_records_nothing_yet(fx, capsys, tmp_path):
    box = Mailbox(fx["box"])
    box.append_inbox(InboxRow(**{k: v for k, v in _ask("q-8").items() if k != "v"}))
    answer = tmp_path / "a.md"
    answer.write_text("Looking…\n")
    head = git(fx["export-csv"], "rev-parse", "HEAD")
    args = ["reply", "--repo", str(fx["repo"]), "--box", str(fx["box"]),
            "--worktrees-root", str(fx["wt"]), "--to", "q-8", "--file", str(answer), "--more"]  # fmt: skip
    assert main(args) == 0
    assert "RECORD" not in capsys.readouterr().out
    assert git(fx["export-csv"], "rev-parse", "HEAD") == head


def test_reply_without_a_stop_skips_the_record(fx, capsys, tmp_path):
    box = Mailbox(fx["box"])
    box.append_inbox(
        InboxRow(**{k: v for k, v in _ask("q-9", stop=None).items() if k != "v"})
    )
    answer = tmp_path / "a.md"
    answer.write_text("Sure.\n")
    assert main(["reply", "--repo", str(fx["repo"]), "--box", str(fx["box"]),
                 "--to", "q-9", "--file", str(answer)]) == 0  # fmt: skip
    assert "RECORD:SKIPPED:the question names no stop" in capsys.readouterr().out


def test_pm_is_the_asker_in_the_helpers():
    # The fixture's PM is the asker these tests name.
    assert PM.name == PAT["name"]
