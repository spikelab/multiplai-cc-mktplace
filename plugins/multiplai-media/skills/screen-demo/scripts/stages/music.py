from __future__ import annotations
import hashlib
import os
import shutil
import subprocess
from pathlib import Path


def _cache_dir() -> Path:
    ws = os.environ.get("WORKSPACE")
    base = Path(ws) / ".screen-demo-cache" if ws and Path(ws).is_dir() else Path.home() / ".cache" / "screen-demo"
    p = base / "music"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ffmpeg synth presets — pure-ffmpeg ambient beds, no model, container-native.
# Each value is the -af filter chain applied to pink noise input.
SYNTH_PRESETS: dict[str, str] = {
    "calm":   "lowpass=f=900,tremolo=f=0.3:d=0.4,loudnorm=I=-14:TP=-1.5:LRA=11",
    "warm":   "lowpass=f=700,bass=g=4,tremolo=f=0.25:d=0.35,loudnorm=I=-14:TP=-1.5:LRA=11",
    "bright": "lowpass=f=1400,highpass=f=120,tremolo=f=0.5:d=0.5,loudnorm=I=-14:TP=-1.5:LRA=11",
}


def resolve(source: str | None, url: str | None, synth: str | None = None,
            synth_duration: float | None = None) -> Path | None:
    if source:
        p = Path(source).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"music file not found: {p}")
        return p
    if url:
        return _fetch_from_url(url)
    if synth:
        if synth_duration is None:
            raise ValueError("synth requires a target duration")
        return _synthesize_bed(synth, synth_duration)
    return None


def _synthesize_bed(preset: str, duration: float) -> Path:
    if preset not in SYNTH_PRESETS:
        raise ValueError(f"unknown synth preset {preset!r}; choices: {list(SYNTH_PRESETS)}")
    dst = _cache_dir() / f"synth-{preset}-{int(duration*10):05d}.wav"
    if dst.exists():
        return dst
    print(f"→ music: synthesizing {preset} bed, {duration:.1f}s (ffmpeg, no model)")
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"anoisesrc=color=pink:duration={duration + 5:.2f}:amplitude=0.35",
        "-af", SYNTH_PRESETS[preset],
        str(dst),
    ], check=True)
    return dst


_DIRECT_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}


def _fetch_from_url(url: str) -> Path:
    cache = _cache_dir()
    key = hashlib.sha1(url.encode()).hexdigest()[:12]
    ext = Path(url.split("?")[0]).suffix.lower()
    if ext in _DIRECT_AUDIO_EXTS:
        return _direct_download(url, cache / f"{key}{ext}")
    dst = cache / f"{key}.m4a"
    if dst.exists():
        return dst
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp not on PATH. install: pip install yt-dlp")
    print(f"→ music: fetching via yt-dlp: {url}")
    subprocess.run([
        "yt-dlp", "-x", "--audio-format", "m4a",
        "-o", str(dst.with_suffix(".%(ext)s")),
        "--quiet", "--no-warnings",
        url,
    ], check=True)
    if not dst.exists():
        cands = list(cache.glob(f"{key}.*"))
        if cands:
            cands[0].rename(dst)
    return dst


def _direct_download(url: str, dst: Path) -> Path:
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    print(f"→ music: direct download {url}")
    # Pixabay and similar CDNs gate generic clients — set a realistic UA.
    subprocess.run([
        "curl", "-sLf", "--max-time", "120",
        "-A", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "-o", str(dst), url,
    ], check=True)
    if dst.stat().st_size < 1024:
        raise RuntimeError(f"downloaded file is suspiciously small ({dst.stat().st_size}B): {dst}")
    return dst


GENERATION_NOTE = """\
ACE-Step local music generation is supported only on hosts with:
  • Apple Silicon (macOS, MPS backend)
  • Linux x86_64 with CUDA 12.8
  • Linux aarch64 with CUDA 13.0 (NVIDIA DGX Spark)
This container is CPU-only aarch64 Linux — install would force CUDA wheels and fail,
and a CPU fallback would take ~30+ minutes per 90s clip.

For now, pass --music-file <path> or --music-url <url>.
Recommended free-for-commercial sources:
  • YouTube Audio Library (https://studio.youtube.com/channel/UC/music) → paste track URL
  • Pixabay Music (https://pixabay.com/music/) → paste track page URL
  • Uppbeat (https://uppbeat.io/) → download .mp3 and pass with --music-file

If you have an Apple Silicon Mac, install ACE-Step there:
  git clone https://github.com/ace-step/ACE-Step-1.5
  cd ACE-Step-1.5 && pip install -r requirements.txt
  python -m acestep.gen --prompt "lo-fi minimal SaaS" --duration 90 --out bed.wav
"""


def generate_note(prompt: str) -> str:
    return f"prompt was: {prompt!r}\n\n{GENERATION_NOTE}"
