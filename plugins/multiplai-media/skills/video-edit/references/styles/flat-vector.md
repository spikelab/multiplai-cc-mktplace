---
name: flat-vector
description: Bright, friendly explainer look — flat shapes, simple icons drawn in SVG, playful but tidy.
palette:
  background: "#FFF8EC"
  surface: "#FFFFFF"
  text: "#1F2A44"
  muted: "#6B7690"
  accent: "#FF6B4A"
  accent_2: "#2EC4B6"
  key: "#00FF00"
fonts:
  display: "'Avenir Next', 'Nunito', -apple-system, 'Helvetica Neue', Arial, sans-serif"
  body: "'Avenir Next', 'Nunito', -apple-system, 'Helvetica Neue', Arial, sans-serif"
  weights: {display: 800, body: 600}
shape:
  radius_px: 28
  stroke_px: 6
motion:
  easing: "cubic-bezier(.34,1.56,.64,1)"
  enter_s: 0.6
  exit_s: 0.3
  stagger_s: 0.15
  max_moving: 3
layout_9x16:
  safe_top: 200
  safe_bottom: 480
  safe_side: 120
  caption_band: [1180, 1420]
---

# flat-vector

## Look

Warm off-white or transparent (keyed) backgrounds, solid flat shapes with
thick dark outlines, two accents (coral and teal). Icons are simple
geometric SVG drawn inline: circles, rounded rectangles, arrows, checkmarks.
Elements pop in with a slight overshoot.

## Do

- Draw icons with inline `<svg>` using `stroke="#1F2A44"` at `stroke-width`
  6 (at 1080 wide) and flat fills from the palette.
- Pop shapes in with `transform: scale(.6)` → `scale(1)` and the style's
  overshoot easing; stagger siblings by 0.15 s.
- Draw lines and arrows on with `stroke-dasharray`/`stroke-dashoffset`
  animations.
- Use the second accent for contrast pairs (before/after, yes/no).

## Don't

- No photos, gradients, textures or shadows.
- No thin strokes (under 4 px at 1080 wide): they shimmer after encoding.
- No text over a busy part of the video without a solid card behind it.
- For keyed overlays, no green anywhere in the graphic (teal `accent_2` is
  far enough from `#00FF00`; check any new colour).

## Example (keyed)

```html
<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;width:1080px;height:420px;overflow:hidden;background:#00FF00;
  font-family:"Avenir Next",Nunito,-apple-system,Arial,sans-serif}
.card{position:absolute;left:120px;top:60px;width:840px;height:300px;box-sizing:border-box;
  background:#fff;border:6px solid #1F2A44;border-radius:28px;display:flex;align-items:center;gap:36px;
  padding:0 48px;transform:scale(.6);opacity:0;animation:pop .6s cubic-bezier(.34,1.56,.64,1) .1s forwards}
.t{font-size:56px;font-weight:800;color:#1F2A44}
svg path{stroke-dasharray:120;stroke-dashoffset:120;animation:draw .5s ease-out .6s forwards}
@keyframes pop{to{transform:none;opacity:1}}
@keyframes draw{to{stroke-dashoffset:0}}
</style></head><body><div class="card">
<svg width="160" height="160" viewBox="0 0 160 160"><circle cx="80" cy="80" r="70" fill="#2EC4B6" stroke="#1F2A44" stroke-width="6"/>
<path d="M45 82 L70 106 L116 58" fill="none" stroke="#1F2A44" stroke-width="12" stroke-linecap="round" stroke-linejoin="round"/></svg>
<div class="t">Done in 3 steps</div></div></body></html>
```
