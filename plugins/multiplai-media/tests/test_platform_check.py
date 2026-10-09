"""Reels spec check (stages/platform.py) from ffprobe JSON, and the text-box lint."""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import platform as pf  # noqa: E402
from stages.edl import EDL, Captions, Headline, Output, Segment  # noqa: E402


def info(w=1080, h=1920, vcodec="h264", fps="30/1", acodec="aac", rate="48000",
         bitrate=8_000_000, size=40_000_000, duration=45.0, audio=True):
    streams = [{"codec_type": "video", "codec_name": vcodec, "width": w, "height": h, "avg_frame_rate": fps}]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": acodec, "sample_rate": rate})
    return {"streams": streams, "format": {"bit_rate": str(bitrate), "size": str(size), "duration": str(duration)}}


def status(results, name):
    return next(r.status for r in results if r.name == name)


def test_a_good_reel_passes_everything() -> None:
    assert all(r.status == pf.PASS for r in pf.evaluate(info()))


def test_each_requirement_fails_on_its_own() -> None:
    assert status(pf.evaluate(info(w=1920, h=1080)), "resolution") == pf.FAIL
    assert status(pf.evaluate(info(vcodec="hevc")), "video codec") == pf.FAIL
    assert status(pf.evaluate(info(fps="25/1")), "frame rate") == pf.FAIL
    assert status(pf.evaluate(info(acodec="opus")), "audio codec") == pf.FAIL
    assert status(pf.evaluate(info(rate="44100")), "audio sample rate") == pf.FAIL
    assert status(pf.evaluate(info(bitrate=30_000_000)), "bitrate") == pf.FAIL
    assert status(pf.evaluate(info(size=301_000_000)), "file size") == pf.FAIL
    assert status(pf.evaluate(info(audio=False)), "audio") == pf.FAIL


def test_ntsc_frame_rate_passes() -> None:
    assert status(pf.evaluate(info(fps="30000/1001")), "frame rate") == pf.PASS


def test_duration_warns_over_90_and_fails_over_180() -> None:
    assert status(pf.evaluate(info(duration=90.0)), "duration") == pf.PASS
    assert status(pf.evaluate(info(duration=120.0)), "duration") == pf.WARN
    assert status(pf.evaluate(info(duration=181.0)), "duration") == pf.FAIL


def _edl(captions=None, headline=None) -> EDL:
    return EDL(source="/x.mp4", segments=[Segment(0, 10)], output=Output(width=1080, height=1920),
               captions=captions, headline=headline)


def test_default_caption_and_headline_boxes_are_in_the_safe_area() -> None:
    results = pf.lint_text_boxes(_edl(Captions(), Headline(text="Hook")))
    assert [r.status for r in results] == [pf.PASS, pf.PASS]


def test_caption_in_the_bottom_band_fails_and_near_it_warns() -> None:
    assert status(pf.lint_text_boxes(_edl(Captions(position_y=0.95))), "caption box") == pf.FAIL
    assert status(pf.lint_text_boxes(_edl(Captions(position_y=0.78))), "caption box") == pf.WARN


def test_caption_in_the_top_band_fails() -> None:
    assert status(pf.lint_text_boxes(_edl(Captions(position_y=0.05))), "caption box") == pf.FAIL


def test_late_headline_warns() -> None:
    results = pf.lint_text_boxes(_edl(headline=Headline(text="x", start=5.0, end=8.0)))
    assert status(results, "headline timing") == pf.WARN
