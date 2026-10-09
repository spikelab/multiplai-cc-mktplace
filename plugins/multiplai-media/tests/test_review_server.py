"""Review page server (stages/review_server.py): Range, mailbox, token, origin.

Runs a real server on a loopback port with stand-in video files (bytes, not
media: the server never decodes them).
"""
from __future__ import annotations

import http.client
import json
import sys
import threading
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import review_server as rs  # noqa: E402

PAYLOAD = bytes(range(256)) * 40          # 10240 bytes


@pytest.fixture
def server(tmp_path: Path):
    videos = tmp_path / "renders"
    videos.mkdir()
    (videos / "clip-01.mp4").write_bytes(PAYLOAD)
    (videos / "clip-01.v2.mp4").write_bytes(PAYLOAD[:5000])
    (videos / "clip-02.mp4").write_bytes(b"x" * 10)
    (videos / "notes.txt").write_text("not a video")
    httpd, token, _urls = rs.serve(videos, tmp_path / "box", host="127.0.0.1", port=18765, span=200)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield {"port": httpd.server_address[1], "token": token, "videos": videos, "box": tmp_path / "box"}
    httpd.shutdown()
    httpd.server_close()
    rs.unpublish(tmp_path / "box")


def _req(srv, method: str, path: str, headers: dict | None = None, body: bytes | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", srv["port"], timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res, data


def _auth(srv, **extra) -> dict:
    return {"X-Review-Token": srv["token"], **extra}


# --- parse_range ---------------------------------------------------------------

@pytest.mark.parametrize("header,expected", [
    (None, None),
    ("bytes=0-99", (0, 99)),
    ("bytes=100-", (100, 999)),
    ("bytes=-200", (800, 999)),
    ("bytes=-5000", (0, 999)),                # suffix longer than the file: whole file
    ("bytes=900-5000", (900, 999)),           # last past the end is clamped
    ("bytes=0-0", (0, 0)),
    ("bytes=0-9,20-29", None),                # several ranges: whole file
])
def test_parse_range(header, expected) -> None:
    assert rs.parse_range(header, 1000) == expected


@pytest.mark.parametrize("header", ["bytes=1000-", "bytes=50-10", "bytes=-0", "bytes=a-b",
                                    "items=0-9", "bytes=", "bytes=5"])
def test_parse_range_rejects(header) -> None:
    with pytest.raises(rs.RangeNotSatisfiable):
        rs.parse_range(header, 1000)


# --- media over HTTP -------------------------------------------------------------

def test_full_file_is_200_with_accept_ranges(server) -> None:
    res, data = _req(server, "GET", "/media/clip-01.mp4", _auth(server))
    assert res.status == 200
    assert res.getheader("Accept-Ranges") == "bytes"
    assert data == PAYLOAD


def test_range_is_206_with_content_range(server) -> None:
    res, data = _req(server, "GET", "/media/clip-01.mp4", _auth(server, Range="bytes=0-99"))
    assert res.status == 206
    assert res.getheader("Content-Range") == f"bytes 0-99/{len(PAYLOAD)}"
    assert res.getheader("Content-Length") == "100"
    assert data == PAYLOAD[:100]


def test_open_ended_range(server) -> None:
    res, data = _req(server, "GET", "/media/clip-01.mp4", _auth(server, Range="bytes=10000-"))
    assert res.status == 206
    assert res.getheader("Content-Range") == f"bytes 10000-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"
    assert data == PAYLOAD[10000:]


def test_suffix_range(server) -> None:
    res, data = _req(server, "GET", "/media/clip-01.mp4", _auth(server, Range="bytes=-300"))
    assert res.status == 206
    assert data == PAYLOAD[-300:]


def test_invalid_range_is_416(server) -> None:
    res, _ = _req(server, "GET", "/media/clip-01.mp4", _auth(server, Range=f"bytes={len(PAYLOAD)}-"))
    assert res.status == 416
    assert res.getheader("Content-Range") == f"bytes */{len(PAYLOAD)}"


def test_media_token_in_query_for_the_video_element(server) -> None:
    res, _ = _req(server, "GET", f"/media/clip-01.mp4?t={server['token']}", {"Range": "bytes=0-9"})
    assert res.status == 206


@pytest.mark.parametrize("name", ["notes.txt", "..%2Fbox%2Fserver.token", ".hidden.mp4", "missing.mp4"])
def test_media_serves_only_videos_in_the_directory(server, name) -> None:
    res, _ = _req(server, "GET", f"/media/{name}", _auth(server))
    assert res.status == 404


# --- videos and versions ---------------------------------------------------------

def test_videos_lists_clips_with_versions(server) -> None:
    res, data = _req(server, "GET", "/api/videos", _auth(server))
    assert res.status == 200
    assert json.loads(data)["videos"] == [
        {"clip": "clip-01", "versions": [{"version": 1, "name": "clip-01.mp4"},
                                          {"version": 2, "name": "clip-01.v2.mp4"}]},
        {"clip": "clip-02", "versions": [{"version": 1, "name": "clip-02.mp4"}]},
    ]


def test_next_version_path(tmp_path: Path) -> None:
    (tmp_path / "a.mp4").write_bytes(b"")
    (tmp_path / "a.v3.mp4").write_bytes(b"")
    assert rs.next_version_path(tmp_path, "a") == tmp_path / "a.v4.mp4"
    assert rs.next_version_path(tmp_path, "new") == tmp_path / "new.v1.mp4"


# --- mailbox -----------------------------------------------------------------------

def _post(server, comments, **headers):
    body = json.dumps({"comments": comments}).encode()
    return _req(server, "POST", "/api/comments",
                {"Content-Type": "application/json", **_auth(server), **headers}, body)


def test_comment_round_trip_through_the_mailbox(server) -> None:
    batch = [{"video": "clip-01", "version": 2, "t": 3.25, "x": 0.5, "y": 0.68, "text": "caption too low"},
             {"video": "clip-01", "version": 2, "t": 7.0, "x": 0.1, "y": 0.1, "text": "cut the logo"}]
    res, data = _post(server, batch)
    assert res.status == 200
    ids = json.loads(data)["ids"]
    rows = [json.loads(line) for line in (server["box"] / "comments.jsonl").read_text().splitlines()]
    assert [r["id"] for r in rows] == ids
    assert set(rows[0]) == {"id", "video", "version", "t", "x", "y", "text", "created_at"}
    assert (rows[0]["video"], rows[0]["version"], rows[0]["t"], rows[0]["text"]) == ("clip-01", 2, 3.25, "caption too low")
    res, data = _req(server, "GET", "/api/comments?video=clip-01", _auth(server))
    assert [c["id"] for c in json.loads(data)["comments"]] == ids
    res, data = _req(server, "GET", "/api/comments?video=clip-02", _auth(server))
    assert json.loads(data)["comments"] == []


@pytest.mark.parametrize("bad", [
    {"video": "clip-09", "version": 1, "t": 1, "x": 0.5, "y": 0.5, "text": "no such clip"},
    {"video": "clip-01", "version": 7, "t": 1, "x": 0.5, "y": 0.5, "text": "no such version"},
    {"video": "clip-01", "version": 1, "t": None, "x": 0.5, "y": 0.5, "text": "no time"},
    {"video": "clip-01", "version": 1, "t": 1, "x": 1.5, "y": 0.5, "text": "outside the picture"},
    {"video": "clip-01", "version": 1, "t": 1, "x": 0.5, "y": 0.5, "text": "   "},
])
def test_invalid_comment_rejects_the_whole_batch(server, bad) -> None:
    good = {"video": "clip-01", "version": 1, "t": 1, "x": 0.5, "y": 0.5, "text": "fine"}
    res, _ = _post(server, [good, bad])
    assert res.status == 400
    assert (server["box"] / "comments.jsonl").read_text() == ""


@pytest.mark.parametrize("t", ["nan", "inf", "Infinity", float("nan"), float("inf")])
def test_non_finite_time_is_rejected(server, t) -> None:
    # A float in the list goes out as a bare NaN / Infinity token, which json.loads accepts.
    res, _ = _post(server, [{"video": "clip-01", "version": 1, "t": t, "x": 0.5, "y": 0.5, "text": "when?"}])
    assert res.status == 400
    assert (server["box"] / "comments.jsonl").read_text() == ""


def test_post_needs_json_content_type(server) -> None:
    res, _ = _req(server, "POST", "/api/comments", {"Content-Type": "text/plain", **_auth(server)}, b"{}")
    assert res.status == 415


# --- token and origin --------------------------------------------------------------

@pytest.mark.parametrize("path", ["/api/videos", "/media/clip-01.mp4", "/api/comments"])
def test_requests_without_the_token_are_rejected(server, path) -> None:
    res, _ = _req(server, "GET", path)
    assert res.status == 401
    res, _ = _req(server, "GET", path, {"X-Review-Token": "wrong"})
    assert res.status == 401


def test_foreign_origin_is_rejected_even_with_the_token(server) -> None:
    res, _ = _post(server, [{"video": "clip-01", "version": 1, "t": 1, "x": 0.5, "y": 0.5, "text": "x"}],
                   Origin="https://evil.example")
    assert res.status == 403
    assert (server["box"] / "comments.jsonl").read_text() == ""


def test_same_origin_is_allowed(server) -> None:
    res, _ = _req(server, "GET", "/api/videos",
                  _auth(server, Origin=f"http://127.0.0.1:{server['port']}"))
    assert res.status == 200


def test_page_and_assets_need_no_token_and_carry_csp(server) -> None:
    for path in ("/", "/static/review.js", "/static/review.css"):
        res, _ = _req(server, "GET", path)
        assert res.status == 200, path
        assert "default-src 'none'" in (res.getheader("Content-Security-Policy") or "")
    res, _ = _req(server, "GET", "/static/../stages/review_server.py")
    assert res.status == 404


def test_token_files_are_private_and_removed_on_unpublish(server) -> None:
    box = rs.Mailbox(server["box"])
    assert box.token_file.read_text().strip() == server["token"]
    assert box.token_file.stat().st_mode & 0o077 == 0
    assert server["token"] in box.open_html.read_text()
    rs.unpublish(server["box"])
    assert not box.token_file.exists() and not box.open_html.exists()


def test_page_loads_nothing_from_a_cdn() -> None:
    static = _SCRIPTS / "static"
    for f in static.iterdir():
        text = f.read_text()
        assert "http://" not in text and "https://" not in text, f.name


@pytest.mark.parametrize("sig", ["SIGTERM", "SIGHUP"])
def test_stopping_the_review_command_removes_the_token_files(tmp_path: Path, sig: str) -> None:
    import signal
    import subprocess
    import time
    videos = tmp_path / "renders"
    videos.mkdir()
    (videos / "a.mp4").write_bytes(b"x")
    box = rs.Mailbox(tmp_path / "box")
    proc = subprocess.Popen([sys.executable, str(_SCRIPTS / "pipeline.py"), "review", str(videos),
                             "--mailbox", str(box.dir), "--port", "19765"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env={"PATH": "/usr/bin:/bin", "MULTIPLAI_CONTAINER": "0"})
    try:
        deadline = time.monotonic() + 10
        while not box.token_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert box.token_file.exists() and box.open_html.exists()
        proc.send_signal(getattr(signal, sig))
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not box.token_file.exists() and not box.open_html.exists()
    assert box.comments.exists()
