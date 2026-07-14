---
name: screen-demo
description: Turn a raw screen recording (.mov/.mp4) into a polished 1-3 minute landscape product demo video. Free + local — uses ffmpeg + PySceneDetect for editing and mlx_whisper on the macOS host (over the SSH bridge) for multilingual transcription. No SaaS, no API keys. The user provides a recording and a prose description ("keep it 90s, hook in first 10s, money shot at 2:30, lo-fi vibe") plus an optional music file/URL; the orchestrating Claude runs prep, authors an EDL, renders the reel. Triggers on "make a demo video", "edit this screencast", "turn this recording into a demo", "product demo from screen recording", "screen-demo skill".
---

# screen-demo

Free + local pipeline that turns a raw screen recording into a polished 1-3 min landscape product demo. Three commands: `prep`, `render`, `make` (the orchestration wrapper).

## When the user invokes this skill

Typical: "make a demo video from `/path/to/recording.mov` — keep it 90s, hook is the first 10s, the money shot is around 2:30, lo-fi vibe. Use `https://pixabay.com/music/some-track`."

**Run this workflow:**

### 0. Transcription prerequisites (host bridge — read this first)

Transcription runs **exclusively on the macOS host** via `mlx_whisper` (Apple
Metal GPU). MLX cannot run in the Linux container, so there is **no in-container
whisper build** (no whisper.cpp, no cmake). From the container, prep bridges to
the host over SSH. Requirements:

- `mlx_whisper` installed on the host (`pip install mlx-whisper`, Apple Silicon).
- An SSH key readable in the container (`/home/agent/.ssh/build_key`, or
  `TRANSCRIBE_KEY` / `SSH_BUILD_KEY`).
- A bridge user (`SSH_BUILD_USER`, or `TRANSCRIBE_USER`).
- The host gateway allowlisting `mlx_whisper`.

Preflight (also run by `bootstrap.sh`):
```bash
ssh -i /home/agent/.ssh/build_key "$SSH_BUILD_USER@host.docker.internal" 'command -v mlx_whisper'
```
If the bridge is down, prep **fails loudly** with a fix-it message — it never
silently falls back to building anything in the container.

### 1. Bootstrap (first run only)

```bash
bash ${CLAUDE_PLUGIN_ROOT}/skills/screen-demo/scripts/bootstrap.sh
```
Verifies `ffmpeg`, ensures PySceneDetect + OpenCV are importable (baked into the
image; else installed into a `uv venv`), and preflights the host transcription
bridge. Idempotent. **No whisper build, no cmake, no PEP-668 breakage.**

### 2. Prep

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/screen-demo/scripts/pipeline.py prep <source.mov> \
  --language it \                     # ISO code; omit to auto-detect. NEVER English-only.
  --model mlx-community/whisper-large-v3-mlx \  # optional; default whisper-medium-mlx
  --prompt-hint "Proper Noun, Other Name"
```
Builds a 720p proxy, extracts 16 kHz audio, transcribes on the host with a
**multilingual** mlx_whisper model (default `mlx-community/whisper-medium-mlx`),
runs silencedetect + scenedetect. Caches everything under
`$WORKSPACE/.screen-demo-cache/<source-hash>/` (or `~/.cache/screen-demo/` when
no `WORKSPACE`) so re-runs are instant. **Output: prints `CONTEXT: <path>` — that's
the file you need to read next.**

**Language:** `--language` takes an ISO code (`it`, `es`, `fr`, …). Omit it to let
mlx_whisper auto-detect. The model is always multilingual — an `.en` model is
never used, so non-English audio transcribes correctly (no "(speaking in foreign
language)").

### 3. Author the EDL

Read `<CONTEXT>` (a markdown file with timecoded transcript + cut candidates), then **write an EDL JSON** that realizes the user's prose description. Schema in `examples/demo-narrated.edl.json`.

EDL authoring guidance:
- **`source` must be the ORIGINAL recording — never the 720p proxy.** The proxy is an analysis artifact; render refuses it.
- Map each user beat to a segment with `src_start`/`src_end` from the transcript anchors.
- Use `speed > 1` for "fast-forward" sections (anything >4 auto-mutes audio; that's where the music bed carries).
- Use `speed = 1.0` and a `zoom: {scale, x, y}` for "money shot" moments.
- Use silencedetect timestamps as natural cut points.
- Total target duration: usually 60–120 s.
- Add a small title card and optional logo per the user's request.

**Zoom rules (a zoom is a crop — everything outside it is invisible):**
- Zooms are short money shots (≤12 s). After every zoomed segment, return to a full-frame segment so the viewer regains context.
- **Never end the video zoomed** — render errors on a zoomed final segment. If a close-up ending is genuinely intended, set `"hold": true` inside that zoom.
- Keep most of the runtime full-frame; render warns when >50% is zoomed.

Write the EDL to a sensible location (e.g. `~/.cache/screen-demo/<key>/edl.json`).

### 3b. Choose music that fits

Music is part of the edit, not an afterthought. Before rendering:
- If the user named a track or vibe, honor it. If not, **propose a specific track (with source link) and say why it fits** — don't silently pick one.
- Match genre and tempo to the content, e.g.:

| Demo type | Fits | Avoid |
|---|---|---|
| Dev tool / terminal workflow | minimal electronic, lo-fi beats | orchestral, vocals |
| Business / hiring / SaaS pitch | upbeat corporate, light house | lo-fi sleepy beats, heavy EDM |
| Consumer app, playful | indie pop, funk | dark ambient |
| Data/AI "wow" reveal | cinematic electronic build | anything with lyrics |

- Tempo should roughly match cut density: fast-forward-heavy reels want energy; calm narrated walkthroughs want restraint.
- Search Pixabay Music by mood ("corporate upbeat", "lo-fi chill", "cinematic tech") rather than taking the first result.
- The `synth` pink-noise bed is a last-resort fallback — never ship it in a final deliverable without telling the user.

### 4. Render

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/screen-demo/scripts/pipeline.py render <edl.json> \
  --out <output.mp4> \
  --music-file <path>          # or --music-url <url>
  --music-volume-db -22         # optional, default -18
```

### 5. Quality check + report back

Before declaring done, verify the render (ffprobe + spot-check frames with `ffmpeg -ss <t> -frames:v 1`):
- **Sharpness:** output is 1080p by default; screen text must be legible. If it isn't, check the EDL `source` points at the original recording.
- **Framing:** extract a frame from the last 2 s — it must be full-frame (no leftover zoom crop).
- **Music:** the bed fits the content type and ducks under narration.

Print the output path and total duration. If quality issues are visible (caption errors, wrong segment, etc.), iterate on the EDL and re-render — the cached prep means re-renders are fast (the bulk of time is the per-segment cut pass).

## Architecture

```
$WORKSPACE/.screen-demo-cache/<source-hash>/   (or ~/.cache/screen-demo/ if no $WORKSPACE)
  proxy_720p.mp4    ← 720p proxy
  audio16k.wav      ← 16 kHz mono audio
  transcript.srt    ← mlx_whisper timestamps (host, multilingual)
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
  - `zoom` — `{scale, x, y, hold?}` where x,y are normalized 0..1 crop position; `hold: true` permits a zoom on the final segment (otherwise render errors)
- `transitions` — `[{after, kind, duration}]` (currently only `fade`)
- `logo` — `{path, position: br|bl|tr|tl, scale, start_at}`
- `music` — `{file}` OR `{prompt}` (prompt is documented but generation is not available in CPU-only Linux containers — pass `file` or `url`)
- `output` — `{width, height, fps, crf}` (defaults 1920×1080, 30 fps, CRF 18)

## What it does NOT do

- **Subtitles.** Transcription runs internally for orchestrator context only. No burn-in.
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
| Transcription (macOS host, multilingual) | mlx_whisper + whisper-medium-mlx | MIT |
| Music fetching (URL path) | yt-dlp | Unlicense |
| Music generation (optional, Mac/GPU only) | ACE-Step / ACE-Step-1.5 | Apache-2.0 / MIT |
