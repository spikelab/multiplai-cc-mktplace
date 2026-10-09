"""The word-level transcript contract, and the two places it comes from.

`transcript.json` is the one file everything downstream reads — retakes,
sentences, captions, the output timeline, speaker framing:

    {"language": "it", "engine": "mlx_whisper:mlx-community/whisper-large-v3-mlx",
     "words": [{"text": "Ciao", "start": 0.52, "end": 0.81, "speaker": "SPEAKER_0"}, ...]}

`speaker` is optional and present only when the source transcript carried
speaker labels.

Prep fills it from one of two sources, in this order:

  (a) The `transcribe` skill of this plugin (its own scripts/transcribe.sh,
      never one found on PATH), when its `transcribe.sh --help` lists
      WORDS_JSON_FLAG. That skill is where Parakeet and diarization
      arrive; this skill never calls FluidAudio itself.
  (b) Otherwise prep's own host `mlx_whisper` call (stages/prep.py), run with
      word timestamps and JSON output.

The `transcribe` skill does not offer word-level JSON yet. WORDS_JSON_FLAG is
the flag this skill looks for; whoever adds word output to `transcribe.sh`
should use this name, or change it here in the same change.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

WORDS_JSON_FLAG = "--words-json"

# skills/video-edit/scripts/stages/transcript.py → skills/transcribe/scripts/
_SIBLING_TRANSCRIBE = Path(__file__).resolve().parents[3] / "transcribe" / "scripts" / "transcribe.sh"


def transcribe_skill_script() -> Path | None:
    """The sibling `transcribe` skill's entry point in this plugin, if present.

    Only that file: a `transcribe.sh` elsewhere on PATH is never run, since
    prep would execute it with the user's credentials.
    """
    return _SIBLING_TRANSCRIBE if _SIBLING_TRANSCRIBE.exists() else None


def transcribe_skill_has_words(script: Path | None) -> bool:
    """True when the transcribe skill can emit word-level JSON (source a)."""
    if script is None:
        return False
    try:
        proc = subprocess.run(["bash", str(script), "--help"], capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return WORDS_JSON_FLAG in (proc.stdout + proc.stderr)


def choose_source() -> tuple[str, Path | None]:
    """("transcribe-skill", script) when source (a) is usable, else ("mlx_whisper", None)."""
    script = transcribe_skill_script()
    if transcribe_skill_has_words(script):
        return "transcribe-skill", script
    return "mlx_whisper", None


def transcribe_skill_argv(script: Path, audio: Path, out_json: Path,
                          language: str | None) -> list[str]:
    argv = ["bash", str(script), str(audio), str(out_json), "--override", WORDS_JSON_FLAG]
    if language:
        argv += ["--language", language]
    return argv


def _word(text: Any, start: Any, end: Any, speaker: Any = None) -> dict | None:
    t = str(text or "").strip()
    if not t or start is None or end is None:
        return None
    w: dict[str, Any] = {"text": t, "start": round(float(start), 3), "end": round(float(end), 3)}
    if speaker not in (None, ""):
        w["speaker"] = str(speaker)
    return w


def from_whisper_json(data: dict, engine: str, language: str | None = None) -> dict:
    """mlx_whisper `--word-timestamps True --output-format json` → contract.

    whisper prefixes each word with its leading space (" Ciao"); that is
    stripped. A token with no leading space continues the word before it
    ("dell" + "'intelligenza", "sub" + "-milliseconds", "30" + ",000"), so it
    is joined onto that word, which keeps its start and takes the token's end.
    Segments without `words` (word timestamps off) contribute nothing.
    """
    words: list[dict] = []
    for seg in data.get("segments", []):
        for w in seg.get("words", []) or []:
            raw = str(w.get("word") or "")
            cw = _word(raw, w.get("start"), w.get("end"))
            if not cw:
                continue
            if words and not raw[0].isspace():
                words[-1]["text"] += cw["text"]
                words[-1]["end"] = max(words[-1]["end"], cw["end"])
            else:
                words.append(cw)
    return {"language": language or data.get("language") or "", "engine": engine, "words": words}


def from_transcribe_skill(data: dict) -> dict:
    """The transcribe skill's word JSON → contract.

    Accepts the words either flat (`words: [...]`) or nested in segments
    (`segments: [{speaker, words: [...]}]`). Each word's text may be under
    `text` or `word`; its speaker on the word or inherited from its segment.
    """
    words = []

    def take(items: list, inherited_speaker: Any = None) -> None:
        for w in items or []:
            cw = _word(w.get("text", w.get("word")), w.get("start"), w.get("end"),
                       w.get("speaker", inherited_speaker))
            if cw:
                words.append(cw)

    take(data.get("words", []))
    for seg in data.get("segments", []):
        take(seg.get("words", []), seg.get("speaker"))
    words.sort(key=lambda w: w["start"])
    return {
        "language": data.get("language", ""),
        "engine": "transcribe-skill:" + str(data.get("engine", "unknown")),
        "words": words,
    }


def write(contract: dict, path: Path) -> None:
    path.write_text(json.dumps(contract, ensure_ascii=False, indent=1) + "\n")


def load(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text())
    for key in ("language", "engine", "words"):
        if key not in data:
            raise ValueError(f"{path} is not a video-edit transcript: missing {key!r}")
    return data


def has_speakers(contract: dict) -> bool:
    words = contract.get("words", [])
    return bool(words) and all("speaker" in w for w in words)


def to_srt(contract: dict, max_words: int = 12) -> str:
    """A plain SRT from the words, for people and tools that want subtitles."""
    def ts(t: float) -> str:
        ms = int(round(t * 1000))
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    words = contract["words"]
    out, n = [], 0
    for i in range(0, len(words), max_words):
        chunk = words[i:i + max_words]
        n += 1
        out.append(f"{n}\n{ts(chunk[0]['start'])} --> {ts(chunk[-1]['end'])}\n"
                   + " ".join(w["text"] for w in chunk) + "\n")
    return "\n".join(out)
