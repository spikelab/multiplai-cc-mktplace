"""server.py: per-person auth, the read routes and their path limits, writes."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest
from conftest import ENG_TOKEN, OTHER_TOKEN, PM_TOKEN
from helpers import git
from pipeline_viewer import server
from viewer_kit.mailbox import Mailbox


class Client:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"

    def call(
        self,
        method,
        path,
        token=PM_TOKEN,
        body=None,
        headers=None,
        ctype="application/json",
    ):
        h = {"Content-Type": ctype} if body is not None else {}
        if token:
            h["X-Review-Token"] = token
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, headers=h, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return resp.status, (
                    json.loads(raw) if "json" in resp.headers["Content-Type"] else raw
                )
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body, **kw):
        return self.call("POST", path, body=body, **kw)


def _serve(fx, public=None):
    viewer = server.Viewer(
        repo=fx["repo"],
        box=Mailbox(fx["box"]),
        worktrees_root=fx["wt"],
        public_origin=public,
        public_url=public,
    )
    assert server.bind(viewer, "127.0.0.1", 18800, 200) is not None
    t = threading.Thread(
        target=viewer.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    t.start()
    return viewer, Client(viewer.port)


@pytest.fixture
def ro(shared_fx):
    viewer, client = _serve(shared_fx, public="https://pub.example.org")
    yield client
    viewer.httpd.shutdown()
    viewer.httpd.server_close()


@pytest.fixture
def rw(fx):
    viewer, client = _serve(fx)
    yield client, fx
    viewer.httpd.shutdown()
    viewer.httpd.server_close()


def test_page_needs_no_token(ro):
    status, body = ro.get("/", token=None)
    assert status == 200 and b"<html" in body


def test_api_needs_a_listed_token(ro):
    assert ro.get("/api/changes", token=None) == (401, {"error": "unauthorized"})
    assert ro.get("/api/changes", token="not-a-listed-token") == (
        401,
        {"error": "unauthorized"},
    )


def test_whoami_names_the_person(ro):
    status, body = ro.get("/api/whoami", token=ENG_TOKEN)
    assert status == 200 and body["person"]["name"] == "Eli Engineer"


def test_origin_checks(ro):
    assert (
        ro.get("/api/whoami", headers={"Origin": "https://evil.example.org"})[0] == 403
    )
    assert (
        ro.get("/api/whoami", headers={"Origin": "https://pub.example.org"})[0] == 200
    )


def test_changes_lists_waiting_on(ro):
    status, body = ro.get("/api/changes")
    assert status == 200
    got = {c["name"]: (c["current_stop"], c["waiting_on"]) for c in body["changes"]}
    assert got["export-csv"] == (1, ["Pat Planner"])
    assert got["dark-mode"] == (3, ["Pat Planner", "Eli Engineer"])
    assert got["rate-limit"] == (4, ["Eli Engineer"])
    assert got["help-page"] == (5, ["Eli Engineer"])


def test_change_and_unknown_change(ro):
    status, body = ro.get("/api/change?name=search-box")
    assert status == 200 and body["stops"][1]["state"] == "waiting"
    assert ro.get("/api/change?name=nope")[0] == 404


def test_doc_inside_the_change_only(ro, shared_fx, tmp_path):
    status, body = ro.get("/api/doc?name=export-csv&path=proposal.md")
    assert status == 200 and body["text"].startswith("# Proposal")
    for bad in ("../../etc/passwd", "/etc/passwd", "..", "../dark-mode/proposal.md"):
        assert ro.get(f"/api/doc?name=export-csv&path={bad}")[0] == 403, bad
    assert ro.get("/api/doc?name=export-csv&path=missing.md")[0] == 404


def test_doc_symlink_out_is_refused(rw, tmp_path):
    client, fx = rw
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    (fx["export-csv"] / "specs/changes/export-csv/link.md").symlink_to(secret)
    assert client.get("/api/doc?name=export-csv&path=link.md")[0] == 403


def test_build_docs_come_from_the_build_worktree(ro):
    status, body = ro.get("/api/doc?name=audit-log&path=reviews/block-01-round-1.json")
    assert status == 200 and body["text"].strip() == '{"score": 4}'


def test_diff_only_for_commits_on_the_build_branch(ro, shared_fx):
    block1 = shared_fx["audit-block1"]
    status, body = ro.get(f"/api/diff?name=audit-log&commit={block1[:10]}")
    assert status == 200 and body["commit"] == block1
    assert body["files"] == [{"path": "src/audit.py", "status": "A"}]
    assert "+def record(event):" in body["diff"]
    base = git(shared_fx["audit-log"], "rev-parse", f"{block1}^")
    assert ro.get(f"/api/diff?name=audit-log&commit={base}")[0] == 403
    main = git(shared_fx["repo"], "rev-parse", "main")
    assert ro.get(f"/api/diff?name=audit-log&commit={main}")[0] == 403
    assert ro.get("/api/diff?name=audit-log&commit=../../x")[0] == 400
    assert ro.get(f"/api/diff?name=export-csv&commit={block1}")[0] == 404


def test_post_needs_json(ro):
    assert ro.post("/api/ask", {"name": "export-csv"}, ctype="text/plain")[0] == 415


def test_ask_writes_a_row_with_the_author(rw):
    client, fx = rw
    status, body = client.post(
        "/api/ask",
        {
            "name": "export-csv",
            "stop": 1,
            "doc": "proposal.md",
            "section": "Why",
            "text": "Why now?",
        },
    )
    assert status == 200
    row = Mailbox(fx["box"]).read_inbox()[-1]
    assert row["id"] == body["id"] and row["kind"] == "ask"
    assert row["author"]["name"] == "Pat Planner" and row["section"] == "Why"
    assert client.post("/api/ask", {"name": "export-csv", "text": ""})[0] == 400


def test_approve_commits_as_the_person(rw):
    client, fx = rw
    status, body = client.post("/api/approve", {"name": "export-csv", "stop": 1})
    assert status == 200, body
    wt = fx["export-csv"]
    assert body["commit"] == git(wt, "rev-parse", "HEAD")
    assert git(wt, "log", "-1", "--format=%an") == "Pat Planner"
    trailers = git(wt, "log", "-1", "--format=%(trailers)")
    assert (
        "Stop: 1" in trailers
        and "Approved-by: Pat Planner" in trailers
        and "Revision:" in trailers
    )
    row = Mailbox(fx["box"]).read_inbox()[-1]
    assert (row["kind"], row["commit"], row["author"]["name"]) == (
        "approve",
        body["commit"],
        "Pat Planner",
    )
    status, again = client.post("/api/approve", {"name": "export-csv", "stop": 1})
    assert status == 200 and again["already"] and again["commit"] == body["commit"]


def test_writes_refused_for_non_reviewers(rw):
    client, _ = rw
    status, body = client.post(
        "/api/approve", {"name": "export-csv", "stop": 1}, token=OTHER_TOKEN
    )
    assert status == 403 and body["error"] == "Oz Onlooker is not a reviewer of stop 1"


def test_comment_ack_rework_and_poll(rw):
    client, fx = rw
    status, body = client.post(
        "/api/comment",
        {
            "name": "search-box",
            "stop": 2,
            "doc": "use-cases.md",
            "section": "UC1",
            "text": "Unclear.",
        },
    )
    assert status == 200 and body["commit"]
    assert client.post("/api/ack", {"name": "search-box", "stop": 2})[0] == 200
    status, body = client.post(
        "/api/rework", {"name": "search-box", "stop": 2, "text": "Merge them."}
    )
    assert status == 200
    assert (
        client.get("/api/change?name=search-box")[1]["stops"][1]["state"]
        == "rework_requested"
    )
    box = Mailbox(fx["box"])
    assert [r["kind"] for r in box.read_inbox()] == ["comment", "ack", "rework"]
    from pipeline_viewer.models import OutboxRow

    box.append_outbox(OutboxRow(reply_to=body["id"], ts="t", text="Noted."))
    status, polled = client.get("/api/poll?since=0")
    assert polled["n"] == 1 and polled["answers"][0]["text"] == "Noted."


def test_write_to_an_unreached_stop_is_409(rw):
    client, _ = rw
    status, body = client.post("/api/ack", {"name": "export-csv", "stop": 3})
    assert status == 409 and body["error"] == "stop 3 of export-csv is not waiting"


def test_push_route(rw):
    client, _ = rw
    status, body = client.post("/api/push", {"name": "export-csv"})
    assert status == 200 and body == {
        "pushed": True,
        "unpushed": False,
        "push_error": None,
    }
