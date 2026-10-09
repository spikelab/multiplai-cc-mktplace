---
name: clean-tech
description: Dark, precise, product-launch look — numbers, stats, UI callouts, short labels.
palette:
  background: "#0B1220"
  surface: "#111A2E"
  text: "#FFFFFF"
  muted: "#9AA7BD"
  accent: "#3DDC97"
  key: "#00FF00"
fonts:
  display: "Inter, -apple-system, 'Helvetica Neue', Arial, sans-serif"
  body: "Inter, -apple-system, 'Helvetica Neue', Arial, sans-serif"
  weights: {display: 800, body: 500}
shape:
  radius_px: 18
  stroke_px: 0
motion:
  easing: "cubic-bezier(.2,.8,.2,1)"
  enter_s: 0.5
  exit_s: 0.3
  stagger_s: 0.12
  max_moving: 2
layout_9x16:
  safe_top: 200
  safe_bottom: 480
  safe_side: 120
  caption_band: [1180, 1420]
---

# clean-tech

## Look

Near-black surfaces, white type, one green accent used for the single thing
the viewer should read first. Big numbers, small labels, generous padding.
Movement is short and decisive: things slide a short distance and settle.

## Do

- Lead with one number or one short phrase per card; a label above it in
  small caps, `muted` colour, letter-spaced.
- Use the accent for one element per card: the number, a progress bar, or an
  underline — not all three.
- Slide in from 24–40 px away with a fade; never from off-screen.
- Align everything to a left edge with `padding: 48px 56px` at 900 px wide.
- Progress bars and counters that fill in over 1–2 s are on-style.

## Don't

- No gradients, glows, drop shadows or glass blur.
- No more than two type sizes on one card.
- No bounce or elastic easing; no rotation.
- No icons unless the user supplies them.

## Example

```html
<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;width:900px;height:560px;overflow:hidden;background:#0B1220;
  font-family:Inter,-apple-system,"Helvetica Neue",Arial,sans-serif;color:#fff}
.card{position:absolute;inset:0;padding:48px 56px}
.k{font:500 28px/1 inherit;letter-spacing:.1em;text-transform:uppercase;color:#9AA7BD;
  opacity:0;animation:up .5s cubic-bezier(.2,.8,.2,1) .1s forwards}
.n{font-size:150px;font-weight:800;margin-top:24px;
  opacity:0;animation:up .5s cubic-bezier(.2,.8,.2,1) .22s forwards}
.bar{position:absolute;left:56px;right:56px;bottom:64px;height:16px;border-radius:8px;background:#111A2E}
.bar i{display:block;height:100%;width:0;border-radius:8px;background:#3DDC97;
  animation:fill 1.6s cubic-bezier(.2,.8,.2,1) .5s forwards}
@keyframes up{from{opacity:0;transform:translateY(32px)}to{opacity:1;transform:none}}
@keyframes fill{to{width:72%}}
</style></head><body><div class="card"><div class="k">Label</div><div class="n">72%</div>
<div class="bar"><i></i></div></div></body></html>
```
