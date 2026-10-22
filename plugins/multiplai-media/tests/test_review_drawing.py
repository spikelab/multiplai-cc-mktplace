"""Drawings on a paused frame: the /api/marks route and `drawing` mailbox rows.

Runs a real server on a loopback port over the synthetic job from
tests/fixtures/make_job.py.
"""

from __future__ import annotations

import http.client
import json
import stat
import sys
import threading
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

import make_job
from stages import review_server as rs

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SHAPES = [
    {"tool": "pen", "color": "#ffd400", "points": [[0.1, 0.1], [0.12345, 0.2]]},
    {"tool": "arrow", "color": "#FF3B30", "points": [[0.5, 0.5], [0.7, 0.4]]},
    {"tool": "box", "color": "#00C2FF", "points": [[0.2, 0.6], [0.4, 0.9]]},
]


@pytest.fixture
def server(tmp_path: Path):
    job = make_job.build(tmp_path / "job", video=False, offers=False)
    (job["dir"] / "clip-01.mp4").write_bytes(b"render")
    httpd, token, _urls = rs.serve(
        job["dir"], job["dir"] / "review", host="127.0.0.1", port=19065, span=200
    )
    t = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    t.start()
    yield {
        "port": httpd.server_address[1],
        "token": token,
        "job": job,
        "box": job["dir"] / "review",
        "httpd": httpd,
    }
    httpd.shutdown()
    httpd.server_close()
    rs.unpublish(job["dir"] / "review")


def _post(srv, path: str, body: bytes, ctype: str, **headers):
    conn = http.client.HTTPConnection("127.0.0.1", srv["port"], timeout=10)
    conn.request(
        "POST",
        path,
        body=body,
        headers={"X-Review-Token": srv["token"], "Content-Type": ctype, **headers},
    )
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res, (json.loads(data) if data else {})


def _mark(srv, body: bytes = JPEG, ctype: str = "image/jpeg", **headers):
    return _post(srv, "/api/marks", body, ctype, **headers)


def _comments(srv, rows: list[dict]):
    return _post(
        srv,
        "/api/comments",
        json.dumps({"comments": rows}).encode(),
        "application/json",
    )


# --- /api/marks ----------------------------------------------------------------------------


def test_a_jpeg_is_saved_privately_under_marks(server) -> None:
    res, data = _mark(server)
    assert res.status == 200, data
    path = server["box"] / "marks" / f"{data['image']}.jpg"
    assert path.read_bytes() == JPEG
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert rs.MARK_ID_RE.match(data["image"])


def test_a_jpeg_of_exactly_4_mb_is_accepted(server) -> None:
    body = JPEG + b"\x00" * (rs.MAX_MARK_BYTES - len(JPEG))
    assert len(body) == 4 * 1024 * 1024
    res, data = _mark(server, body)
    assert res.status == 200, data
    assert (server["box"] / "marks" / f"{data['image']}.jpg").stat().st_size == len(
        body
    )


def test_a_png_is_refused_even_when_labelled_jpeg(server) -> None:
    res, _ = _mark(server, PNG, "image/png")
    assert res.status == 415
    res, data = _mark(server, PNG)  # says JPEG, starts like a PNG
    assert res.status == 400 and "not a JPEG" in data["error"]
    assert not (server["box"] / "marks").exists() or not list(
        (server["box"] / "marks").iterdir()
    )


def test_a_5_mb_body_is_refused_before_it_is_read(server) -> None:
    # Headers only: the server answers from Content-Length and closes the connection.
    conn = http.client.HTTPConnection("127.0.0.1", server["port"], timeout=10)
    conn.putrequest("POST", "/api/marks")
    conn.putheader("X-Review-Token", server["token"])
    conn.putheader("Content-Type", "image/jpeg")
    conn.putheader("Content-Length", str(5 * 1024 * 1024))
    conn.endheaders()
    res = conn.getresponse()
    data = json.loads(res.read())
    conn.close()
    assert res.status == 413 and "bytes" in data["error"]
    assert res.getheader("Connection") == "close"
    assert not (server["box"] / "marks").exists()


def test_a_5_mb_jpeg_sent_whole_is_refused(server) -> None:
    body = JPEG + b"\x00" * (5 * 1024 * 1024 - len(JPEG))
    try:
        res, _ = _mark(server, body)
    except (BrokenPipeError, ConnectionResetError):
        return  # the server answered and closed before the client finished sending
    assert res.status == 413


@pytest.mark.parametrize("body", [b"", b"\xff\xd8"])
def test_an_empty_or_truncated_body_is_refused(server, body) -> None:
    res, _ = _mark(server, body)
    assert res.status == 400


def test_marks_need_the_token_and_this_origin(server) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server["port"], timeout=5)
    conn.request(
        "POST", "/api/marks", body=JPEG, headers={"Content-Type": "image/jpeg"}
    )
    res = conn.getresponse()
    assert res.status == 401
    # The body was never read, so the connection is closed rather than parsed as a next request.
    assert res.getheader("Connection") == "close"
    conn.close()
    res, _ = _mark(server, Origin="https://evil.example")
    assert res.status == 403 and res.getheader("Connection") == "close"


def test_a_refused_comments_body_closes_the_connection(server) -> None:
    res, _ = _post(
        server, "/api/comments", b"x" * (rs.MAX_BODY_BYTES + 1), "application/json"
    )
    assert res.status == 413 and res.getheader("Connection") == "close"


def test_a_refusal_to_a_client_that_already_left_is_dropped_quietly(
    server, monkeypatch
) -> None:
    """Writing the refusal to a client that hung up raises BrokenPipeError; it must not reach
    socketserver's handle_error, which prints a traceback to the terminal the session reads."""
    errors: list = []
    monkeypatch.setattr(
        server["httpd"],
        "handle_error",
        lambda request, address: errors.append(sys.exc_info()),
    )

    def gone(self, *a, **k):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(server["httpd"].RequestHandlerClass, "_json", gone)
    with pytest.raises(http.client.RemoteDisconnected):
        _mark(server, PNG)  # refused (415); the answer cannot be written
    assert errors == []


def test_marks_is_post_only(server) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server["port"], timeout=5)
    conn.request("GET", "/api/marks", headers={"X-Review-Token": server["token"]})
    res = conn.getresponse()
    res.read()
    conn.close()
    assert res.status == 404


# --- drawing rows ---------------------------------------------------------------------------


def _drawing(image: str, **over) -> dict:
    return {
        "kind": "drawing",
        "video": "clip-01",
        "version": 1,
        "t": 2.34567,
        "image": image,
        "shapes": SHAPES,
        "text": "",
        **over,
    }


def test_a_drawing_row_names_a_saved_mark(server) -> None:
    _, mark = _mark(server)
    res, data = _comments(
        server, [_drawing(mark["image"], text="  the logo is cut off ")]
    )
    assert res.status == 200, data
    (row,) = [
        json.loads(x)
        for x in (server["box"] / "comments.jsonl").read_text().splitlines()
    ]
    assert set(row) == {
        "id",
        "kind",
        "video",
        "version",
        "t",
        "image",
        "shapes",
        "text",
        "created_at",
    }
    assert (row["kind"], row["t"], row["image"], row["text"]) == (
        "drawing",
        2.346,
        mark["image"],
        "the logo is cut off",
    )
    assert row["shapes"][0] == {
        "tool": "pen",
        "color": "#FFD400",
        "points": [[0.1, 0.1], [0.1235, 0.2]],
    }
    assert [s["tool"] for s in row["shapes"]] == ["pen", "arrow", "box"]


def test_a_drawing_row_may_have_no_text(server) -> None:
    _, mark = _mark(server)
    res, _ = _comments(
        server, [{k: v for k, v in _drawing(mark["image"]).items() if k != "text"}]
    )
    assert res.status == 200


def test_a_drawing_row_for_an_image_never_posted_is_refused(server) -> None:
    res, data = _comments(server, [_drawing("0" * 32)])
    assert res.status == 400 and "unknown image" in data["error"]
    assert (server["box"] / "comments.jsonl").read_text() == ""


KNOWN = {("clip-01", 1)}


@pytest.fixture
def marks(tmp_path: Path) -> tuple[Path, str]:
    d = tmp_path / "marks"
    d.mkdir()
    image = "a" * 32
    (d / f"{image}.jpg").write_bytes(JPEG)
    return d, image


@pytest.mark.parametrize(
    "over,match",
    [
        ({"image": "../comments"}, "unknown image"),
        ({"image": "A" * 32}, "unknown image"),
        ({"image": None}, "unknown image"),
        ({"t": -1}, "t must be"),
        ({"t": "nan"}, "finite"),
        ({"video": "clip-02"}, "unknown video"),
        ({"shapes": []}, "1..100 shapes"),
        ({"shapes": [SHAPES[0]] * 101}, "1..100 shapes"),
        ({"shapes": [{**SHAPES[0], "tool": "circle"}]}, "tool must be"),
        ({"shapes": [{**SHAPES[0], "color": "yellow"}]}, "#RRGGBB"),
        ({"shapes": [{**SHAPES[0], "width": 3}]}, "tool, color, points"),
        ({"shapes": [{**SHAPES[1], "points": [[0.1, 0.1]]}]}, "2..2 points"),
        (
            {"shapes": [{**SHAPES[2], "points": [[0.1, 0.1], [0.2, 0.2], [0.3, 0.3]]}]},
            "2..2 points",
        ),
        ({"shapes": [{**SHAPES[0], "points": [[0.1, 0.1]] * 2001}]}, "1..2000 points"),
        (
            {
                "shapes": [{**SHAPES[0], "points": [[0.1, 0.1]] * 2000}] * 3
                + [SHAPES[0]]
            },
            "at most 6000 points",
        ),
        ({"shapes": [{**SHAPES[0], "points": [[0.1, 1.5]]}]}, "within 0..1"),
        ({"shapes": [{**SHAPES[0], "points": [[0.1, True]]}]}, "finite"),
        ({"shapes": [{**SHAPES[0], "points": [{"x": 0.1, "y": 0.1}]}]}, r"\[x, y\]"),
        ({"x": 0.5}, "no field"),
    ],
)
def test_drawing_rejected(marks, over, match) -> None:
    d, image = marks
    with pytest.raises(ValueError, match=match):
        rs.validate_comment({**_drawing(image), **over}, KNOWN, marks_dir=d)


def test_the_largest_drawing_row_fits_in_one_comments_body(marks) -> None:
    """The page sends a drawing whole, so the biggest one the server accepts must fit in a body."""
    d, image = marks
    pt = [0.1234, 0.5678]  # the longest a point gets
    boxes = [{"tool": "box", "color": "#FFD400", "points": [pt, pt]}] * (
        rs.MAX_SHAPES - 3
    )
    left = rs.MAX_DRAWING_POINTS - 2 * len(boxes)  # every shape, every point
    pens = [
        {"tool": "pen", "color": "#FFD400", "points": [pt] * n}
        for n in (rs.MAX_PEN_POINTS, rs.MAX_PEN_POINTS, left - 2 * rs.MAX_PEN_POINTS)
    ]
    shapes = pens + boxes
    assert (
        len(shapes) == rs.MAX_SHAPES
        and sum(len(x["points"]) for x in shapes) == rs.MAX_DRAWING_POINTS
    )
    row = {
        **_drawing(image),
        "shapes": shapes,
        "text": "\u0001" * rs.MAX_TEXT,
    }  # 6 bytes each as JSON
    rs.validate_comment(row, KNOWN, marks_dir=d)
    assert len(json.dumps({"comments": [row]}).encode()) < rs.MAX_BODY_BYTES


def test_a_drawing_row_needs_the_marks_directory(marks) -> None:
    _d, image = marks
    with pytest.raises(ValueError, match="unknown image"):
        rs.validate_comment(_drawing(image), KNOWN)
