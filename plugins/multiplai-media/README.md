# multiplai-media

Media skill pack for Claude Code: **audio transcription, YouTube transcripts,
screen-recording demo videos, diagrams, and host-browser automation**. Part of
the [`multiplai`](../../README.md) marketplace.

## Installation

```
/plugin marketplace add spikelab/multiplai-cc-mktplace
/plugin install multiplai-media@multiplai
```

## Skills

| Skill | What it does |
|-------|--------------|
| `transcribe` | Transcribe audio files (.mp3, .m4a, .wav, …) to text using mlx-whisper. |
| `youtube-transcript` | Download transcripts from YouTube videos — subtitle download (fast) with audio-transcription fallback. |
| `screen-demo` | Turn a raw screen recording into a polished 1–3 minute product demo video — ffmpeg + PySceneDetect editing, mlx-whisper transcription. Free and local, no SaaS. |
| `excalidraw` | Generate and iteratively refine Excalidraw diagrams for architecture and design exploration. |
| `host-browser` | Drive the user's real logged-in Chrome on the macOS host (via the `ab`/agent-browser bridge) — logins, forms, JS/bot-walled pages, signups. |

## Composition

- `transcribe` composes upstream of `pm-jtbd-synthesis` (multiplai-pm): audio →
  text → discovery synthesis.

## Compatibility

- `excalidraw` — vanilla Claude Code, any OS.
- `youtube-transcript` — subtitle path works anywhere; the audio-transcription
  fallback needs Apple-Silicon macOS (mlx-whisper) or the multiplai-kit SSH bridge.
- `transcribe` — mlx-whisper needs Apple-Silicon macOS; from the kit container,
  the SSH bridge. On plain Linux use whisper.cpp / faster-whisper instead.
- `screen-demo` — needs ffmpeg + mlx-whisper on a Mac; from the kit container,
  the SSH bridge.
- `host-browser` — needs the multiplai-kit container→host SSH bridge; on a Mac a
  local CDP Chrome also works. Page content it reads is externally-authored text,
  delivered fenced as data — see the
  [untrusted-content contract](../../docs/untrusted-content.md).

Full details: [compatibility matrix](../../README.md#compatibility-matrix) and
the [degradation contract](../../docs/degradation-contract.md).
