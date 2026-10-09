"""Contact sheets (stages/contact_sheet.py): frame times and ffmpeg/yt-dlp argv."""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import contact_sheet as cs  # noqa: E402


def test_is_url() -> None:
    assert cs.is_url("https://www.youtube.com/watch?v=x")
    assert cs.is_url("http://example.com/a.mp4")
    assert not cs.is_url("/videos/a.mp4")


def test_fetch_argv_caps_quality_and_length() -> None:
    argv = cs.fetch_argv("https://v.example/x", Path("/out"), 600)
    assert argv[0] == "yt-dlp" and argv[-1] == "https://v.example/x"
    assert "--no-playlist" in argv
    assert argv[argv.index("-f") + 1] == "bv*[height<=720]+ba/b[height<=720]/b"
    assert argv[argv.index("-S") + 1] == "vcodec:h264"
    assert argv[argv.index("--download-sections") + 1] == "*0-600"
    assert argv[argv.index("-o") + 1] == "/out/reference.%(ext)s"
    assert "--download-sections" not in cs.fetch_argv("https://v.example/x", Path("/out"), None)


def test_fetch_replaces_a_reference_left_by_an_earlier_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cs.shutil, "which", lambda name: "/usr/bin/yt-dlp")
    (tmp_path / "reference.mkv").write_bytes(b"old video")
    (tmp_path / "reference.mp4.part").write_bytes(b"old partial")

    def fake_yt_dlp(argv, **k):
        assert not list(tmp_path.glob("reference.*")), "earlier reference still present"
        (tmp_path / "reference.webm").write_bytes(b"new video")
    got = cs.fetch("https://v.example/new", tmp_path, 600, run=fake_yt_dlp)
    assert got == tmp_path / "reference.webm"
    assert sorted(p.name for p in tmp_path.glob("reference.*")) == ["reference.webm"]


def test_sample_times_merges_scenes_and_interval() -> None:
    # interval 0,5,10,15,20 + scene changes 3.0, 5.4 (too close to 5 → the scene wins), 19.9
    assert cs.sample_times([3.0, 5.4, 19.9], 21.0, 5.0) == [0.05, 3.05, 5.05, 10.05, 15.05, 19.95]


def test_sample_times_never_passes_the_end() -> None:
    times = cs.sample_times([], 10.0, 5.0)
    assert times == [0.05, 5.05, 9.95]
    assert all(t < 10.0 for t in times)


def test_sample_times_scene_only() -> None:
    assert cs.sample_times([1.0, 1.5, 4.0], 6.0, 0) == [1.05, 4.05]


def test_frame_argv_labels_the_time() -> None:
    argv = cs.frame_argv(Path("/v.mp4"), 65.25, Path("/f/t_0001.png"), "/fonts/a.ttf")
    assert argv[argv.index("-ss") + 1] == "65.250"
    assert argv[argv.index("-i") + 1] == "/v.mp4"
    vf = argv[argv.index("-vf") + 1]
    assert vf.startswith(f"scale={cs.THUMB_W}:-2,drawtext=fontfile='/fonts/a.ttf':text='1\\:05.2'")
    assert argv[-1] == "/f/t_0001.png"


def test_tile_argv_makes_4x4_sheets() -> None:
    argv = cs.tile_argv(Path("/f"), Path("/out"))
    assert argv[argv.index("-i") + 1] == "/f/t_%04d.png"
    assert argv[argv.index("-vf") + 1] == "tile=4x4:padding=6:margin=6:color=white"
    assert argv[-1] == "/out/sheet_%02d.png"


def test_proxy_is_silent_h264() -> None:
    argv = cs.proxy_argv(Path("/ref.webm"), Path("/w/proxy.mp4"), 600)
    assert "-an" in argv and argv[argv.index("-c:v") + 1] == "libx264"
    assert argv[argv.index("-t") + 1] == "600"
    assert "-t" not in cs.proxy_argv(Path("/ref.webm"), Path("/w/proxy.mp4"), None)
