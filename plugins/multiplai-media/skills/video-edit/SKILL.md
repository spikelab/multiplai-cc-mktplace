---
name: video-edit
description: Edit an existing video into a finished output — today, a polished 1-3 minute landscape product demo from a raw screen recording (.mov/.mp4). Free + local — uses ffmpeg + PySceneDetect for editing and mlx_whisper on the macOS host (over the SSH bridge) for multilingual transcription. No SaaS, no API keys. The user provides a recording and a prose description ("keep it 90s, hook in first 10s, money shot at 2:30, lo-fi vibe") plus an optional music file/URL; the orchestrating Claude runs prep, authors an EDL, renders the result. Triggers on "make a demo video", "edit this screencast", "turn this recording into a demo", "product demo from screen recording", "screen demo", "video-edit skill".
---

# video-edit

Free + local pipeline that turns an existing video into edited outputs. Every
job follows the same steps: analyse the video (transcript, silences, scene
changes, motion), decide what to keep, write an edit list (EDL), render it,
and check the result. Commands: `prep`, `render`, `make` (prints the workflow).

## Pick the job, then load its guide

| The user has… | and wants… | Load |
|---|---|---|
| a screen recording | one 1–3 min landscape product demo | `references/screencast.md` |
| a long interview or podcast | several 15–90 s vertical 9:16 reels | not built yet — tell the user this skill cannot make reels in this version |

Read the guide for the job before writing the EDL. It holds the rules for
what to keep, what to cut and how to frame.

## Workflow

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
bash ${CLAUDE_PLUGIN_ROOT}/skills/video-edit/scripts/bootstrap.sh
```
Verifies `ffmpeg`, checks PySceneDetect + OpenCV are importable, and preflights
the host transcription bridge. Idempotent, and it installs nothing — it reports
what is missing and names the fix. **No whisper build, no cmake, no PEP-668
breakage.**

The Python deps are declared in `scripts/pyproject.toml`. Run the pipeline
through it and uv provides them:

```bash
uv run --project "${CLAUDE_PLUGIN_ROOT}/skills/video-edit/scripts" python3 "${CLAUDE_PLUGIN_ROOT}/skills/video-edit/scripts/pipeline.py" …
```

This works for an installed plugin (the directory resolves standalone) and in
the marketplace repo (uv walks up to the root workspace and its shared
environment). If `import scenedetect, cv2` fails, that is the missing piece —
not a venv this skill should build for itself.

### 2. Prep

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/video-edit/scripts/pipeline.py prep <source.mov> \
  --language it \                     # ISO code; omit to auto-detect. NEVER English-only.
  --model mlx-community/whisper-large-v3-mlx \  # optional; default whisper-medium-mlx
  --prompt-hint "Proper Noun, Other Name"
```
Builds a 720p proxy, extracts 16 kHz audio, transcribes on the host with a
**multilingual** mlx_whisper model (default `mlx-community/whisper-medium-mlx`),
runs silencedetect + scenedetect, and profiles on-screen activity (blackdetect +
a per-second motion score) into a **dead-span table** — black gaps, frozen
frames, and typing/cursor-only stretches where nothing watchable happens.
Caches everything under
`$WORKSPACE/.video-edit-cache/<source-hash>/` (or `~/.cache/video-edit/` when
no `WORKSPACE`) so re-runs are instant. **Output: prints `CONTEXT: <path>` — that's
the file you need to read next.**

**Language:** `--language` takes an ISO code (`it`, `es`, `fr`, …). Omit it to let
mlx_whisper auto-detect. The model is always multilingual — an `.en` model is
never used, so non-English audio transcribes correctly (no "(speaking in foreign
language)").

### 3. Author the EDL

Read `<CONTEXT>` (a markdown file with timecoded transcript + cut candidates),
then **write an EDL JSON** that realizes the user's prose description, following
the job's guide. Schema below; a full example is `examples/demo-narrated.edl.json`.

- **`source` must be the ORIGINAL recording — never the 720p proxy.** The proxy is an analysis artifact; render refuses it.
- Map each user beat to a segment with `src_start`/`src_end` from the transcript anchors.

Write the EDL to a sensible location (e.g. `~/.cache/video-edit/<key>/edl.json`).

### 4. Render

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/video-edit/scripts/pipeline.py render <edl.json> \
  --out <output.mp4> \
  --music-file <path>          # or --music-url <url>
  --music-volume-db -22         # optional, default -18
```

### 5. Quality check + report back

Before declaring done, verify the render (ffprobe + spot-check frames with
`ffmpeg -ss <t> -frames:v 1`) against the checks in the job's guide.

Print the output path and total duration. If quality issues are visible (caption errors, wrong segment, etc.), iterate on the EDL and re-render — the cached prep means re-renders are fast (the bulk of time is the per-segment cut pass).

## Architecture

```
$WORKSPACE/.video-edit-cache/<source-hash>/   (or ~/.cache/video-edit/ if no $WORKSPACE)
  proxy_720p.mp4    ← 720p proxy
  audio16k.wav      ← 16 kHz mono audio
  transcript.srt    ← mlx_whisper timestamps (host, multilingual)
  scenes.csv        ← PySceneDetect content-mode output
  cuts.json         ← merged silence_end + scene_change candidates
  activity.json     ← per-second motion profile (dead-span classification input)
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

## Tools (all permissive licenses, all local)

| Stage | Tool | License |
|---|---|---|
| Proxy / composite / encode | ffmpeg | LGPL/GPL |
| Scene detection | PySceneDetect | BSD-3 |
| Transcription (macOS host, multilingual) | mlx_whisper + whisper-medium-mlx | MIT |
| Music fetching (URL path) | yt-dlp | Unlicense |
| Music generation (optional, Mac/GPU only) | ACE-Step / ACE-Step-1.5 | Apache-2.0 / MIT |
