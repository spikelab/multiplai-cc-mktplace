"""Live overlays on the Review tab: GET /media/overlay, the keys /api/edl hands out, and `restyle` rows."""
from __future__ import annotations

import http.client
import json
import sys
import threading
from pathlib import Path
from urllib.parse import urlencode

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

import make_job  # noqa: E402
from stages import review_server as rs  # noqa: E402

KNOWN = {("clip-01", 1)}


def sidecar(page: Path, **edl) -> dict:
    base = {"source": "/rec/x.mp4", "segments": [], "output": {"width": 720, "height": 1280}, "captions": None,
            "overlays": [{"html": str(page), "start": 1.0, "end": 3.0, "mode": "keyed",
                          "region": {"x": 40, "y": 860, "w": 640, "h": 200}, "style": "flat-vector"}]}
    return {"format": 1, "video": "clip-01.mp4", "edl_file": "/jobs/x/edl.json", "edl": {**base, **edl},
            "duration": 8.0, "segments": [], "captions_events": []}


def restyle(page: Path, **over) -> dict:
    return {"kind": "restyle", "video": "clip-01", "version": 1, "t": 1.80049, "overlay": 0, "html": str(page),
            "selector": "body > div.card > div.t", "changes": {"color": ["rgb(31, 42, 68)", "#FF3B30"],
                                                               "font-size": ["56px", "64px"]},
            "text": "", **over}


def _v(row: dict, record: dict | None) -> dict:
    return rs.validate_comment(row, KNOWN, records={("clip-01", 1): record})


# --- restyle rows -------------------------------------------------------------------------------

def test_a_restyle_row(tmp_path: Path) -> None:
    page = tmp_path / "card.html"
    row = _v(restyle(page), sidecar(page))
    assert set(row) == {"id", "kind", "video", "version", "t", "overlay", "html", "selector", "changes", "text",
                        "created_at"}
    assert (row["t"], row["overlay"], row["html"]) == (1.8, 0, str(page))
    assert row["changes"] == {"color": ["rgb(31, 42, 68)", "#FF3B30"], "font-size": ["56px", "64px"]}


@pytest.mark.parametrize("over,match", [
    ({"overlay": 1}, "overlay must be the index"),
    ({"overlay": -1}, "overlay must be the index"),
    ({"overlay": True}, "overlay must be the index"),
    ({"overlay": "0"}, "overlay must be the index"),
    ({"html": "/elsewhere/card.html"}, "html must be overlay 0's page"),
    ({"selector": ""}, "selector must be"),
    ({"selector": "a\nb"}, "selector must be"),
    ({"selector": "x" * 301}, "selector must be"),
    ({"t": -1}, "t must be >= 0"),
    ({"t": "nan"}, "finite"),
    ({"changes": {}}, "changes must map"),
    ({"changes": {"width": ["1px", "2px"]}}, "a restyle changes only"),
    ({"changes": {"color": ["red", "red"]}}, "the new value is the old one"),
    ({"changes": {"color": ["red", "blue; display: none"]}}, "one CSS value"),
    ({"changes": {"color": ["red", "</style><script>"]}}, "one CSS value"),
    ({"changes": {"background-color": ["red", "url(http://x/y.png)"]}}, "url"),
    ({"changes": {"color": ["x" * 301, "red"]}}, r"must be \[old, new\]"),
    ({"x": 0.5}, "no field"),
])
def test_restyle_rejected(tmp_path: Path, over, match) -> None:
    page = tmp_path / "card.html"
    with pytest.raises(ValueError, match=match):
        _v(restyle(page, **over), sidecar(page))


def test_a_restyle_needs_the_renders_sidecar(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no .edl.json"):
        _v(restyle(tmp_path / "card.html"), None)


# --- the server ------------------------------------------------------------------------------------

PAGE = "<!doctype html><html><head><style>body{background:#00FF00}</style></head><body><div class=t>Hi</div></body></html>"


@pytest.fixture
def server(tmp_path: Path):
    job = make_job.build(tmp_path / "job", video=False, offers=False)
    (job["dir"] / "clip-01.mp4").write_bytes(b"render")
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "card.html").write_text(PAGE)
    (pages / "other.html").write_text(PAGE)
    httpd, token, _urls = rs.serve(job["dir"], job["dir"] / "review", host="127.0.0.1", port=19565, span=200)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield {"port": httpd.server_address[1], "token": token, "job": job, "pages": pages}
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
    return res, data


def _sidecar(srv) -> Path:
    page = srv["pages"] / "card.html"
    (srv["job"]["dir"] / "clip-01.edl.json").write_text(json.dumps(sidecar(page)))
    return page


def test_edl_hands_out_a_key_per_overlay_page(server) -> None:
    page = _sidecar(server)
    res, data = _req(server, "GET", "/api/edl?video=clip-01&version=1")
    body = json.loads(data)
    assert res.status == 200 and body["overlay_keys"] == {str(page): rs.overlay_key(server["token"], str(page))}
    # the key opens only that page, and is not the sample page key for it
    assert rs.overlay_key(server["token"], str(page)) != rs.sample_key(server["token"], str(page))


def test_overlay_page_is_served_with_the_shim_and_bridge_under_the_sample_csp(server) -> None:
    page = _sidecar(server)
    q = urlencode({"path": str(page), "k": rs.overlay_key(server["token"], str(page))})
    res, data = _req(server, "GET", f"/media/overlay?{q}", token=False)
    assert res.status == 200
    assert res.getheader("Content-Security-Policy") == rs.SAMPLE_CSP
    html = data.decode()
    assert "window.__veFrame" in html and "window.__veBridge" in html
    assert html.index("__veFrame") < html.index("__veBridge") < html.index("body{background:#00FF00}")
    assert server["token"] not in html


def test_overlay_route_needs_its_own_key_or_the_token(server) -> None:
    page = _sidecar(server)
    for k in ("", "wrong", rs.sample_key(server["token"], str(page)), server["token"]):
        q = urlencode({"path": str(page), "k": k})
        assert _req(server, "GET", f"/media/overlay?{q}", token=False)[0].status == 401, k
    # the token works too (as on every /media route)
    assert _req(server, "GET", f"/media/overlay?{urlencode({'path': str(page)})}")[0].status == 200


@pytest.mark.parametrize("which", ["other.html", "../pages/card.html", "/etc/passwd"])
def test_overlay_route_refuses_pages_no_sidecar_names(server, which) -> None:
    _sidecar(server)
    path = which if which.startswith("/") else str(server["pages"] / which) if "/" not in which else which
    q = urlencode({"path": path, "k": rs.overlay_key(server["token"], path)})
    assert _req(server, "GET", f"/media/overlay?{q}", token=False)[0].status == 404


def test_overlay_route_refuses_everything_without_a_sidecar(server) -> None:
    page = server["pages"] / "card.html"
    q = urlencode({"path": str(page), "k": rs.overlay_key(server["token"], str(page))})
    assert _req(server, "GET", f"/media/overlay?{q}", token=False)[0].status == 404


def test_overlay_route_refuses_a_foreign_origin(server) -> None:
    page = _sidecar(server)
    q = urlencode({"path": str(page), "k": rs.overlay_key(server["token"], str(page))})
    conn = http.client.HTTPConnection("127.0.0.1", server["port"], timeout=5)
    conn.request("GET", f"/media/overlay?{q}", headers={"Origin": "https://evil.example"})
    assert conn.getresponse().status == 403
    conn.close()


def test_restyle_rows_land_in_the_mailbox(server) -> None:
    page = _sidecar(server)
    res, data = _req(server, "POST", "/api/comments", json.dumps({"comments": [restyle(page)]}).encode())
    assert res.status == 200, data
    _, data = _req(server, "GET", "/api/comments?video=clip-01&kind=restyle")
    [row] = json.loads(data)["comments"]
    assert (row["overlay"], row["selector"]) == (0, "body > div.card > div.t")
