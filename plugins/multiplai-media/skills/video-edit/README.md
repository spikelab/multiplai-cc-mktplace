# video-edit: how to use it

video-edit turns a video you already have into edited outputs, on your own
machine, with no paid service:

- **A product demo.** A raw screen recording becomes a 1–3 minute landscape demo.
- **Vertical reels.** A long interview, podcast or talk becomes 15–90 s
  vertical (9:16) clips for Instagram, Facebook or TikTok. Each reel gets
  word-timed captions, a headline, and framing on whoever is speaking.

You do not run the commands yourself. You tell Claude what you want, and
the skill runs the pipeline and shows you the results. This page explains
what happens, what you are asked to decide, and where the files go.
`SKILL.md` is the same workflow written for Claude.

## What you need

- **ffmpeg** on the machine that runs Claude Code.
- **uv.** It provides the skill's Python dependencies (PySceneDetect and
  OpenCV); nothing is installed globally.
- **mlx-whisper on an Apple-Silicon Mac**, for transcription:
  `pip install mlx-whisper`. If Claude Code runs in a Linux container, it
  reaches the Mac over an SSH bridge (`SKILL.md`, step 0).

On first use, Claude runs `scripts/bootstrap.sh`. It installs nothing; it
checks each of the above and names whatever is missing.

## Making reels from a long recording

Say what you want, for example: *"Make three reels from
`HostShow-ep4.mp4`, in Italian, about the fines, the taxes and why people
buy houses. Use my brand file."*

1. **Prep** (minutes, once per recording). The skill:
   - transcribes the whole recording with word timings;
   - finds pauses and camera cuts;
   - caches everything, so later steps are fast.

   It asks Whisper to expect the names it can read on screen and in your
   request, written as one sentence in the spoken language.
2. **Corrections.** Misheard words would be burned into every caption, so
   Claude reads the transcript, lists the words it thinks are wrong, and
   applies `corrections.json` with `correct`. Check names and jargon here,
   before any reel is rendered.
3. **Candidates.** Claude proposes 15–90 s clips with a hook in the first
   seconds, and you pick which to make.
4. **Edit lists and render.** Each clip becomes an edit list (EDL, a small
   JSON file) and then an MP4:
   - **Video call:** Claude marks each person's window once, keeping it clear
     of name tags and watermarks, and frames whoever is talking.
   - **Keynote or TV feed:** it splits at camera cuts, shows slides whole
     over a blurred copy, and follows the speaker with a moving crop.
5. **Checks.** Every reel must pass two checks before you see it:
   - `check` verifies the platform requirements: resolution, codecs, frame
     rate, bitrate, file size and length, plus where the caption and headline
     sit.
   - `proof` makes one sheet of labelled frames. Claude answers five
     questions from it:
     - Is the person on screen the one speaking?
     - Are the captions clear of faces?
     - Are the crops free of graphics?
     - Is the brand real?
     - Is all slide text visible?

   You get the reels, the check results and those answers.
6. **Review in the browser.** Ask to review the renders, and Claude serves
   a local page:
   - Click on the picture to pause and comment on that moment and that spot
     ("caption covers his mouth", "cut here").
   - Send all your comments together.
   - Claude renders the next version, and the page offers it next to the
     old one.

   This loop is where a reel gets from "much better" to "usable".
7. **Preferences.** At the end, Claude lists the corrections that would
   apply to every job ("captions 2 words per line"). It saves only the ones
   you approve, to `~/.claude/multiplai-media/video-edit-preferences.md`,
   and reads them at the start of the next job.

## Making a product demo from a screen recording

Give the recording and a short brief: *"90 s, hook in the first 10 s, the
money shot at 2:30, lo-fi music from this URL."*

The skill finds dead time (frozen screens, typing, black frames) and cuts or
speeds it up. It adds zooms, a title card, a logo and music, and renders a
1080p landscape MP4. `references/screencast.md` holds the rules it follows.

## Animated graphics

Stat cards, lower thirds and labels are short HTML/CSS animations placed
over the video for a span of seconds. They use a named style; Claude asks
which one if you have not said. It can also derive a style from a video you
like, from a sheet of its frames. See `references/motion.md`.

## Commands

Claude runs these; they are listed so you can read what it is doing.
Every command takes `--help`.

| Command | What it does |
|---|---|
| `prep <video>` | Transcribe, find pauses and camera cuts, write the context Claude reads |
| `correct <cache> <corrections.json>` | Replace misheard words, keeping their timing |
| `snap` | Move clip edges to sentence ends and pauses |
| `timeline <edl>` | Show each segment's place and join, and what the reel will say, and when |
| `render <edl> --out <mp4>` | Render an EDL |
| `check <mp4> --preset reels` | Check a reel's resolution, codecs, bitrate, size, length and caption placement |
| `proof <mp4> --edl <edl>` | One sheet of labelled frames of a reel |
| `shots <cache> --from --to` | List the camera cuts in a span, with three frames of each |
| `frame <video> --at <s> [--grid]` | One frame as a PNG, optionally with a pixel grid |
| `review <dir>` | The local review page |
| `contact-sheet <video-or-url>` | Frames of a reference video, for deriving a style |
| `prefs show` / `prefs add` | Read or append your saved preferences |
| `outdir`, `make` | Create a job's output folder; print the whole workflow |

## Where files go

- **Caches.** Analysis caches go under `$WORKSPACE/.video-edit-cache/`, or
  `~/.cache/video-edit/` without a workspace. They are keyed by the
  recording's path, size and date, so moving or re-saving the recording
  starts a fresh prep.
- **Outputs.** Reels, EDLs and proof sheets go in the job's output folder
  (`outdir`).

## What it does not do

- **Detect faces.** Framing comes from the windows Claude marks, from
  speaker labels in the transcript, or from positions Claude reads off
  frames.
- **Tell speakers apart in the audio.** That needs speaker labels from the
  transcribe skill, which it cannot produce yet. Until then, Claude chooses
  who is on screen per segment, and the proof sheet is how you check it.
- **Choose the clips on its own.** It proposes them; you pick.
- **Post to social platforms**, or generate music inside a Linux container.
