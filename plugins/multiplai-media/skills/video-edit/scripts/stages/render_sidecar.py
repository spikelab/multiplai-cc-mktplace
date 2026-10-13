"""What a render was made from: `<out stem>.edl.json`, written beside the mp4.

`pipeline.py render` writes it once the mp4 is in place, so the job page can
draw a render's timeline (segments, caption lines, headline, overlays, sound
effects) and the session can tell which EDL a comment on that render is about.
It holds:

    video            the mp4's file name
    edl_file         the EDL file the render read, absolute
    edl              that EDL as loaded, with --music-* applied and every file
                     path made absolute (relative paths are read from the
                     working directory, as the render reads them)
    duration         the render's length in seconds (EDL.total_duration)
    segments         each segment's place in the output (stages/timeline.py),
                     with `xfade_in`, its crossfade with what comes before
    captions_events  the caption lines in output time, exactly as
                     stages/captions.py burns them: {start, end, text, words}

The file is written to a hidden partial name and renamed, so a reader never
sees half of one.
"""
from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from stages import brand as brand_mod, composite, timeline
from stages.edl import EDL

SUFFIX = ".edl.json"
FORMAT = 1


def path_for(out: Path) -> Path:
    """`clip-01.v2.mp4` → `clip-01.v2.edl.json`, in the same directory."""
    return out.with_name(out.stem + SUFFIX)


def _abs(p: str | None) -> str | None:
    return os.path.abspath(os.path.expanduser(p)) if p else p


def snapshot(edl: EDL) -> dict:
    """The EDL as a plain dict with every file path absolute."""
    d = copy.deepcopy(asdict(edl))
    for key in ("source", "transcript", "brand"):
        d[key] = _abs(d.get(key))
    if d.get("logo"):
        d["logo"]["path"] = _abs(d["logo"]["path"])
    if d.get("music"):
        d["music"]["file"] = _abs(d["music"].get("file"))
    for o in d.get("overlays", []):
        o["html"] = _abs(o["html"])
    for s in d.get("sfx", []):
        s["sound"] = _abs(s["sound"])
    return d


def segments(edl: EDL) -> list[dict]:
    """Each segment where the render puts it, and its crossfade in."""
    out = []
    for p in timeline.place_segments(edl):
        joins = p.index > 0 or edl.title is not None
        out.append({"index": p.index, "src_start": p.src_start, "src_end": p.src_end, "speed": p.speed,
                    "out_start": round(p.out_start, 3), "out_end": round(p.out_end, 3), "muted": p.muted,
                    "xfade_in": edl.xfade_before(p.index) if joins else 0.0})
    return out


def captions_events(edl: EDL) -> list[dict]:
    """The caption lines the render burns in; [] for an EDL without captions."""
    if edl.captions is None:
        return []
    from stages import captions as cap
    words = timeline.map_words(composite._load_words(edl), timeline.place_segments(edl))
    brand = brand_mod.load(edl.brand) if edl.brand else brand_mod.default()
    return cap.caption_lines(words, edl.captions, brand)


def build(edl: EDL, edl_file: Path, video_name: str) -> dict:
    """The sidecar for a render of `edl` (as loaded: call before render changes it)."""
    return {
        "format": FORMAT,
        "video": video_name,
        "rendered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "edl_file": str(Path(edl_file).resolve()),
        "edl": snapshot(edl),
        "duration": round(edl.total_duration(), 3),
        "segments": segments(edl),
        "captions_events": captions_events(edl),
    }


def write(out: Path, record: dict) -> Path:
    """Write the sidecar for the mp4 `out`: a hidden partial file, then a rename."""
    final = path_for(out)
    part = out.with_name(f".{out.stem}.edl.partial.json")
    try:
        part.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(part, final)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return final
