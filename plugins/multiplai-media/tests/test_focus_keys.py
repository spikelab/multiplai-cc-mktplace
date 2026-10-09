"""Focus keyframes (Segment.focus as a list): a crop window that follows a
moving subject, built as a time-varying ffmpeg crop expression."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import composite, layouts as L  # noqa: E402
from stages.edl import EDL, FocusKey  # noqa: E402

W, H = 1080, 1920


def _eval(expr: str, t: float) -> float:
    """Evaluate the subset of ffmpeg's expression language lerp_expr writes."""
    py = expr.replace("\\,", ",").replace("if(", "IF(").replace("lt(", "LT(")
    py = py.replace("max(", "MAX(").replace("min(", "MIN(")
    return eval(py, {"IF": lambda c, a, b: a if c else b, "LT": lambda a, b: a < b,
                     "MAX": max, "MIN": min, "t": t})


def _edl(focus, **seg):
    return EDL.from_dict({"source": "/rec/talk.mp4", "output": {"width": W, "height": H, "fit": "crop"},
                          "segments": [{"src_start": 100, "src_end": 110, "focus": focus, **seg}]})


def test_lerp_moves_in_straight_lines_and_holds_at_the_ends() -> None:
    e = L.lerp_expr([(1.0, 0.2), (3.0, 0.6), (7.0, 0.4)])
    assert _eval(e, 0.0) == pytest.approx(0.2)
    assert _eval(e, 2.0) == pytest.approx(0.4)
    assert _eval(e, 3.0) == pytest.approx(0.6)
    assert _eval(e, 5.0) == pytest.approx(0.5)
    assert _eval(e, 9.0) == pytest.approx(0.4)


def test_a_single_key_is_a_fixed_point() -> None:
    assert _eval(L.lerp_expr([(2.0, 0.7)]), 0.0) == pytest.approx(0.7)
    assert _eval(L.lerp_expr([(2.0, 0.7)]), 5.0) == pytest.approx(0.7)


def test_the_edl_reads_a_focus_list_as_keys() -> None:
    edl = _edl([{"t": 101, "x": 0.3, "y": 0.4}, {"t": 105, "x": 0.7}])
    assert edl.segments[0].focus == [FocusKey(101, 0.3, 0.4), FocusKey(105, 0.7, 0.5)]
    assert edl.validate() == []


def test_the_crop_follows_the_keys_in_segment_time() -> None:
    # Keys are source seconds; t in the crop starts at 0 at src_start (100).
    edl = _edl([{"t": 102, "x": 0.25}, {"t": 106, "x": 0.75}])
    v = composite._segment_video(edl, edl.segments[0], None, L.DEFAULT_BG)
    crop = v.split("crop=", 1)[1].split(",scale=")[0]
    x_expr = crop.split(":")[2]
    assert x_expr.startswith("max(0\\,min(iw-ow\\,iw*(if(lt(t\\,2)")
    centre = x_expr.split("iw*", 1)[1].rsplit("-ow/2", 1)[0]
    assert _eval(centre, 0.0) == pytest.approx(0.25)
    assert _eval(centre, 4.0) == pytest.approx(0.5)
    assert _eval(centre, 9.0) == pytest.approx(0.75)


def test_a_panel_window_follows_the_keys_inside_the_panel() -> None:
    panel = L.Rect(100, 0, 1000, 1080)
    chain = L.panel_chain_moving(panel, W, H, L.lerp_expr([(0, 0.0), (4, 1.0)]), "0.5")
    w, h, x, _y = chain.split("crop=")[1].split(",scale=")[0].split(":", 3)
    assert (int(w), int(h)) == (608, 1080)
    # Clamped to the panel: the window never leaves 100..(1100-608).
    assert _eval(x, 0.0) == pytest.approx(100)
    assert _eval(x, 4.0) == pytest.approx(1100 - 608)
    assert _eval(x, 2.0) == pytest.approx(100 + 500 - 304)


def test_static_focus_keeps_the_old_crop_string() -> None:
    edl = _edl({"x": 0.3, "y": 0.5})
    v = composite._segment_video(edl, edl.segments[0], None, L.DEFAULT_BG)
    assert L.crop_chain(W, H, 0.3, 0.5) in v


@pytest.mark.parametrize("focus,match", [
    ([{"t": 99, "x": 0.5}], "outside the segment"),
    ([{"t": 101, "x": 1.2}], "both must be 0–1"),
    ([{"t": 105, "x": 0.5}, {"t": 103, "x": 0.5}], "must go up"),
    ([{"t": 103, "x": 0.5}, {"t": 103, "x": 0.6}], "must go up"),
    ([], "empty focus list"),
])
def test_bad_keys_are_refused(focus, match) -> None:
    with pytest.raises(ValueError, match=match):
        _edl(focus).validate()


def test_keys_need_a_crop_window() -> None:
    edl = _edl([{"t": 101, "x": 0.5}], fit="blur")
    with pytest.raises(ValueError, match='need fit "crop" or a single panel frame'):
        edl.validate()
