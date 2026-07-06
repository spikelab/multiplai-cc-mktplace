from __future__ import annotations
import hashlib
import json
import os
import re
import shlex
import shutil
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

# Locally-built whisper.cpp (see bootstrap.sh) — the default, no-Mac-required path.
WHISPER_CLI = SKILL_ROOT / "vendor" / "whisper.cpp" / "build" / "bin" / "whisper-cli"
WHISPER_MODEL = SKILL_ROOT / "vendor" / "whisper.cpp" / "models" / "ggml-small.en.bin"

# SSH bridge (opt-in fallback only): used when no local whisper binary is present
# AND a bridge is configured. The bridge user must come from the environment
# (SSH_BUILD_USER) or an explicit TRANSCRIBE_USER override.
SSH_KEY = Path(
    os.environ.get("TRANSCRIBE_KEY") or os.environ.get("SSH_BUILD_KEY") or "/home/agent/.ssh/build_key"
)
SSH_HOST = os.environ.get("TRANSCRIBE_HOST", "host.docker.internal")
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
    """Transcribe to SRT. Default path is local + free (no Mac required):

      1. whisper.cpp `whisper-cli` built by bootstrap.sh (vendor/whisper.cpp)
      2. `mlx_whisper` on PATH (Apple Silicon)
      3. SSH bridge to a Mac host — opt-in fallback, only when no local binary
         is available AND a bridge is configured (SSH_BUILD_USER/TRANSCRIBE_USER
         + a reachable key).
    """
    srt = Path(str(dst_stem) + ".srt")
    if srt.exists():
        return srt

    # 1. Locally-built whisper.cpp (the default, no-Mac path).
    if WHISPER_CLI.exists() and WHISPER_MODEL.exists():
        return _transcribe_whisper_cli(audio, dst_stem, srt, prompt_hint)

    # 2. mlx_whisper on PATH.
    if shutil.which(MLX_BIN):
        return _transcribe_mlx_local(audio, dst_stem, srt, prompt_hint)

    # 3. SSH bridge — opt-in fallback only.
    if SSH_USER and SSH_KEY.exists():
        return _transcribe_ssh(audio, dst_stem, srt, prompt_hint)

    raise RuntimeError(
        "No transcription backend available. Run bootstrap.sh to build the "
        "local whisper.cpp binary, or install mlx_whisper (Apple Silicon). "
        "To use a Mac over the SSH bridge instead, set SSH_BUILD_USER (or "
        "TRANSCRIBE_USER) and provide an SSH key (TRANSCRIBE_KEY/SSH_BUILD_KEY)."
    )


def _transcribe_whisper_cli(audio: Path, dst_stem: Path, srt: Path, prompt_hint: str) -> Path:
    cmd = [
        str(WHISPER_CLI),
        "-m", str(WHISPER_MODEL),
        "-f", str(audio),
        "--output-srt",
        "--output-file", str(dst_stem),  # whisper-cli appends .srt
    ]
    if prompt_hint:
        cmd.extend(["--prompt", prompt_hint])
    subprocess.run(cmd, check=True)
    if not srt.exists():
        raise RuntimeError(f"whisper-cli completed but {srt} not found")
    return srt


def _transcribe_mlx_local(audio: Path, dst_stem: Path, srt: Path, prompt_hint: str) -> Path:
    cmd = [
        MLX_BIN,
        "--model", MLX_MODEL_EN,
        "--output-format", "srt",
        "--output-dir", str(dst_stem.parent),
        "--output-name", dst_stem.name,
        "--verbose", "False",
    ]
    if prompt_hint:
        cmd.extend(["--initial-prompt", prompt_hint])
    cmd.append(str(audio))
    subprocess.run(cmd, check=True)
    if not srt.exists():
        raise RuntimeError(f"mlx_whisper completed but {srt} not found")
    return srt


def _transcribe_ssh(audio: Path, dst_stem: Path, srt: Path, prompt_hint: str) -> Path:
    parts = [
        MLX_BIN,
        "--model", MLX_MODEL_EN,
        "--output-format", "srt",
        "--output-dir", str(dst_stem.parent),
        "--output-name", dst_stem.name,
        "--verbose", "False",
    ]
    if prompt_hint:
        parts.extend(["--initial-prompt", prompt_hint])
    parts.append(str(audio))
    # shlex.quote every arg so an attacker-controlled path/filename cannot inject
    # shell on the remote host (CWE-78).
    remote_cmd = " ".join(shlex.quote(p) for p in parts)
    ssh_cmd = [
        "ssh", "-q", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
        "-i", str(SSH_KEY), f"{SSH_USER}@{SSH_HOST}", remote_cmd,
    ]
    subprocess.run(ssh_cmd, check=True)
    if not srt.exists():
        raise RuntimeError(f"mlx_whisper completed but {srt} not found")
    return srt


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
    print(f"→ prep: {'reusing' if transcript.exists() else 'transcribing'} (local whisper; SSH bridge only if no local binary)")
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
