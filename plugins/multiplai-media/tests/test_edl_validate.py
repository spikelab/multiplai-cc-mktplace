"""Tripwire tests for EDL.validate() — the render-quality guards.

validate() is pure Python (no ffmpeg, no I/O) and encodes the exact
guarantees the 0.1.3 quality fixes exist for: never render from the 720p
analysis proxy, never end the video zoomed (unless zoom.hold is a
deliberate choice), and warn on zoom overuse. These tests lock in the
path heuristic, the hold escape hatch, and both warning thresholds.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "screen-demo" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages.edl import EDL, Segment, Zoom  # noqa: E402


def _edl(source: str = "/rec/original.mov", segments: list[Segment] | None = None) -> EDL:
    if segments is None:
        segments = [Segment(src_start=0.0, src_end=5.0)]
    return EDL(source=source, segments=segments)


# --- proxy-source rejection -------------------------------------------------

@pytest.mark.parametrize(
    "source",
    [
        "/work/.screen-demo-cache/proxy_720p.mp4",          # project cache root
        "/home/u/.cache/screen-demo/rec/proxy_720p.mp4",    # user cache root
        "/anywhere/proxy_720p.mp4",                          # bare proxy_ name
    ],
)
def test_proxy_source_rejected(source: str) -> None:
    with pytest.raises(ValueError, match="analysis proxy"):
        _edl(source=source).validate()


def test_original_recording_accepted() -> None:
    assert _edl(source="/rec/demo-original.mov").validate() == []


def test_cache_dirname_alone_is_not_a_proxy() -> None:
    # ".cache" only trips the check together with a "screen-demo" component.
    assert _edl(source="/home/u/.cache/other-tool/original.mov").validate() == []


# --- final-segment zoom guard -----------------------------------------------

def test_final_segment_zoomed_raises() -> None:
    segs = [
        Segment(src_start=0.0, src_end=5.0),
        Segment(src_start=5.0, src_end=8.0, zoom=Zoom(scale=1.4)),
    ]
    with pytest.raises(ValueError, match="final segment is zoomed"):
        _edl(segments=segs).validate()


def test_final_segment_zoom_hold_opts_out() -> None:
    segs = [
        Segment(src_start=0.0, src_end=5.0),
        Segment(src_start=5.0, src_end=8.0, zoom=Zoom(scale=1.4, hold=True)),
    ]
    assert _edl(segments=segs).validate() == []


def test_mid_video_zoom_with_full_frame_ending_is_fine() -> None:
    segs = [
        Segment(src_start=0.0, src_end=5.0, zoom=Zoom(scale=1.3)),
        Segment(src_start=5.0, src_end=10.0),
    ]
    assert _edl(segments=segs).validate() == []


# --- warning thresholds -----------------------------------------------------

def test_long_zoom_warns_over_12s() -> None:
    segs = [
        Segment(src_start=0.0, src_end=13.0, zoom=Zoom(scale=1.3)),
        Segment(src_start=13.0, src_end=40.0),
    ]
    warnings = _edl(segments=segs).validate()
    assert any("zoomed for 13s" in w for w in warnings)


def test_zoom_at_12s_boundary_does_not_warn() -> None:
    segs = [
        Segment(src_start=0.0, src_end=12.0, zoom=Zoom(scale=1.3)),
        Segment(src_start=12.0, src_end=40.0),
    ]
    assert _edl(segments=segs).validate() == []


def test_majority_zoomed_runtime_warns() -> None:
    segs = [
        Segment(src_start=0.0, src_end=6.0, zoom=Zoom(scale=1.3)),
        Segment(src_start=6.0, src_end=10.0),
    ]
    warnings = _edl(segments=segs).validate()
    assert any("of the runtime is zoomed" in w for w in warnings)


def test_speed_affects_zoomed_ratio() -> None:
    # 8s zoomed source at 4x -> 2s output vs 4s full-frame: under 50%, no warning.
    segs = [
        Segment(src_start=0.0, src_end=8.0, speed=4.0, zoom=Zoom(scale=1.3)),
        Segment(src_start=8.0, src_end=12.0),
    ]
    assert _edl(segments=segs).validate() == []


def test_shipped_example_edl_validates_clean() -> None:
    example = (
        _SCRIPTS.parent / "examples" / "demo-narrated.edl.json"
    )
    edl = EDL.load(example)
    assert edl.validate() == []
