"""Framing filters (stages/layouts.py) and the EDL checks that guard them."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import composite, layouts as L  # noqa: E402
from stages.edl import EDL, Focus, Layout, Output, Panel, Segment, Zoom  # noqa: E402

W, H = 1080, 1920
A = L.Rect(36, 200, 920, 744)
B = L.Rect(966, 200, 918, 744)


def test_pad_is_the_unchanged_screencast_chain() -> None:
    assert L.pad_chain(1920, 1080) == (
        "scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=#0a0a0a,setsar=1")


def test_crop_covers_and_centres_on_focus() -> None:
    assert L.crop_chain(W, H, 0.25, 0.5) == (
        "crop=min(iw\\,ih*1080/1920):min(ih\\,iw*1920/1080):"
        "max(0\\,min(iw-ow\\,iw*0.25-ow/2)):max(0\\,min(ih-oh\\,ih*0.5-oh/2)),"
        "scale=1080:1920,setsar=1")


def test_blur_scales_to_fit_over_a_blurred_fill() -> None:
    g = L.blur_graph(W, H)
    assert g.startswith("[vin]split=2[bgs][fgs];")
    assert "[bgs]scale=108:192:force_original_aspect_ratio=increase,crop=108:192,boxblur=4:2,scale=1080:1920" in g
    assert "[fgs]scale=1080:1920:force_original_aspect_ratio=decrease" in g
    assert g.endswith("[bgb][fgc]overlay=(W-w)/2:(H-h)/2[vfit]")


def test_panel_crop_is_9x16_inside_the_panel_around_its_centre() -> None:
    r = L.panel_crop_rect(A, W, H)
    assert (r.w, r.h) == (418, 744)
    assert r.w / r.h == pytest.approx(9 / 16, abs=0.01)
    assert r.x == round(36 + 920 / 2 - 418 / 2) and r.y == 200
    assert L.panel_chain(A, W, H) == f"crop=418:744:{r.x}:200,scale=1080:1920,setsar=1"


def test_panel_focus_clamps_at_both_edges() -> None:
    left = L.panel_crop_rect(A, W, H, fx=0.0)
    right = L.panel_crop_rect(A, W, H, fx=1.0)
    assert left.x == A.x                          # flush with the panel's left edge
    assert right.x + right.w == A.x + A.w         # flush with its right edge
    # Far outside the panel still clamps.
    assert L.panel_crop_rect(A, W, H, fx=-3).x == A.x
    assert L.panel_crop_rect(A, W, H, fx=7).x == A.x + A.w - right.w


def test_stack_geometry_for_1080x1920_fills_the_frame() -> None:
    # Panels 920x740 and 918x740 are wider than 1080:960, so each loses a
    # little of its sides and the pair fills 1920 px with no bars.
    g = L.stack_geometry(A, B, W, H)
    assert (g.crop_a.w, g.crop_a.h) == (836, 744)            # 744 * 1080/960, even
    assert g.crop_a.x == round(36 + 920 / 2 - 836 / 2)       # centred in the panel
    assert (g.width, g.height_a, g.height_b) == (1080, 960, 960)
    assert g.height_a + g.height_b == H
    assert (g.x, g.y) == (0, 0)


def test_stack_crop_follows_focus_and_clamps() -> None:
    g = L.stack_geometry(A, B, W, H, fx_a=0.0, fx_b=1.0)
    assert g.crop_a.x == A.x
    assert g.crop_b.x + g.crop_b.w == B.x + B.w


def test_narrow_panels_stay_whole_and_centre_on_the_background() -> None:
    narrow = L.Rect(0, 0, 600, 740)                         # narrower than 1080:960
    g = L.stack_geometry(narrow, narrow, W, H)
    assert g.crop_a == narrow
    assert g.height_a + g.height_b <= H
    assert g.y == (H - g.height_a - g.height_b) // 2


def test_stack_shrinks_when_panels_are_too_tall() -> None:
    tall = L.Rect(0, 0, 500, 900)
    g = L.stack_geometry(tall, tall, W, H)
    assert g.height_a + g.height_b <= H
    assert g.x == (W - g.width) // 2 and g.width < W


def test_stack_graph_filter_string() -> None:
    g = L.stack_graph(A, B, W, H, "#112233")
    assert g == ("[vin]split=2[sa][sb];"
                 "[sa]crop=836:744:78:200,scale=1080:960,setsar=1[pa];"
                 "[sb]crop=836:744:1007:200,scale=1080:960,setsar=1[pb];"
                 "[pa][pb]vstack=inputs=2,pad=1080:1920:0:0:color=#112233,setsar=1[vfit]")


def _w(text, s, e, spk):
    return {"text": text, "start": s, "end": e, "speaker": spk}


def test_speaker_runs_follow_the_speaker_and_ignore_short_interjections() -> None:
    words = [
        _w("a", 10.0, 11.0, "S0"), _w("b", 11.0, 13.0, "S0"),
        _w("ok", 13.1, 13.5, "S1"),                       # 0.4 s: keeps S0's panel
        _w("c", 13.6, 15.0, "S0"),
        _w("d", 15.2, 16.0, "S1"), _w("e", 16.0, 18.0, "S1"),
    ]
    runs = L.speaker_runs(words, 10.0, 20.0, {"S0": "A", "S1": "B"})
    assert runs == [(0.0, 5.2, "A"), (5.2, 10.0, "B")]


def test_speaker_graph_overlays_the_other_panel_while_it_speaks() -> None:
    g = L.speaker_graph([(0.0, 5.2, "A"), (5.2, 10.0, "B")], {"A": A, "B": B}, W, H)
    assert g.startswith("[vin]split=2[s0][s1];")
    assert g.endswith("[p0][p1]overlay=0:0:enable='between(t,5.2,10.0)'[vfit]")


def _reel(**kw) -> EDL:
    return EDL(source="/rec/x.mp4", segments=kw.pop("segments", [Segment(0, 10)]),
               output=Output(width=1080, height=1920, fit=kw.pop("fit", "pad")),
               layout=kw.pop("layout", None))


def test_validate_rejects_speaker_frame_without_labels() -> None:
    edl = _reel(segments=[Segment(0, 10, frame="speaker")],
                layout=Layout(panels={"A": Panel(36, 200, 920, 744), "B": Panel(966, 200, 918, 744)},
                              speakers={"S0": "A"}))
    with pytest.raises(ValueError, match="speaker labels.*transcribe skill"):
        edl.validate(words=[{"text": "x", "start": 0, "end": 1}])


def test_validate_rejects_unknown_panel_and_panel_outside_frame() -> None:
    with pytest.raises(ValueError, match="not a panel"):
        _reel(segments=[Segment(0, 10, frame="C")],
              layout=Layout(panels={"A": Panel(0, 0, 10, 10)})).validate()
    with pytest.raises(ValueError, match="not inside"):
        _reel(layout=Layout(panels={"A": Panel(1500, 0, 600, 100)})).validate(source_size=(1920, 1080))


def test_validate_warns_on_letterboxed_portrait() -> None:
    warnings = _reel().validate(source_size=(1920, 1080))
    assert any("letterbox" in w for w in warnings)
    assert _reel(fit="blur").validate(source_size=(1920, 1080)) == []


def test_segment_video_uses_a_graph_for_stack_and_a_chain_for_a_panel() -> None:
    layout = Layout(panels={"A": Panel(36, 200, 920, 744), "B": Panel(966, 200, 918, 744)})
    edl = _reel(segments=[Segment(5, 9, frame="stack"), Segment(9, 12, frame="B", focus=Focus(0.0, 0.5))],
                layout=layout)
    v0 = composite._segment_video(edl, edl.segments[0], None, "#000000")
    v1 = composite._segment_video(edl, edl.segments[1], None, "#000000")
    assert v0.startswith("[0:v]trim=duration=4,setpts=PTS-STARTPTS[vin];[vin]split=2")
    assert v0.endswith("[vfit]fps=30,trim=duration=4.0[v]")
    assert v1 == "[0:v]trim=duration=3,setpts=PTS-STARTPTS,crop=418:744:966:200,scale=1080:1920,setsar=1,fps=30,trim=duration=3.0[v]"


@pytest.mark.parametrize("frame", ["A", "stack", "speaker"])
def test_validate_rejects_zoom_on_a_framed_segment(frame) -> None:
    # The zoom crop runs before the panel crop, whose rectangle is in source
    # pixels: the panel would point past the zoomed frame's edge.
    layout = Layout(panels={"A": Panel(36, 200, 920, 744), "B": Panel(966, 200, 918, 744)},
                    speakers={"S0": "A"})
    edl = _reel(segments=[Segment(0, 10, frame=frame, zoom=Zoom(scale=1.5))], layout=layout)
    with pytest.raises(ValueError, match="both zoom and frame"):
        edl.validate(source_size=(1920, 1080),
                     words=[{"text": "x", "start": 0, "end": 1, "speaker": "S0"}])


def test_zoom_without_a_frame_is_still_allowed() -> None:
    edl = _reel(segments=[Segment(0, 10, zoom=Zoom(scale=1.5)), Segment(10, 20)], fit="crop")
    assert edl.validate(source_size=(1920, 1080)) == []
