"""`range` mailbox rows: a stretch of a render, optionally naming a timeline block."""
from __future__ import annotations

import http.client
import json
import sys
import threading
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

import make_job
from stages import review_server as rs

KNOWN = {("clip-01", 1)}
RANGE = {"kind": "range", "video": "clip-01", "version": 1, "t_start": 1.00049, "t_end": 3.0,
         "target": "overlay:2", "text": "this card comes in too late"}


def _v(row: dict) -> dict:
    return rs.validate_comment(row, KNOWN)


def test_a_range_row() -> None:
    row = _v(RANGE)
    assert set(row) == {"id", "kind", "video", "version", "t_start", "t_end", "target", "text", "created_at"}
    assert (row["t_start"], row["t_end"], row["target"]) == (1.0, 3.0, "overlay:2")


@pytest.mark.parametrize("target", ["captions", "headline", "title", "segment:0", "segment:12", "sfx:3", "overlay:0"])
def test_every_timeline_target_is_accepted(target) -> None:
    assert _v({**RANGE, "target": target})["target"] == target


def test_the_target_is_optional() -> None:
    assert _v({k: v for k, v in RANGE.items() if k != "target"})["target"] is None
    assert _v({**RANGE, "target": None})["target"] is None


@pytest.mark.parametrize("over,match", [
    ({"text": ""}, "text must be"),                    # a range says what to change
    ({"text": "   "}, "text must be"),
    ({"t_end": 1.0}, "t_start < t_end"),
    ({"t_start": -0.5}, "t_start < t_end"),
    ({"t_start": "nan"}, "finite"),
    ({"t_end": None}, "numbers"),
    ({"target": "overlay"}, "target must be"),
    ({"target": "overlay:-1"}, "target must be"),
    ({"target": "overlay:2;rm"}, "target must be"),
    ({"target": "caption:1"}, "target must be"),
    ({"target": 2}, "target must be"),
    ({"video": "clip-02"}, "unknown video"),
    ({"t": 1.0}, "no field"),
])
def test_range_rejected(over, match) -> None:
    with pytest.raises(ValueError, match=match):
        _v({**RANGE, **over})


def test_a_range_without_its_ends_is_rejected() -> None:
    with pytest.raises(ValueError, match="need t_end"):
        _v({k: v for k, v in RANGE.items() if k != "t_end"})


@pytest.fixture
def server(tmp_path: Path):
    job = make_job.build(tmp_path / "job", video=False, offers=False)
    (job["dir"] / "clip-01.mp4").write_bytes(b"render")
    httpd, token, _urls = rs.serve(job["dir"], job["dir"] / "review", host="127.0.0.1", port=19165, span=200)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield {"port": httpd.server_address[1], "token": token, "box": job["dir"] / "review"}
    httpd.shutdown()
    httpd.server_close()
    rs.unpublish(job["dir"] / "review")


def test_range_rows_land_in_the_mailbox_and_filter_by_kind(server) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server["port"], timeout=5)
    body = json.dumps({"comments": [RANGE, {**RANGE, "target": None, "text": "cut this"}]}).encode()
    conn.request("POST", "/api/comments", body=body,
                 headers={"X-Review-Token": server["token"], "Content-Type": "application/json"})
    res = conn.getresponse()
    assert res.status == 200, res.read()
    res.read()
    conn.request("GET", "/api/comments?video=clip-01&kind=range", headers={"X-Review-Token": server["token"]})
    rows = json.loads(conn.getresponse().read())["comments"]
    conn.close()
    assert [(r["target"], r["text"]) for r in rows] == [("overlay:2", "this card comes in too late"), (None, "cut this")]
