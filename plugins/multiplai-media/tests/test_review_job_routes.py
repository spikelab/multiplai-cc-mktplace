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
from urllib.parse import quote

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

import make_job  # noqa: E402
from stages import review_server as rs  # noqa: E402


@pytest.fixture
def server(tmp_path: Path):
    job = make_job.build(tmp_path / "job", video=False)
    (job["dir"] / "clip-01.mp4").write_bytes(b"render")
    media = tmp_path / "media"
    media.mkdir()
    for n in ("bed.m4a", "whoosh.wav", "sample.html", "style.md", "brand.json", "unlisted.wav"):
        (media / n).write_bytes(b"<p>sample</p>" if n.endswith(".html") else b"RIFF....")
    httpd, token, _urls = rs.serve(job["dir"], job["dir"] / "review", host="127.0.0.1", port=18865, span=200)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield {"port": httpd.server_address[1], "token": token, "job": job, "media": media,
           "box": job["dir"] / "review"}
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
    return res, (json.loads(data) if res.getheader("Content-Type", "").startswith("application/json") else data)


def _music(srv) -> None:
    m = srv["media"]
    (srv["job"]["dir"] / "music.json").write_text(json.dumps({
        "beds": [{"name": "calm", "file": str(m / "bed.m4a")}],
        "sfx_sets": [{"name": "soft", "files": [str(m / "whoosh.wav")]}]}))


def _looks(srv) -> None:
    m = srv["media"]
    (srv["job"]["dir"] / "looks.json").write_text(json.dumps({
        "styles": [{"name": "clean", "file": str(m / "style.md"), "sample_html": str(m / "sample.html")}],
        "brands": [{"name": "house", "file": str(m / "brand.json")}]}))


# --- input files ------------------------------------------------------------------------

def test_input_files_report_their_status(server) -> None:
    res, job = _get(server, "/api/job")
    assert res.status == 200 and job["status"] == "ok"
    assert job["data"]["kind"] == "reels"
    _, cands = _get(server, "/api/candidates")
    assert cands["status"] == "ok" and len(cands["data"]) == 4
    _, looks = _get(server, "/api/looks")
    assert looks == {"status": "missing", "file": str(server["job"]["dir"] / "looks.json")}


def test_an_invalid_file_comes_back_with_its_error(server) -> None:
    (server["job"]["dir"] / "music.json").write_text(json.dumps({"beds": [{"name": "x"}]}))
    res, music = _get(server, "/api/music")
    assert res.status == 200
    assert music["status"] == "invalid" and music["error"] == "$.beds[0]: missing 'file'"


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
    assert [w["text"] for w in data["words"]] == ["What", "was", "the", "biggest", "surprise?"]
    assert set(data["words"][0]) == {"text", "start", "end"}


@pytest.mark.parametrize("q", ["start=5", "start=a&end=3", "start=10&end=2", "start=0&end=5000", "start=nan&end=3"])
def test_transcript_window_must_be_sane(server, q) -> None:
    res, _ = _get(server, f"/api/transcript?{q}")
    assert res.status == 400


def test_speaker_lines_spread_over_the_recording_and_cut_long_turns() -> None:
    words = []
    for i in range(10):                       # ten turns of SPEAKER_0, each 5 words, one per 10 s
        words += [{"text": f"w{i}-{k}", "start": i * 10 + k, "end": i * 10 + k + 0.5, "speaker": "SPEAKER_0"}
                  for k in range(5)]
        words.append({"text": "uh", "start": i * 10 + 6, "end": i * 10 + 6.2, "speaker": "SPEAKER_1"})
    words.append({"text": "nobody", "start": 200, "end": 201})
    out = rs.speaker_lines(words, per_speaker=3, max_words=4)
    assert [s["label"] for s in out] == ["SPEAKER_0", "SPEAKER_1"]
    assert [ln["start"] for ln in out[0]["lines"]] == [0, 40, 90]          # first, middle, last
    assert out[0]["lines"][0]["text"] == "w0-0 w0-1 w0-2 w0-3 …"
    assert [ln["text"] for ln in out[1]["lines"]] == ["uh", "uh", "uh"]   # short turns when there is nothing longer
    assert rs.speaker_lines([{"text": "a", "start": 0, "end": 1}]) == []


# --- named media --------------------------------------------------------------------------

def test_source_streams_with_range(server) -> None:
    payload = server["job"]["source"].read_bytes()
    res, data = _get(server, "/media/source", Range="bytes=4-7")
    assert res.status == 206 and data == payload[4:8]
    assert res.getheader("Content-Range") == f"bytes 4-7/{len(payload)}"
    res, _ = _req(server, f"/media/source?t={server['token']}")
    assert res.status == 200


def test_source_is_refused_without_a_valid_job(server) -> None:
    server["job"]["job"].write_text("{}")
    res, _ = _get(server, "/media/source")
    assert res.status == 404


def test_audio_is_served_only_when_music_json_names_it(server) -> None:
    bed = server["media"] / "bed.m4a"
    res, _ = _get(server, f"/media/audio?path={quote(str(bed))}")
    assert res.status == 404                  # no music.json yet
    _music(server)
    res, data = _get(server, f"/media/audio?path={quote(str(bed))}")
    assert res.status == 200 and data == bed.read_bytes()
    assert res.getheader("Content-Type").startswith("audio/")


@pytest.mark.parametrize("path", [
    "unlisted.wav",                           # in the same folder, not named
    "../job/review/server.token",             # the token, through a named folder
    "sample.html",                            # named, but as a sample page, not audio
])
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
    assert res.status == 404                  # no looks.json yet
    _looks(server)
    res, data = _get(server, f"/media/sample?path={quote(str(sample))}")
    assert res.status == 200 and data == sample.read_bytes()
    csp = res.getheader("Content-Security-Policy")
    assert "sandbox" in csp and "script-src" not in csp
    for other in (server["media"] / "style.md", server["job"]["source"]):
        res, _ = _get(server, f"/media/sample?path={quote(str(other))}")
        assert res.status == 404


@pytest.mark.parametrize("path", ["/api/job", "/api/candidates", "/api/looks", "/api/music", "/api/sentences",
                                  "/api/transcript", "/media/source", "/media/audio", "/media/sample"])
def test_new_routes_need_the_token(server, path) -> None:
    res, _ = _req(server, path)
    assert res.status == 401
    res, _ = _req(server, path, {"X-Review-Token": "wrong"})
    assert res.status == 401


def test_new_routes_refuse_a_foreign_origin(server) -> None:
    res, _ = _get(server, "/api/candidates", Origin="https://evil.example")
    assert res.status == 403
