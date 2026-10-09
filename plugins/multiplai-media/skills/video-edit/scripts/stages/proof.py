"""Proof sheet: a rendered reel moment by moment, on one PNG.

It tiles frames of the render: one every SAMPLE_EVERY_S, plus one
AFTER_CHANGE_S after every point where the picture changes (each segment
join, each panel switch of a `frame: "speaker"` segment). Under each tile it
prints the output time, the source time and segment, the caption on screen
then, and the speaker label of the words being said, when the transcript
has labels. It is what the session looks at before reporting a reel
(references/reels.md step 5), so every label comes from the same
arithmetic the render used: timeline.place_segments for the times, the
caption grouping of captions.build_ass for the text.
"""
from __future__ import annotations

import subprocess
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path

from stages import captions as cap, layouts, timeline
from stages.edl import EDL

SAMPLE_EVERY_S = 3.0
AFTER_CHANGE_S = 0.5
MIN_APART_S = 0.25       # two sample times closer than this show the same frame
TILE_W, TILE_H = 360, 640
LABEL_H = 112
LABEL_CHARS = 30         # characters per label line at LABEL_SIZE on a TILE_W tile
LABEL_SIZE = 16
COLUMNS = 6


@dataclass
class Tile:
    t: float                 # output seconds
    src: float | None        # source seconds, None on the title card
    segment: int | None
    framing: str             # the segment's frame, or its fit
    caption: str             # the caption on screen, "" when none
    speaker: str             # speaker label(s) of the words being said, "" when unlabelled


def change_times(edl: EDL, words: list[dict] | None) -> list[float]:
    """Output times where the picture changes: every segment join, and
    every panel switch inside a `frame: "speaker"` segment."""
    out = []
    for p in timeline.place_segments(edl):
        seg = edl.segments[p.index]
        if p.index > 0 or edl.title:
            out.append(p.out_start)
        if seg.frame == "speaker" and words and edl.layout:
            runs = layouts.speaker_runs(words, seg.src_start, seg.src_end, edl.layout.speakers)
            out.extend(p.out_start + s / seg.speed for s, _e, _panel in runs[1:])
    return sorted(out)


def sample_times(edl: EDL, words: list[dict] | None = None,
                 duration: float | None = None) -> list[float]:
    """Every SAMPLE_EVERY_S from 0, plus AFTER_CHANGE_S after each change;
    a time within MIN_APART_S of an earlier one is dropped."""
    total = edl.total_duration() if duration is None else duration
    times = []
    t = 0.0
    while t < total:
        times.append(round(t, 3))
        t += SAMPLE_EVERY_S
    times += [round(c + AFTER_CHANGE_S, 3) for c in change_times(edl, words) if c + AFTER_CHANGE_S < total]
    out: list[float] = []
    for x in sorted(times):
        if not out or x - out[-1] >= MIN_APART_S:
            out.append(x)
    return out


def _caption_events(edl: EDL, out_words: list[dict]) -> list[tuple[float, float, str]]:
    if edl.captions is None:
        return []
    groups = cap.group_words(out_words, edl.captions.words_per_line, edl.captions.max_chars)
    return cap.forward_only(cap.caption_events(groups, False, "#FFFFFF", "#FFFFFF"))


def tiles(edl: EDL, words: list[dict] | None, times: list[float]) -> list[Tile]:
    placed = timeline.place_segments(edl)
    out_words = timeline.map_words(words or [], placed)
    events = _caption_events(edl, out_words)
    result = []
    for t in times:
        # Inside a crossfade the incoming segment is the one fading in; the
        # label names the later one, which is what the eye follows.
        p = next((q for q in reversed(placed) if q.out_start <= t < q.out_end), None)
        if p is None:
            result.append(Tile(t, None, None, "title", "", ""))
            continue
        seg = edl.segments[p.index]
        src = p.src_start + (t - p.out_start) * p.speed
        framing = seg.frame or f"fit {seg.fit or edl.output.fit}"
        if isinstance(seg.focus, list):
            framing += " (moving focus)"
        caption = next((text for s, e, text in events if s <= t < e), "")
        near = [w for w in out_words if w["start"] - 0.25 <= t <= w["end"] + 0.25 and w.get("speaker")]
        speaker = ",".join(sorted({w["speaker"] for w in near}))
        result.append(Tile(t, round(src, 2), p.index, framing, caption, speaker))
    return result


def label_lines(tile: Tile) -> list[str]:
    head = f"{tile.t:5.1f}s"
    if tile.src is not None:
        head += f"  src {tile.src:.1f}  seg {tile.segment}"
    lines = [head, tile.framing + (f"  [{tile.speaker}]" if tile.speaker else "")]
    lines.append(f'"{tile.caption}"' if tile.caption else "(no caption)")
    return [part for line in lines for part in textwrap.wrap(line, LABEL_CHARS) or [""]]


def label_text(tile: Tile) -> str:
    """The label as drawtext reads it. ffmpeg 6.1's drawtext drops one
    character from the end of a line for every extra byte a UTF-8 character
    in it takes ("città" loses its last letter), so each line is padded with
    that many spaces, which do not show."""
    return "\n".join(line + " " * (len(line.encode()) - len(line)) for line in label_lines(tile))


def _tile_cmd(mp4: str, tile: Tile, label_file: Path, font: str, dst: Path) -> list[str]:
    vf = (f"scale={TILE_W}:{TILE_H}:force_original_aspect_ratio=decrease,"
          f"pad={TILE_W}:{TILE_H + LABEL_H}:(ow-iw)/2:0:color=#111111,"
          f"drawtext=fontfile={font}:textfile={label_file}:x=6:y={TILE_H + 6}:fontsize={LABEL_SIZE}:"
          "fontcolor=white:line_spacing=6:expansion=none")
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{tile.t:.3f}",
            "-i", mp4, "-frames:v", "1", "-vf", vf, str(dst)]


def make(mp4: str, edl: EDL, words: list[dict] | None, out: Path, font: str,
         duration: float | None = None) -> list[Tile]:
    """Write the proof sheet to `out`; returns the tiles it shows."""
    shown = tiles(edl, words, sample_times(edl, words, duration))
    work = Path(tempfile.mkdtemp(prefix="video-edit-proof-"))
    for i, tile in enumerate(shown):
        label = work / f"label{i:03d}.txt"
        label.write_text(label_text(tile), encoding="utf-8")
        subprocess.run(_tile_cmd(mp4, tile, label, font, work / f"tile{i:03d}.png"), check=True)
    rows = -(-len(shown) // COLUMNS)
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-start_number", "0",
                    "-i", str(work / "tile%03d.png"),
                    "-vf", f"tile={COLUMNS}x{rows}:padding=4:color=#000000", "-frames:v", "1",
                    str(out)], check=True)
    return shown
