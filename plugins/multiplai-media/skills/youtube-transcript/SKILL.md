---
name: youtube-transcript
description: Download transcripts from YouTube videos. Use when the user provides a YouTube URL and wants the transcript, captions, subtitles, or text content from the video. Supports subtitle download (fast) with audio transcription fallback.
model: opus
effort: low
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
| 1 | Error (missing deps, bad URL, download failed) |
| 2 | No subtitles available and `--audio-fallback` not set — tell the user and offer to re-run with the flag |

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

## Dependencies

- **yt-dlp** — required (subtitle download and audio extraction); auto-installed/updated only inside the multiplai container (`MULTIPLAI_CONTAINER=1`)
- **python3** — required (VTT cleanup)
- **ffmpeg** — required only for audio fallback
- **mlx-whisper** — required only for audio fallback

## In Case of Errors

If the script returns an error, show it to the user verbatim. Common issues:
- yt-dlp not installed (exit 3) → user installs it: `brew install yt-dlp` (macOS) or `uv tool install yt-dlp`
- Video is private/geo-blocked → nothing we can do, tell user
- Network issues → suggest retry

## Resources

- `${CLAUDE_PLUGIN_ROOT}/skills/youtube-transcript/scripts/yt-transcript.sh` — Main script with subtitle download and VTT cleanup
