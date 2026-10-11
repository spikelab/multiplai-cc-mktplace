# Motion graphics overlays

Load this when a video needs animated graphics on top: a stat card, a lower
third, a label, a step counter. Each graphic is an HTML page animated with
CSS, the Web Animations API, GSAP or Lottie; `render` turns it into a clip and
lays it over the video for the seconds the EDL gives.

In the commands below, `P` stands for
`uv run --project "${CLAUDE_PLUGIN_ROOT}/skills/video-edit/scripts" python3 "${CLAUDE_PLUGIN_ROOT}/skills/video-edit/scripts/pipeline.py"`.

## What it needs

Headless Chromium, which `render` launches itself through Playwright. Run the
pipeline through `P` above: the project declares the `playwright` package.
Playwright also needs the Chromium build it expects. Where none is installed,
`render` stops and names the command:

```bash
uv run --project "${CLAUDE_PLUGIN_ROOT}/skills/video-edit/scripts" playwright install chromium-headless-shell
```

Overlays never use a browser the user has open, and the page gets no
network: only the page itself is served to it.

## 1. Pick a style

Every overlay follows a style file. Shipped styles are in `styles/`
([format](styles/README.md), [`clean-tech`](styles/clean-tech.md),
[`flat-vector`](styles/flat-vector.md)). If the user has not named one, ask
which — or offer to derive one from a video they like (section 4).

## 2. Write each overlay page

One self-contained HTML file per graphic, in the job's output directory
(`P outdir <job>` → `overlays/<name>.html`). Follow the page rules in
[`styles/README.md`](styles/README.md#page-rules-every-style): sized exactly
to the region, no network, no randomness, `forwards` (or `both`) fill. Time
inside the page starts at 0 when the overlay appears; the page's `Date`,
`performance.now` and `requestAnimationFrame` follow that time, so CSS
animations, the Web Animations API, GSAP timelines, lottie-web and
`requestAnimationFrame` loops all render frame-exact.

GSAP and lottie-web must be inlined, because the page gets no network. Put
each library in its own `<script data-library="gsap@3.12.5">…</script>`:
the page checks skip a script marked that way. Pause nothing yourself:
`render` pauses GSAP's global timeline and every Lottie animation and sets
them to each frame's time.

Three modes:

| `mode` | Page background | Shown | Use for |
|---|---|---|---|
| `"alpha"` | transparent (render clears it) | the graphic with its real transparency: soft shadows, glows and smooth edges | **anything over the video**: lower thirds, labels, arrows, cards |
| `"panel"` | the style's `background` | the whole region, opaque | a full-screen graphic or a card that covers its region |
| `"keyed"` | the style's `key` colour (default `#00FF00`) | only the graphic; the key colour is removed | pages written before `alpha` existed |

An overlay without a `mode` is `keyed`, so old EDLs render as they did. Use
`alpha` for new graphics over video: `keyed` cannot keep a soft shadow or a
semi-transparent fill, and anything near the key colour disappears.

Check every page before rendering it; `render` runs the same checks and
refuses a page with an error:

```bash
P lint-overlay <outdir>/overlays/<name>.html --mode alpha
```

Errors: `Math.random`, `crypto.getRandomValues`, `Date.now` and `new Date()`
(the page clock starts at 0, January 1970), a CSS `transition`, an
`infinite` iteration count, a font stack with no available named font, and
any `http(s)://` URL. Warnings: a first-choice font that is not available, an
animated `width`/`height`/`left`/`top`/`font-size`/`letter-spacing`, and a
keyed-page colour close to the key.

## Motion rules

How a graphic moves matters as much as how it looks. Unless the style file
says otherwise:

- **Start 0.1–0.3 s in**, never at 0: a graphic already moving on its first
  frame reads as a jump cut. (HyperFrames `skills/hyperframes-creative/references/motion-principles.md:20`)
- **Exits are shorter than entrances**: about 0.4 s in, 0.25 s out. (`motion-principles.md:65`)
- **Stagger in order of importance**, not page order: what moves first
  reads as most important. Keep the whole stagger under 500 ms, however
  many items. (`motion-principles.md:61`)
- **Duration sets the register**: 0.15–0.3 s urgent, 0.3–0.5 s professional,
  0.5–0.8 s calm or luxurious. Vary durations across a video; the slowest
  move should be about 3× the fastest. (`motion-principles.md:16,38-40`)
- **Ease out to enter, ease in to exit, ease in-out to move** between two
  positions (GSAP `.out`, `.in`, `.inOut`; CSS `cubic-bezier(.2,.8,.2,1)`,
  `cubic-bezier(.6,0,.8,.2)`, `ease-in-out`). (`motion-principles.md:30-32`)
- **No back, elastic or bounce** eases unless the style is explicitly
  playful: an overshooting ease reads as playful whatever the content
  (`motion-principles.md:26` names elastic as the playful ease).
- **Animate `transform` and `opacity` only.** Width, height, position and
  font size re-lay out the page every frame and stutter; `lint-overlay`
  warns about them. (HyperFrames `packages/lint/src/rules/gsap.ts:1918`)
- **Swap text in place with a zoom-through**: the outgoing text scales
  1 → 1.2 over 0.2 s (ease in) while fading, a hard swap, then the incoming
  text scales 0.75 → 1 over 0.5 s (ease out). Two texts are never on screen
  together, and the move reads as one push forward. (HyperFrames
  `skills/pr-to-video/references/cut-catalog.md:38-71`)

## 3. Add them to the EDL

```json
"overlays": [
  {"html": "/abs/path/overlays/stat.html", "style": "clean-tech",
   "start": 10.0, "end": 17.0, "mode": "panel",
   "region": {"x": 90, "y": 520, "w": 900, "h": 560}},
  {"html": "/abs/path/overlays/strap.html", "style": "clean-tech",
   "start": 11.0, "end": 16.0, "mode": "alpha",
   "region": {"x": 0, "y": 1500, "w": 1080, "h": 300}}
]
```

- `start`/`end` are output time (check with `P timeline <edl>`).
- `region` is in output pixels, even width and height; omit it for the whole
  frame. On a reel keep text out of the caption band and the unsafe bands
  (`styles/README.md` lists them).
- `render` refuses an overlay outside the video's duration, a region outside
  the frame, and two overlays covering the same part of the frame at the
  same time.

`render` prints an estimate before each overlay and the measured cost after
it. A 1080×1920 card at 30 fps took 1.3 s of rendering per second of overlay
on one page, and 0.75 s with the default four pages rendering at once (on 10
cores; set `VIDEO_EDIT_OVERLAY_WORKERS` to change the page count). Each overlay is
cached by its content, size, fps and length, so re-rendering the video after
an EDL change that leaves a page alone costs nothing extra.

Then extract a frame inside each overlay's span and look at it: the graphic
must be complete and readable, an `alpha` graphic's shadow must show over the
video, and a `keyed` one must be free of a green fringe.

## 4. Derive a style from a reference video

```bash
P contact-sheet <video-or-url> --out <outdir>/reference --every 5
# SHEET: <outdir>/reference/sheet_01.png …
```

It takes a frame at every scene change plus one every `--every` seconds,
labels each with its time, and tiles them 4×4 (URLs are fetched with yt-dlp:
the first 600 s, at most 720p). Read every sheet, then write a style file in
the [format](styles/README.md) — palette, fonts (closest freely available),
shape, motion (judged from consecutive frames), layout — and **show it to the
user before saving it** to `<outdir>/styles/<name>.md`. Never save it into
the plugin.

A frame from someone else's video is only a reference: describe its look in
the style file; do not copy its logos, text or artwork into a page.
