# Motion graphics overlays

Load this when a video needs animated graphics on top: a stat card, a lower
third, a label, a step counter. Each graphic is an HTML page animated with
CSS; `render` turns it into a clip and lays it over the video for the
seconds the EDL gives.

In the commands below, `P` stands for
`uv run --project "${CLAUDE_PLUGIN_ROOT}/skills/video-edit/scripts" python3 "${CLAUDE_PLUGIN_ROOT}/skills/video-edit/scripts/pipeline.py"`.

## What it needs

A headless browser driven by `agent-browser` (`npm i -g agent-browser`) on a
Mac. Where the host's browser is reached through an `ab` command instead,
that is used, and screenshots are written on the host, so the job's output
directory must be under the shared workspace. If `ab` answers `DENIED: host
browser is not enabled`, pass that line and the command it comes with to the
user; do not look for another browser. Overlays never attach to a browser the
user has open.

## 1. Pick a style

Every overlay follows a style file. Shipped styles are in `styles/`
([format](styles/README.md), [`clean-tech`](styles/clean-tech.md),
[`flat-vector`](styles/flat-vector.md)). If the user has not named one, ask
which — or offer to derive one from a video they like (section 4).

## 2. Write each overlay page

One self-contained HTML file per graphic, in the job's output directory
(`P outdir <job>` → `overlays/<name>.html`). Follow the page rules in
[`styles/README.md`](styles/README.md#page-rules-every-style): CSS animations
or the Web Animations API only, sized exactly to the region, no network,
`forwards` fill. Time inside the page starts at 0 when the overlay appears.

Two modes:

| `mode` | Page background | Shown | Use for |
|---|---|---|---|
| `"panel"` | the style's `background` | the whole region, opaque | a card or full-screen graphic |
| `"keyed"` | the style's `key` colour (default `#00FF00`) | only the graphic; the key colour is removed | lower thirds, labels, arrows over the video |

Browser screenshots are always opaque, which is why `keyed` exists: keep the
key colour, and anything close to it, out of the graphic.

## 3. Add them to the EDL

```json
"overlays": [
  {"html": "/abs/path/overlays/stat.html", "style": "clean-tech",
   "start": 10.0, "end": 17.0, "mode": "panel",
   "region": {"x": 90, "y": 520, "w": 900, "h": 560}},
  {"html": "/abs/path/overlays/strap.html", "style": "clean-tech",
   "start": 11.0, "end": 16.0, "mode": "keyed",
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
it: about 5 s of rendering per second of overlay at 30 fps. Each overlay is
cached by its content, size, fps and length, so re-rendering the video after
an EDL change that leaves a page alone costs nothing extra.

Then extract a frame inside each overlay's span and look at it: the graphic
must be complete, readable, and (for `keyed`) free of a green fringe.

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
