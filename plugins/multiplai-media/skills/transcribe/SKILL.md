---
name: transcribe
description: Transcribe audio files to text using mlx-whisper. Use when the user wants to transcribe audio files (.mp3, .m4a, .wav, etc.) to text, convert speech to text, or extract text from audio recordings.
model: opus
effort: low
---

# Transcribe

Transcribe audio files to text using mlx-whisper.

## Quick Start

Run the transcription script:

```bash
${CLAUDE_PLUGIN_ROOT}/skills/transcribe/scripts/transcribe.sh <audio_file> [output_file] [--override] [--model <model_name>] [--task <transcribe|translate>] [--language <code>]
```
## Models

| Scenario | Model (auto-selected) |
|----------|----------------------|
| English audio | `mlx-community/whisper-medium.en-mlx-8bit` (default) |
| Non-English audio | `mlx-community/whisper-medium-mlx` (auto when --task translate or --language is non-English) |

The script auto-selects the right model. Override with `--model` if needed (e.g., `mlx-community/whisper-large-v3-mlx` for best quality).

## Workflow

1. **Identify the audio file** - the user will mention the path in the prompt
2. **Run transcription** using the transcription script ${CLAUDE_PLUGIN_ROOT}/skills/transcribe/scripts/transcribe.sh

## Example Usage

English audio (default):
```bash
${CLAUDE_PLUGIN_ROOT}/skills/transcribe/scripts/transcribe.sh /path/to/audio/file.m4a
```

Non-English audio → English translation:
```bash
${CLAUDE_PLUGIN_ROOT}/skills/transcribe/scripts/transcribe.sh /path/to/audio/chinese.m4a --task translate --language zh
```

Non-English audio → original language transcription:
```bash
${CLAUDE_PLUGIN_ROOT}/skills/transcribe/scripts/transcribe.sh /path/to/audio/spanish.m4a --language es
```

Override existing output:
```bash
${CLAUDE_PLUGIN_ROOT}/skills/transcribe/scripts/transcribe.sh /path/to/audio/file.m4a --override
```

Custom output file:
```bash
${CLAUDE_PLUGIN_ROOT}/skills/transcribe/scripts/transcribe.sh /path/to/audio/file.m4a /path/to/output.txt
```

## Container Support

The script auto-detects containers (`uname -s` != Darwin) and bridges to the macOS host via SSH for Metal GPU access. Same pattern as `swift-host.sh`.

Requirements for container use:
- SSH key at `/home/agent/.ssh/build_key` (or set `TRANSCRIBE_KEY`)
- `mlx-whisper` allowed in the host's SSH gateway (`~/.local/bin/container-build-gateway.sh`)
- Workspace mounted at identical paths (default with `dclaude.sh`)

## In case of errors

If `scripts/transcribe.sh` returns an Error, show that to the user and restate how to invoke the skill correctly


## Resources

- `${CLAUDE_PLUGIN_ROOT}/skills/transcribe/scripts/transcribe.sh` - Wrapper script with built-in safety checks
