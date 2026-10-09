"""Reels spec check: what an MP4 must be for Instagram, Facebook and TikTok.

SOURCES AND CAVEATS. These values come from secondary blog posts gathered on
2026-10-08; no platform's own documentation was fetched, and the platforms
change them without notice. Treat them as a conservative common floor, not as
any platform's published limit:

  - 1080×1920 (9:16), H.264 video, AAC audio, about 30 fps;
  - Instagram: at most 25 Mbps and 300 MB per upload;
  - keep text out of the bands the app's own buttons and captions cover:
    the top ~200 px, the right ~120 px, the bottom ~300–480 px (at 1080×1920);
  - put the hook in the first 3 s; reels over 3 min are not recommended to
    new audiences.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

WIDTH, HEIGHT = 1080, 1920
VIDEO_CODEC = "h264"
AUDIO_CODEC = "aac"
AUDIO_RATE = 48000
FPS = 30.0
FPS_TOLERANCE = 0.5          # 29.97 passes
MAX_BITRATE = 25_000_000     # bits per second, whole file
MAX_BYTES = 300_000_000
DURATION_WARN_S = 90.0
DURATION_FAIL_S = 180.0
HOOK_S = 3.0
# Unsafe bands at 1080×1920; scaled with the output height.
UNSAFE_TOP = 200
UNSAFE_RIGHT = 120
UNSAFE_BOTTOM_HARD = 300
UNSAFE_BOTTOM_SOFT = 480

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Result:
    name: str
    status: str
    detail: str


def probe(path: str) -> dict:
    out = subprocess.check_output(["ffprobe", "-v", "error", "-print_format", "json",
                                   "-show_format", "-show_streams", path], text=True)
    return json.loads(out)


def _fps(rate: str) -> float:
    n, _, d = rate.partition("/")
    return float(n) / float(d or 1) if float(d or 1) else 0.0


def evaluate(info: dict) -> list[Result]:
    """Pass/warn/fail for each reels requirement, from ffprobe JSON."""
    streams = info.get("streams", [])
    fmt = info.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    r: list[Result] = []
    if v is None:
        return [Result("video stream", FAIL, "no video stream")]
    w, h = v.get("width"), v.get("height")
    r.append(Result("resolution", PASS if (w, h) == (WIDTH, HEIGHT) else FAIL, f"{w}x{h} (need {WIDTH}x{HEIGHT})"))
    r.append(Result("video codec", PASS if v.get("codec_name") == VIDEO_CODEC else FAIL,
                    f"{v.get('codec_name')} (need {VIDEO_CODEC})"))
    fps = _fps(v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1")
    r.append(Result("frame rate", PASS if abs(fps - FPS) <= FPS_TOLERANCE else FAIL, f"{fps:.2f} fps (need ~{FPS:g})"))
    if a is None:
        r.append(Result("audio", FAIL, "no audio stream"))
    else:
        r.append(Result("audio codec", PASS if a.get("codec_name") == AUDIO_CODEC else FAIL,
                        f"{a.get('codec_name')} (need {AUDIO_CODEC})"))
        rate = int(a.get("sample_rate") or 0)
        r.append(Result("audio sample rate", PASS if rate == AUDIO_RATE else FAIL, f"{rate} Hz (need {AUDIO_RATE})"))
    bitrate = int(fmt.get("bit_rate") or 0)
    r.append(Result("bitrate", PASS if 0 < bitrate <= MAX_BITRATE else FAIL,
                    f"{bitrate / 1e6:.2f} Mbps (max {MAX_BITRATE / 1e6:g})"))
    size = int(fmt.get("size") or 0)
    r.append(Result("file size", PASS if 0 < size < MAX_BYTES else FAIL, f"{size / 1e6:.1f} MB (max {MAX_BYTES / 1e6:g})"))
    dur = float(fmt.get("duration") or 0)
    status = FAIL if dur > DURATION_FAIL_S or dur <= 0 else WARN if dur > DURATION_WARN_S else PASS
    r.append(Result("duration", status, f"{dur:.1f}s (warn > {DURATION_WARN_S:g}s, fail > {DURATION_FAIL_S:g}s)"))
    return r


def lint_text_boxes(edl) -> list[Result]:
    """Caption and headline boxes against the unsafe bands, from the same
    sizes the ASS writer uses (captions.layout_metrics). A caption is measured
    as two lines, its worst case."""
    from stages.captions import layout_metrics
    W, H = edl.output.width, edl.output.height
    k = H / HEIGHT
    m = layout_metrics(W, H, edl.captions)
    out: list[Result] = []
    if m["margin"] < UNSAFE_RIGHT * W / WIDTH:
        out.append(Result("text right margin", FAIL, f"{m['margin']}px < {UNSAFE_RIGHT * W / WIDTH:.0f}px"))
    if edl.captions is not None:
        half = m["caption_size"] * 1.25
        top, bottom = m["caption_y"] - half, m["caption_y"] + half
        if top < UNSAFE_TOP * k:
            out.append(Result("caption box", FAIL, f"top {top:.0f}px is inside the top {UNSAFE_TOP * k:.0f}px"))
        elif bottom > H - UNSAFE_BOTTOM_HARD * k:
            out.append(Result("caption box", FAIL,
                              f"bottom {bottom:.0f}px is inside the bottom {UNSAFE_BOTTOM_HARD * k:.0f}px"))
        elif bottom > H - UNSAFE_BOTTOM_SOFT * k:
            out.append(Result("caption box", WARN,
                              f"bottom {bottom:.0f}px is inside the bottom {UNSAFE_BOTTOM_SOFT * k:.0f}px"))
        else:
            out.append(Result("caption box", PASS, f"{top:.0f}–{bottom:.0f}px"))
    if edl.headline is not None:
        pad = m["headline_size"] * 0.25
        top = m["headline_y"] - pad
        bottom = m["headline_y"] + m["headline_size"] * 2.5 + pad
        if top < UNSAFE_TOP * k:
            out.append(Result("headline box", FAIL, f"top {top:.0f}px is inside the top {UNSAFE_TOP * k:.0f}px"))
        elif bottom > H - UNSAFE_BOTTOM_SOFT * k:
            out.append(Result("headline box", FAIL, f"bottom {bottom:.0f}px is inside the bottom band"))
        else:
            out.append(Result("headline box", PASS, f"{top:.0f}–{bottom:.0f}px"))
        if edl.headline.start > HOOK_S:
            out.append(Result("headline timing", WARN,
                              f"starts at {edl.headline.start:g}s; the hook belongs in the first {HOOK_S:g}s"))
    return out
