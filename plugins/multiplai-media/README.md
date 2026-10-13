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
| `video-edit` | Edit an existing video: turn a screen recording into a 1–3 minute landscape product demo, or a long interview into vertical 9:16 reels with speaker framing, word-timed captions, a headline and optional brand styling; pick clips, draw speaker boxes, choose a look and music, and comment on renders in one local job page; animated HTML/CSS overlays in a named style, styles derived from a reference video, and corrections remembered across jobs — ffmpeg + PySceneDetect editing, mlx-whisper transcription. Free and local, no SaaS. How to use it: [`skills/video-edit/README.md`](skills/video-edit/README.md). |
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
- `video-edit` — needs ffmpeg + mlx-whisper on a Mac; from the kit container,
  the SSH bridge. Motion-graphics overlays render in a headless Chromium that
  the skill launches through Playwright; where that browser is missing,
  `render` names the `playwright install chromium-headless-shell` command.
  `capture` (a screenshot of a real web page, shown as a proof card) uses the
  same browser and fetches the page over the network; pages behind a login
  take a screenshot you saved instead (`--from-file`).
- `host-browser` — needs the multiplai-kit container→host SSH bridge **and an
  explicit opt-in on the host**: in container releases after v0.9.6 the gateway
  refuses `agent-browser` unless
  `~/.local/state/multiplai/host-browser-enabled` exists on the Mac
  (`mkdir -p ~/.local/state/multiplai && touch ~/.local/state/multiplai/host-browser-enabled`).
  This is the one bridge tool that reaches the user's real logged-in Chrome, so
  enabling the bridge does not enable it. On a Mac with no container there is no
  gateway and a local CDP Chrome works directly. Page content it reads is
  externally-authored text, delivered fenced as data — see the
  [untrusted-content contract](../../docs/untrusted-content.md).

Full details: [compatibility matrix](../../README.md#compatibility-matrix) and
the [degradation contract](../../docs/degradation-contract.md).
