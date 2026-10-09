"""Captions and headline (stages/captions.py): grouping, timing, colour, glyphs."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import captions as cap, timeline, transcript as tx  # noqa: E402
from stages.brand import Brand, ass_colour  # noqa: E402
from stages.edl import EDL, Captions, Headline, Segment  # noqa: E402

DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FIX = Path(__file__).resolve().parent / "fixtures"


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


# --- phrase-aware line breaks -------------------------------------------------

def _lines(words, **kw):
    return [" ".join(x["text"] for x in g) for g in cap.group_words(words, **kw)]


def _lone_words_not_allowed(words, groups):
    """One-word lines whose word neither ends a sentence nor precedes a pause
    longer than MAX_GAP_S (the last word counts as followed by a pause)."""
    pos = {id(x): i for i, x in enumerate(words)}
    bad = []
    for g in groups:
        if len(g) != 1:
            continue
        i = pos[id(g[0])]
        nxt = words[i + 1] if i + 1 < len(words) else None
        if g[0]["text"].endswith(cap.SENTENCE_END) or nxt is None \
                or nxt["start"] - g[0]["end"] > cap.MAX_GAP_S:
            continue
        bad.append(g[0]["text"])
    return bad


def _seq(texts, step=0.3):
    return [w(t, i * step, i * step + step) for i, t in enumerate(texts)]


def test_default_settings_leave_no_lone_word_on_the_whisper_fixture() -> None:
    words = tx.from_whisper_json(json.loads((_FIX / "whisper-words.json").read_text()), engine="t")["words"]
    groups = cap.group_words(words)
    assert _lone_words_not_allowed(words, groups) == []
    assert all(len(" ".join(x["text"] for x in g)) <= 22 for g in groups)


def test_default_settings_leave_no_lone_word_on_the_keynote_fixture() -> None:
    words = tx.load(_FIX / "keynote-words.json")["words"]
    groups = cap.group_words(words)
    assert _lone_words_not_allowed(words, groups) == []
    assert all(len(" ".join(x["text"] for x in g)) <= 22 for g in groups)
    # The old 3-word grouping split "Writing code by | hand." and "a couple of | weeks ago, We".
    lines = [" ".join(x["text"] for x in g) for g in groups]
    assert "Writing code by hand." in lines
    assert "a couple of weeks ago," in lines


def test_a_forced_break_moves_back_to_the_last_comma() -> None:
    # 22 characters stop the line at "that"; it breaks after "Well," instead.
    words = _seq(["so", "we", "went,", "okay", "that", "was", "wrong."])
    assert _lines(words, words_per_line=9) == ["so we went,", "okay that was wrong."]


def test_a_comma_too_close_to_the_start_is_not_used() -> None:
    # Breaking after "Well," would leave it alone on its line.
    words = _seq(["Well,", "the", "thing", "about", "Rust", "is", "this."])
    lines = _lines(words, words_per_line=9)
    assert lines[0] != "Well,"
    assert all(len(x) <= 22 for x in lines)


def test_the_soft_cap_grows_the_line_to_a_clause_end_that_fits() -> None:
    # Five words reach the cap; the sentence ends one word later, inside 22 chars.
    words = _seq(["ma", "io", "lo", "so", "che", "c'è."])
    assert _lines(words) == ["ma io lo so che c'è."]


def test_the_soft_cap_still_breaks_when_no_clause_end_fits() -> None:
    words = _seq(["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l"])
    assert _lines(words, words_per_line=5)[0] == "a b c d e"


def test_a_lone_word_is_joined_to_its_neighbour() -> None:
    # 3-word lines would leave "hand." alone; it joins the line before.
    words = _seq(["Writing", "code", "by", "hand."])
    assert _lines(words, words_per_line=3) == ["Writing code by hand."]


def test_a_lone_word_after_a_comma_joins_the_next_line() -> None:
    # "four" belongs to the clause after "three,", not to the one it ends.
    one, two, three, four, five, six = _seq(["one", "two", "three,", "four", "five", "six"])
    lines = cap._no_lone_words([[one, two, three], [four], [five, six]], max_chars=22)
    assert [[x["text"] for x in g] for g in lines] == [["one", "two", "three,"], ["four", "five", "six"]]


def test_a_lone_word_that_fits_nowhere_borrows_a_word() -> None:
    a, b, c, d, e = _seq(["abcdef", "ghijkl", "mnopqr", "stuvwx", "yz"])
    lines = cap._no_lone_words([[a, b, c], [d], [e]], max_chars=14)
    # "stuvwx" cannot join "abcdef ghijkl mnopqr" within 14 characters, so it
    # takes "mnopqr"; "yz" ends the words, so it may stay alone.
    assert [" ".join(x["text"] for x in g) for g in lines] == ["abcdef ghijkl", "mnopqr stuvwx", "yz"]


def test_a_lone_word_before_a_pause_may_stay_alone() -> None:
    words = [w("Allora", 0.0, 0.4), w("vediamo", 1.0, 1.4), w("bene.", 1.4, 1.8)]
    assert _lines(words) == ["Allora", "vediamo bene."]


def test_a_word_longer_than_the_line_still_gets_a_line_of_its_own() -> None:
    words = _seq(["la", "sovrintendenza_digitalizzata", "ok"])
    assert all(len(x.split()) >= 1 for x in _lines(words))
    assert "sovrintendenza_digitalizzata" in " ".join(_lines(words))
