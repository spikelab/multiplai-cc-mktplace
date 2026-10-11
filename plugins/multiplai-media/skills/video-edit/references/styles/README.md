# Overlay styles

A style file tells whoever writes an overlay page (`overlays[].html` in the
EDL) what it should look like and how it should move, so every graphic in a
video — and across videos — matches. Every overlay names a style
(`overlays[].style`); if the user has not said which, ask them before
writing the page.

Styles shipped here: [`clean-tech.md`](clean-tech.md) and
[`flat-vector.md`](flat-vector.md). A style derived from a reference video
(`pipeline.py contact-sheet`, see `SKILL.md`) is saved to the job's output
directory, never into this folder.

## Format

Markdown with YAML frontmatter. The frontmatter holds the values a page
uses; the body holds the rules that need words.

```yaml
---
name: clean-tech                 # matches the file name
description: one line on when to use it
brand: path/to/brand.json        # optional: take colours and fonts from a brand file
palette:
  background: "#0B1220"          # panel overlays: the page background
  surface: "#111A2E"             # cards on the background
  text: "#FFFFFF"
  muted: "#9AA7BD"
  accent: "#3DDC97"              # one accent; a second only if the style says so
  key: "#00FF00"                 # keyed overlays only: the page background, removed at render
fonts:
  display: "Inter, -apple-system, 'Helvetica Neue', Arial, sans-serif"
  body:    "Inter, -apple-system, 'Helvetica Neue', Arial, sans-serif"
  weights: {display: 800, body: 500}
shape:
  radius_px: 18                  # corner radius at 1080 px wide; scale with the region
  stroke_px: 0                   # outline width; 0 = no outlines
motion:
  easing: "cubic-bezier(.2,.8,.2,1)"
  enter_s: 0.5                   # how long an element takes to arrive
  exit_s: 0.3
  stagger_s: 0.12                # delay between elements arriving in sequence
  max_moving: 2                  # elements in motion at the same moment
layout_9x16:                     # for a 1080x1920 output, in output pixels
  safe_top: 200                  # keep text below this (platform UI)
  safe_bottom: 480               # keep text above 1920 - this
  safe_side: 120
  caption_band: [1180, 1420]     # where reel captions sit; never put a graphic here
---
```

Body sections, in this order:

- **Look** — two or three sentences a designer would recognise.
- **Do** and **Don't** — short lists.
- **Example** — a minimal page in this style (see the page rules below).

When a brand file is named, its `primary`, `accent`, `background` and fonts
replace the palette and fonts above; the shape, motion and layout rules stay.

## Page rules (every style)

The renderer (`stages/overlay.py`) seeks the page to every frame's time and
captures it, so a page must:

- animate with CSS animations, the Web Animations API, GSAP or lottie-web,
  or a `requestAnimationFrame` loop that reads the time it is given: the
  page's clock (`Date`, `performance.now`, `requestAnimationFrame`) is the
  overlay's time, starting at 0. No CSS `transition` (it ignores seeking),
  no `infinite` iteration count, no video;
- give the same frame for the same time: no `Math.random`,
  `crypto.getRandomValues`, `Date.now` or `new Date()` (it would read
  January 1970);
- use `animation-fill-mode: forwards` (or `both`) so elements stay put after
  they arrive;
- animate `transform` and `opacity`, not `width`, `height`, `left`, `top`,
  `font-size` or `letter-spacing`, which stutter;
- be sized to the overlay's region exactly (`html, body { width: Wpx;
  height: Hpx; overflow: hidden; margin: 0 }`);
- be one self-contained file: inline CSS, inline SVG, images and fonts as
  `data:` URIs, and GSAP or lottie-web inlined in a `<script
  data-library="…">`. Nothing loads from the network;
- name a font the page embeds with `@font-face`, or one installed where it
  renders, before any generic family. The style files name their preferred
  font first and widely installed fallbacks after it;
- for `mode: "alpha"` (over the video), leave the background to the
  renderer, which makes it transparent;
- for `mode: "panel"`, fill the background with `palette.background`: the
  whole region is shown;
- for `mode: "keyed"`, fill the background with the style's `key` colour and
  keep that colour (and anything close to it) out of the graphic itself.

`pipeline.py lint-overlay <page>` checks these rules; `render` runs the same
checks and refuses a page with an error.

Render time is about 1.3 s per second of overlay at 1080×1920 and 30 fps on
one page, 0.75 s with four pages at once (measured on 10 cores), so keep each
overlay to the seconds it is needed.
