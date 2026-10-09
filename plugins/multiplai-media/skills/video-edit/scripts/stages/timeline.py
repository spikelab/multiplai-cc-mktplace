"""Map source time to output time through an EDL.

The render lays segments end to end after the optional title card, and each
join is a crossfade that overlaps the two neighbours by the transition's
duration (composite.build_filter_complex). place_segments() repeats that
arithmetic in the same order, so a caption timed here lands on the frame the
render produces. A source time inside a cut, or inside a muted segment
(speed > 4, or `mute`), has no output time: its audio is not heard.
"""
from __future__ import annotations

from dataclasses import dataclass

from stages.edl import EDL

MUTE_SPEED = 4.0


@dataclass
class Placed:
    index: int
    src_start: float
    src_end: float
    speed: float
    out_start: float
    out_end: float
    muted: bool


def _xfade(edl: EDL, segment_index: int) -> float:
    return edl.xfade_before(segment_index)


def place_segments(edl: EDL) -> list[Placed]:
    placed: list[Placed] = []
    cur_off = edl.title.duration if edl.title else 0.0
    for i, seg in enumerate(edl.segments):
        if i == 0 and not edl.title:
            start = 0.0
        else:
            start = cur_off - _xfade(edl, i)
        cur_off = start + seg.duration
        placed.append(Placed(
            index=i, src_start=seg.src_start, src_end=seg.src_end, speed=seg.speed,
            out_start=start, out_end=cur_off, muted=seg.mute or seg.speed > MUTE_SPEED,
        ))
    return placed


def to_output(t: float, placed: list[Placed]) -> float | None:
    """Output time of source time t, or None when t is cut or muted."""
    for p in placed:
        if p.src_start <= t < p.src_end:
            if p.muted:
                return None
            return p.out_start + (t - p.src_start) / p.speed
    return None


def map_span(start: float, end: float, placed: list[Placed]) -> tuple[float, float] | None:
    """Output span of a word [start, end].

    A word that straddles a cut belongs to the segment holding most of it and
    is clipped to that segment's edge. None when no kept, unmuted segment
    holds any of it.
    """
    best: Placed | None = None
    best_overlap = 0.0
    for p in placed:
        overlap = min(end, p.src_end) - max(start, p.src_start)
        if overlap > best_overlap:
            best, best_overlap = p, overlap
    if best is None or best.muted:
        return None
    s = max(start, best.src_start)
    e = min(end, best.src_end)
    return (best.out_start + (s - best.src_start) / best.speed,
            best.out_start + (e - best.src_start) / best.speed)


def map_words(words: list[dict], placed: list[Placed]) -> list[dict]:
    """Words in output time; words with no output time are dropped."""
    out = []
    for w in words:
        span = map_span(w["start"], w["end"], placed)
        if span is not None:
            out.append({**w, "start": round(span[0], 3), "end": round(span[1], 3),
                        "src_start": w["start"]})
    out.sort(key=lambda w: w["start"])
    return out
