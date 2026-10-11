"""Segment `follow`: the camera pushes in on each click and pans between them,
built as ffmpeg `zoompan` expressions."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import pipeline  # noqa: E402
from stages import composite, follow  # noqa: E402
from stages.edl import EDL  # noqa: E402

TV = "(on/30)"


def _eval(expr: str, t: float) -> float:
    """Evaluate the subset of ffmpeg's expression language ease_expr writes."""
    py = expr.replace("\\,", ",").replace("if(", "IF(").replace("lt(", "LT(").replace(TV, f"({t})")
    return eval(py, {"IF": lambda c, a, b: a if c else b, "LT": lambda a, b: a < b})


def _edl(follow_spec, **seg):
    s = {"src_start": 100, "src_end": 120, "follow": follow_spec, **seg}
    return EDL.from_dict({"source": "/rec/demo.mov", "output": {"width": 1920, "height": 1080},
                          "segments": [s]})


def test_ease_starts_slow_passes_the_middle_and_holds_at_the_ends() -> None:
    e = follow.ease_expr([(1.0, 1.0), (3.0, 2.0)], TV)
    assert _eval(e, 0.0) == pytest.approx(1.0)
    assert _eval(e, 1.2) == pytest.approx(1.0 + 0.028)    # smoothstep(0.1)
    assert _eval(e, 2.0) == pytest.approx(1.5)
    assert _eval(e, 3.0) == pytest.approx(2.0)
    assert _eval(e, 9.0) == pytest.approx(2.0)


def test_the_camera_arrives_on_each_click_at_its_time() -> None:
    cam = follow.keys([(5.0, 0.2, 0.8)], scale=1.5, lead=0.6, hold=1.0, duration=20.0)
    z = follow.ease_expr([(t, k) for t, k, _, _ in cam], TV)
    x = follow.ease_expr([(t, v) for t, _, v, _ in cam], TV)
    assert _eval(z, 4.3) == pytest.approx(1.0)             # still wide before the move
    assert _eval(z, 5.0) == pytest.approx(1.5)
    assert _eval(x, 5.0) == pytest.approx(0.2)
    assert _eval(z, 5.9) == pytest.approx(1.5)             # holding
    assert _eval(z, 7.0) == pytest.approx(1.0)             # back to the whole frame


def test_close_clicks_pan_without_going_wide() -> None:
    cam = follow.keys([(5.0, 0.2, 0.2), (7.5, 0.8, 0.8)], scale=1.4, lead=0.6, hold=1.0, duration=20.0)
    z = follow.ease_expr([(t, k) for t, k, _, _ in cam], TV)
    assert min(_eval(z, t / 10) for t in range(50, 76)) == pytest.approx(1.4)


def test_a_click_inside_the_hold_is_dropped_and_the_camera_still_goes_wide() -> None:
    cam = follow.keys([(5.0, 0.2, 0.2), (5.4, 0.9, 0.9)], scale=1.4, lead=0.6, hold=1.2, duration=20.0)
    assert all(x != 0.9 for _, _, x, _ in cam)
    z = follow.ease_expr([(t, k) for t, k, _, _ in cam], TV)
    assert _eval(z, 10.0) == pytest.approx(1.0)


def test_zoompan_keeps_the_source_size_one_frame_per_frame() -> None:
    f = follow.zoompan_filter(follow.keys([(1.0, 0.5, 0.5)], 1.4, 0.6, 1.0, 5.0), 30, 1994, 1100)
    assert f.startswith("zoompan=") and ":d=1:s=1994x1100:fps=30" in f
    assert "'" not in f                                  # escaped commas, as lerp_expr writes


def test_follow_runs_before_the_framing_and_scales_lead_by_speed() -> None:
    edl = _edl({"targets": [{"t": 110, "x": 0.3, "y": 0.6}]}, speed=2.0)
    edl.validate()
    v = composite._segment_video(edl, edl.segments[0], None, "#000000", (1994, 1100))
    pre = v.split("[v]")[0]
    assert pre.index("fps=30") < pre.index("zoompan=") < pre.index("setpts=PTS/2.0")
    cam = follow.keys([(10.0, 0.3, 0.6)], 1.4, 0.6 * 2, 1.2 * 2, 20.0)
    assert follow.zoompan_filter(cam, 30, 1994, 1100) in v


def test_follow_needs_the_source_size() -> None:
    edl = _edl({"targets": [{"t": 110, "x": 0.3, "y": 0.6}]})
    assert composite._needs_source_size(edl)
    with pytest.raises(ValueError, match="source size"):
        composite._segment_video(edl, edl.segments[0], None, "#000000")


@pytest.mark.parametrize("spec, seg, msg", [
    ({"targets": []}, {}, "no targets"),
    ({"targets": [{"t": 130, "x": 0.5, "y": 0.5}]}, {}, "outside the segment"),
    ({"targets": [{"t": 110, "x": 1.5, "y": 0.5}]}, {}, "must be 0–1"),
    ({"targets": [{"t": 112, "x": 0.5, "y": 0.5}, {"t": 110, "x": 0.5, "y": 0.5}]}, {}, "must go up"),
    ({"targets": [{"t": 110}], "scale": 1.0}, {}, "above 1"),
    ({"targets": [{"t": 110}]}, {"zoom": {"scale": 1.3}}, "both zoom and follow"),
    ({"targets": [{"t": 110}]}, {"fit": "crop"}, "pad"),
])
def test_validation_names_the_mistake(spec, seg, msg) -> None:
    with pytest.raises(ValueError, match=msg):
        _edl(spec, **seg).validate()


def test_mark_parses_a_review_comment_position() -> None:
    assert pipeline._mark("12.5,0.4,0.75") == (12.5, 0.4, 0.75)
    for bad in ("12.5,0.4", "1,2,0.5", "a,b,c"):
        with pytest.raises(Exception, match="--mark"):
            pipeline._mark(bad)
