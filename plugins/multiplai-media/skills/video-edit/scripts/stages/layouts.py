"""Framing filters: how one segment's picture fills the output frame.

Every builder returns ffmpeg filter text and runs nothing, so tests can check
the exact strings. Two shapes come back:

  * a chain ("crop=…,scale=…") that slots between the segment's trim and its
    speed change, when one input makes one output;
  * a graph that reads [vin] and writes [vfit], when the picture is built from
    several copies of the frame (blur background, stacked panels, speaker
    switching).

Modes:
  fit "pad"   scale to fit, pad the rest (the default; screencasts)
  fit "blur"  scale to fit over a blurred, zoomed copy of the same frame
  fit "crop"  fill the frame, cropping around Segment.focus
  frame "A"   one declared panel, cropped to the output aspect around its
              centre or focus
  frame "stack"   panel A over panel B, each scaled to the output width
                  (sides trimmed so the pair fills the frame)
  frame "speaker" switch between panels as the transcript's speaker changes
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_BG = "#0a0a0a"
# A speaker turn shorter than this keeps the previous panel on screen, so a
# one-word interjection does not flash the other speaker.
MIN_SPEAKER_RUN_S = 1.5


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def pad_chain(W: int, H: int, bg: str = DEFAULT_BG) -> str:
    return (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={bg},setsar=1")


def crop_chain(W: int, H: int, fx: float = 0.5, fy: float = 0.5) -> str:
    """Cover the output: crop the largest W:H window, centred on (fx, fy) of the
    frame and clamped inside it, then scale to W×H."""
    cw = f"min(iw\\,ih*{W}/{H})"
    ch = f"min(ih\\,iw*{H}/{W})"
    x = f"max(0\\,min(iw-ow\\,iw*{fx}-ow/2))"
    y = f"max(0\\,min(ih-oh\\,ih*{fy}-oh/2))"
    return f"crop={cw}:{ch}:{x}:{y},scale={W}:{H},setsar=1"


def blur_graph(W: int, H: int) -> str:
    """The picture scaled to fit, over a blurred copy that fills the frame.

    The background is blurred at a tenth of the output size and scaled back
    up: the same look as a full-size blur at a fraction of the cost.
    """
    bw, bh = _even(W / 10), _even(H / 10)
    return (f"[vin]split=2[bgs][fgs];"
            f"[bgs]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"boxblur=4:2,scale={W}:{H},setsar=1[bgb];"
            f"[fgs]scale={W}:{H}:force_original_aspect_ratio=decrease,setsar=1[fgc];"
            f"[bgb][fgc]overlay=(W-w)/2:(H-h)/2[vfit]")


def panel_crop_rect(panel: Rect, W: int, H: int, fx: float = 0.5, fy: float = 0.5) -> Rect:
    """The largest W:H window inside `panel`, centred on (fx, fy) of the panel
    and clamped to its edges. Source pixels; even sizes for yuv420p."""
    if panel.w / panel.h > W / H:
        ch = panel.h - panel.h % 2
        cw = min(panel.w, _even(ch * W / H))
    else:
        cw = panel.w - panel.w % 2
        ch = min(panel.h, _even(cw * H / W))
    cx = panel.x + fx * panel.w - cw / 2
    cy = panel.y + fy * panel.h - ch / 2
    cx = int(round(min(max(cx, panel.x), panel.x + panel.w - cw)))
    cy = int(round(min(max(cy, panel.y), panel.y + panel.h - ch)))
    return Rect(cx, cy, cw, ch)


def panel_chain(panel: Rect, W: int, H: int, fx: float = 0.5, fy: float = 0.5) -> str:
    r = panel_crop_rect(panel, W, H, fx, fy)
    return f"crop={r.w}:{r.h}:{r.x}:{r.y},scale={W}:{H},setsar=1"


@dataclass
class StackGeometry:
    crop_a: Rect     # the part of each panel that is shown, source pixels
    crop_b: Rect
    width: int       # width of each scaled panel
    height_a: int
    height_b: int
    x: int           # left edge of the stack in the output
    y: int           # top edge of panel A in the output


def _stack_crop(panel: Rect, W: int, H: int, fx: float) -> Rect:
    """Trim a panel's sides so it is no wider than W:(H/2), keeping fx in view.
    A panel already that narrow is kept whole."""
    max_w = _even(panel.h * W / (H / 2))
    if panel.w <= max_w:
        return panel
    cx = panel.x + fx * panel.w - max_w / 2
    cx = int(round(min(max(cx, panel.x), panel.x + panel.w - max_w)))
    return Rect(cx, panel.y, max_w, panel.h)


def stack_geometry(a: Rect, b: Rect, W: int, H: int,
                   fx_a: float = 0.5, fx_b: float = 0.5) -> StackGeometry:
    """Panel A over panel B, each scaled to the output width.

    Panels wider than W:(H/2) lose a little of each side, so the two fill the
    output with no bars. Narrower panels stay whole: the pair is centred
    vertically on the background, and shrinks if together it is taller than
    the output.
    """
    ca, cb = _stack_crop(a, W, H, fx_a), _stack_crop(b, W, H, fx_b)
    if ca != a and cb != b:
        # Both trimmed to W:(H/2): each fills exactly half the frame.
        half = _even(H / 2)
        return StackGeometry(crop_a=ca, crop_b=cb, width=W, height_a=half,
                             height_b=H - half, x=0, y=0)
    ha, hb = ca.h * W / ca.w, cb.h * W / cb.w
    k = min(1.0, H / (ha + hb))
    width = _even(W * k)
    height_a, height_b = _even(ha * k), _even(hb * k)
    if height_a + height_b > H:          # rounding up both halves can overshoot by 2
        height_b -= height_a + height_b - H
    return StackGeometry(crop_a=ca, crop_b=cb, width=width, height_a=height_a, height_b=height_b,
                         x=(W - width) // 2, y=(H - height_a - height_b) // 2)


def stack_graph(a: Rect, b: Rect, W: int, H: int, bg: str = DEFAULT_BG,
                fx_a: float = 0.5, fx_b: float = 0.5) -> str:
    g = stack_geometry(a, b, W, H, fx_a, fx_b)
    ca, cb = g.crop_a, g.crop_b
    return (f"[vin]split=2[sa][sb];"
            f"[sa]crop={ca.w}:{ca.h}:{ca.x}:{ca.y},scale={g.width}:{g.height_a},setsar=1[pa];"
            f"[sb]crop={cb.w}:{cb.h}:{cb.x}:{cb.y},scale={g.width}:{g.height_b},setsar=1[pb];"
            f"[pa][pb]vstack=inputs=2,pad={W}:{H}:{g.x}:{g.y}:color={bg},setsar=1[vfit]")


def speaker_runs(words: list[dict], src_start: float, src_end: float,
                 speakers: dict[str, str], min_run: float = MIN_SPEAKER_RUN_S
                 ) -> list[tuple[float, float, str]]:
    """Which panel shows when, in segment-local time (0 = src_start).

    Each run of one speaker's words shows that speaker's panel, from its first
    word until the next run begins. A run shorter than `min_run` keeps the
    previous panel. The first run starts at 0 and the last ends at the segment
    end.
    """
    dur = src_end - src_start
    raw: list[list] = []   # [start, end, panel]
    for w in words:
        if w["end"] <= src_start or w["start"] >= src_end or "speaker" not in w:
            continue
        panel = speakers.get(w["speaker"])
        if panel is None:
            continue
        s = max(0.0, w["start"] - src_start)
        e = min(dur, w["end"] - src_start)
        if raw and raw[-1][2] == panel:
            raw[-1][1] = e
        else:
            raw.append([s, e, panel])
    if not raw:
        return []
    runs: list[list] = []
    for s, e, panel in raw:
        if runs and (e - s < min_run or runs[-1][2] == panel):
            runs[-1][1] = e
        else:
            runs.append([s, e, panel])
    # A run lasts until the next one starts; the edges reach the segment edges.
    out = []
    for i, (s, _e, panel) in enumerate(runs):
        start = 0.0 if i == 0 else s
        end = runs[i + 1][0] if i + 1 < len(runs) else dur
        out.append((round(start, 3), round(end, 3), panel))
    return out


def speaker_graph(runs: list[tuple[float, float, str]], panels: dict[str, Rect],
                  W: int, H: int, focus: tuple[float, float] = (0.5, 0.5)) -> str:
    """The first run's panel as the base, each other panel overlaid while its
    runs are on screen."""
    order: list[str] = []
    for _s, _e, p in runs:
        if p not in order:
            order.append(p)
    if len(order) == 1:
        return f"[vin]{panel_chain(panels[order[0]], W, H, *focus)}[vfit]"
    n = len(order)
    parts = [f"[vin]split={n}" + "".join(f"[s{i}]" for i in range(n))]
    for i, p in enumerate(order):
        parts.append(f"[s{i}]{panel_chain(panels[p], W, H, *focus)}[p{i}]")
    cur = "p0"
    for i, p in enumerate(order[1:], start=1):
        enable = "+".join(f"between(t,{s},{e})" for s, e, rp in runs if rp == p)
        out = "vfit" if i == n - 1 else f"o{i}"
        parts.append(f"[{cur}][p{i}]overlay=0:0:enable='{enable}'[{out}]")
        cur = out
    return ";".join(parts)
