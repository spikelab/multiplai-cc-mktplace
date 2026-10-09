"""Shot list for a broadcast feed or any recording cut between cameras.

Prep's scene detection (scenes.csv, PySceneDetect) splits the source into
shots. `shots` lists those inside a time range and draws a sheet with three
frames of each shot: just after it starts, its middle, and just before it
ends. The session reads the sheet to choose per shot how to frame it (crop
on the speaker, blur a slide), to see whether the speaker moves within a
shot (focus keyframes), and to spot a cut the detector missed: a dissolve
or a slide sliding in shows up as start and end frames that differ.
"""
from __future__ import annotations

import csv
import subprocess
import tempfile
from pathlib import Path

EDGE_S = 0.2             # how far inside a shot its first and last frames are taken
TILE_W, TILE_H = 384, 216
LABEL_H = 28


def read_scenes(path: Path) -> list[tuple[float, float]]:
    """(start, end) source seconds of each scene in a PySceneDetect list-scenes CSV."""
    rows = list(csv.reader(path.read_text().splitlines()))
    header = next((i for i, r in enumerate(rows) if "Start Time (seconds)" in r), None)
    if header is None:
        raise ValueError(f"{path} is not a PySceneDetect scene list (no 'Start Time (seconds)' column).")
    cols = rows[header]
    si, ei = cols.index("Start Time (seconds)"), cols.index("End Time (seconds)")
    return [(float(r[si]), float(r[ei])) for r in rows[header + 1:] if len(r) > max(si, ei)]


def shots_in(scenes: list[tuple[float, float]], start: float, end: float) -> list[tuple[float, float]]:
    """The scenes overlapping [start, end], clipped to it."""
    return [(max(s, start), min(e, end)) for s, e in scenes if min(e, end) > max(s, start)]


def frame_times(shot: tuple[float, float]) -> list[float]:
    s, e = shot
    edge = min(EDGE_S, (e - s) / 4)
    return [round(s + edge, 3), round((s + e) / 2, 3), round(e - edge, 3)]


def make(proxy: Path, shots: list[tuple[float, float]], out: Path, font: str) -> None:
    """Write the sheet: one row per shot, its three frames left to right,
    each labelled with the shot number and the frame's source time."""
    work = Path(tempfile.mkdtemp(prefix="video-edit-shots-"))
    n = 0
    for k, shot in enumerate(shots, start=1):
        for t in frame_times(shot):
            label = f"shot {k}  {t:.2f}s"
            vf = (f"scale={TILE_W}:{TILE_H}:force_original_aspect_ratio=decrease,"
                  f"pad={TILE_W}:{TILE_H + LABEL_H}:(ow-iw)/2:0:color=#111111,"
                  f"drawtext=fontfile={font}:text='{label}':x=6:y={TILE_H + 6}:fontsize=16:"
                  "fontcolor=white")
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}",
                            "-i", str(proxy), "-frames:v", "1", "-vf", vf,
                            str(work / f"f{n:04d}.png")], check=True)
            n += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-start_number", "0",
                    "-i", str(work / "f%04d.png"), "-vf", f"tile=3x{len(shots)}:padding=4:color=#000000",
                    "-frames:v", "1", str(out)], check=True)
