---
name: video-edit
description: Edit an existing video into finished outputs — a polished 1-3 minute landscape product demo from a raw screen recording (.mov/.mp4), or short 9:16 reels from a long podcast or interview recording. Free + local — uses ffmpeg + PySceneDetect for editing and mlx_whisper on the macOS host (over the SSH bridge) for multilingual transcription. No SaaS, no API keys. The user provides a recording and a prose description ("keep it 90s, hook in first 10s, money shot at 2:30, lo-fi vibe") plus an optional music file/URL; the orchestrating Claude runs prep, authors an EDL, renders the result. Triggers on "make a demo video", "edit this screencast", "turn this recording into a demo", "product demo from screen recording", "screen demo", "make reels from this podcast", "make shorts from this interview", "make TikToks from this video", "clip this video for Instagram", "let me comment on the video", "review the renders", "video-edit skill".
---

# video-edit

Free + local pipeline that turns an existing video into edited outputs. Every
job follows the same steps: analyse the video (transcript, silences, scene
changes, motion), decide what to keep, write an edit list (EDL), render it,
and check the result. Commands: `prep`, `correct`, `render`, `check`, `proof`,
`review`, `timeline`, `snap`, `frame`, `shots`, `outdir`, `contact-sheet`, `prefs`,
`make` (prints the workflow); `pipeline.py <command> --help` for each.

## Pick the job, then load its guide

| The user has… | and wants… | Load |
|---|---|---|
| a screen recording | one 1–3 min landscape product demo | `references/screencast.md` |
| a long interview or podcast | several 15–90 s vertical 9:16 reels | `references/reels.md` |
| any video being edited | animated graphics on top (stat cards, lower thirds), or a style taken from a reference video | `references/motion.md` |

Read the guide for the job before writing the EDL. It holds the rules for
what to keep, what to cut and how to frame.

**Read the user's preferences first**, every job:
`python3 ${CLAUDE_PLUGIN_ROOT}/skills/video-edit/scripts/pipeline.py prefs show`.
They are corrections the user approved in earlier jobs ("captions 2 words
per line"); follow them unless this job's brief says otherwise.

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
  --model mlx-community/whisper-large-v3-mlx \  # optional; default: large-v3 unless English
  --prompt-hint "A sentence, in the spoken language, that uses the names and terms."
```
Builds a 720p proxy, extracts 16 kHz audio, transcribes on the host with a
**multilingual** mlx_whisper model with word timestamps (`whisper-large-v3`
when `--language` is not English, else `whisper-medium`), runs silencedetect +
scenedetect, and profiles on-screen activity (blackdetect +
a per-second motion score) into a **dead-span table** — black gaps, frozen
frames, and typing/cursor-only stretches where nothing watchable happens.
Caches everything under
`$WORKSPACE/.video-edit-cache/<source-hash>/` (or `~/.cache/video-edit/` when
no `WORKSPACE`) so re-runs are instant. **Output: prints `CONTEXT: <path>` — that's
the file you need to read next.**

**Transcript:** prep writes `transcript.json` — every word with its `start` and
`end` (and `speaker`, when the source has speaker labels) — and everything
later reads it. It comes from this plugin's `transcribe` skill (its own
`transcribe.sh`, never one found on PATH) when that skill can emit word
timings, else from prep's own `mlx_whisper` call; `context.md` names the
engine. If the large-v3 model fails to load on the host, stop and tell the
user rather than switching to a smaller model.

**Hint and corrections:** write `--prompt-hint` as a sentence in the spoken
language that uses the names and terms (from the user's request, the video's
title, and names on screen). A bare comma list ("CIN fuori, DolceBot") made
whisper-large-v3 return two words for 20 s of Italian speech; the same terms
in a sentence transcribed normally. If the transcript is far shorter than the
speech, re-run prep without the hint. Then fix what was still misheard with
`pipeline.py correct <cache> corrections.json` before any caption is rendered
(`references/reels.md` step 2).

**Audio dropouts:** prep fills gaps in the source's audio timestamps with
silence, so word times stay on source time, and prints a warning if the
extracted audio's length still differs from the source's.

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

Before declaring done, verify the render against the checks in the job's
guide. For a reel that means `pipeline.py proof <mp4> --edl <edl> --out
<png>`, one sheet of labelled frames, and the five questions in
`references/reels.md` step 7. For a
vertical reel, `pipeline.py check <mp4> --preset reels --edl <edl>` checks the
platform requirements and exits 1 on any failure.

Print the output path and total duration. If quality issues are visible (caption errors, wrong segment, etc.), iterate on the EDL and re-render — the cached prep means re-renders are fast (the bulk of time is the per-segment cut pass).

### 6. Review loop: the user comments on the video itself

When the user wants to review renders (or the job has several clips), serve
them a review page instead of asking for timestamps in chat. In the page they
click the picture to pause and comment on that moment and spot, then send
all comments in one batch.

1. **Start the server in the background** (Bash with `run_in_background`),
   pointing it at the directory holding the renders:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/video-edit/scripts/pipeline.py review <dir>
   ```

   It prints `URL:` lines, `OPEN:` and `MAILBOX:`. Give the user the `OPEN:`
   path (`<dir>/review/open.html`): opening that file sends the browser to
   the page with its access token. **Never print or read out the token**; it
   is in `open.html` and `server.token` only, both deleted when the server
   stops. Inside a container the server binds `0.0.0.0`; the first URL is
   `<hostname>.orb.local` under OrbStack, otherwise the container IP, and
   `open.html` links every URL in case the first does not load. Set
   `VIDEO_EDIT_REVIEW_URL_HOST` (e.g. `localhost` for a port published with
   `-p`) to put that host first.

2. **Watch the mailbox** with the Monitor tool, one event per comment:

   ```bash
   tail -n0 -F <dir>/review/comments.jsonl
   ```

   Each line is `{id, video, version, t, x, y, text, created_at}`: `video` is
   the clip name, `t` the time in that render in seconds, `x`/`y` the click
   position as a fraction of the width and height (0,0 is top left). Without
   the Monitor tool, read the file when the user says they sent comments.

3. **Wait for the batch to finish** (lines arriving within a few seconds of
   each other belong to one Send all), then map each comment to the EDL:
   `pipeline.py timeline <edl>` turns `t` back into source time; `y` near the
   caption line or the headline points at those. Make every change the batch
   asks for, then render the **next version** of that clip:

   ```bash
   PIPE=${CLAUDE_PLUGIN_ROOT}/skills/video-edit/scripts/pipeline.py
   python3 $PIPE review <dir> --next-version clip-01      # NEXT: <dir>/clip-01.v2.mp4
   python3 $PIPE render <edl> --out <dir>/clip-01.v2.mp4
   python3 $PIPE check <dir>/clip-01.v2.mp4 --preset reels --edl <edl>
   ```

   The page notices the new version within 5 s and offers it in its version
   menu; earlier versions stay so the user can compare. Tell the user in one
   line what changed in the new version.

4. Stop the server (TaskStop, or end the background task) when the user is done.

### 7. Learning loop: keep the corrections that should stick

At the end of a job, list the corrections the user made — in the review
mailbox and in chat — that would apply to the next job too (a caption size,
a colour, "never cut mid-laugh"), each as one short preference line. Leave
out ones about this video's content. Show the list and ask which to keep.
**Never write without the user's approval.** Then append the approved lines:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/video-edit/scripts/pipeline.py prefs add "Captions: 2 words per line" "…"
```

They go to `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/multiplai-media/video-edit-preferences.md`,
outside the plugin, so they survive plugin updates.

## Architecture

```
$WORKSPACE/.video-edit-cache/<source-hash>/   (or ~/.cache/video-edit/ if no $WORKSPACE)
  proxy_720p.mp4    ← 720p proxy
  audio16k.wav      ← 16 kHz mono audio
  transcript.json   ← every word with start/end (and speaker, when labelled)
  transcript.srt    ← the same, as subtitles
  sentences.json    ← sentences built from the words (clip edges snap to these)
  retakes.json      ← sentences said again soon after ("Likely retakes" in context.md)
  silences.json     ← short pauses (clip edges snap into these)
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
- `segments` — `[{src_start, src_end, speed?, zoom?, mute?, fit?, focus?}]`
  - `speed` — `>1` faster, `<1` slower; default 1.0; auto-mutes audio if >4
  - `zoom` — `{scale, x, y, hold?}` where x,y are normalized 0..1 crop position; `hold: true` permits a zoom on the final segment (otherwise render errors)
- `transitions` — `[{after, kind, duration}]` (currently only `fade`). Undeclared joins crossfade 0.5 s, except a segment that starts where the previous one ended in the source: that join is a hard cut with unbroken audio. `duration: 0` asks for a hard cut anywhere.
- `logo` — `{path, position: br|bl|tr|tl, scale, start_at}`
- `music` — `{file}` OR `{prompt}` (prompt is documented but generation is not available in CPU-only Linux containers — pass `file` or `url`)
- `output` — `{width, height, fps, crf, fit, audio_rate}` (defaults 1920×1080, 30 fps, CRF 18, fit `pad`)
  - `fit` — how a segment without a `frame` fills the output: `pad` (letterbox), `blur` (over a blurred copy), `crop` (fill, around the segment's `focus`)
  - `audio_rate` — resample the final audio (reels: `48000`)

Keys for reels (see `references/reels.md`):
- `layout` — `{panels: {A: {x, y, w, h}, B: …}, speakers: {"SPEAKER_0": "A", …}}`, rectangles in source pixels
- segment `frame` — `"stack"` (A over B), a panel name (that panel cropped to the output aspect), or `"speaker"` (follows the transcript's speaker labels); a segment with a `frame` takes no `zoom` (render refuses the pair)
- segment `fit` — `pad`, `blur` or `crop` for this segment only, overriding `output.fit` (a slide whole, the speaker cropped); not with a `frame`
- segment `focus` — `{x, y}` 0..1, the point a crop keeps in view; or a list `[{t, x, y}]` (`t` in source seconds, inside the segment, ascending) that the crop follows in straight lines, for a speaker who walks. Keys work with fit `crop` and a single panel frame.
- `captions` — `{words_per_line, max_chars, position_y, highlight, size}` — word-timed captions burned in from the transcript. `max_chars` (22) is a hard limit; `words_per_line` (5) is soft: lines break after a comma, semicolon or colon where they can, and never leave one word alone unless it ends a sentence or a pause follows
- `headline` — `{text, start, end}` in output time, shown at the top
- `brand` — path to a brand file (fonts, colours, logo)
- `transcript` — a `transcript.json` other than the prep cache's
- `overlays` — `[{html, style, start, end, mode: panel|keyed, region: {x, y, w, h}, key_color}]` — HTML/CSS motion graphics in output time (`references/motion.md`)

## What it does NOT do

- **Subtitles on a screencast.** Captions are a reels feature; a landscape demo gets none unless its EDL asks for `captions`.
- **Face tracking.** Reels frame speakers from declared panels, from speaker labels in the transcript, or from focus keys the session places after looking at frames — never by detecting faces.
- **Hosted review.** The review page is served from this machine to the user's browser; sharing it with someone elsewhere means putting the renders somewhere they can reach.
- **Posting.** It renders files; uploading or scheduling to Instagram, Facebook or TikTok is up to the user.
- **Cursor zoom-on-click.** Would require a macOS sidecar logger at record time. Out of scope.
- **AI music generation in this container.** ACE-Step pyproject hard-pins CUDA/MPS wheels — won't install on CPU-only aarch64 Linux. Use `--music-file` or `--music-url`. See `stages/music.py` `GENERATION_NOTE` for Mac/GPU install path if the user wants it there.

## Tools (all permissive licenses, all local)

| Stage | Tool | License |
|---|---|---|
| Proxy / composite / encode | ffmpeg | LGPL/GPL |
| Scene detection | PySceneDetect | BSD-3 |
| Transcription (macOS host, multilingual) | mlx_whisper + whisper-large-v3 / whisper-medium | MIT |
| Captions | libass (ffmpeg `subtitles` filter) | ISC |
| Music fetching, reference videos (URL path) | yt-dlp | Unlicense |
| Motion graphics frames | agent-browser (headless Chromium) | Apache-2.0 |
| Music generation (optional, Mac/GPU only) | ACE-Step / ACE-Step-1.5 | Apache-2.0 / MIT |
