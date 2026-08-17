---
name: youtube-transcript
description: Download transcripts from YouTube videos. Use when the user provides a YouTube URL and wants the transcript, captions, subtitles, or text content from the video. Supports subtitle download (fast) with audio transcription fallback.
model: opus
effort: medium
---

# YouTube Transcript

Download transcripts from YouTube videos. Tries subtitles first (fast), falls back to audio download + local transcription.

## Quick Start

Run the script:

```bash
${CLAUDE_PLUGIN_ROOT}/skills/youtube-transcript/scripts/yt-transcript.sh <youtube_url> [output_file] [--timestamps] [--audio-fallback]
```

## Workflow

1. **Get the YouTube URL** from the user's prompt
2. **Determine output path** — if user specifies one, use it. Otherwise the script auto-names from the video title.
3. **Run the script** — it tries manual subs → auto-generated subs → audio fallback (if `--audio-fallback` flag is set)
4. **Read the transcript** and present it to the user, or summarize as requested

## Options

| Flag | Purpose |
|------|---------|
| `--timestamps` / `-t` | Include timestamps in output (not yet implemented for subtitle mode) |
| `--audio-fallback` | If no subtitles exist, download audio and transcribe with mlx-whisper |

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error (bad URL, missing ffmpeg/mlx-whisper for the audio fallback, output path outside the workspace, transcription failed) |
| 2 | The video has no subtitles and `--audio-fallback` not set — tell the user and offer to re-run with the flag |
| 3 | yt-dlp is not installed |
| 4 | yt-dlp itself failed — **not** "no subtitles". Its output is printed above the error; show it verbatim |
| 5 | A subtitle track exists but holds no caption text, and `--audio-fallback` not set |

Codes 2, 4 and 5 are three different facts and must not be reported to the user
as the same one. Only 2 says anything about the video's captions.

## Example Usage

User provides a YouTube URL:
```bash
${CLAUDE_PLUGIN_ROOT}/skills/youtube-transcript/scripts/yt-transcript.sh "https://www.youtube.com/watch?v=VIDEO_ID"
```

User wants it saved to a specific file:
```bash
${CLAUDE_PLUGIN_ROOT}/skills/youtube-transcript/scripts/yt-transcript.sh "https://youtu.be/VIDEO_ID" /path/to/output.txt
```

User wants audio fallback for a video with no subtitles:
```bash
${CLAUDE_PLUGIN_ROOT}/skills/youtube-transcript/scripts/yt-transcript.sh "https://youtu.be/VIDEO_ID" --audio-fallback
```

## When exit code is 2 (no subtitles)

Tell the user: "This video has no subtitles available. I can download the audio and transcribe it locally using mlx-whisper — this is slower but works on any video. Want me to proceed?"

If yes, re-run with `--audio-fallback`.

## When exit code is 3 (yt-dlp missing)

yt-dlp is not installed and this is not the multiplai container (auto-install
only runs there — the script never installs software onto the user's own
machine as a side effect). Show the printed install instructions verbatim and
let the user install it, then re-run.

## When exit code is 4 (yt-dlp failed)

A yt-dlp call failed — fetching the video's metadata, downloading subtitles, or
downloading audio. **Do not tell the user the video has no subtitles**: the
script may never have reached the captions at all. The last 20 lines of what
yt-dlp wrote are printed above the error; show them verbatim. The usual causes
are a network problem, a private / geo-blocked / removed video, or a yt-dlp too
old for a YouTube change (`uv tool install --upgrade yt-dlp`). Offer a retry.

## When exit code is 5 (caption track with no text)

The video has a subtitle track, but it holds no cues — there was nothing to
save, and no file was written. Tell the user that, and offer `--audio-fallback`
to transcribe the audio instead.

## Dependencies

- **yt-dlp** — required (subtitle download and audio extraction); auto-installed/updated only inside the multiplai container (`MULTIPLAI_CONTAINER=1`)
- **python3** — required (VTT cleanup)
- **ffmpeg** — required only for audio fallback
- **mlx-whisper** — required only for audio fallback

## In Case of Errors

If the script returns an error, show it to the user verbatim. Common issues:
- yt-dlp not installed (exit 3) → user installs it: `brew install yt-dlp` (macOS) or `uv tool install yt-dlp`
- Video is private/geo-blocked (exit 4) → nothing we can do, tell user
- Network issues (exit 4) → suggest retry
- yt-dlp out of date for a YouTube change (exit 4) → `uv tool install --upgrade yt-dlp`

## Resources

- `${CLAUDE_PLUGIN_ROOT}/skills/youtube-transcript/scripts/yt-transcript.sh` — Main script with subtitle download and VTT cleanup
