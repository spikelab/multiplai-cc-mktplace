"""Captions and headline (stages/captions.py): grouping, timing, colour, glyphs."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import captions as cap, timeline  # noqa: E402
from stages.brand import Brand, ass_colour  # noqa: E402
from stages.edl import EDL, Captions, Headline, Segment  # noqa: E402

DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def w(text, s, e):
    return {"text": text, "start": s, "end": e}


def test_groups_break_on_a_pause_longer_than_0_3s() -> None:
    words = [w("one", 0.0, 0.3), w("two", 0.32, 0.6), w("three", 1.0, 1.3), w("four", 1.32, 1.6)]
    groups = cap.group_words(words)
    assert [[x["text"] for x in g] for g in groups] == [["one", "two"], ["three", "four"]]


def test_groups_respect_word_and_character_limits() -> None:
    words = [w(t, i * 0.2, i * 0.2 + 0.18) for i, t in enumerate(
        ["a", "b", "c", "d", "extraordinarily", "long", "words"])]
    groups = [[x["text"] for x in g] for g in cap.group_words(words, words_per_line=3, max_chars=22)]
    assert groups[0] == ["a", "b", "c"]
    assert all(len(" ".join(g)) <= 22 for g in groups)
    assert all(len(g) <= 3 for g in groups)


def test_groups_break_after_a_sentence() -> None:
    words = [w("Done.", 0.0, 0.3), w("Next", 0.32, 0.6)]
    assert len(cap.group_words(words)) == 2


def test_forward_only_pass_removes_overlaps() -> None:
    # whisper's timings jitter backwards: 'b' starts before 'a' ends.
    events = [(1.0, 1.6, "a"), (1.4, 2.0, "b"), (1.9, 1.95, "c")]
    out = cap.forward_only(events)
    for (s1, e1, _), (s2, e2, _) in zip(out, out[1:]):
        assert s2 >= e1
    assert all(e > s for s, e, _ in out)


def test_highlight_colours_the_active_word_with_the_accent() -> None:
    groups = [[w("ciao", 0.0, 0.3), w("mondo", 0.35, 0.7)]]
    events = cap.caption_events(groups, True, accent="#FF5A36", primary="#FFFFFF")
    accent = ass_colour("#FF5A36")
    assert accent == "&H00365AFF"
    assert events[0] == (0.0, 0.35, f"{{\\1c{accent}}}ciao{{\\1c&H00FFFFFF}} mondo")
    assert events[1][2] == f"ciao {{\\1c{accent}}}mondo{{\\1c&H00FFFFFF}}"


def test_no_highlight_is_one_event_per_group() -> None:
    groups = [[w("ciao", 0.0, 0.3), w("mondo", 0.35, 0.7)]]
    assert cap.caption_events(groups, False, "#FF0000", "#FFFFFF") == [(0.0, 0.7, "ciao mondo")]


def test_words_in_cuts_are_dropped_before_captioning() -> None:
    edl = EDL(source="/x.mp4", segments=[Segment(0, 2)])
    words = [w("kept", 0.5, 0.9), w("gone", 3.0, 3.4)]
    out = timeline.map_words(words, timeline.place_segments(edl))
    ass = cap.build_ass(out, Captions(), None, 1080, 1920, "DejaVu Sans", Brand())
    assert "kept" in ass and "gone" not in ass


def test_ass_file_has_wrapstyle_0_and_the_headline_on_top() -> None:
    ass = cap.build_ass([w("ciao", 0.0, 0.4)], Captions(), Headline(text="Il punto {chiave}", end=3.0),
                        1080, 1920, "DejaVu Sans", Brand())
    assert "WrapStyle: 0" in ass
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    headline = [ln for ln in ass.splitlines() if ",Headline,," in ln]
    assert headline == ["Dialogue: 1,0:00:00.00,0:00:03.00,Headline,,0,0,0,,{\\an8\\pos(540,240)}Il punto (chiave)"]


def test_fc_query_charset_parsing() -> None:
    assert cap.parse_fc_charset("20-22 e0 e8-e9") == {0x20, 0x21, 0x22, 0xE0, 0xE8, 0xE9}


@pytest.mark.skipif(not Path(DEJAVU).exists(), reason="needs DejaVu Sans Bold")
def test_missing_glyphs_names_the_characters_the_font_lacks() -> None:
    assert cap.missing_glyphs(DEJAVU, "perché città") == []
    missing = cap.missing_glyphs(DEJAVU, "ciao 你好")
    assert missing == sorted(["你", "好"])
