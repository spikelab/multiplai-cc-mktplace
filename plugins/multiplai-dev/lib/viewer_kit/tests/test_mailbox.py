"""The mailbox as a viewer uses it: rows with and without an author, pending
rows, decisions. review-viewer's own tests cover the rest of the mailbox
through its `review_viewer.mailbox` alias."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict
from viewer_kit.mailbox import Mailbox, new_question_id, read_rows, utc_now
from viewer_kit.people import Person, add_person, load_people
from viewer_kit.request_checks import (
    TOKEN_HEADER,
    Reject,
    check_origin,
    check_person,
    public_origin,
    read_json_body,
)

ANA = Person(name="Ana", email="ana@example.com", role="pm", slack_id="U001")


class Row(BaseModel):
    """An inbox row as a viewer would model it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    ts: str
    kind: Literal["ask", "comment"]
    text: str
    author: Person | None = None


class Reply(BaseModel):
    reply_to: str
    ts: str
    text: str
    done: bool


@pytest.fixture
def box(tmp_path) -> Mailbox:
    mb = Mailbox(tmp_path / "box")
    mb.create()
    return mb


def _row(text="why?", author=None) -> Row:
    return Row(id=new_question_id(), ts=utc_now(), kind="ask", text=text, author=author)


def test_author_is_on_the_row(box):
    box.append_inbox(_row(author=ANA))
    got = box.read_inbox()[-1]
    assert got["author"] == {
        "name": "Ana",
        "email": "ana@example.com",
        "role": "pm",
        "slack_id": "U001",
    }
    assert Row.model_validate(got).author == ANA


def test_a_row_without_an_author_has_no_author_key(box):
    box.append_inbox(_row())
    raw = box.inbox.read_text(encoding="utf-8")
    assert "author" not in raw
    assert Row.model_validate(box.read_inbox()[-1]).author is None


def test_older_rows_without_author_still_load(box):
    with box.inbox.open("a", encoding="utf-8") as fh:
        fh.write('{"id":"q-1","ts":"t","kind":"comment","text":"old"}\n')
    assert Row.model_validate(box.read_inbox()[-1]).author is None


def test_pending_is_every_row_without_a_final_reply(box):
    a, b, c = _row("a"), _row("b"), _row("c")
    for r in (a, b, c):
        box.append_inbox(r)
    box.append_outbox(Reply(reply_to=a.id, ts=utc_now(), text="done", done=True))
    box.append_outbox(Reply(reply_to=b.id, ts=utc_now(), text="part", done=False))
    assert [r["id"] for r in box.pending()] == [b.id, c.id]


def test_decisions_keep_the_latest_per_key(box):
    box.write_decision("k", "approve", "fine")
    entry = box.write_decision("k", "rework", "not yet")
    assert box.read_decisions() == {
        "k": {"decision": "rework", "note": "not yet", "ts": entry.ts}
    }


# --- the checks and the mailbox together, over HTTP ----------------------------


def _serve(box: Mailbox, people_path, public: str | None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                check_origin(self.headers, public)
                person = check_person(self.headers, load_people(people_path))
                body = read_json_body(self.headers, self.rfile, 64 * 1024)
                box.append_inbox(_row(body["text"], person))
                status, out = 200, {"ok": True}
            except Reject as rej:
                status, out = rej.status, {"error": rej.error}
            data = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _post(port: int, token: str, origin: str | None = None) -> int:
    headers = {"Content-Type": "application/json", TOKEN_HEADER: token}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/ask",
        data=json.dumps({"text": "what changed?"}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code


def test_a_request_through_the_public_origin_writes_its_author(box, tmp_path):
    people = tmp_path / "people.json"
    token = add_person(people, ANA)
    srv = _serve(box, people, public_origin("https://demo.ngrok.app"))
    try:
        port = srv.server_address[1]
        assert _post(port, token, "https://demo.ngrok.app") == 200
        assert _post(port, token, "https://evil.example") == 403
        assert _post(port, "not-listed", "https://demo.ngrok.app") == 401
    finally:
        srv.shutdown()
        srv.server_close()
    rows = read_rows(box.inbox)
    assert len(rows) == 1
    assert rows[0]["author"]["name"] == "Ana" and rows[0]["text"] == "what changed?"
    assert token not in box.inbox.read_text(encoding="utf-8")
