"""The job page's server routes: input files, sentences, transcript lines, named media.

Runs a real server on a loopback port over the synthetic job from
tests/fixtures/make_job.py (stand-in bytes for the video: the server never
decodes media).
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
from pathlib import Path
from urllib.parse import quote, urlencode

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

import make_job
from stages import review_server as rs


@pytest.fixture
def server(tmp_path: Path):
    job = make_job.build(tmp_path / "job", video=False, offers=False)
    (job["dir"] / "clip-01.mp4").write_bytes(b"render")
    media = tmp_path / "media"
    media.mkdir()
    for n in (
        "bed.m4a",
        "whoosh.wav",
        "sample.html",
        "style.md",
        "brand.json",
        "unlisted.wav",
    ):
        (media / n).write_bytes(
            b"<p>sample</p>" if n.endswith(".html") else b"RIFF...."
        )
    httpd, token, _urls = rs.serve(
        job["dir"], job["dir"] / "review", host="127.0.0.1", port=18865, span=200
    )
    t = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    t.start()
    yield {
        "port": httpd.server_address[1],
        "token": token,
        "job": job,
        "media": media,
        "box": job["dir"] / "review",
    }
    httpd.shutdown()
    httpd.server_close()
    rs.unpublish(job["dir"] / "review")


def _req(srv, path: str, headers: dict | None = None, method: str = "GET"):
    conn = http.client.HTTPConnection("127.0.0.1", srv["port"], timeout=5)
    conn.request(method, path, headers=headers or {})
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res, data


def _get(srv, path: str, **headers):
    res, data = _req(srv, path, {"X-Review-Token": srv["token"], **headers})
    return res, (
        json.loads(data)
        if res.getheader("Content-Type", "").startswith("application/json")
        else data
    )


def _music(srv) -> None:
    m = srv["media"]
    (srv["job"]["dir"] / "music.json").write_text(
        json.dumps(
            {
                "beds": [{"name": "calm", "file": str(m / "bed.m4a")}],
                "sfx_sets": [{"name": "soft", "files": [str(m / "whoosh.wav")]}],
            }
        )
    )


def _looks(srv) -> None:
    m = srv["media"]
    (srv["job"]["dir"] / "looks.json").write_text(
        json.dumps(
            {
                "styles": [
                    {
                        "name": "clean",
                        "file": str(m / "style.md"),
                        "sample_html": str(m / "sample.html"),
                    }
                ],
                "brands": [{"name": "house", "file": str(m / "brand.json")}],
            }
        )
    )


# --- input files ------------------------------------------------------------------------


def test_input_files_report_their_status(server) -> None:
    res, job = _get(server, "/api/job")
    assert res.status == 200 and job["status"] == "ok"
    assert job["data"]["kind"] == "reels"
    _, cands = _get(server, "/api/candidates")
    assert cands["status"] == "ok" and len(cands["data"]) == 4
    _, looks = _get(server, "/api/looks")
    assert looks == {
        "status": "missing",
        "file": str(server["job"]["dir"] / "looks.json"),
    }


def test_an_invalid_file_comes_back_with_its_error(server) -> None:
    (server["job"]["dir"] / "music.json").write_text(
        json.dumps({"beds": [{"name": "x"}]})
    )
    res, music = _get(server, "/api/music")
    assert res.status == 200
    assert (
        music["status"] == "invalid" and music["error"] == "$.beds[0]: missing 'file'"
    )


def test_a_file_written_after_start_is_served_without_a_restart(server) -> None:
    _, before = _get(server, "/api/looks")
    assert before["status"] == "missing"
    _looks(server)
    _, after = _get(server, "/api/looks")
    assert after["status"] == "ok" and after["data"]["styles"][0]["name"] == "clean"


def test_the_source_in_the_job_directory_is_not_listed_as_a_render(server) -> None:
    _, data = _get(server, "/api/videos")
    assert [c["clip"] for c in data["videos"]] == ["clip-01"]


# --- sentences and transcript -------------------------------------------------------------


def test_sentences_are_the_ones_job_json_names(server) -> None:
    _, data = _get(server, "/api/sentences")
    assert data["sentences"] == json.loads(server["job"]["sentences"].read_text())


def test_without_a_valid_job_there_are_no_sentences(server) -> None:
    server["job"]["job"].unlink()
    res, data = _get(server, "/api/sentences")
    assert res.status == 404 and "job.json" in data["error"]


def test_transcript_gives_sample_lines_per_speaker(server) -> None:
    _, data = _get(server, "/api/transcript")
    assert [s["label"] for s in data["speakers"]] == ["SPEAKER_0", "SPEAKER_1"]
    assert all(1 <= len(s["lines"]) <= rs.SAMPLE_LINES for s in data["speakers"])
    _, one = _get(server, "/api/transcript?speaker=SPEAKER_1")
    assert [s["label"] for s in one["speakers"]] == ["SPEAKER_1"]
    assert one["speakers"][0]["lines"][0]["text"].startswith("Thanks for having me.")


def test_transcript_window_gives_words(server) -> None:
    _, data = _get(server, "/api/transcript?start=9&end=11")
    assert [w["text"] for w in data["words"]] == [
        "What",
        "was",
        "the",
        "biggest",
        "surprise?",
    ]
    assert set(data["words"][0]) == {"text", "start", "end"}


@pytest.mark.parametrize(
    "q",
    [
        "start=5",
        "start=a&end=3",
        "start=10&end=2",
        "start=0&end=5000",
        "start=nan&end=3",
    ],
)
def test_transcript_window_must_be_sane(server, q) -> None:
    res, _ = _get(server, f"/api/transcript?{q}")
    assert res.status == 400


def test_speaker_lines_spread_over_the_recording_and_cut_long_turns() -> None:
    words = []
    for i in range(10):  # ten turns of SPEAKER_0, each 5 words, one per 10 s
        words += [
            {
                "text": f"w{i}-{k}",
                "start": i * 10 + k,
                "end": i * 10 + k + 0.5,
                "speaker": "SPEAKER_0",
            }
            for k in range(5)
        ]
        words.append(
            {
                "text": "uh",
                "start": i * 10 + 6,
                "end": i * 10 + 6.2,
                "speaker": "SPEAKER_1",
            }
        )
    words.append({"text": "nobody", "start": 200, "end": 201})
    out = rs.speaker_lines(words, per_speaker=3, max_words=4)
    assert [s["label"] for s in out] == ["SPEAKER_0", "SPEAKER_1"]
    assert [ln["start"] for ln in out[0]["lines"]] == [0, 40, 90]  # first, middle, last
    assert out[0]["lines"][0]["text"] == "w0-0 w0-1 w0-2 w0-3 …"
    assert [ln["text"] for ln in out[1]["lines"]] == [
        "uh",
        "uh",
        "uh",
    ]  # short turns when there is nothing longer
    assert rs.speaker_lines([{"text": "a", "start": 0, "end": 1}]) == []


# --- named media --------------------------------------------------------------------------


def test_source_streams_with_range(server) -> None:
    payload = server["job"]["source"].read_bytes()
    res, data = _get(server, "/media/source", Range="bytes=4-7")
    assert res.status == 206 and data == payload[4:8]
    assert res.getheader("Content-Range") == f"bytes 4-7/{len(payload)}"
    res, _ = _req(server, f"/media/source?t={server['token']}")
    assert res.status == 200


def test_renders_are_served_by_name_but_the_source_is_not_a_render(server) -> None:
    res, data = _get(server, "/media/clip-01.mp4", Range="bytes=0-1")
    assert res.status == 206 and data == b"re"
    res, _ = _get(server, f"/media/{server['job']['source'].name}", Range="bytes=0-1")
    assert res.status == 404


def test_source_is_refused_without_a_valid_job(server) -> None:
    server["job"]["job"].write_text("{}")
    res, _ = _get(server, "/media/source")
    assert res.status == 404


def test_audio_is_served_only_when_music_json_names_it(server) -> None:
    bed = server["media"] / "bed.m4a"
    res, _ = _get(server, f"/media/audio?path={quote(str(bed))}")
    assert res.status == 404  # no music.json yet
    _music(server)
    res, data = _get(server, f"/media/audio?path={quote(str(bed))}")
    assert res.status == 200 and data == bed.read_bytes()
    assert res.getheader("Content-Type").startswith("audio/")


@pytest.mark.parametrize(
    "path",
    [
        "unlisted.wav",  # in the same folder, not named
        "../job/review/server.token",  # the token, through a named folder
        "sample.html",  # named, but as a sample page, not audio
    ],
)
def test_audio_refuses_unlisted_paths(server, path) -> None:
    _music(server)
    _looks(server)
    target = str(server["media"] / path)
    res, _ = _get(server, f"/media/audio?path={quote(target)}")
    assert res.status == 404


@pytest.mark.parametrize("path", ["bed.m4a", "whoosh.wav", "", "relative/sample.html"])
def test_audio_refuses_relative_and_empty_paths(server, path) -> None:
    _music(server)
    res, _ = _get(server, f"/media/audio?path={quote(path)}")
    assert res.status == 404


def test_sample_page_is_sandboxed_and_refuses_unlisted_paths(server) -> None:
    sample = server["media"] / "sample.html"
    res, _ = _get(server, f"/media/sample?path={quote(str(sample))}")
    assert res.status == 404  # no looks.json yet
    _looks(server)
    res, data = _get(server, f"/media/sample?path={quote(str(sample))}")
    assert res.status == 200 and data.endswith(sample.read_bytes())
    csp = [d.strip() for d in res.getheader("Content-Security-Policy").split(";")]
    assert "sandbox allow-scripts" in csp  # an opaque origin, even opened directly
    assert "script-src 'unsafe-inline'" in csp and "connect-src 'none'" in csp
    assert "frame-ancestors 'self'" in csp and "default-src 'none'" in csp
    assert not any("allow-same-origin" in d for d in csp)
    for other in (server["media"] / "style.md", server["job"]["source"]):
        res, _ = _get(server, f"/media/sample?path={quote(str(other))}")
        assert res.status == 404


def test_sample_page_gets_the_seek_shim_and_loop_listener_before_its_own_scripts(
    server,
) -> None:
    sample = server["media"] / "sample.html"
    sample.write_text(
        '<!doctype html><html><head><meta charset="utf-8"><script>window.mine = 1</script>'
        "</head><body></body></html>"
    )
    _looks(server)
    _, data = _get(server, f"/media/sample?path={quote(str(sample))}")
    html = data.decode()
    shim, loop, mine = (
        html.index("window.__veFrame = function"),
        html.index("__veSampleLoop"),
        html.index("window.mine"),
    )
    assert html.index("<head>") < shim < loop < mine
    assert html.startswith("<!doctype html>")


def test_a_sample_page_that_is_not_utf8_is_refused_with_its_reason(server) -> None:
    sample = server["media"] / "sample.html"
    sample.write_bytes(b"<p>caf\xe9</p>")  # Latin-1, not UTF-8
    _looks(server)
    res, data = _get(server, f"/media/sample?path={quote(str(sample))}")
    assert res.status == 422 and data == {"error": "the sample page is not UTF-8 text"}


def test_main_page_may_frame_only_its_own_server(server) -> None:
    res, _ = _req(server, "/")
    csp = [d.strip() for d in res.getheader("Content-Security-Policy").split(";")]
    assert (
        "frame-src 'self'" in csp
        and "frame-ancestors 'none'" in csp
        and "script-src 'self'" in csp
    )


def test_a_sample_key_opens_that_sample_page_and_nothing_else(server) -> None:
    _looks(server)
    sample = str(server["media"] / "sample.html")
    _, looks = _get(server, "/api/looks")
    key = looks["sample_keys"][sample]
    assert key == rs.sample_key(server["token"], sample) and server[
        "token"
    ] not in json.dumps(looks)
    res, data = _req(
        server, f"/media/sample?{urlencode({'path': sample, 'k': key})}"
    )  # no token
    assert res.status == 200 and b"<p>sample</p>" in data
    res, _ = _req(
        server, f"/media/sample?{urlencode({'path': sample, 'k': key[:-1] + 'x'})}"
    )
    assert res.status == 401
    other = str(server["media"] / "style.md")
    res, _ = _req(server, f"/media/sample?{urlencode({'path': other, 'k': key})}")
    assert res.status == 401  # a key opens only the page it was made for
    for path in (
        "/api/job",
        "/media/source",
        f"/media/audio?path={quote(str(server['media'] / 'bed.m4a'))}",
    ):
        sep = "&" if "?" in path else "?"
        res, _ = _req(server, f"{path}{sep}{urlencode({'path': sample, 'k': key})}")
        assert res.status == 401, path


def test_sample_keys_are_absent_without_a_valid_looks_file(server) -> None:
    _, looks = _get(server, "/api/looks")
    assert "sample_keys" not in looks


def test_inject_scripts_places_scripts_first_in_head_or_after_the_doctype() -> None:
    assert (
        rs.inject_scripts("<html><HEAD lang=x><title>t</title>", ["a()"])
        == "<html><HEAD lang=x>\n<script>\na()\n</script>\n<title>t</title>"
    )
    assert rs.inject_scripts("<!DOCTYPE html><p>x", ["a()", "b()"]).startswith(
        "<!DOCTYPE html>\n<script>\na()\n</script>\n<script>\nb()\n</script>\n<p>x"
    )
    assert rs.inject_scripts("<p>x", ["a()"]) == "<script>\na()\n</script>\n<p>x"
    assert rs.inject_scripts("<header>x</header>", ["a()"]).startswith(
        "<script>"
    )  # <header> is not <head>
    with pytest.raises(ValueError):
        rs.inject_scripts("<p>", ["x = '</script>'"])


def test_the_shipped_injected_scripts_can_be_inlined() -> None:
    for name in rs.SAMPLE_SCRIPTS:
        assert "</script" not in (rs.STATIC_DIR / name).read_text().lower()


# --- brands -------------------------------------------------------------------------------


def test_brands_show_colours_and_font_or_their_error(server) -> None:
    _, data = _get(server, "/api/brands")
    assert data == {"brands": []}  # no looks.json yet
    m = server["media"]
    (m / "brand.json").write_text(
        json.dumps({"accent": "#3DDC97", "font_bold_file": "Brand-Bold.ttf"})
    )
    (m / "Brand-Bold.ttf").write_bytes(b"font")
    (m / "bad.json").write_text(json.dumps({"accent": "green"}))
    _looks(server)
    looks = json.loads((server["job"]["dir"] / "looks.json").read_text())
    looks["brands"].append({"name": "bad", "file": str(m / "bad.json")})
    (server["job"]["dir"] / "looks.json").write_text(json.dumps(looks))
    _, data = _get(server, "/api/brands")
    house, bad = data["brands"]
    assert (
        house["name"] == "house"
        and house["status"] == "ok"
        and house["font"] == "Brand-Bold"
    )
    assert house["colours"] == {
        "primary": "#FFFFFF",
        "accent": "#3DDC97",
        "background": "#0a0a0a",
        "caption_outline": "#000000",
    }
    assert bad["status"] == "invalid" and "#RRGGBB" in bad["error"]


def test_brands_need_the_token(server) -> None:
    res, _ = _req(server, "/api/brands")
    assert res.status == 401


@pytest.mark.parametrize(
    "path",
    [
        "/api/job",
        "/api/candidates",
        "/api/looks",
        "/api/music",
        "/api/sentences",
        "/api/transcript",
        "/media/source",
        "/media/audio",
        "/media/sample",
        "/api/edl?video=clip-01&version=1",
    ],
)
def test_new_routes_need_the_token(server, path) -> None:
    res, _ = _req(server, path)
    assert res.status == 401
    res, _ = _req(server, path, {"X-Review-Token": "wrong"})
    assert res.status == 401


def test_new_routes_refuse_a_foreign_origin(server) -> None:
    res, _ = _get(server, "/api/candidates", Origin="https://evil.example")
    assert res.status == 403


# --- render sidecars (<stem>.edl.json, stages/render_sidecar.py) ---------------------------------

SIDECAR = {
    "format": 1,
    "video": "clip-01.mp4",
    "edl_file": "/jobs/x/edl-c2.json",
    "edl": {"source": "/rec/x.mp4", "segments": []},
    "duration": 12.5,
    "segments": [],
    "captions_events": [],
}


def test_edl_serves_the_sidecar_of_one_render(server) -> None:
    d = server["job"]["dir"]
    (d / "clip-01.edl.json").write_text(json.dumps(SIDECAR))
    res, body = _get(server, "/api/edl?video=clip-01&version=1")
    assert res.status == 200 and body == {
        "status": "ok",
        "data": SIDECAR,
        "overlay_keys": {},
    }
    # a second version reads its own sidecar, not the first one's
    (d / "clip-01.v2.mp4").write_bytes(b"render 2")
    (d / "clip-01.v2.edl.json").write_text(
        json.dumps({**SIDECAR, "video": "clip-01.v2.mp4"})
    )
    _, body = _get(server, "/api/edl?video=clip-01&version=2")
    assert body["data"]["video"] == "clip-01.v2.mp4"


def test_edl_is_404_for_a_render_without_a_sidecar(server) -> None:
    res, body = _get(server, "/api/edl?video=clip-01&version=1")
    assert res.status == 404 and "clip-01.edl.json" in body["error"]


@pytest.mark.parametrize(
    "q",
    [
        "video=clip-02&version=1",
        "video=clip-01&version=7",
        "video=source&version=1",
        "video=..%2Fclip-01&version=1",
    ],
)
def test_edl_is_404_for_a_render_not_in_the_list(server, q) -> None:
    (server["job"]["dir"] / "clip-01.edl.json").write_text(json.dumps(SIDECAR))
    res, _ = _get(server, f"/api/edl?{q}")
    assert res.status == 404


def test_edl_needs_a_numeric_version(server) -> None:
    res, _ = _get(server, "/api/edl?video=clip-01&version=one")
    assert res.status == 400


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '{"video": "clip-01.mp4"}'])
def test_edl_reports_a_broken_sidecar(server, text) -> None:
    (server["job"]["dir"] / "clip-01.edl.json").write_text(text)
    res, body = _get(server, "/api/edl?video=clip-01&version=1")
    assert res.status == 422 and "clip-01.edl.json" in body["error"]


def test_the_json_cache_rereads_a_file_that_changed_on_disk(tmp_path: Path) -> None:
    import os

    f = tmp_path / "sentences.json"
    f.write_text("[1]")
    cache = rs._JsonCache()
    first = cache.get(f)
    assert first == [1] and cache.get(f) is first  # unchanged: the parsed copy
    f.write_text("[1, 2]")  # new size
    assert cache.get(f) == [1, 2]
    st = f.stat()
    f.write_text("[3, 4]")  # same size, new mtime
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert cache.get(f) == [3, 4]
