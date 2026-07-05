---
name: screen-demo
description: Turn a raw screen recording (.mov/.mp4) into a polished 1-3 minute landscape product demo video. Free + local — uses ffmpeg, PySceneDetect, whisper.cpp. No SaaS, no API keys, no Mac required. The user provides a recording and a prose description ("keep it 90s, hook in first 10s, money shot at 2:30, lo-fi vibe") plus an optional music file/URL; the orchestrating Claude runs prep, authors an EDL, renders the reel. Triggers on "make a demo video", "edit this screencast", "turn this recording into a demo", "product demo from screen recording", "screen-demo skill".
---

# screen-demo

Free + local pipeline that turns a raw screen recording into a polished 1-3 min landscape product demo. Three commands: `prep`, `render`, `make` (the orchestration wrapper).

## When the user invokes this skill

Typical: "make a demo video from `/path/to/recording.mov` — keep it 90s, hook is the first 10s, the money shot is around 2:30, lo-fi vibe. Use `https://pixabay.com/music/some-track`."

**Run this workflow:**

### 1. Bootstrap (first run only)

```bash
bash scripts/bootstrap.sh
```
Builds whisper.cpp, fetches the small.en model (~466 MB), installs PySceneDetect. Idempotent — skip if `vendor/whisper.cpp/build/bin/whisper-cli` already exists.

### 2. Prep

```bash
python3 scripts/pipeline.py prep <source.mov> --prompt-hint "Proper Noun, Other Name"
```
Builds a 720p proxy, extracts 16 kHz audio, transcribes with whisper.cpp, runs silencedetect + scenedetect. Caches everything under `~/.cache/screen-demo/<source-hash>/` so re-runs are instant. **Output: prints `CONTEXT: <path>` — that's the file you need to read next.**

### 3. Author the EDL

Read `<CONTEXT>` (a markdown file with timecoded transcript + cut candidates), then **write an EDL JSON** that realizes the user's prose description. Schema in `examples/demo-narrated.edl.json`.

EDL authoring guidance:
- Map each user beat to a segment with `src_start`/`src_end` from the transcript anchors.
- Use `speed > 1` for "fast-forward" sections (anything >4 auto-mutes audio; that's where the music bed carries).
- Use `speed = 1.0` and a `zoom: {scale, x, y}` for "money shot" moments.
- Use silencedetect timestamps as natural cut points.
- Total target duration: usually 60–120 s.
- Add a small title card and optional logo per the user's request.

Write the EDL to a sensible location (e.g. `~/.cache/screen-demo/<key>/edl.json`).

### 4. Render

```bash
python3 scripts/pipeline.py render <edl.json> \
  --out <output.mp4> \
  --music-file <path>          # or --music-url <url>
  --music-volume-db -22         # optional, default -18
```

### 5. Report back

Print the output path and total duration. If quality issues are visible (caption errors, wrong segment, etc.), iterate on the EDL and re-render — the cached prep means re-renders are fast (the bulk of time is the per-segment cut pass).

## Architecture

```
.cache/screen-demo/<source-hash>/
  proxy_720p.mp4    ← 720p proxy
  audio16k.wav      ← 16 kHz mono audio
  transcript.srt    ← whisper word-timestamps
  scenes.csv        ← PySceneDetect content-mode output
  cuts.json         ← merged silence_end + scene_change candidates
  context.md        ← human-readable bundle for the orchestrator
  edl.json          ← (orchestrator-authored) cut plan
```

## EDL schema (intermediate, hand-editable)

See `examples/demo-narrated.edl.json`. Top-level keys:
- `source` — path to the source recording
- `title` — `{line1, line2, duration}` (omit to skip)
- `segments` — `[{src_start, src_end, speed?, zoom?, mute?}]`
  - `speed` — `>1` faster, `<1` slower; default 1.0; auto-mutes audio if >4
  - `zoom` — `{scale, x, y}` where x,y are normalized 0..1 (center coords)
- `transitions` — `[{after, kind, duration}]` (currently only `fade`)
- `logo` — `{path, position: br|bl|tr|tl, scale, start_at}`
- `music` — `{file}` OR `{prompt}` (prompt is documented but generation is not available in CPU-only Linux containers — pass `file` or `url`)
- `output` — `{width, height, fps, crf}` (defaults 1280×720, 30 fps, CRF 22)

## What it does NOT do

- **Subtitles.** Whisper runs internally for orchestrator context only. No burn-in.
- **Cursor zoom-on-click.** Would require a macOS sidecar logger at record time. Out of scope.
- **AI music generation in this container.** ACE-Step pyproject hard-pins CUDA/MPS wheels — won't install on CPU-only aarch64 Linux. Use `--music-file` or `--music-url`. See `stages/music.py` `GENERATION_NOTE` for Mac/GPU install path if the user wants it there.

## Free-for-commercial music sources

Two URL types are supported by `--music-url`:
- **Direct CDN audio URLs** (`.mp3`/`.wav`/`.m4a`/`.ogg`/`.flac`) — downloaded with `curl` + realistic User-Agent.
- **Page URLs** (YouTube, Vimeo, etc.) — extracted via `yt-dlp`.

Recommended sources:
- **Pixabay Music** — https://pixabay.com/music/ — Pixabay Content License (commercial OK, no attribution). Open the track page, click ⋯ on the player → "Copy audio URL" (or right-click the player → "Copy audio address"). That gives you a `https://cdn.pixabay.com/audio/.../track.mp3` URL to pass to `--music-url`.
- **YouTube Audio Library** — https://studio.youtube.com/channel/UC/music — many CC0 tracks; paste the YouTube URL.
- **Uppbeat** — https://uppbeat.io/ — free with attribution; download and pass via `--music-file`.

## Tools (all permissive licenses, all local)

| Stage | Tool | License |
|---|---|---|
| Proxy / composite / encode | ffmpeg | LGPL/GPL |
| Scene detection | PySceneDetect | BSD-3 |
| Transcription | whisper.cpp + ggml-small.en | MIT |
| Music fetching (URL path) | yt-dlp | Unlicense |
| Music generation (optional, Mac/GPU only) | ACE-Step / ACE-Step-1.5 | Apache-2.0 / MIT |

Spec + empirical timings on 17-min 4K source: `INBOX/screen-demo-skill-spec-2026-05-28.md`.
