"""The sidecar `pipeline.py render` writes beside each render (stages/render_sidecar.py).

composite.render is replaced, so no ffmpeg runs; the sidecar is built from the
EDL and the transcript alone.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

import make_job
import pipeline
from stages import brand as brand_mod
from stages import captions as cap
from stages import render_sidecar, review_server, timeline
from stages.edl import EDL


def _edl(tmp_path: Path, **extra) -> Path:
    """An EDL with relative paths, read from tmp_path (the working directory)."""
    tx = tmp_path / "cache" / "transcript.json"
    tx.parent.mkdir(parents=True, exist_ok=True)
    tx.write_text(
        json.dumps({"language": "en", "engine": "fixture", "words": make_job.words()})
    )
    d = {
        "source": "rec/show.mp4",
        "transcript": "cache/transcript.json",
        "segments": [
            {"src_start": 0.5, "src_end": 6.0},
            {"src_start": 9.0, "src_end": 15.0},
        ],
        "transitions": [{"after": 0, "kind": "fade", "duration": 0.4}],
        "output": {"width": 1080, "height": 1920},
        "captions": {"words_per_line": 3, "highlight": False},
        "headline": {"text": "Ship small", "start": 0, "end": 3},
        "overlays": [
            {
                "html": "pages/lower-third.html",
                "start": 2.0,
                "end": 5.0,
                "mode": "alpha",
                "style": "clean-tech",
            }
        ],
        "sfx": [{"at": 2.0, "sound": "sfx/pop.wav"}],
        **extra,
    }
    p = tmp_path / "edl.json"
    p.write_text(json.dumps(d))
    return p


def _args(edl: Path, out: Path, **music) -> argparse.Namespace:
    return argparse.Namespace(
        edl=str(edl),
        out=str(out),
        work_dir=None,
        music_file=music.get("file"),
        music_url=None,
        music_synth=music.get("synth"),
        music_volume_db=music.get("volume_db"),
    )


def _fake_render(edl, out_path, work_dir=None):
    edl.logo = "render changed the EDL"  # what the sidecar records is the EDL as loaded
    out_path.write_bytes(b"video")
    return out_path


@pytest.fixture
def job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pipeline.composite, "render", _fake_render)
    return {"edl": _edl(tmp_path), "out": tmp_path / "out" / "clip-01.v2.mp4"}


# --- when it is written ------------------------------------------------------------------


def test_the_sidecar_is_renamed_into_place_after_the_mp4(
    job, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = job["out"]
    seen: list[tuple[str, str, bool]] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append((Path(src).name, Path(dst).name, out.exists()))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)  # Path.replace calls it too
    assert pipeline.cmd_render(_args(job["edl"], out)) == 0
    # the mp4's rename, then the sidecar's, each from a hidden partial file
    assert seen == [
        (".clip-01.v2.partial.mp4", "clip-01.v2.mp4", False),
        (".clip-01.v2.edl.partial.json", "clip-01.v2.edl.json", True),
    ]
    assert sorted(p.name for p in out.parent.iterdir()) == [
        "clip-01.v2.edl.json",
        "clip-01.v2.mp4",
    ]


def test_a_failed_render_writes_no_sidecar(
    job, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(edl, out_path, work_dir=None):
        out_path.write_bytes(b"half")
        raise subprocess.CalledProcessError(1, ["ffmpeg"])

    monkeypatch.setattr(pipeline.composite, "render", failing)
    with pytest.raises(subprocess.CalledProcessError):
        pipeline.cmd_render(_args(job["edl"], job["out"]))
    assert list(job["out"].parent.iterdir()) == []


def test_a_failed_re_render_keeps_the_old_render_and_its_sidecar(
    job, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline.cmd_render(_args(job["edl"], job["out"]))
    before = render_sidecar.path_for(job["out"]).read_text()

    def failing(edl, out_path, work_dir=None):
        raise subprocess.CalledProcessError(1, ["ffmpeg"])

    monkeypatch.setattr(pipeline.composite, "render", failing)
    with pytest.raises(subprocess.CalledProcessError):
        pipeline.cmd_render(_args(job["edl"], job["out"]))
    assert render_sidecar.path_for(job["out"]).read_text() == before


def test_a_sidecar_that_cannot_be_built_leaves_the_render_without_one(
    job, capsys
) -> None:
    pipeline.cmd_render(_args(job["edl"], job["out"]))  # an older sidecar for this name
    (job["edl"].parent / "cache" / "transcript.json").unlink()
    assert pipeline.cmd_render(_args(job["edl"], job["out"])) == 0
    assert job["out"].read_bytes() == b"video"
    assert not render_sidecar.path_for(job["out"]).exists()  # never the old render's
    assert "no clip-01.v2.edl.json" in capsys.readouterr().out


def test_an_edl_where_the_sidecar_goes_is_refused_before_rendering(
    job, tmp_path: Path, monkeypatch, capsys
) -> None:
    out = tmp_path / "out" / "clip-01.mp4"
    out.parent.mkdir()
    edl = out.parent / "clip-01.edl.json"  # the sidecar's name, beside the mp4
    edl.write_text(job["edl"].read_text())
    called = []
    monkeypatch.setattr(pipeline.composite, "render", lambda *a, **k: called.append(a))
    assert pipeline.cmd_render(_args(edl, out)) == 2
    assert called == [] and edl.read_text() == job["edl"].read_text()
    assert "move the EDL" in capsys.readouterr().err


def test_the_edl_under_another_name_for_the_sidecar_is_refused(
    job, tmp_path: Path, monkeypatch, capsys
) -> None:
    """A hard link here; on a case-insensitive disk, Clip-01.edl.json for clip-01.mp4 is the same."""
    out = tmp_path / "out" / "clip-01.mp4"
    out.parent.mkdir()
    edl = out.parent / "edl-01.json"
    edl.write_text(job["edl"].read_text())
    os.link(edl, out.parent / "clip-01.edl.json")
    called = []
    monkeypatch.setattr(pipeline.composite, "render", lambda *a, **k: called.append(a))
    assert pipeline.cmd_render(_args(edl, out)) == 2
    assert called == [] and edl.read_text() == job["edl"].read_text()
    assert "move the EDL" in capsys.readouterr().err


def test_a_sidecar_that_cannot_be_written_leaves_the_render_in_place(
    job, monkeypatch, capsys
) -> None:
    def full(out, record):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(render_sidecar, "write", full)
    assert pipeline.cmd_render(_args(job["edl"], job["out"])) == 0
    assert job["out"].read_bytes() == b"video"
    assert not render_sidecar.path_for(job["out"]).exists()
    assert "could not write clip-01.v2.edl.json: [Errno 28]" in capsys.readouterr().out


def test_an_old_sidecar_that_cannot_be_removed_still_lets_the_render_finish(
    job, capsys
) -> None:
    render_sidecar.path_for(job["out"]).mkdir(
        parents=True
    )  # a directory where the sidecar goes
    assert pipeline.cmd_render(_args(job["edl"], job["out"])) == 0
    assert job["out"].read_bytes() == b"video"
    assert not list(job["out"].parent.glob(".*partial*"))
    out = capsys.readouterr().out
    assert "could not remove the old clip-01.v2.edl.json" in out
    assert "could not write clip-01.v2.edl.json" in out


# --- what it holds -----------------------------------------------------------------------


def test_the_sidecar_holds_the_edl_as_loaded_with_absolute_paths_and_cli_music(
    job, tmp_path: Path
) -> None:
    (tmp_path / "bed.wav").write_bytes(b"RIFF")
    pipeline.cmd_render(_args(job["edl"], job["out"], file="bed.wav", volume_db=-20))
    side = json.loads(render_sidecar.path_for(job["out"]).read_text())
    assert side["video"] == "clip-01.v2.mp4"
    assert side["edl_file"] == str(job["edl"].resolve())
    e = side["edl"]
    assert e["source"] == str(tmp_path / "rec" / "show.mp4")
    assert e["transcript"] == str(tmp_path / "cache" / "transcript.json")
    assert e["overlays"][0]["html"] == str(tmp_path / "pages" / "lower-third.html")
    assert e["overlays"][0]["style"] == "clean-tech"  # a style name, not a path
    assert e["sfx"][0]["sound"] == str(tmp_path / "sfx" / "pop.wav")
    assert (
        e["music"]["file"] == str((tmp_path / "bed.wav").resolve())
        and e["music"]["volume_db"] == -20
    )
    assert e["logo"] is None  # as loaded, not as render changed it
    assert e["headline"] == {"text": "Ship small", "start": 0, "end": 3}


def test_segments_are_placed_as_the_render_places_them(job) -> None:
    pipeline.cmd_render(_args(job["edl"], job["out"]))
    side = json.loads(render_sidecar.path_for(job["out"]).read_text())
    edl = EDL.load(job["edl"])
    placed = timeline.place_segments(edl)
    assert [(s["out_start"], s["out_end"]) for s in side["segments"]] == [
        (round(p.out_start, 3), round(p.out_end, 3)) for p in placed
    ]
    assert [s["xfade_in"] for s in side["segments"]] == [0.0, 0.4]
    assert side["duration"] == round(edl.total_duration(), 3)


def _dialogues(ass: str) -> list[tuple[str, str, str]]:
    return re.findall(
        r"^Dialogue: 0,([^,]+),([^,]+),Caption,,0,0,0,,\{[^}]*\}(.*)$",
        ass,
        re.MULTILINE,
    )


@pytest.mark.parametrize("highlight", [False, True])
def test_captions_events_are_the_lines_the_render_burns_in(
    job, highlight: bool
) -> None:
    edl_path = _edl(
        job["edl"].parent, captions={"words_per_line": 3, "highlight": highlight}
    )
    pipeline.cmd_render(_args(edl_path, job["out"]))
    events = json.loads(render_sidecar.path_for(job["out"]).read_text())[
        "captions_events"
    ]

    edl = EDL.load(edl_path)
    words = timeline.map_words(
        json.loads(Path(edl.transcript).read_text())["words"],
        timeline.place_segments(edl),
    )
    ass = cap.build_ass(
        words, edl.captions, None, 1080, 1920, "Font", brand_mod.default()
    )
    burned = _dialogues(ass)
    assert events and all(set(e) == {"start", "end", "text", "words"} for e in events)
    if not highlight:  # one dialogue per line
        assert [
            (cap._ts(e["start"]), cap._ts(e["end"]), e["text"]) for e in events
        ] == burned
    else:  # one dialogue per word: a line spans its first to its last
        assert sum(len(e["words"]) for e in events) == len(burned)
        k = 0
        for e in events:
            n = len(e["words"])
            assert (cap._ts(e["start"]), cap._ts(e["end"])) == (
                burned[k][0],
                burned[k + n - 1][1],
            )
            k += n
    # every word the render keeps, in output time; none from the cut between the segments
    assert [w["start"] for e in events for w in e["words"]] == [
        w["start"] for w in words
    ]
    assert len(words) < sum(1 for w in make_job.words() if 0.5 <= w["start"] < 15.0)


def test_an_edl_without_captions_has_no_caption_events(job) -> None:
    edl_path = _edl(job["edl"].parent, captions=None)
    pipeline.cmd_render(_args(edl_path, job["out"]))
    assert (
        json.loads(render_sidecar.path_for(job["out"]).read_text())["captions_events"]
        == []
    )


def test_the_server_and_the_renderer_agree_on_the_sidecar_name() -> None:
    assert review_server.EDL_SIDECAR == render_sidecar.SUFFIX
    assert render_sidecar.path_for(Path("/x/clip-01.v2.mp4")) == Path(
        "/x/clip-01.v2.edl.json"
    )


def test_a_title_card_crossfades_into_segment_0_and_logo_and_brand_paths_are_absolute(
    job, tmp_path: Path
) -> None:
    edl = EDL.load(
        _edl(
            tmp_path,
            title={"line1": "Ship small", "duration": 2.0},
            logo={"path": "brand/logo.png"},
            brand="brand/brand.json",
        )
    )
    snap = render_sidecar.snapshot(edl)
    assert snap["logo"]["path"] == str(tmp_path / "brand" / "logo.png")
    assert snap["brand"] == str(tmp_path / "brand" / "brand.json")
    segs = render_sidecar.segments(edl)
    assert (
        segs[0]["xfade_in"] == edl.xfade_before(0) > 0
    )  # the card fades into the first segment
    assert segs[0]["out_start"] > 0
