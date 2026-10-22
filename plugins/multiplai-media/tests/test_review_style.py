"""The Review tab's style panel: GET /api/style and `style` mailbox rows."""
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

STYLES = _SCRIPTS.parent / "references" / "styles"
KNOWN = {("clip-01", 1)}
CAPTIONS = {"words_per_line": 3, "max_chars": 22, "position_y": 0.68, "highlight": True, "size": None}


def sidecar(tmp: Path, **edl) -> dict:
    page = tmp / "card.html"
    page.write_text("<p>card</p>")
    base = {"source": "/rec/x.mp4", "segments": [], "output": {"width": 720, "height": 1280},
            "captions": dict(CAPTIONS), "brand": None,
            "overlays": [{"html": str(page), "start": 1.0, "end": 3.0, "mode": "alpha",
                          "region": {"x": 40, "y": 860, "w": 640, "h": 200}, "style": "clean-tech"}]}
    return {"format": 1, "video": "clip-01.mp4", "edl_file": str(tmp / "edl.json"), "edl": {**base, **edl},
            "duration": 8.0, "segments": [], "captions_events": []}


STYLE = {"kind": "style", "video": "clip-01", "version": 1, "target": "captions",
         "changes": {"size": [46, 60], "highlight": [True, False]}, "text": "bigger, no highlight"}


def _v(row: dict, record: dict | None) -> dict:
    return rs.validate_comment(row, KNOWN, records={("clip-01", 1): record})


# --- style rows ---------------------------------------------------------------------------------

def test_a_caption_style_row(tmp_path: Path) -> None:
    row = _v(STYLE, sidecar(tmp_path))
    assert set(row) == {"id", "kind", "video", "version", "target", "changes", "save_as", "text", "created_at"}
    assert row["changes"] == {"size": [46, 60], "highlight": [True, False]} and row["save_as"] is None


def test_a_brand_row_with_save_as_uppercases_its_colours(tmp_path: Path) -> None:
    row = _v({**STYLE, "target": "brand", "changes": {"accent": ["#FFD400", "#ff6b4a"]}, "save_as": "warm-2"},
             sidecar(tmp_path))
    assert row["changes"] == {"accent": ["#FFD400", "#FF6B4A"]} and row["save_as"] == "warm-2"


def test_a_style_row_for_a_style_the_render_uses(tmp_path: Path) -> None:
    changes = {"palette.accent": ["#3DDC97", "#FF0000"], "fonts.display": ["Inter", "Georgia, serif"],
               "fonts.weights.display": [800, 600], "shape.radius_px": [18, 4], "motion.enter_s": [0.5, 0.8]}
    row = _v({**STYLE, "target": "style:clean-tech", "changes": changes, "save_as": "clean-tech-red"},
             sidecar(tmp_path))
    assert row["target"] == "style:clean-tech" and row["changes"]["palette.accent"] == ["#3DDC97", "#FF0000"]


@pytest.mark.parametrize("over,match", [
    ({"target": "headline"}, "target must be"),
    ({"target": "style:flat-vector"}, "no overlay in this render uses style"),
    ({"target": "style:"}, "target must be"),
    ({"changes": {}}, "changes must map"),
    ({"changes": {"size": 60}}, r"must be \[old, new\]"),
    ({"changes": {"size": [46, 60, 1]}}, r"must be \[old, new\]"),
    ({"changes": {"size": [{"x": 1}, 60]}}, r"must be \[old, new\]"),
    ({"changes": {"size": [46, 46]}}, "the new value is the old one"),
    ({"changes": {"size": [46, 4]}}, "must be >= 8"),
    ({"changes": {"size": [46, 60.5]}}, "integer"),
    ({"changes": {"position_y": [0.68, 1.5]}}, "must be <= 1"),
    ({"changes": {"font": ["a", "b"]}}, "not allowed|unknown|additional"),
    ({"changes": {"highlight": [True, "no"]}}, "boolean"),
    ({"save_as": "nice"}, "save_as is for a brand or a style"),
    ({"target": "brand", "changes": {"accent": ["#FFD400", "red"]}}, "#RRGGBB"),
    ({"target": "brand", "changes": {"logo": ["a", "b"]}}, "brand has no"),
    ({"target": "brand", "changes": {"accent": ["#FFD400", "#FF0000"]}, "save_as": "../x"}, "save_as must be"),
    ({"target": "brand", "changes": {"accent": ["#FFD400", "#FF0000"]}, "save_as": "a/b"}, "save_as must be"),
    ({"target": "style:clean-tech", "changes": {"palette.accent": ["#3DDC97", "#FF000"]}}, "#RRGGBB"),
    ({"target": "style:clean-tech", "changes": {"motion.easing": ["a", "b"]}}, "panel edits"),
    ({"target": "style:clean-tech", "changes": {"shape.radius_px": [18, -1]}}, "within 0..1000"),
    ({"target": "style:clean-tech", "changes": {"motion.enter_s": [0.5, 11]}}, "within 0..10"),
    ({"target": "style:clean-tech", "changes": {"motion.enter_s": [0.5, "0.8"]}}, "must be a number"),
    ({"target": "style:clean-tech", "changes": {"shape.radius_px": [18, "18"]}}, "must be a number"),
    ({"target": "style:clean-tech", "changes": {"shape.radius_px": [18, True]}}, "must be a number"),
    ({"target": "style:clean-tech", "changes": {"fonts.weights.body": [500, 550.5]}}, "whole number"),
    ({"target": "style:clean-tech", "changes": {"fonts.body": ["Inter", "x; } body { color: red"]}}, "one CSS value"),
    ({"target": "style:clean-tech", "changes": {"fonts.body": ["Inter", "x /* y */"]}}, "one CSS value"),
    ({"target": "style:clean-tech", "changes": {"fonts.body": ["Inter", "Evil, url(http://x/f.woff)"]}}, "url"),
    ({"video": "clip-02"}, "unknown video"),
    ({"t": 1.0}, "no field"),
])
def test_style_rejected(tmp_path: Path, over, match) -> None:
    with pytest.raises(ValueError, match=match):
        _v({**STYLE, **over}, sidecar(tmp_path))


def test_a_style_row_needs_the_renders_sidecar() -> None:
    with pytest.raises(ValueError, match="no .edl.json"):
        _v(STYLE, None)


def test_captions_cannot_be_restyled_on_a_render_without_captions(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no captions"):
        _v(STYLE, sidecar(tmp_path, captions=None))


def test_a_style_named_by_path_is_a_valid_target(tmp_path: Path) -> None:
    path = str(STYLES / "flat-vector.md")
    record = sidecar(tmp_path)
    record["edl"]["overlays"][0]["style"] = path
    row = _v({**STYLE, "target": f"style:{path}", "changes": {"palette.accent": ["#FF6B4A", "#000000"]}}, record)
    assert row["target"] == f"style:{path}"


# --- which style file a name reads ----------------------------------------------------------------

def _offer(job: dict, styles: list[dict]) -> None:
    looks = json.loads(job["looks"].read_text())
    looks["styles"] = styles
    job["looks"].write_text(json.dumps(looks))


def test_style_summary_reads_the_file_looks_json_names_before_the_shipped_one(tmp_path: Path) -> None:
    job = make_job.build(tmp_path / "job", video=False, offers=True)
    own = tmp_path / "clean-tech-house.md"
    own.write_text((STYLES / "clean-tech.md").read_text().replace("#3DDC97", "#123456"))
    sample = job["dir"] / "looks" / "clean-tech.html"
    _offer(job, [{"name": "clean-tech", "file": str(own), "sample_html": str(sample)},
                 {"name": "house-only", "file": str(own), "sample_html": str(sample)}])
    got = rs.style_summary("clean-tech", job["dir"])
    assert got["status"] == "ok" and got["file"] == str(own) and got["values"]["palette.accent"] == "#123456"
    # a name only looks.json lists resolves too
    assert rs.style_summary("house-only", job["dir"])["file"] == str(own)
    # a shipped style looks.json does not list is still found among the shipped ones
    assert rs.style_summary("flat-vector", job["dir"])["file"] == str(STYLES / "flat-vector.md")


def test_style_summary_falls_back_to_the_shipped_style_when_looks_json_is_invalid(tmp_path: Path) -> None:
    job = make_job.build(tmp_path / "job", video=False, offers=True)
    job["looks"].write_text("{not json")
    assert rs.style_summary("clean-tech", job["dir"])["file"] == str(STYLES / "clean-tech.md")
    assert rs.style_summary("house-only", job["dir"])["status"] == "missing"


# --- /api/style ---------------------------------------------------------------------------------

@pytest.fixture
def server(tmp_path: Path):
    job = make_job.build(tmp_path / "job", video=False, offers=True)
    (job["dir"] / "clip-01.mp4").write_bytes(b"render")
    httpd, token, _urls = rs.serve(job["dir"], job["dir"] / "review", host="127.0.0.1", port=19465, span=200)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield {"port": httpd.server_address[1], "token": token, "job": job, "tmp": tmp_path}
    httpd.shutdown()
    httpd.server_close()
    rs.unpublish(job["dir"] / "review")


def _req(srv, method: str, path: str, body: bytes | None = None, token: bool = True):
    conn = http.client.HTTPConnection("127.0.0.1", srv["port"], timeout=5)
    headers = {"X-Review-Token": srv["token"]} if token else {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=body, headers=headers)
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res.status, json.loads(data) if data else None


def _write(srv, record: dict) -> None:
    (srv["job"]["dir"] / "clip-01.edl.json").write_text(json.dumps(record))


def test_style_route_shows_captions_brand_and_each_style_once(server) -> None:
    record = sidecar(server["tmp"], brand=str(server["job"]["dir"] / "brands" / "warm.json"))
    record["edl"]["overlays"].append({**record["edl"]["overlays"][0], "start": 4.0, "end": 5.0})   # same style again
    _write(server, record)
    status, body = _req(server, "GET", "/api/style?video=clip-01&version=1")
    assert status == 200
    assert body["output"] == {"width": 720, "height": 1280} and body["captions"] == CAPTIONS
    assert body["brand"]["status"] == "ok" and body["brand"]["colours"]["accent"] == "#FF6B4A"
    assert body["brand"]["file"].endswith("brands/warm.json")
    [style] = body["styles"]
    # looks.json (make_job) names clean-tech's shipped file, so that is the one read
    assert style["name"] == "clean-tech" and style["status"] == "ok" and style["file"] == str(STYLES / "clean-tech.md")
    assert style["values"]["palette.accent"] == "#3DDC97" and style["values"]["motion.stagger_s"] == 0.12


def test_style_route_gives_the_default_brand_and_reports_missing_styles(server) -> None:
    record = sidecar(server["tmp"])
    record["edl"]["overlays"][0]["style"] = "no-such-style"
    _write(server, record)
    _, body = _req(server, "GET", "/api/style?video=clip-01&version=1")
    assert body["brand"]["file"] is None and body["brand"]["colours"]["accent"] == "#FFD400"
    assert body["styles"] == [{"name": "no-such-style", "status": "missing",
                               "error": "no style file for 'no-such-style': not in looks.json, not shipped, "
                                        "not a path to a .md file"}]


def test_style_route_reports_a_style_file_it_cannot_read(server) -> None:
    bad = server["tmp"] / "bad.md"
    bad.write_text("---\npalette:\n\taccent: '#fff'\n---\n")
    record = sidecar(server["tmp"])
    record["edl"]["overlays"][0]["style"] = str(bad)
    _write(server, record)
    _, body = _req(server, "GET", "/api/style?video=clip-01&version=1")
    assert body["styles"][0]["status"] == "invalid" and "tabs" in body["styles"][0]["error"]


def test_style_route_is_404_without_a_sidecar_and_needs_the_token(server) -> None:
    assert _req(server, "GET", "/api/style?video=clip-01&version=1")[0] == 404
    _write(server, sidecar(server["tmp"]))
    assert _req(server, "GET", "/api/style?video=clip-01&version=1", token=False)[0] == 401
    assert _req(server, "GET", "/api/style?video=clip-02&version=1")[0] == 404
    assert _req(server, "GET", "/api/style?video=clip-01&version=x")[0] == 400


def test_style_rows_are_checked_against_the_sidecar_and_land_in_the_mailbox(server) -> None:
    body = json.dumps({"comments": [STYLE]}).encode()
    status, res = _req(server, "POST", "/api/comments", body)
    assert status == 400 and "no .edl.json" in res["error"]                 # no sidecar yet
    _write(server, sidecar(server["tmp"]))
    status, res = _req(server, "POST", "/api/comments", body)
    assert status == 200, res
    _, got = _req(server, "GET", "/api/comments?video=clip-01&kind=style")
    assert [r["changes"] for r in got["comments"]] == [STYLE["changes"]]
