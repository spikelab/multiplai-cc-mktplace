"""Output-time mapping (stages/timeline.py): source time → time in the render."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import composite, timeline  # noqa: E402
from stages.edl import EDL, Segment, Title, Transition  # noqa: E402


def _edl(segments, title=None, transitions=()):
    return EDL(source="/rec/x.mov", segments=segments, title=title, transitions=list(transitions))


def test_a_cut_has_no_output_time() -> None:
    edl = _edl([Segment(0, 10), Segment(20, 30)], transitions=[Transition(after=0, duration=0.5)])
    placed = timeline.place_segments(edl)
    assert timeline.to_output(5.0, placed) == 5.0
    assert timeline.to_output(15.0, placed) is None          # inside the cut
    # The second segment starts 0.5 s before the first ends (crossfade).
    assert timeline.to_output(20.0, placed) == pytest.approx(9.5)
    assert timeline.to_output(25.0, placed) == pytest.approx(14.5)


def test_a_2x_segment_halves_elapsed_time() -> None:
    edl = _edl([Segment(0, 4), Segment(10, 20, speed=2.0)], transitions=[Transition(after=0, duration=0.5)])
    placed = timeline.place_segments(edl)
    assert timeline.to_output(10.0, placed) == pytest.approx(3.5)
    assert timeline.to_output(14.0, placed) == pytest.approx(3.5 + 2.0)


def test_title_offsets_everything() -> None:
    edl = _edl([Segment(100, 110)], title=Title(line1="Hi", duration=3.0),
               transitions=[Transition(after=-1, duration=0.5)])
    placed = timeline.place_segments(edl)
    assert timeline.to_output(100.0, placed) == pytest.approx(2.5)
    assert timeline.to_output(104.0, placed) == pytest.approx(6.5)


def test_muted_fast_segment_has_no_output_time() -> None:
    edl = _edl([Segment(0, 10), Segment(10, 100, speed=20.0)])
    placed = timeline.place_segments(edl)
    assert timeline.to_output(50.0, placed) is None
    assert timeline.map_span(50.0, 50.4, placed) is None


def test_word_straddling_a_cut_is_clipped_to_the_segment_holding_most_of_it() -> None:
    edl = _edl([Segment(0, 10), Segment(20, 30)], transitions=[Transition(after=0, duration=0.5)])
    placed = timeline.place_segments(edl)
    # 9.8–10.4: 0.2 s inside segment 0, 0.4 s in the cut → clipped to 9.8–10.0.
    assert timeline.map_span(9.8, 10.4, placed) == pytest.approx((9.8, 10.0))
    # 19.7–20.3: mostly inside segment 1 → clipped to its start, 20.0–20.3.
    assert timeline.map_span(19.7, 20.3, placed) == pytest.approx((9.5, 9.8))
    assert timeline.map_span(12.0, 13.0, placed) is None


def test_map_words_drops_words_without_output_time() -> None:
    edl = _edl([Segment(0, 2)])
    words = [{"text": "kept", "start": 0.5, "end": 0.9}, {"text": "cut", "start": 5.0, "end": 5.3}]
    out = timeline.map_words(words, timeline.place_segments(edl))
    assert [w["text"] for w in out] == ["kept"]


def test_segment_starts_match_the_render_crossfade_offsets() -> None:
    # The mapping must agree with what composite actually renders.
    example = _SCRIPTS.parent / "examples" / "demo-narrated.edl.json"
    edl = EDL.load(example)
    fc, *_ = composite.build_filter_complex(edl, [Path(f"s{i}") for i in range(len(edl.segments))], Path("t"))
    offsets = [float(x) for x in re.findall(r"xfade=transition=fade:duration=[\d.]+:offset=([\d.]+)", fc)]
    starts = [p.out_start for p in timeline.place_segments(edl)]
    assert starts == offsets
