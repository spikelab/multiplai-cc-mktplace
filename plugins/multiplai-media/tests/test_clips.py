"""Sentences from words, and clip edges snapped to sentences and pauses (stages/clips.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import clips  # noqa: E402
from stages.prep import parse_silences  # noqa: E402


def w(text, s, e, spk=None):
    d = {"text": text, "start": s, "end": e}
    if spk:
        d["speaker"] = spk
    return d


WORDS = [
    w("First", 0.0, 0.4), w("idea.", 0.45, 1.0),
    w("Second", 1.5, 1.9), w("one", 1.95, 2.2), w("here.", 2.25, 2.9),
    w("Third", 3.6, 4.0), w("sentence", 4.05, 4.6), w("now?", 4.65, 5.2),
    w("Fourth", 5.5, 6.0), w("ends.", 6.05, 6.8),
]
# Pauses between sentences (silencedetect intervals).
SILENCES = [(1.05, 1.45), (2.95, 3.55), (5.25, 5.45)]


def test_sentences_split_on_punctuation() -> None:
    s = clips.build_sentences(WORDS)
    assert [x.text for x in s] == ["First idea.", "Second one here.", "Third sentence now?", "Fourth ends."]
    assert (s[1].start, s[1].end) == (1.5, 2.9)


def test_sentences_split_on_a_long_pause_without_punctuation() -> None:
    s = clips.build_sentences([w("no", 0, 0.3), w("stop", 0.35, 0.7), w("then", 2.5, 2.8)])
    assert [x.text for x in s] == ["no stop", "then"]


def test_sentence_takes_the_majority_speaker() -> None:
    s = clips.build_sentences([w("a", 0, 0.2, "S0"), w("b", 0.3, 0.5, "S0"), w("c.", 0.6, 0.8, "S1")])
    assert s[0].speaker == "S0"


def test_snap_widens_to_sentences_then_moves_into_pauses() -> None:
    sentences = clips.build_sentences(WORDS)
    # A rough pick from mid "Second..." to mid "Third...".
    start, end = clips.snap(2.0, 4.2, sentences, SILENCES)
    # Sentence edges are 1.5 and 5.2; the pause middles nearby are 1.25 and 5.35.
    assert (start, end) == (1.25, 5.35)


def test_snap_keeps_sentence_edge_when_no_pause_is_close() -> None:
    sentences = clips.build_sentences(WORDS)
    assert clips.snap(2.0, 4.2, sentences, []) == (1.5, 5.2)
    # A pause more than 0.5 s away is ignored.
    assert clips.snap(2.0, 4.2, sentences, [(0.2, 0.4), (6.0, 6.2)]) == (1.5, 5.2)


def test_snap_never_moves_into_kept_speech() -> None:
    sentences = clips.build_sentences(WORDS)
    # A pause right after the start edge would cut the first word: not taken.
    assert clips.snap(1.6, 2.8, sentences, [(1.6, 1.7)])[0] == 1.5


def test_snap_rejects_an_empty_clip() -> None:
    with pytest.raises(ValueError):
        clips.snap(5.0, 5.0, [], [])


def test_parse_silences_pairs_start_and_end() -> None:
    stderr = ("[silencedetect @ 0x1] silence_start: 1.05\n"
              "[silencedetect @ 0x1] silence_end: 1.45 | silence_duration: 0.4\n"
              "[silencedetect @ 0x1] silence_start: 2.95\n"
              "[silencedetect @ 0x1] silence_end: 3.55 | silence_duration: 0.6\n")
    assert parse_silences(stderr) == [(1.05, 1.45), (2.95, 3.55)]
