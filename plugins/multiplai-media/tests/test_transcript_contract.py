"""The word-level transcript contract (stages/transcript.py) and where prep gets it.

Both converters must produce {language, engine, words: [{text, start, end,
speaker?}]}; speaker labels must survive; and source (a), the transcribe skill,
must win over source (b), prep's own mlx_whisper, whenever it advertises
word-level JSON. A fake transcribe.sh in place of the sibling skill's script
stands in for the skill.
"""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "video-edit" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from stages import prep  # noqa: E402
from stages import transcript as tx  # noqa: E402

_FIX = Path(__file__).resolve().parent / "fixtures"


def test_whisper_json_converts_to_contract() -> None:
    data = json.loads((_FIX / "whisper-words.json").read_text())
    c = tx.from_whisper_json(data, engine="mlx_whisper:test-model", language="en")
    assert c["language"] == "en"
    assert c["engine"] == "mlx_whisper:test-model"
    assert [w["text"] for w in c["words"]] == [
        "Hello", "and", "welcome.", "Today", "we", "talk", "about", "boats."]
    assert c["words"][0] == {"text": "Hello", "start": 0.12, "end": 0.48}
    assert all("speaker" not in w for w in c["words"])
    assert not tx.has_speakers(c)


def test_transcribe_skill_json_converts_and_keeps_speaker() -> None:
    data = json.loads((_FIX / "transcribe-skill-words.json").read_text())
    c = tx.from_transcribe_skill(data)
    assert c["language"] == "it"
    assert c["engine"] == "transcribe-skill:parakeet-tdt-v3"
    assert [w["text"] for w in c["words"]] == [
        "Buongiorno", "a", "tutti.", "Grazie", "dell'invito."]
    assert [w["speaker"] for w in c["words"]] == [
        "SPEAKER_0", "SPEAKER_0", "SPEAKER_0", "SPEAKER_1", "SPEAKER_1"]
    assert tx.has_speakers(c)


def test_contract_round_trips_through_disk(tmp_path: Path) -> None:
    c = tx.from_transcribe_skill(json.loads((_FIX / "transcribe-skill-words.json").read_text()))
    tx.write(c, tmp_path / "transcript.json")
    assert tx.load(tmp_path / "transcript.json") == c


def test_load_rejects_a_non_contract(tmp_path: Path) -> None:
    (tmp_path / "x.json").write_text(json.dumps({"segments": []}))
    with pytest.raises(ValueError, match="not a video-edit transcript"):
        tx.load(tmp_path / "x.json")


def _fake_transcribe(bin_dir: Path, help_text: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "transcribe.sh"
    script.write_text(f"#!/bin/bash\ncat <<'TXT'\n{help_text}\nTXT\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_transcribe_skill_with_word_flag_is_chosen_first(tmp_path: Path, monkeypatch) -> None:
    fake = _fake_transcribe(tmp_path / "skill", f"Usage: transcribe.sh <audio> [out] {tx.WORDS_JSON_FLAG}")
    monkeypatch.setattr(tx, "_SIBLING_TRANSCRIBE", fake)
    source, script = tx.choose_source()
    assert source == "transcribe-skill"
    assert script == fake


def test_transcribe_skill_without_word_flag_falls_back_to_mlx_whisper(tmp_path: Path, monkeypatch) -> None:
    fake = _fake_transcribe(tmp_path / "skill", "Usage: transcribe.sh <audio_file> [output_file] [--language <code>]")
    monkeypatch.setattr(tx, "_SIBLING_TRANSCRIBE", fake)
    assert tx.choose_source() == ("mlx_whisper", None)


def test_a_transcribe_sh_on_path_is_never_run(tmp_path: Path, monkeypatch) -> None:
    # Only the plugin's own transcribe skill is trusted; a same-named script
    # elsewhere on PATH must not even be asked for --help.
    marker = tmp_path / "ran"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stray = bin_dir / "transcribe.sh"
    stray.write_text(f"#!/bin/bash\ntouch {marker}\necho {tx.WORDS_JSON_FLAG}\n")
    stray.chmod(stray.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr(tx, "_SIBLING_TRANSCRIBE", tmp_path / "absent" / "transcribe.sh")
    assert tx.transcribe_skill_script() is None
    assert tx.choose_source() == ("mlx_whisper", None)
    assert not marker.exists()


def test_transcribe_skill_argv_asks_for_words() -> None:
    argv = tx.transcribe_skill_argv(Path("/s/transcribe.sh"), Path("/c/a.wav"), Path("/c/w.json"), "it")
    assert argv == ["bash", "/s/transcribe.sh", "/c/a.wav", "/c/w.json", "--override",
                    tx.WORDS_JSON_FLAG, "--language", "it"]


# --- source (b): prep's own mlx_whisper argv ---------------------------------

def test_mlx_args_ask_for_word_timestamps_and_json() -> None:
    args = prep._mlx_args(Path("/c/audio16k.wav"), Path("/c/whisper"), "", "it", None)
    assert args[args.index("--word-timestamps") + 1] == "True"
    assert args[args.index("--condition-on-previous-text") + 1] == "False"
    assert args[args.index("--output-format") + 1] == "all"
    assert args[args.index("--model") + 1] == "mlx-community/whisper-large-v3-mlx"


def test_english_and_undetected_keep_the_medium_model() -> None:
    for lang in ("en", None):
        args = prep._mlx_args(Path("/c/a.wav"), Path("/c/whisper"), "", lang, None)
        assert args[args.index("--model") + 1] == "mlx-community/whisper-medium-mlx"


def test_explicit_model_wins() -> None:
    args = prep._mlx_args(Path("/c/a.wav"), Path("/c/whisper"), "", "it", "my/model")
    assert args[args.index("--model") + 1] == "my/model"


def test_cache_key_has_no_spaces(tmp_path: Path) -> None:
    src = tmp_path / "My Show - ep 4 (6 ott).mp4"
    src.write_bytes(b"x")
    key = prep._source_key(src)
    assert " " not in key and "(" not in key
    assert key.startswith("My-Show-ep-4-6-ott-")


# --- prep._transcript_contract: reuse, both sources, and no words -------------

def _no_transcriber(*_a, **_k):
    raise AssertionError("must not transcribe")


def test_contract_reuses_an_existing_transcript_without_transcribing(tmp_path: Path, monkeypatch) -> None:
    c = tx.from_transcribe_skill(json.loads((_FIX / "transcribe-skill-words.json").read_text()))
    tx.write(c, tmp_path / "transcript.json")
    monkeypatch.setattr(tx, "choose_source", _no_transcriber)
    monkeypatch.setattr(prep, "_transcribe", _no_transcriber)
    monkeypatch.setattr(prep.subprocess, "run", _no_transcriber)
    got, path = prep._transcript_contract(tmp_path / "a.wav", tmp_path, "", None, None)
    assert got == c and path == tmp_path / "transcript.json"


def test_contract_from_the_transcribe_skill_writes_json_and_srt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(tx, "choose_source", lambda: ("transcribe-skill", Path("/s/transcribe.sh")))
    seen: list[list[str]] = []

    def fake_run(argv, *a, **k):
        seen.append(argv)
        (tmp_path / "transcribe-words.json").write_text((_FIX / "transcribe-skill-words.json").read_text())

    monkeypatch.setattr(prep.subprocess, "run", fake_run)
    monkeypatch.setattr(prep, "_transcribe", _no_transcriber)
    got, path = prep._transcript_contract(tmp_path / "a.wav", tmp_path, "", "it", None)
    assert seen == [tx.transcribe_skill_argv(Path("/s/transcribe.sh"), tmp_path / "a.wav",
                                             tmp_path / "transcribe-words.json", "it")]
    assert tx.load(path) == got and got["engine"] == "transcribe-skill:parakeet-tdt-v3"
    assert (tmp_path / "transcript.srt").read_text() == tx.to_srt(got)


def _fake_whisper(tmp_path: Path, monkeypatch, data: dict, srt: str | None) -> None:
    def fake_transcribe(audio, dst_stem, prompt_hint="", language=None, model=None):
        raw = Path(str(dst_stem) + ".json")
        raw.write_text(json.dumps(data))
        if srt is not None:
            (tmp_path / "whisper.srt").write_text(srt)
        return raw

    monkeypatch.setattr(tx, "choose_source", lambda: ("mlx_whisper", None))
    monkeypatch.setattr(prep, "_transcribe", fake_transcribe)


def test_contract_from_mlx_whisper_copies_its_srt(tmp_path: Path, monkeypatch) -> None:
    _fake_whisper(tmp_path, monkeypatch, json.loads((_FIX / "whisper-words.json").read_text()),
                  srt="1\n00:00:00,120 --> 00:00:01,000\nHello\n")
    got, path = prep._transcript_contract(tmp_path / "a.wav", tmp_path, "", "en", None)
    assert got["engine"].startswith("mlx_whisper:") and len(got["words"]) == 8
    assert tx.load(path) == got
    assert (tmp_path / "transcript.srt").read_text() == "1\n00:00:00,120 --> 00:00:01,000\nHello\n"


def test_contract_from_mlx_whisper_without_srt_writes_one(tmp_path: Path, monkeypatch) -> None:
    _fake_whisper(tmp_path, monkeypatch, json.loads((_FIX / "whisper-words.json").read_text()), srt=None)
    got, _ = prep._transcript_contract(tmp_path / "a.wav", tmp_path, "", "en", None)
    assert (tmp_path / "transcript.srt").read_text() == tx.to_srt(got)


def test_contract_with_no_words_raises_and_writes_no_transcript(tmp_path: Path, monkeypatch) -> None:
    # Word timestamps off: segments carry no `words`.
    _fake_whisper(tmp_path, monkeypatch,
                  {"language": "it", "segments": [{"start": 0, "end": 2, "text": " Ciao"}]}, srt=None)
    with pytest.raises(RuntimeError, match="no word timings"):
        prep._transcript_contract(tmp_path / "a.wav", tmp_path, "", "it", None)
    assert not (tmp_path / "transcript.json").exists()


def test_a_whisper_token_without_a_leading_space_joins_the_word_before() -> None:
    # whisper writes "dell'intelligenza" as " dell" + "'intelligenza" and
    # "sub-milliseconds" as " sub" + "-milliseconds"; the caption must show
    # one word, not "dell 'intelligenza".
    data = {"segments": [
        {"words": [{"word": " dell", "start": 1.0, "end": 1.2},
                   {"word": "'intelligenza", "start": 1.2, "end": 1.9},
                   {"word": " in", "start": 2.0, "end": 2.1},
                   {"word": " sub", "start": 2.1, "end": 2.3}]},
        {"words": [{"word": "-milliseconds,", "start": 2.3, "end": 2.9},
                   {"word": " 30", "start": 3.0, "end": 3.2},
                   {"word": ",000", "start": 3.2, "end": 3.5}]}]}
    c = tx.from_whisper_json(data, engine="mlx_whisper:test")
    assert c["words"] == [
        {"text": "dell'intelligenza", "start": 1.0, "end": 1.9},
        {"text": "in", "start": 2.0, "end": 2.1},
        {"text": "sub-milliseconds,", "start": 2.1, "end": 2.9},
        {"text": "30,000", "start": 3.0, "end": 3.5}]


def test_a_first_token_without_a_leading_space_stays_a_word() -> None:
    data = {"segments": [{"words": [{"word": "Ciao", "start": 0.0, "end": 0.3},
                                    {"word": " a", "start": 0.3, "end": 0.4}]}]}
    assert [w["text"] for w in tx.from_whisper_json(data, engine="t")["words"]] == ["Ciao", "a"]


def test_audio_extraction_fills_dropouts_with_silence(tmp_path: Path, monkeypatch) -> None:
    # Packets that jump seconds ahead must become silence, not be joined up,
    # or every later word is timed early.
    calls: list[list[str]] = []
    monkeypatch.setattr(prep.subprocess, "run", lambda cmd, *a, **k: calls.append(list(cmd)))
    prep._extract_audio(tmp_path / "proxy_720p.mp4", tmp_path / "audio16k.wav")
    cmd = calls[0]
    assert cmd[cmd.index("-af") + 1] == "aresample=async=1:first_pts=0"
    assert cmd.index("-af") < cmd.index("-ar")


def test_a_short_audio_track_is_reported() -> None:
    assert prep.audio_length_warning(1756.47, 1756.47) is None
    assert prep.audio_length_warning(1756.47, 1756.2) is None
    msg = prep.audio_length_warning(1756.47, 1704.46)
    assert msg and "1704.5s" in msg and "1756.5s" in msg


def test_audio_gaps_finds_long_packets_and_jumps() -> None:
    d = 0.02322
    packets = [(i * d, d) for i in range(100)]
    packets[50] = (50 * d, 4.04)                       # a packet that claims 4 s
    packets = packets[:51] + [(p + 4.04 - d, dd) for p, dd in packets[51:]]
    packets[80] = (packets[80][0] + 0.5, d)            # a 0.5 s jump before packet 80
    packets = packets[:81] + [(p + 0.5, dd) for p, dd in packets[81:]]
    gaps = prep.audio_gaps(packets)
    assert gaps == [{"at": round(50 * d + d, 3), "length": round(4.04 - d, 3)},
                    {"at": round(packets[79][0] + d, 3), "length": 0.5}]


def test_audio_gaps_is_empty_for_a_clean_track() -> None:
    assert prep.audio_gaps([(i * 0.02, 0.02) for i in range(50)]) == []
    assert prep.audio_gaps([]) == []
