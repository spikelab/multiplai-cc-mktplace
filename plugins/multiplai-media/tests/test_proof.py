"""Proof sheet (stages/proof.py): which moments of a reel it shows, and what
each tile's label says. subprocess.run is replaced, so no ffmpeg runs."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import proof  # noqa: E402
from stages.edl import EDL  # noqa: E402


def _edl(segments, **extra) -> EDL:
    return EDL.from_dict({"source": "/rec/talk.mp4", "segments": segments,
                          "output": {"width": 1080, "height": 1920, "fit": "crop"}, **extra})


def w(text, s, e, **kw):
    return {"text": text, "start": s, "end": e, **kw}


def test_one_tile_every_3s_for_a_single_segment() -> None:
    edl = _edl([{"src_start": 100, "src_end": 110}])
    assert proof.sample_times(edl) == [0.0, 3.0, 6.0, 9.0]


def test_a_tile_half_a_second_after_each_join() -> None:
    # 0–4 crossfades into 20–28 at 3.5 s; 20–28 cuts hard into 28–30 at 11.5 s.
    edl = _edl([{"src_start": 0, "src_end": 4}, {"src_start": 20, "src_end": 28},
                {"src_start": 28, "src_end": 30}])
    assert proof.change_times(edl, None) == [3.5, 11.5]
    assert proof.sample_times(edl) == [0.0, 3.0, 4.0, 6.0, 9.0, 12.0]


def test_a_change_time_close_to_a_regular_one_is_not_doubled() -> None:
    edl = _edl([{"src_start": 0, "src_end": 2.6}, {"src_start": 2.6, "src_end": 8}])
    # The join is at 2.6, so its tile at 3.1 sits within 0.25 s of the 3.0 tile.
    assert proof.sample_times(edl) == [0.0, 3.0, 6.0]


def test_speaker_switches_add_tiles() -> None:
    edl = _edl([{"src_start": 0, "src_end": 10, "frame": "speaker"}],
               layout={"panels": {"A": {"x": 0, "y": 0, "w": 900, "h": 1000},
                                  "B": {"x": 960, "y": 0, "w": 900, "h": 1000}},
                       "speakers": {"S0": "A", "S1": "B"}})
    words = [w("uno", 0.0, 2.0, speaker="S0"), w("due", 4.0, 7.0, speaker="S1")]
    assert proof.change_times(edl, words) == [4.0]
    assert 4.5 in proof.sample_times(edl, words)


def test_a_label_names_time_source_framing_caption_and_speaker() -> None:
    edl = _edl([{"src_start": 100, "src_end": 104},
                {"src_start": 104, "src_end": 110, "fit": "blur"}], captions={})
    words = [w("Ciao", 100.2, 100.6, speaker="SPEAKER_1"), w("a", 100.6, 100.8, speaker="SPEAKER_1"),
             w("tutti.", 100.8, 101.4, speaker="SPEAKER_1"), w("Slide", 105.0, 105.5)]
    t0, t1, t2 = proof.tiles(edl, words, [0.5, 2.5, 5.2])
    assert (t0.src, t0.segment, t0.framing, t0.caption, t0.speaker) == \
        (100.5, 0, "fit crop", "Ciao a tutti.", "SPEAKER_1")
    assert t1.caption == "" and t1.speaker == ""
    assert (t2.segment, t2.framing, t2.caption) == (1, "fit blur", "Slide")
    assert proof.label_lines(t0) == ["  0.5s  src 100.5  seg 0", "fit crop  [SPEAKER_1]", '"Ciao a tutti."']


def test_a_moving_focus_is_named_in_the_label() -> None:
    edl = _edl([{"src_start": 0, "src_end": 6, "focus": [{"t": 1, "x": 0.2}, {"t": 5, "x": 0.8}]}])
    assert proof.tiles(edl, None, [1.0])[0].framing == "fit crop (moving focus)"


def test_long_label_lines_wrap() -> None:
    tile = proof.Tile(1.0, 2.0, 0, "fit crop", "x" * 20 + " " + "y" * 20, "")
    assert all(len(line) <= proof.LABEL_CHARS for line in proof.label_lines(tile))


def test_label_text_pads_each_line_by_its_extra_utf8_bytes() -> None:
    tile = proof.Tile(1.0, 2.0, 0, "fit crop", "città è", "")
    last = proof.label_text(tile).split("\n")[-1]
    assert last == '"città è"  '          # à and è take one extra byte each


def test_make_writes_one_tile_per_time_then_the_sheet(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(proof.subprocess, "run", lambda cmd, *a, **k: calls.append([str(c) for c in cmd]))
    edl = _edl([{"src_start": 0, "src_end": 4}, {"src_start": 20, "src_end": 28}])
    out = tmp_path / "sheet.png"
    shown = proof.make("/r/reel.mp4", edl, None, out, "/fonts/Bold.ttf", duration=11.5)
    assert [t.t for t in shown] == [0.0, 3.0, 4.0, 6.0, 9.0]
    tiles, sheet = calls[:-1], calls[-1]
    assert len(tiles) == 5
    assert tiles[2][tiles[2].index("-ss") + 1] == "4.000"
    vf = tiles[2][tiles[2].index("-vf") + 1]
    assert "drawtext=fontfile=/fonts/Bold.ttf:textfile=" in vf and "expansion=none" in vf
    assert sheet[-1] == str(out)
    assert "tile=6x1" in sheet[sheet.index("-vf") + 1]


@pytest.mark.parametrize("duration,n", [(2.9, 1), (3.0, 1), (3.1, 2)])
def test_the_render_length_bounds_the_samples(duration, n) -> None:
    edl = _edl([{"src_start": 0, "src_end": 30}])
    assert len(proof.sample_times(edl, None, duration)) == n
