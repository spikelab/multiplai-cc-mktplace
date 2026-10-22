"""Typed mailbox rows (review_server.validate_comment) and posting them over HTTP."""

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
CANDS = frozenset({"c1", "c2"})
POINT = {
    "video": "clip-01",
    "version": 1,
    "t": 2.5,
    "x": 0.5,
    "y": 0.25,
    "text": "logo too big",
}
PICK = {
    "kind": "pick",
    "candidate": "c1",
    "decision": "accept",
    "start": 9.28,
    "end": 15.6,
    "text": "",
}
PANELS = {
    "kind": "panels",
    "panels": {
        "Host": {"x": 0, "y": 0, "w": 640, "h": 720},
        "Guest": {"x": 640.4, "y": 0, "w": 640, "h": 720},
    },
    "speakers": {"SPEAKER_0": "Host", "SPEAKER_1": "Guest"},
    "text": "guest on the right",
}
LOOK = {
    "kind": "look",
    "style": "clean-tech",
    "brand": "house",
    "captions": {
        "size": 84,
        "position_y": 0.66,
        "words_per_line": 2,
        "highlight": True,
    },
    "text": "",
}
MUSIC = {
    "kind": "music",
    "bed": "calm",
    "volume_db": -12.04,
    "sfx_set": "soft",
    "text": "quieter at the end",
}
OFFERED = {
    "styles": {"clean-tech", "flat-vector"},
    "brands": {"house"},
    "beds": {"calm", "bright"},
    "sfx_sets": {"soft"},
}
COMMON = {"id", "kind", "text", "created_at"}


def _v(row: dict) -> dict:
    return rs.validate_comment(row, KNOWN, CANDS, OFFERED)


# --- valid rows --------------------------------------------------------------------------


def test_a_row_with_no_kind_is_a_point_row() -> None:
    row = _v(POINT)
    assert row["kind"] == "point"
    assert set(row) == COMMON | {"video", "version", "t", "x", "y"}
    assert (row["t"], row["x"], row["y"], row["text"]) == (
        2.5,
        0.5,
        0.25,
        "logo too big",
    )


def test_an_explicit_point_row() -> None:
    assert _v({**POINT, "kind": "point"})["kind"] == "point"


def test_pick_row() -> None:
    row = _v(PICK)
    assert set(row) == COMMON | {"candidate", "decision", "start", "end"}
    assert (
        row["candidate"],
        row["decision"],
        row["start"],
        row["end"],
        row["text"],
    ) == ("c1", "accept", 9.28, 15.6, "")


def test_pick_row_text_is_optional_and_a_reject_is_a_decision() -> None:
    row = _v({k: v for k, v in {**PICK, "decision": "reject"}.items() if k != "text"})
    assert row["decision"] == "reject" and row["text"] == ""


def test_panels_row_rounds_to_source_pixels() -> None:
    row = _v(PANELS)
    assert set(row) == COMMON | {"panels", "speakers"}
    assert row["panels"]["Guest"] == {"x": 640, "y": 0, "w": 640, "h": 720}
    assert row["speakers"] == {"SPEAKER_0": "Host", "SPEAKER_1": "Guest"}


def test_look_row() -> None:
    row = _v(LOOK)
    assert set(row) == COMMON | {"style", "brand", "captions"}
    assert (row["style"], row["brand"]) == ("clean-tech", "house")
    assert row["captions"] == LOOK["captions"]


def test_look_row_may_leave_out_any_two_choices_as_null() -> None:
    row = _v({**LOOK, "style": None, "brand": None})
    assert (row["style"], row["brand"], row["captions"]["size"]) == (None, None, 84)
    assert _v({**LOOK, "captions": None, "brand": None})["style"] == "clean-tech"
    assert (
        _v({**LOOK, "captions": {}})["captions"] == {}
    )  # an option that keeps every default


def test_music_row() -> None:
    row = _v(MUSIC)
    assert set(row) == COMMON | {"bed", "volume_db", "sfx_set"}
    assert (row["bed"], row["volume_db"], row["sfx_set"], row["text"]) == (
        "calm",
        -12.0,
        "soft",
        "quieter at the end",
    )
    assert _v({**MUSIC, "sfx_set": None})["sfx_set"] is None
    assert _v({**MUSIC, "bed": None})["bed"] is None


# --- rejected rows -----------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["comment", "Look", "", None, 3])
def test_unknown_kind_is_rejected(kind) -> None:
    with pytest.raises(ValueError, match="unknown kind"):
        _v({**POINT, "kind": kind})


@pytest.mark.parametrize(
    "row,match",
    [
        ({**POINT, "id": "c-mine"}, "no field"),  # the server assigns ids
        ({**POINT, "candidate": "c1"}, "no field"),  # a pick field on a point row
        (
            {**PICK, "video": "clip-01", "version": 1},
            "no field",
        ),  # picks are not about a render
        ({**PANELS, "colour": "red"}, "no field"),
    ],
)
def test_unknown_fields_are_rejected(row, match) -> None:
    with pytest.raises(ValueError, match=match):
        _v(row)


@pytest.mark.parametrize(
    "bad",
    [
        {**POINT, "text": ""},  # a point needs words
        {**POINT, "video": "clip-02"},
        {**POINT, "x": 1.2},
        {**POINT, "t": True},
    ],
)
def test_point_rejected(bad) -> None:
    with pytest.raises(ValueError):
        _v(bad)


@pytest.mark.parametrize(
    "bad",
    [
        {**PICK, "candidate": "c9"},
        {**PICK, "decision": "maybe"},
        {**PICK, "end": 9.0},
        {**PICK, "start": -1},
        {**PICK, "start": "nan"},
        {k: v for k, v in PICK.items() if k != "end"},
        {**PICK, "text": 5},
    ],
)
def test_pick_rejected(bad) -> None:
    with pytest.raises(ValueError):
        _v(bad)


def test_pick_needs_a_valid_candidates_file() -> None:
    with pytest.raises(ValueError, match="unknown candidate"):
        rs.validate_comment(PICK, KNOWN)


@pytest.mark.parametrize(
    "bad",
    [
        {**PANELS, "panels": {}},
        {**PANELS, "panels": {"A": {"x": 0, "y": 0, "w": 0, "h": 10}}, "speakers": {}},
        {**PANELS, "panels": {"A": {"x": 0, "y": 0, "w": 10}}, "speakers": {}},
        {**PANELS, "panels": {" ": {"x": 0, "y": 0, "w": 10, "h": 10}}, "speakers": {}},
        {**PANELS, "speakers": {"SPEAKER_0": "Nobody"}},
        {**PANELS, "speakers": ["Host"]},
        {**PANELS, "speakers": {"SPEAKER_0": ["Host"]}},
        {**PANELS, "speakers": {"SPEAKER_0": {"Host": 1}}},
        {**PANELS, "speakers": {"SPEAKER_0": 3}},
        {
            **PANELS,
            "panels": {f"P{i}": {"x": 0, "y": 0, "w": 1, "h": 1} for i in range(9)},
            "speakers": {},
        },
    ],
)
def test_panels_rejected(bad) -> None:
    with pytest.raises(ValueError):
        _v(bad)


@pytest.mark.parametrize(
    "bad,match",
    [
        ({**LOOK, "style": "neon"}, "not a style the session offers"),
        ({**LOOK, "brand": "other"}, "not a brand"),
        ({**LOOK, "style": 3}, "not a style"),
        ({**LOOK, "captions": {"size": 2}}, "captions.size: must be >= 8"),
        ({**LOOK, "captions": {"font": "Comic Sans"}}, "unknown field 'font'"),
        ({**LOOK, "captions": {"highlight": "yes"}}, "expected boolean"),
        ({**LOOK, "captions": [84]}, "expected object"),
        ({**LOOK, "style": None, "brand": None, "captions": None}, "must pick"),
        ({k: v for k, v in LOOK.items() if k != "brand"}, "need brand"),
        ({**LOOK, "video": "clip-01", "version": 1}, "no field"),
    ],
)
def test_look_rejected(bad, match) -> None:
    with pytest.raises(ValueError, match=match):
        _v(bad)


def test_look_needs_a_valid_looks_file() -> None:
    with pytest.raises(ValueError, match="not a style the session offers"):
        rs.validate_comment(LOOK, KNOWN, CANDS)


@pytest.mark.parametrize(
    "bad,match",
    [
        ({**MUSIC, "bed": "jazz"}, "not a music bed"),
        ({**MUSIC, "sfx_set": "loud"}, "not a sound-effect set"),
        ({**MUSIC, "volume_db": 3}, "volume_db must be within"),
        ({**MUSIC, "volume_db": -61}, "volume_db must be within"),
        ({**MUSIC, "volume_db": "nan"}, "finite"),
        ({**MUSIC, "volume_db": True}, "finite"),
        ({**MUSIC, "bed": None, "sfx_set": None}, "must pick"),
        ({k: v for k, v in MUSIC.items() if k != "volume_db"}, "need volume_db"),
        ({**MUSIC, "level": -3}, "no field"),
    ],
)
def test_music_rejected(bad, match) -> None:
    with pytest.raises(ValueError, match=match):
        _v(bad)


def test_offered_names_come_from_valid_files_only(tmp_path: Path) -> None:
    job = make_job.build(tmp_path / "job", video=False)
    names = rs.offered_names(job["dir"])
    assert names == {
        "styles": {"clean-tech", "flat-vector"},
        "brands": {"house", "warm"},
        "beds": {"calm", "bright"},
        "sfx_sets": {"soft", "clicky"},
    }
    job["music"].write_text('{"beds": [{"name": "x"}]}')  # invalid: no file
    assert rs.offered_names(job["dir"])["beds"] == set()


# --- over HTTP ----------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path: Path):
    job = make_job.build(tmp_path / "job", video=False)
    (job["dir"] / "clip-01.mp4").write_bytes(b"render")
    httpd, token, _urls = rs.serve(
        job["dir"], job["dir"] / "review", host="127.0.0.1", port=18965, span=200
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
    }
    httpd.shutdown()
    httpd.server_close()
    rs.unpublish(job["dir"] / "review")


def _req(srv, method: str, path: str, body: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", srv["port"], timeout=5)
    headers = {"X-Review-Token": srv["token"]}
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn.request(
        method,
        path,
        body=json.dumps(body).encode() if body is not None else None,
        headers=headers,
    )
    res = conn.getresponse()
    data = json.loads(res.read())
    conn.close()
    return res, data


def test_a_mixed_batch_lands_in_the_mailbox_and_filters_by_kind(server) -> None:
    res, data = _req(
        server, "POST", "/api/comments", {"comments": [POINT, PICK, PANELS]}
    )
    assert res.status == 200, data
    rows = [
        json.loads(x)
        for x in (server["box"] / "comments.jsonl").read_text().splitlines()
    ]
    assert [r["kind"] for r in rows] == ["point", "pick", "panels"]
    _, picks = _req(server, "GET", "/api/comments?kind=pick")
    assert [r["candidate"] for r in picks["comments"]] == ["c1"]
    _, points = _req(server, "GET", "/api/comments?video=clip-01&kind=point")
    assert [r["text"] for r in points["comments"]] == ["logo too big"]


def test_rows_written_before_kinds_count_as_points(server) -> None:
    (server["box"] / "comments.jsonl").write_text(
        json.dumps({**POINT, "id": "c-old", "created_at": "x"}) + "\n"
    )
    _, points = _req(server, "GET", "/api/comments?kind=point")
    assert [r["id"] for r in points["comments"]] == ["c-old"]


def test_look_and_music_rows_name_what_the_job_offers(server) -> None:
    look = {**LOOK, "brand": "warm"}
    music_row = {**MUSIC, "sfx_set": "clicky"}
    res, data = _req(server, "POST", "/api/comments", {"comments": [look, music_row]})
    assert res.status == 200, data
    rows = [
        json.loads(x)
        for x in (server["box"] / "comments.jsonl").read_text().splitlines()
    ]
    assert [(r["kind"], r.get("brand"), r.get("sfx_set")) for r in rows] == [
        ("look", "warm", None),
        ("music", None, "clicky"),
    ]
    res, data = _req(
        server, "POST", "/api/comments", {"comments": [{**MUSIC, "bed": "jazz"}]}
    )
    assert res.status == 400 and "jazz" in data["error"]


def test_a_pick_for_a_candidate_not_on_file_rejects_the_batch(server) -> None:
    res, data = _req(
        server,
        "POST",
        "/api/comments",
        {"comments": [PICK, {**PICK, "candidate": "c7"}]},
    )
    assert res.status == 400 and "c7" in data["error"]
    assert (server["box"] / "comments.jsonl").read_text() == ""


def test_a_speaker_mapped_to_a_list_is_a_400_not_a_dropped_connection(server) -> None:
    res, data = _req(
        server,
        "POST",
        "/api/comments",
        {"comments": [{**PANELS, "speakers": {"SPEAKER_0": ["Host"]}}]},
    )
    assert res.status == 400 and "SPEAKER_0" in data["error"]
    assert (server["box"] / "comments.jsonl").read_text() == ""
