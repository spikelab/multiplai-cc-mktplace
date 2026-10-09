"""Contact sheets: a reference video as a few 4x4 grids of frames.

Used to derive an overlay style from someone else's video: the session reads
the sheets (frames at every scene change plus one every N seconds, each
labelled with its time) and writes a style file from what it sees.

A URL is fetched with yt-dlp first (at most 720p, and only the first
`max_seconds`, so a long video does not download in full).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

COLUMNS = ROWS = 4
THUMB_W = 480
MIN_GAP_S = 1.0          # frames closer than this to the previous one are dropped


def is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def fetch_argv(url: str, out_dir: Path, max_seconds: int | None) -> list[str]:
    return ["yt-dlp", "--no-playlist",
            "-f", "bv*[height<=720]+ba/b[height<=720]/b",
            "-S", "vcodec:h264",            # H.264 decodes everywhere; AV1 often does not
            "--merge-output-format", "mp4",
            *(["--download-sections", f"*0-{max_seconds}"] if max_seconds else []),
            "-o", str(out_dir / "reference.%(ext)s"), url]


def fetch(url: str, out_dir: Path, max_seconds: int | None, run=subprocess.run) -> Path:
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp is not on PATH; install it with `pip install yt-dlp`, or pass a local file.")
    out_dir.mkdir(parents=True, exist_ok=True)
    run(fetch_argv(url, out_dir, max_seconds), check=True)
    found = sorted(out_dir.glob("reference.*"))
    if not found:
        raise RuntimeError(f"yt-dlp finished but wrote no reference.* file in {out_dir}")
    return found[0]


def sample_times(scene_times: list[float], duration: float, every: float) -> list[float]:
    """Scene changes plus one frame every `every` seconds, in order, at least
    MIN_GAP_S apart (a scene change wins over a nearby interval frame)."""
    interval = [round(i * every, 3) for i in range(int(duration // every) + 1)] if every > 0 else []
    candidates = sorted({(round(t, 3), 0) for t in scene_times} | {(t, 1) for t in interval})
    out: list[float] = []
    for t, _ in candidates:
        t = min(t + 0.05, duration - 0.05)       # a frame just after the cut, never past the end
        if t < 0:
            continue
        if out and t - out[-1] < MIN_GAP_S:
            continue
        out.append(round(t, 3))
    return out


def _label(t: float) -> str:
    m, s = divmod(t, 60)
    return f"{int(m)}\\:{s:04.1f}"     # drawtext needs the colon escaped


def frame_argv(src: Path, t: float, out_png: Path, font: str) -> list[str]:
    draw = (f"drawtext=fontfile='{font}':text='{_label(t)}':x=8:y=h-th-8:fontsize=26:"
            "fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=6")
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", str(src),
            "-frames:v", "1", "-vf", f"scale={THUMB_W}:-2,{draw}", str(out_png)]


def tile_argv(frames_dir: Path, out_dir: Path) -> list[str]:
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-framerate", "1",
            "-i", str(frames_dir / "t_%04d.png"),
            "-vf", f"tile={COLUMNS}x{ROWS}:padding=6:margin=6:color=white",
            "-fps_mode", "passthrough", str(out_dir / "sheet_%02d.png")]


def proxy_argv(src: Path, out: Path, max_seconds: int | None) -> list[str]:
    """A small silent H.264 copy for scene detection: scenedetect's default
    backend refuses files with audio and cannot decode every codec (AV1 fails
    without hardware support), so it never sees the original."""
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
            *(["-t", str(max_seconds)] if max_seconds else []),
            "-map", "0:v:0", "-an", "-vf", "scale=640:-2", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "28", str(out)]


def probe_duration(src: Path) -> float:
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "csv=p=0", str(src)], text=True)
    return float(out.strip())


def make(source: str, out_dir: Path, every: float = 5.0, max_seconds: int | None = 600) -> list[Path]:
    from stages.composite import _find_font
    from stages.prep import _scenedetect

    out_dir.mkdir(parents=True, exist_ok=True)
    src = fetch(source, out_dir, max_seconds) if is_url(source) else Path(source)
    if not src.exists():
        raise FileNotFoundError(f"video not found: {src}")
    duration = probe_duration(src)
    if max_seconds:
        duration = min(duration, max_seconds)
    work = out_dir / "work"
    work.mkdir(exist_ok=True)
    proxy = work / "proxy.mp4"
    subprocess.run(proxy_argv(src, proxy, max_seconds), check=True)
    (work / "scenes.csv").unlink(missing_ok=True)
    try:
        scenes = [c.t for c in _scenedetect(proxy, work)]
    except FileNotFoundError:
        raise RuntimeError(f"scenedetect read no frames from {proxy}; is the video readable by ffmpeg?") from None
    times = sample_times(scenes, duration, every)
    frames = work / "frames"
    if frames.exists():
        shutil.rmtree(frames)
    frames.mkdir()
    font = _find_font(False)
    for i, t in enumerate(times):
        subprocess.run(frame_argv(src, t, frames / f"t_{i + 1:04d}.png", font), check=True)
    for old in out_dir.glob("sheet_*.png"):
        old.unlink()
    subprocess.run(tile_argv(frames, out_dir), check=True)
    sheets = sorted(out_dir.glob("sheet_*.png"))
    per = COLUMNS * ROWS
    (out_dir / "sheets.json").write_text(json.dumps({
        "source": source, "every_s": every, "scene_changes": len(scenes),
        "sheets": [{"file": s.name, "times": times[i * per:(i + 1) * per]} for i, s in enumerate(sheets)],
    }, indent=2) + "\n")
    return sheets
