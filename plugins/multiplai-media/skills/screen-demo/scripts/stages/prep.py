from __future__ import annotations
import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parent.parent.parent

def _cache_root() -> Path:
    ws = os.environ.get("WORKSPACE")
    if ws and Path(ws).is_dir():
        return Path(ws) / ".screen-demo-cache"
    return Path.home() / ".cache" / "screen-demo"

CACHE_ROOT = _cache_root()
MLX_BIN = "mlx_whisper"
MLX_MODEL_EN = "mlx-community/whisper-medium.en-mlx-8bit"
SSH_KEY = Path("/home/agent/.ssh/build_key")
SSH_HOST = os.environ.get("TRANSCRIBE_HOST", "host.docker.internal")
# No personal default: the bridge user must come from .env (SSH_BUILD_USER) or
# an explicit TRANSCRIBE_USER override. Empty → ssh would get a malformed "@host".
SSH_USER = os.environ.get("TRANSCRIBE_USER") or os.environ.get("SSH_BUILD_USER", "")


@dataclass
class CutCandidate:
    t: float
    kind: str   # "silence_end" | "scene_change" | "freeze_end"
    note: str = ""


@dataclass
class PrepResult:
    source: str
    src_duration: float
    proxy_path: str
    audio_path: str
    transcript_srt_path: str
    cuts_path: str
    context_path: str


def _source_key(src: Path) -> str:
    st = src.stat()
    h = hashlib.sha1(f"{src.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:12]
    return f"{src.stem}-{h}"


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], text=True
    ).strip()
    return float(out)


def _make_proxy(source: Path, dst: Path) -> None:
    if dst.exists():
        return
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-vf", "scale=-2:720",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "copy",
        str(dst),
    ], check=True)


def _extract_audio(proxy: Path, dst: Path) -> None:
    if dst.exists():
        return
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(proxy),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(dst),
    ], check=True)


def _silencedetect(audio: Path) -> list[CutCandidate]:
    p = subprocess.run([
        "ffmpeg", "-hide_banner", "-i", str(audio),
        "-vn", "-af", "silencedetect=noise=-30dB:d=1.0",
        "-f", "null", "-",
    ], capture_output=True, text=True)
    cuts = []
    for m in re.finditer(r"silence_end:\s+([\d.]+)", p.stderr):
        cuts.append(CutCandidate(t=float(m.group(1)), kind="silence_end"))
    return cuts


def _scenedetect(proxy: Path, work: Path) -> list[CutCandidate]:
    csv = work / "scenes.csv"
    if not csv.exists():
        subprocess.run([
            "scenedetect", "-i", str(proxy), "-q",
            "detect-content", "-t", "12", "-m", "60",
            "list-scenes", "-f", str(csv), "-s",
        ], check=True)
    cuts = []
    text = csv.read_text().splitlines()
    for line in text[2:]:
        parts = line.split(",")
        if len(parts) > 3:
            try:
                cuts.append(CutCandidate(t=float(parts[3]), kind="scene_change"))
            except ValueError:
                continue
    return cuts


def _transcribe(audio: Path, dst_stem: Path, prompt_hint: str = "") -> Path:
    """Transcribe via mlx_whisper on the Mac host (Metal GPU) over SSH bridge.

    Requires:
      - SSH key at /home/agent/.ssh/build_key (transcribe-skill convention)
      - mlx_whisper installed on the Mac (`uv tool install mlx-whisper`)
      - Cache dir resolvable to identical path on both sides — ensured by
        rooting cache under $WORKSPACE.
    """
    srt = Path(str(dst_stem) + ".srt")
    if srt.exists():
        return srt
    if not SSH_KEY.exists():
        raise RuntimeError(
            f"SSH key for Mac bridge not found at {SSH_KEY}. "
            f"This skill requires the multiplai-runtime SSH-to-host setup."
        )
    if not SSH_USER:
        raise RuntimeError(
            "No SSH user for the container→host bridge. "
            "Set SSH_BUILD_USER (or TRANSCRIBE_USER) in .env — see .env.example."
        )
    out_dir = dst_stem.parent
    base = dst_stem.name
    parts = [
        MLX_BIN,
        "--model", MLX_MODEL_EN,
        "--output-format", "srt",
        "--output-dir", str(out_dir),
        "--output-name", base,
        "--verbose", "False",
    ]
    if prompt_hint:
        parts.extend(["--initial-prompt", prompt_hint])
    parts.append(str(audio))
    remote_cmd = " ".join(_sh_quote(p) for p in parts)
    ssh_cmd = [
        "ssh", "-q", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
        "-i", str(SSH_KEY), f"{SSH_USER}@{SSH_HOST}", remote_cmd,
    ]
    subprocess.run(ssh_cmd, check=True)
    if not srt.exists():
        raise RuntimeError(f"mlx_whisper completed but {srt} not found")
    return srt


def _sh_quote(s: str) -> str:
    if not s or any(c in s for c in " \t'\"\\$"):
        return "'" + s.replace("'", "'\"'\"'") + "'"
    return s


def _parse_srt(srt: Path) -> list[dict]:
    out = []
    txt = srt.read_text()
    for m in re.finditer(r"(\d+)\n([\d:,]+) --> ([\d:,]+)\n((?:.*\n)+?)\n", txt + "\n\n"):
        def t2s(t: str) -> float:
            h, mi, s = t.split(":")
            s, ms = s.split(",")
            return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000
        out.append({
            "idx": int(m.group(1)),
            "start": t2s(m.group(2)),
            "end": t2s(m.group(3)),
            "text": m.group(4).strip().replace("\n", " "),
        })
    return out


def _write_context(result: PrepResult, segments: list[dict], cuts: list[CutCandidate], dst: Path) -> None:
    lines = [
        "# screen-demo prep context",
        "",
        f"- source: {result.source}",
        f"- duration: {result.src_duration:.1f}s",
        f"- proxy: {result.proxy_path}",
        f"- transcript (SRT): {result.transcript_srt_path}",
        f"- cuts (JSON): {result.cuts_path}",
        "",
        "## Cut candidates (use as anchors when authoring the EDL)",
        "",
        "| t (s) | kind | note |",
        "|---|---|---|",
    ]
    for c in sorted(cuts, key=lambda x: x.t):
        lines.append(f"| {c.t:.2f} | {c.kind} | {c.note} |")
    lines.extend([
        "",
        "## Transcript (timecoded)",
        "",
    ])
    for s in segments:
        lines.append(f"- [{s['start']:.1f}–{s['end']:.1f}] {s['text']}")
    dst.write_text("\n".join(lines))


def prep(source: str | Path, prompt_hint: str = "") -> PrepResult:
    src = Path(source).resolve()
    if not src.exists():
        raise FileNotFoundError(src)
    key = _source_key(src)
    cache = CACHE_ROOT / key
    cache.mkdir(parents=True, exist_ok=True)

    print(f"→ prep: cache dir = {cache}")
    duration = _ffprobe_duration(src)
    proxy = cache / "proxy_720p.mp4"
    audio = cache / "audio16k.wav"
    transcript_stem = cache / "transcript"
    transcript = Path(str(transcript_stem) + ".srt")
    cuts_path = cache / "cuts.json"
    context_path = cache / "context.md"

    print(f"→ prep: {'reusing' if proxy.exists() else 'building'} proxy")
    _make_proxy(src, proxy)
    print(f"→ prep: {'reusing' if audio.exists() else 'extracting'} audio")
    _extract_audio(proxy, audio)
    print(f"→ prep: {'reusing' if transcript.exists() else 'transcribing'} (mlx_whisper via SSH bridge)")
    _transcribe(audio, transcript_stem, prompt_hint=prompt_hint)

    print("→ prep: detecting cuts (silencedetect + scenedetect)")
    silence_cuts = _silencedetect(audio)
    scene_cuts = _scenedetect(proxy, cache)
    all_cuts = silence_cuts + scene_cuts

    cuts_path.write_text(json.dumps([asdict(c) for c in all_cuts], indent=2))

    segments = _parse_srt(transcript)
    result = PrepResult(
        source=str(src),
        src_duration=duration,
        proxy_path=str(proxy),
        audio_path=str(audio),
        transcript_srt_path=str(transcript),
        cuts_path=str(cuts_path),
        context_path=str(context_path),
    )
    _write_context(result, segments, all_cuts, context_path)
    print(f"→ prep: context written to {context_path}")
    print(f"   {len(segments)} transcript segments, {len(all_cuts)} cut candidates")
    return result
