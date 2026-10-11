"""The overlay page for a proof shot (`pipeline.py proof-card`).

Turns a capture directory (shot.png + shot.json, stages/capture.py) into one
self-contained HTML page for an `alpha` overlay: a rounded card with a drop
shadow, a bar on top naming the page's domain and capture date, and the
screenshot in a window below it. The screenshot is inlined as a
data:image/png;base64 URI and its pixels are never changed: the highlight
is a ring drawn on top of the image, around the recorded box, in the style's
accent colour.

Motions (all CSS animations of transform and opacity, so the page passes
stages/overlay_lint.py and renders frame-exact when seeked):
  scroll  the window starts at the top of the page and moves down until the
          highlight box is centred, over the first half of the overlay;
  zoom    the window starts with the highlight box in view and scales in
          towards it, over the first half of the overlay;
  none    the window sits with the highlight box in view (or at the top).
The ring arrives when the move ends and holds until the card leaves.

Not stages/proof.py, which tiles a rendered reel's frames for checking it.
"""
from __future__ import annotations

import base64
import html
import json
import re
from dataclasses import dataclass
from pathlib import Path

from stages.capture import domain, png_size

STYLES_DIR = Path(__file__).resolve().parents[2] / "references" / "styles"
MOTIONS = ("scroll", "zoom", "none")
CARD_SHARE = 0.84            # the card fits in this share of the region's width and height
CARD_MAX_W_SHARE = 0.72      # and is never wider than this share of a landscape region
WINDOW_ASPECT = 10 / 16      # the window's height to width, the default capture's 1280x800
BAR_SHARE = 0.045            # the top bar's height, as a share of the card width
ZOOM_FILL = 0.6              # zoom until the highlight box spans this share of the window width
ZOOM_MAX = 2.5               # never zoom further than this over the starting scale
START_S = 0.2                # the card starts arriving this far in (motion.md: never at 0)
RING_S = 0.4                 # the ring's swipe in
MIN_READ_S = 2.0             # the highlight should stay this long after the move ends
RING_PX = 4                  # the ring's width on screen once the move ends, in pixels
RING_PAD_PX = 8              # space between the box and the ring on screen
DEFAULTS = {                 # used when the style file leaves a value out
    "accent": "#3DDC97", "surface": "#111A2E", "text": "#FFFFFF", "muted": "#9AA7BD",
    "font": "Inter, -apple-system, 'Helvetica Neue', Arial, sans-serif",
    "radius_px": 18.0, "easing": "cubic-bezier(.2,.8,.2,1)", "enter_s": 0.5, "exit_s": 0.3,
}


class ProofCardError(ValueError):
    pass


@dataclass
class Style:
    accent: str
    surface: str
    text: str
    muted: str
    font: str
    radius_px: float
    easing: str
    enter_s: float
    exit_s: float


def _frontmatter(text: str) -> dict[str, dict[str, str]]:
    """The two-level `section: {key: value}` mappings of a style file's
    frontmatter, values as strings. Enough for styles/README.md's format;
    flow mappings (`weights: {…}`) and lists are skipped."""
    m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    out: dict[str, dict[str, str]] = {}
    section = None
    for line in (m.group(1).splitlines() if m else []):
        kv = re.match(r"^(\s*)([\w-]+)\s*:\s*(.*)$", line)
        if not kv:
            continue
        indent, key, value = kv.groups()
        if value.startswith(("'", '"')):
            q = value[0]
            end = value.find(q, 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if not indent:
            section = key
            out.setdefault(key, {})
            if value:
                out[key][""] = value
        elif section is not None and value and not value.startswith(("{", "[")):
            out[section][key] = value
    return out


def load_style(name: str) -> Style:
    """A shipped style by name (references/styles/<name>.md), or a style file's path."""
    path = Path(name).expanduser()
    if not (path.suffix == ".md" and path.is_file()):
        path = STYLES_DIR / f"{name}.md"
    if not path.is_file():
        shipped = sorted(p.stem for p in STYLES_DIR.glob("*.md") if p.stem != "README")
        raise ProofCardError(f"no style {name!r}: give one of {', '.join(shipped)} or the path "
                             "to a style file (.md)")
    fm = _frontmatter(path.read_text(encoding="utf-8"))
    pal, fonts = fm.get("palette", {}), fm.get("fonts", {})
    shape, motion = fm.get("shape", {}), fm.get("motion", {})

    def num(section: dict, key: str) -> float:
        try:
            return float(section[key])
        except (KeyError, ValueError):
            return float(DEFAULTS[key])

    return Style(accent=pal.get("accent", DEFAULTS["accent"]),
                 surface=pal.get("surface", DEFAULTS["surface"]),
                 text=pal.get("text", DEFAULTS["text"]), muted=pal.get("muted", DEFAULTS["muted"]),
                 font=fonts.get("body", DEFAULTS["font"]),
                 radius_px=num(shape, "radius_px"), easing=motion.get("easing", DEFAULTS["easing"]),
                 enter_s=num(motion, "enter_s"), exit_s=num(motion, "exit_s"))


def _rgba(hex_colour: str, alpha: float) -> str:
    h = hex_colour.lstrip("#")
    if len(h) in (3, 4):
        h = "".join(c * 2 for c in h[:3])
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


@dataclass
class Layout:
    card_x: float
    card_y: float
    card_w: float
    bar_h: float
    win_h: float
    start: tuple[float, float, float]     # translate x, translate y, scale of the page image
    end: tuple[float, float, float]


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _centre_on(box: dict, s: float, img_w: int, img_h: int, win_w: float,
               win_h: float) -> tuple[float, float, float]:
    """The translate that puts the box's centre in the window's centre at scale s,
    kept so the image still covers the window where it is large enough to."""
    cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
    tx = _clamp(win_w / 2 - cx * s, min(0.0, win_w - img_w * s), 0.0)
    ty = _clamp(win_h / 2 - cy * s, min(0.0, win_h - img_h * s), 0.0)
    return tx, ty, s


def layout(img_w: int, img_h: int, box: dict | None, motion: str, width: int,
           height: int) -> Layout:
    card_w = min(width * CARD_SHARE, height * CARD_SHARE / (WINDOW_ASPECT + BAR_SHARE))
    if width > height:
        card_w = min(card_w, width * CARD_MAX_W_SHARE)
    bar_h = round(card_w * BAR_SHARE)
    s0 = card_w / img_w
    win_h = min(card_w * WINDOW_ASPECT, img_h * s0)
    card_h = bar_h + win_h
    top = (0.0, 0.0, s0)
    if box is None:
        start = end = top
    else:
        in_view = _centre_on(box, s0, img_w, img_h, card_w, win_h)
        if motion == "scroll":
            start, end = top, in_view
        elif motion == "zoom":
            z = _clamp(ZOOM_FILL * card_w / (box["w"] * s0), 1.0, ZOOM_MAX)
            start, end = in_view, _centre_on(box, s0 * z, img_w, img_h, card_w, win_h)
        else:
            start = end = in_view
    return Layout((width - card_w) / 2, (height - card_h) / 2, card_w, bar_h, win_h, start, end)


def timing(duration: float, style: Style) -> tuple[float, float, float]:
    """(move start, move end, exit start) in seconds: the card arrives, the
    move fills the rest of the overlay's first half, the card leaves at the end."""
    move_from = START_S + style.enter_s
    move_to = max(move_from + 0.3, duration / 2)
    return move_from, move_to, max(move_to, duration - style.exit_s)


def _transform(t: tuple[float, float, float]) -> str:
    return f"translate({t[0]:.2f}px,{t[1]:.2f}px) scale({t[2]:.5f})"


def build(shot_dir: Path, motion: str, style: Style, width: int = 1920, height: int = 1080,
          duration: float = 6.0) -> tuple[str, list[str]]:
    """The page, and warnings about timing the session should pass on."""
    if motion not in MOTIONS:
        raise ProofCardError(f"motion {motion!r}: use one of {', '.join(MOTIONS)}")
    shot_dir = Path(shot_dir)
    try:
        png = (shot_dir / "shot.png").read_bytes()
        meta = json.loads((shot_dir / "shot.json").read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ProofCardError(f"{e.filename} is missing; make the shot with `pipeline.py capture` "
                             "first") from None
    img_w, img_h = png_size(png)
    box = meta.get("highlight")
    if box is None and motion != "none":
        raise ProofCardError(f"--motion {motion} moves to the highlighted line, and {shot_dir}/shot.json "
                             "has none. Capture again with --highlight (or --box), or use --motion none.")
    lay = layout(img_w, img_h, box, motion, width, height)
    move_from, move_to, leave = timing(duration, style)
    warnings = []
    held = leave - move_to
    if box is not None and held < MIN_READ_S:
        warnings.append(f"the highlight holds for {held:.1f} s after the move ends; {MIN_READ_S:.0f} s or "
                        f"more reads well, so make the overlay at least "
                        f"{2 * (MIN_READ_S + style.exit_s):.1f} s long")
    ring_at = move_to if motion != "none" else move_from
    s_end = lay.end[2]
    ring_w, pad = RING_PX / s_end, RING_PAD_PX / s_end
    radius = style.radius_px * width / 1080
    site = html.escape(domain(meta.get("url", "")) or "local file")
    taken = html.escape(str(meta.get("captured_at", ""))[:10])
    data = base64.b64encode(png).decode("ascii")
    ring = ""
    if box is not None:
        ring = (f'<div class="hl" style="left:{box["x"] - pad - ring_w:.2f}px;top:{box["y"] - pad - ring_w:.2f}px;'
                f'width:{box["w"] + 2 * (pad + ring_w):.2f}px;height:{box["h"] + 2 * (pad + ring_w):.2f}px;'
                f'border-width:{ring_w:.2f}px;border-radius:{ring_w * 2:.2f}px"></div>')
    move = (f"animation:move {move_to - move_from:.3f}s ease-in-out {move_from:.3f}s both;"
            if lay.start != lay.end else "")
    font_px = round(lay.bar_h * 0.42)
    page = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;width:{width}px;height:{height}px;overflow:hidden}}
body{{font-family:{style.font}}}
.card{{position:absolute;left:{lay.card_x:.2f}px;top:{lay.card_y:.2f}px;width:{lay.card_w:.2f}px;
  border-radius:{radius:.1f}px;overflow:hidden;background:{style.surface};
  box-shadow:0 {radius * 1.2:.1f}px {radius * 3:.1f}px rgba(0,0,0,.45),0 2px 6px rgba(0,0,0,.3);
  animation:cardin {style.enter_s:.3f}s {style.easing} {START_S:.3f}s both,
            cardout {style.exit_s:.3f}s cubic-bezier(.6,0,.8,.2) {leave:.3f}s forwards}}
.bar{{height:{lay.bar_h}px;display:flex;align-items:center;gap:{lay.bar_h * 0.22:.1f}px;
  padding:0 {lay.bar_h * 0.45:.1f}px;box-sizing:border-box;font-size:{font_px}px;font-weight:500}}
.dot{{width:{lay.bar_h * 0.22:.1f}px;height:{lay.bar_h * 0.22:.1f}px;border-radius:50%;background:{style.muted};opacity:.6}}
.site{{color:{style.text};margin-left:{lay.bar_h * 0.25:.1f}px}}
.date{{color:{style.muted};margin-left:auto}}
.win{{position:relative;width:{lay.card_w:.2f}px;height:{lay.win_h:.2f}px;overflow:hidden;background:#fff}}
.page{{position:absolute;left:0;top:0;width:{img_w}px;height:{img_h}px;transform-origin:0 0;
  transform:{_transform(lay.start)};{move}}}
.page img{{display:block;width:{img_w}px;height:{img_h}px}}
.hl{{position:absolute;box-sizing:border-box;border-style:solid;border-color:{style.accent};
  background:{_rgba(style.accent, 0.16)};transform-origin:0 50%;
  animation:ring {RING_S:.3f}s {style.easing} {ring_at:.3f}s both}}
@keyframes cardin{{from{{opacity:0;transform:translateY({lay.bar_h * 0.6:.1f}px) scale(.97)}}to{{opacity:1;transform:none}}}}
@keyframes cardout{{from{{opacity:1}}to{{opacity:0}}}}
@keyframes move{{from{{transform:{_transform(lay.start)}}}to{{transform:{_transform(lay.end)}}}}}
@keyframes ring{{from{{opacity:0;transform:scaleX(0)}}to{{opacity:1;transform:none}}}}
</style></head><body><div class="card">
<div class="bar"><i class="dot"></i><i class="dot"></i><i class="dot"></i>
<span class="site">{site}</span><span class="date">{taken}</span></div>
<div class="win"><div class="page"><img alt="" src="data:image/png;base64,{data}">{ring}</div></div>
</div></body></html>
"""
    return page, warnings
