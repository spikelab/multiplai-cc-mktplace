# Interview or podcast → vertical reels

Load this when the job is a long conversation (30+ min) that becomes several
15–90 s vertical 9:16 clips for Instagram, Facebook or TikTok. The shared
workflow (prep → EDL → render → check) is in `SKILL.md`; this file holds what
is specific to reels.

Typical request: "make reels from this podcast episode, Italian, use our
brand file".

In the commands below, `P` stands for
`uv run --project "${CLAUDE_PLUGIN_ROOT}/skills/video-edit/scripts" python3 "${CLAUDE_PLUGIN_ROOT}/skills/video-edit/scripts/pipeline.py"`.

## 1. Prep with the spoken language

```bash
P prep <recording.mp4> --language it
```

Always pass `--language` for a non-English recording: it selects
`whisper-large-v3`, which gives word timings and punctuation good enough for
captions. `context.md` names the transcript engine. Prep also writes, in the
cache directory:

- `transcript.json` — every word with `start`/`end` (and `speaker`, when the
  transcript has speaker labels);
- `sentences.json` — sentences built from the words;
- `retakes.json` — sentences the speaker said again soon after (also a
  "Likely retakes" table in `context.md`; keep the later take);
- `silences.json` — short pauses, used to snap clip edges.

If prep fails because the large-v3 model will not load on the host, stop and
tell the user. Do not switch to a smaller model on your own: medium's
punctuation and word timings make visibly worse captions.

## 2. Measure the panels once per show

Most interview recordings keep one layout for the whole show: each speaker in
a fixed rectangle. Write one frame with a grid and read the rectangles off it:

```bash
P frame <recording.mp4> --at 300 --grid --out <outdir>/grid.png
```

Read the PNG, then write the panels in source pixels, for example for a
recording with one speaker on the left and one on the right:

```json
"layout": {
  "panels": {
    "A": {"x": 40,  "y": 200, "w": 900, "h": 740},
    "B": {"x": 980, "y": 200, "w": 900, "h": 740}
  }
}
```

Keep each rectangle just inside the speaker's video (no frame border, no
on-screen name tag if you can avoid it). Check another frame from late in the
recording; if the layout changes mid-show, split the clips by layout.

## 3. Read the whole transcript, then propose candidates

Read the entire transcript in `context.md` before choosing anything — the
best moment is often late. Then write `<outdir>/candidates.md` with **8–12
candidates**. For each:

- start and end (source seconds), and the duration;
- which speaker carries it;
- the hook line: the first spoken line, or the headline you would add;
- a 1–5 score on each of five axes, with one line of reasoning:
  - **hook strength** — do the first 3 s make someone stop scrolling?
  - **emotional payoff** — surprise, laughter, conviction, a strong opinion;
  - **quotability** — a line people would repeat or share;
  - **self-contained** — makes sense with no context from the episode;
  - **density** — no dead air, no throat-clearing, no tangent.

Rules for a good clip:

- Target 20–60 s. Reels over 90 s get a warning from `check`; over 180 s fail.
- The first spoken line must work without context, or the headline must
  supply the context ("The one habit that doubled our sales").
- Skip filler, false starts and the first take of a retake.
- Prefer one complete thought over two half ones.

Snap every candidate's edges before writing it down:

```bash
P snap <recording.mp4> <start> <end>
# SNAPPED: 812.340 851.960 (39.6s)
```

`snap` widens the clip to whole sentences and moves each edge into the
nearest pause, so no clip starts mid-word.

**Present the candidates to the user and render only the ones they pick.**

## 4. Write one EDL per chosen clip

Get the output directory (the workspace `INBOX/` if there is one, else the
current directory) and write `edl/clip-NN.edl.json` there:

```bash
P outdir <show-and-episode>
# OUTDIR: …/INBOX/video-edit/<show-and-episode>
```

A reel EDL:

```json
{
  "source": "/path/to/recording.mp4",
  "brand": "/path/to/brand.json",
  "layout": {"panels": {"A": {"x": 40, "y": 200, "w": 900, "h": 740},
                        "B": {"x": 980, "y": 200, "w": 900, "h": 740}}},
  "segments": [
    {"src_start": 812.34, "src_end": 851.96, "frame": "stack"}
  ],
  "captions": {"words_per_line": 3, "max_chars": 22, "highlight": true},
  "headline": {"text": "The one habit that doubled our sales", "start": 0, "end": 3},
  "output": {"width": 1080, "height": 1920, "fps": 30, "crf": 20, "audio_rate": 48000}
}
```

Framing, per segment:

| `frame` | Shows | Use when |
|---|---|---|
| `"stack"` | panel A over panel B | a back-and-forth; both reactions matter |
| `"A"` / `"B"` | that panel, cropped to 9:16 around its centre (or `focus: {x, y}`, 0–1 within the panel) | one person talks for the whole clip |
| `"speaker"` | whoever is talking; turns under 1.5 s keep the previous panel | the transcript has speaker labels and `layout.speakers` maps them: `{"SPEAKER_0": "A", "SPEAKER_1": "B"}` |
| none | the whole frame, filled per `output.fit` | a single-camera recording |

`frame: "speaker"` needs speaker labels in `transcript.json`. Those come from
the transcribe skill once it can tell speakers apart; without them, `render`
refuses the EDL — pick `"A"`, `"B"` or `"stack"` per segment instead.

For a recording without panels, set `output.fit`: `"blur"` (the frame scaled
to fit over a blurred copy of itself) or `"crop"` (fill the frame, cropping
around each segment's `focus`). The default `"pad"` letterboxes, and `render`
warns about it for a portrait output.

Several segments in one reel are allowed (cut a tangent out of the middle);
each join is a crossfade of `transitions[].duration` (default 0.5 s).

Captions are built from the transcript words, timed to the output with the
cuts and speeds applied; check them with:

```bash
P timeline <outdir>/edl/clip-01.edl.json
```

`render` fails if the caption font lacks a character the transcript uses —
pick a brand font that covers the language.

### Brand file

`brand.json` sets the caption and headline font, the colours and a logo:

```json
{
  "font_bold_file": "fonts/Brand-Bold.ttf",
  "primary": "#FFFFFF", "accent": "#FFD400",
  "background": "#0A0A0A", "caption_outline": "#000000",
  "logo": {"path": "logo.png", "position": "tl", "scale": 0.16}
}
```

Paths are relative to the brand file. `accent` colours the spoken word and the
headline box; `background` fills behind stacked panels. Without a brand file,
reels use the title-card font, white captions, a yellow highlight and no logo.

## 5. Render and check every clip

```bash
OUT=…/INBOX/video-edit/<show-and-episode>
for edl in "$OUT"/edl/clip-*.edl.json; do
  name=$(basename "$edl" .edl.json)
  P render "$edl" --out "$OUT/$name.mp4" &&
  P check "$OUT/$name.mp4" --preset reels --edl "$edl" || echo "✗ $name"
done
```

`check` exits 1 on any failing requirement (size, codec, 48 kHz audio, frame
rate, bitrate, file size, length, text inside the unsafe bands). Fix the EDL
and re-render; prep is cached, so a re-render costs only the encode.

Then extract a frame at 2 s and at the midpoint of each reel and look at
them: a caption and the headline must be readable, and there must be no
black bars.

```bash
ffmpeg -ss 2 -i "$OUT/clip-01.mp4" -frames:v 1 "$OUT/clip-01-2s.png"
```

Report the reel paths and the `check` output to the user. Caption accuracy in
the spoken language and whether the picks are good are the user's call.
