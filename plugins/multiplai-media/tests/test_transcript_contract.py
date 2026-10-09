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
