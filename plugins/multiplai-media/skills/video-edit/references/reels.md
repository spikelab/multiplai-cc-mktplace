# Interview, podcast or talk → vertical reels

Load this when the job is a long recording (30+ min) — a conversation, a
video call, a keynote — that becomes several 15–90 s vertical 9:16 clips for
Instagram, Facebook or TikTok. The shared
workflow (prep → EDL → render → check) is in `SKILL.md`; this file holds what
is specific to reels.

Typical request: "make reels from this podcast episode, Italian, use our
brand file".

In the commands below, `P` stands for
`uv run --project "${CLAUDE_PLUGIN_ROOT}/skills/video-edit/scripts" python3 "${CLAUDE_PLUGIN_ROOT}/skills/video-edit/scripts/pipeline.py"`.

## 1. Prep with the spoken language and a glossary

Before prep, build a glossary: the names and terms whisper is likely to
mishear. Take them from the user's request, the video's title and
description, and the names on screen (write one frame with
`P frame <recording.mp4> --at 300 --grid` and read the name tags, titles and
logos). Pass them as **one sentence in the spoken language** that uses them:

```bash
P prep <recording.mp4> --language it \
  --prompt-hint "Sergio di Vita da Host parla con Giovanni di DolceBot del CIN, il codice identificativo nazionale."
```

Never pass a bare comma list: "CIN fuori, DolceBot" made whisper-large-v3
return two words for 20 s of Italian speech, while the same terms inside a
sentence transcribed normally. After prep, compare the transcript's word
count with the length of the speech; if it is far short, re-run prep without
the hint (delete `transcript.json` and `whisper.*` in the cache first).

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

Ignore the dead-span table in `context.md` for an interview: it measures how
much the picture moves, and a talking head barely moves.

If prep fails because the large-v3 model will not load on the host, stop and
tell the user. Do not switch to a smaller model on your own: medium's
punctuation and word timings make visibly worse captions.

## 2. Correct the transcript before choosing clips

Captions are rendered from `transcript.json`, so a misheard word is burned
into every reel that contains it. Read the transcript in `context.md` for
words that look misheard — names, brands, acronyms, numbers, words that make
no sense in the sentence — and write `<outdir>/corrections.json`:

```json
[{"at": 1465.9, "from": "CINFUORI", "to": "CIN fuori"},
 {"at": 1282.7, "from": "Century", "to": "Sentry"},
 {"at": 1293.4, "from": "dot it down", "to": "jot it down"}]
```

`at` is the source time from the transcript; `from` is the words exactly as
the transcript has them (case matters, punctuation does not). Apply it:

```bash
P correct <cache-dir> <outdir>/corrections.json
```

`correct` keeps each word's timing (a run that becomes more or fewer words
shares its time out by length), saves the original as `transcript.raw.json`
once, and rebuilds sentences, retakes and `context.md`. An entry that matches
nothing exits 1 and writes nothing — fix its `at` or spelling. The file
always applies to the original transcript, so add to it and re-run it; never
render captions from an uncorrected transcript. Correct again whenever a
proof sheet (step 7) shows a caption that is wrong.

## 3. Measure the panels once per show

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

Keep each rectangle just inside the speaker's video and clear of every
broadcast graphic: frame borders, name tags, lower thirds, tickers, channel
logos and semi-transparent watermark boxes (a watermark that overlaps a panel
shows as a dark shadow in the crop). Shrink the rectangle or move it, rather
than cut a graphic in half. Check another frame from late in the recording;
if the layout changes mid-show, split the clips by layout.

## 4. Read the whole transcript, then propose candidates

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

## 5. Write one EDL per chosen clip

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
  "captions": {"highlight": true},
  "headline": {"text": "The one habit that doubled our sales", "start": 0, "end": 3},
  "output": {"width": 1080, "height": 1920, "fps": 30, "crf": 20, "audio_rate": 48000}
}
```

Framing is chosen **per segment, by looking**: split a clip into segments
wherever who is talking, or what is on screen, changes, and frame each one.
One framing for a whole clip is wrong whenever two people talk in it.

| `frame` | Shows | Use when |
|---|---|---|
| `"stack"` | panel A over panel B, sides trimmed so the pair fills the frame | a back-and-forth; both reactions matter |
| `"A"` / `"B"` | that panel, cropped to 9:16 around its centre (or `focus: {x, y}`, 0–1 within the panel) | one person talks for the whole clip |
| `"speaker"` | whoever is talking; turns under 1.5 s keep the previous panel | the transcript has speaker labels and `layout.speakers` maps them: `{"SPEAKER_0": "A", "SPEAKER_1": "B"}` |
| none | the whole frame, filled per `output.fit` | a single-camera recording |

`frame: "speaker"` needs speaker labels in `transcript.json`. Those come from
the transcribe skill once it can tell speakers apart; without them, `render`
refuses the EDL. Then the framing is a guess from the words and the picture:
split each clip at every change of speaker, set `"A"`, `"B"` or `"stack"` per
segment, confirm each on the proof sheet (step 7: is the person on screen the
one whose mouth moves?), and tell the user the framing was chosen by hand.

For a recording without panels, set `output.fit`: `"blur"` (the frame scaled
to fit over a blurred copy of itself) or `"crop"` (fill the frame, cropping
around each segment's `focus`), and override it on any segment with that
segment's own `fit`. The default `"pad"` letterboxes, and `render` warns
about it for a portrait output.

Several segments in one reel are allowed. A join between segments that are
contiguous in the source (`src_start` equals the previous `src_end`) is a
hard cut with unbroken audio: that is how to change framing inside one
continuous take. Any other join (a tangent cut out) is a crossfade of
`transitions[].duration` (default 0.5 s); `duration: 0` makes it a hard cut.

### Single-camera and broadcast-feed sources

A keynote, a lecture or a TV feed has no fixed panels: it cuts between
close-ups of the speaker, full-screen slides, a wide stage shot and
dissolves between them. Frame each **shot** on its own terms:

```bash
P shots <cache-dir> --from <clip start> --to <clip end> --out <outdir>/shots/clip-01.png
# SHOT 1: 1733.12–1742.17 (9.1s)
# SHOT 2: 1742.17–1744.67 (2.5s) …
```

`shots` lists the cuts prep found inside the clip and draws three frames of
each shot (just after it starts, its middle, just before it ends). Then:

- **Split the clip's segments at the shot cuts.** Adjacent segments are
  contiguous, so the joins are hard cuts and the speech runs on unbroken.
- **Slides and full-frame graphics:** `"fit": "blur"`, so all of the text is
  inside the frame. A 9:16 crop of a 16:9 slide cuts its title.
- **The speaker:** `"fit": "crop"` with `focus` on them (`x` is their centre as a
  fraction of the frame width).
- **A speaker who moves within a shot** (their position differs between the
  start, middle and end frames): give `focus` as keyframes read off frames you
  looked at — `"focus": [{"t": 1250.0, "x": 0.62}, {"t": 1256.0, "x": 0.35}]`,
  `t` in source seconds. Write frames with `P frame <src> --at <t>` every 2–3
  s where they move, and put a key where they change direction. Nothing
  detects them for you.
- **Missed cuts:** scene detection misses most dissolves and slides that
  slide in. When a shot's start and end frames show different things, find
  the change with `P frame` at a few times and split there yourself.

```json
"segments": [
  {"src_start": 1243.06, "src_end": 1249.03, "fit": "blur"},
  {"src_start": 1249.03, "src_end": 1299.74, "fit": "crop",
   "focus": [{"t": 1250, "x": 0.45}, {"t": 1254, "x": 0.62}, {"t": 1262, "x": 0.4}]}
]
```

Captions are built from the (corrected) transcript words, timed to the
output with the cuts and speeds applied. A line holds at most `max_chars`
(22) characters; `words_per_line` (5) is a soft cap. Lines break after a
comma, semicolon or colon where they can, and a single word is never left
alone on a line unless it ends a sentence or a pause follows it. Check the
timing with:

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

## 6. Render and check every clip

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

## 7. Proof every reel before reporting it

`check` passing says nothing about whether the right person is on screen or
the captions are right. Make a proof sheet of each reel and read it:

```bash
P proof "$OUT/clip-01.mp4" --edl "$OUT/edl/clip-01.edl.json" --out "$OUT/clip-01.proof.png"
```

It tiles a frame every 3 s plus one 0.5 s after every join and speaker
switch. Each tile is labelled with the output and source time, the segment
and its framing, the caption on screen, and the speaker label of the words
being said (when the transcript has labels). Answer these five questions for
each reel, in your report, and fix every "no" — edit the EDL or the
corrections, re-render, re-proof — before reporting:

1. **Is the person on screen the one speaking?** Compare the mouth in each
   tile with the caption and the speaker label.
2. **Is every caption clear of faces and mouths?** If not, move
   `captions.position_y`.
3. **Are crops free of broadcast graphics** — name tags, watermarks, lower
   thirds, tickers, channel logos?
4. **Are the brand assets real?** The `brand` file and logo must be the
   user's, never a file under the skill's `tests/` directory.
5. **Is all slide text inside the frame?** A slide cut at the sides needs
   `fit: "blur"` on its segment.

Report the reel paths, the `check` output, the proof-sheet paths and the five
answers per reel. Say that the answers are your own reading of the proof
sheets. Caption accuracy in the spoken language, the clip choices and how the
framing feels are the user's call.
