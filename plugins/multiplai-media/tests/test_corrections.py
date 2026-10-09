"""Transcript corrections (stages/corrections.py): replace misheard words,
keep their timing, back up the original, write nothing on a miss."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import corrections as co  # noqa: E402
from stages import transcript as tx  # noqa: E402


def w(text, s, e, **kw):
    return {"text": text, "start": s, "end": e, **kw}


WORDS = [w("It", 10.0, 10.2), w("is", 10.2, 10.4), w("a", 10.4, 10.5), w("bug", 10.5, 10.8),
         w("in", 10.8, 10.9), w("Century.", 10.9, 11.5), w("Sono", 20.0, 20.3),
         w("CINFUORI", 20.3, 21.1), w("ok,", 21.2, 21.5), w("una", 30.0, 30.2),
         w("ostra", 30.2, 30.8), w("sub", 40.0, 40.2), w("-milliseconds,", 40.2, 40.9)]


def texts(words):
    return [x["text"] for x in words]


def test_one_word_for_one_keeps_its_timing_and_its_punctuation() -> None:
    out = co.apply(WORDS, [{"at": 11, "from": "Century", "to": "Sentry"}])
    assert out[5] == w("Sentry.", 10.9, 11.5)
    assert texts(out)[:5] == texts(WORDS)[:5]


def test_one_word_for_two_splits_the_time_by_length() -> None:
    out = co.apply(WORDS, [{"at": 20.3, "from": "CINFUORI", "to": "CIN fuori"}])
    assert texts(out)[6:10] == ["Sono", "CIN", "fuori", "ok,"]
    cin, fuori = out[7], out[8]
    # 0.8 s for 3 + 5 characters: 0.3 s and 0.5 s, edge to edge.
    assert (cin["start"], cin["end"]) == (20.3, 20.6)
    assert (fuori["start"], fuori["end"]) == (20.6, 21.1)


def test_two_words_for_one_spans_the_run() -> None:
    out = co.apply(WORDS, [{"at": 40, "from": "sub -milliseconds", "to": "sub-milliseconds"}])
    assert out[-1] == w("sub-milliseconds,", 40.0, 40.9)
    assert len(out) == len(WORDS) - 1


def test_two_words_for_two_keep_their_own_timing() -> None:
    out = co.apply(WORDS, [{"at": 30, "from": "una ostra", "to": "una nostra"}])
    assert out[9:11] == [w("una", 30.0, 30.2), w("nostra", 30.2, 30.8)]


def test_match_is_case_sensitive_ignores_punctuation_and_stays_near_at() -> None:
    assert co.apply(WORDS, [{"at": 21, "from": "ok", "to": "okay"}])[8]["text"] == "okay,"
    with pytest.raises(co.CorrectionError):
        co.apply(WORDS, [{"at": 11, "from": "century", "to": "Sentry"}])
    with pytest.raises(co.CorrectionError):
        co.apply(WORDS, [{"at": 14, "from": "Century", "to": "Sentry"}])   # 3.1 s away


def test_the_nearest_of_two_matches_wins() -> None:
    words = [w("Rust", 1.0, 1.2), w("x", 2.0, 2.1), w("Rust", 4.0, 4.2)]
    out = co.apply(words, [{"at": 3.5, "from": "Rust", "to": "rust"}])
    assert texts(out) == ["Rust", "x", "rust"]


def test_a_new_word_keeps_the_runs_speaker() -> None:
    words = [w("CINFUORI", 1.0, 1.8, speaker="SPEAKER_1")]
    out = co.apply(words, [{"at": 1, "from": "CINFUORI", "to": "CIN fuori"}])
    assert [x["speaker"] for x in out] == ["SPEAKER_1", "SPEAKER_1"]


def _cache(tmp_path: Path) -> Path:
    tx.write({"language": "en", "engine": "test", "words": WORDS}, tmp_path / "transcript.json")
    return tmp_path


def test_correct_backs_up_the_original_and_writes_the_srt(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    fixes = tmp_path / "fixes.json"
    fixes.write_text(json.dumps([{"at": 11, "from": "Century", "to": "Sentry"}]))
    co.correct(cache, fixes)
    assert "Century." in texts(tx.load(cache / "transcript.raw.json")["words"])
    assert "Sentry." in texts(tx.load(cache / "transcript.json")["words"])
    assert "Sentry." in (cache / "transcript.srt").read_text()
    # A second run starts again from the original, so it gives the same result.
    co.correct(cache, fixes)
    assert texts(tx.load(cache / "transcript.json")["words"]).count("Sentry.") == 1
    assert "Century." in texts(tx.load(cache / "transcript.raw.json")["words"])


def test_an_entry_that_matches_nothing_writes_nothing_and_names_it(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    before = (cache / "transcript.json").read_text()
    fixes = tmp_path / "fixes.json"
    fixes.write_text(json.dumps([{"at": 11, "from": "Century", "to": "Sentry"},
                                 {"at": 99, "from": "Sentinel", "to": "x"}]))
    with pytest.raises(co.CorrectionError, match="'Sentinel' within 2s of 99"):
        co.correct(cache, fixes)
    assert (cache / "transcript.json").read_text() == before
    assert not (cache / "transcript.raw.json").exists()


def test_a_malformed_entry_is_refused(tmp_path: Path) -> None:
    fixes = tmp_path / "fixes.json"
    fixes.write_text(json.dumps([{"at": 1, "from": "a"}]))
    with pytest.raises(co.CorrectionError, match="exactly at, from and to"):
        co.load(fixes)


def test_source_of_reads_the_context_line(tmp_path: Path) -> None:
    (tmp_path / "context.md").write_text("# video-edit prep context\n\n- source: /rec/talk.mp4\n")
    assert co.source_of(tmp_path) == "/rec/talk.mp4"
