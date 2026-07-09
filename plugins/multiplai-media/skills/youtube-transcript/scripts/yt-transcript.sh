#!/bin/bash
# Download YouTube transcript (subtitles or audio transcription fallback)
# Detects local (macOS) vs remote (container) and routes mlx-whisper via SSH.
# Usage: yt-transcript.sh <youtube_url> [output_file] [--timestamps] [--audio-fallback]
#
# Priority: manual subs → auto-generated subs → audio download + transcribe
# Audio fallback requires ffmpeg and the transcribe skill's mlx-whisper setup.

set -euo pipefail

# --- SSH Configuration (for container → host bridge) ---
# Only consulted when running inside a container and the SSH bridge is actually
# used (see run_on_host). Subtitle-only downloads and local macOS transcription
# never need these.
TRANSCRIBE_HOST="${TRANSCRIBE_HOST:-host.docker.internal}"
TRANSCRIBE_USER="${TRANSCRIBE_USER:-${SSH_BUILD_USER:-}}"
TRANSCRIBE_KEY="${TRANSCRIBE_KEY:-}"

# Key discovery (same pattern as swift-host.sh)
if [ -z "$TRANSCRIBE_KEY" ]; then
  for candidate in /home/agent/.ssh/build_key "$HOME/.ssh/build_key"; do
    if [ -f "$candidate" ]; then
      TRANSCRIBE_KEY="$candidate"
      break
    fi
  done
fi

# --- Environment detection ---
IS_CONTAINER=false
if [ "$(uname -s)" != "Darwin" ]; then
  IS_CONTAINER=true
fi

# Run a command given as an argv array (NOT a shell string). Locally we exec the
# argv directly — no eval, so an attacker-controlled arg (e.g. a video title)
# cannot inject shell (CWE-78). Over SSH the transport forces a single string, so
# we shell-quote every arg with printf '%q' (the same discipline as the `ab`
# wrapper) and let the remote shell re-parse it as literal data.
run_on_host() {
  if [ "$IS_CONTAINER" = "false" ]; then
    "$@"
  else
    if [ -z "$TRANSCRIBE_USER" ]; then
      echo "Error: no SSH user for the container→host bridge." >&2
      echo "  Set SSH_BUILD_USER or TRANSCRIBE_USER when using the container→host bridge." >&2
      exit 1
    fi
    if [ -z "$TRANSCRIBE_KEY" ]; then
      echo "ERROR: No SSH key found. Set TRANSCRIBE_KEY or place key at ~/.ssh/build_key" >&2
      echo "MLX Whisper requires macOS Metal GPU — cannot run in container." >&2
      exit 1
    fi
    local remote_cmd="" a
    for a in "$@"; do
      remote_cmd+=" $(printf '%q' "$a")"
    done
    ssh -q -o StrictHostKeyChecking=accept-new -o BatchMode=yes \
      -i "$TRANSCRIBE_KEY" \
      "${TRANSCRIBE_USER}@${TRANSCRIBE_HOST}" \
      "$remote_cmd"
  fi
}

# --- Parse arguments ---
URL=""
OUTPUT_FILE=""
TIMESTAMPS=false
AUDIO_FALLBACK=false
TASK=""
LANGUAGE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --timestamps|-t)
            # NOTE: not yet implemented — parsed for forward-compat but the
            # output is currently always de-timestamped flowing text.
            TIMESTAMPS=true
            shift
            ;;
        --audio-fallback)
            AUDIO_FALLBACK=true
            shift
            ;;
        --task)
            TASK="$2"
            shift 2
            ;;
        --language)
            LANGUAGE="$2"
            shift 2
            ;;
        -*)
            echo "Error: Unknown option: $1" >&2
            echo "Usage: yt-transcript.sh <youtube_url> [output_file] [--timestamps] [--audio-fallback] [--task <transcribe|translate>] [--language <code>]" >&2
            exit 1
            ;;
        *)
            if [[ -z "$URL" ]]; then
                URL="$1"
            elif [[ -z "$OUTPUT_FILE" ]]; then
                OUTPUT_FILE="$1"
            else
                echo "Error: Too many arguments" >&2
                exit 1
            fi
            shift
            ;;
    esac
done

# --- Validate URL ---
if [[ -z "$URL" ]]; then
    echo "Error: No YouTube URL provided" >&2
    echo "Usage: yt-transcript.sh <youtube_url> [output_file] [--timestamps] [--audio-fallback]" >&2
    exit 1
fi

# Basic URL validation — the host must be a youtube.com / youtu.be domain (not
# merely a string that contains "youtube.com" somewhere), or a bare 11-char ID.
if ! echo "$URL" | grep -qiE '^(https?://)?([a-z0-9-]+\.)*(youtube\.com|youtu\.be)([/?]|$)|^[a-zA-Z0-9_-]{11}$'; then
    echo "Error: Does not look like a YouTube URL or video ID: $URL" >&2
    exit 1
fi

# --- Resolve yt-dlp invocation ---
# Prefer `python3 -m yt_dlp`: it uses the active interpreter and is immune to a
# broken console-script shebang (happens when the venv is moved/recreated and the
# yt-dlp wrapper still points at a dead python path). Fall back to the binary.
#
# yt-dlp is NOT baked into the container image, so ensure it's present *and
# current* on every run via uv (installs into ~/.local/bin, which is on PATH).
# Running `--upgrade` each time is deliberate: YouTube breaks stale yt-dlp within
# weeks, and self-healing here beats failing at runtime on a missing/old binary.
# Non-fatal (`|| true`): if the network is down we still fall through to whatever
# is already installed. Skipped on the macOS host, where the user manages their
# own yt-dlp (brew, etc.) and we shouldn't shadow it.
if [ "$IS_CONTAINER" = "true" ] && command -v uv &>/dev/null \
   && ! python3 -c "import yt_dlp" &>/dev/null; then
    echo "[yt-transcript] Ensuring yt-dlp is installed/current (uv tool install --upgrade yt-dlp) ..." >&2
    uv tool install --upgrade yt-dlp 1>&2 || true
fi

YTDLP=""
if python3 -c "import yt_dlp" &>/dev/null; then
    YTDLP="python3 -m yt_dlp"
elif command -v yt-dlp &>/dev/null && yt-dlp --version &>/dev/null; then
    YTDLP="yt-dlp"
elif command -v yt-dlp &>/dev/null; then
    echo "Error: yt-dlp is installed but not runnable (broken shebang?) and the" >&2
    echo "       yt_dlp module isn't importable by python3." >&2
    echo "Fix with: uv tool install --force yt-dlp" >&2
    exit 1
else
    echo "Error: yt-dlp is not installed and could not be auto-installed." >&2
    echo "  Container: 'uv tool install --upgrade yt-dlp' (needs network + uv on PATH)." >&2
    echo "  macOS host: 'brew install yt-dlp' or 'uv tool install yt-dlp'." >&2
    exit 1
fi

# --- Set up temp directory (cleaned up on exit) ---
# In containers, use INBOX/ in the workspace so the host can access audio files via SSH.
# /tmp is container-local and invisible to the macOS host.
if [ "$IS_CONTAINER" = "true" ]; then
  # INBOX is the workspace landing zone — always on the shared mount
  CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  WS_FILE="$CONFIG_DIR/.workspace"
  if [ -f "$WS_FILE" ]; then
    INBOX_DIR="$(cat "$WS_FILE")/INBOX"
  else
    INBOX_DIR="$HOME/INBOX"
  fi
  mkdir -p "$INBOX_DIR"
  TMPDIR_WORK=$(mktemp -d -p "$INBOX_DIR" .yt-transcript-XXXXXX)
else
  TMPDIR_WORK=$(mktemp -d)
fi
trap 'rm -rf "$TMPDIR_WORK"' EXIT

# --- Get video title and ID for naming ---
echo "[yt-transcript] Fetching video info ..."
VIDEO_TITLE=$($YTDLP --print "%(title)s" --no-download "$URL" 2>/dev/null) || {
    echo "Error: Could not fetch video info. Check the URL and your network." >&2
    exit 1
}
VIDEO_ID=$($YTDLP --print "%(id)s" --no-download "$URL" 2>/dev/null)

# Sanitize title for filesystem
SAFE_TITLE=$(echo "$VIDEO_TITLE" | tr '/:*?"<>|\\' '-' | sed 's/  */ /g' | sed 's/^[.-]*//' | head -c 200)

echo "[yt-transcript] Video: $VIDEO_TITLE"

# --- Default output file ---
if [[ -z "$OUTPUT_FILE" ]]; then
    OUTPUT_FILE="${SAFE_TITLE}-transcript.txt"
fi

# Confine the final transcript to the workspace (defense-in-depth): the container
# must not write outside $WORKSPACE. Resolve relative names against the container
# cwd (never leave them to resolve host-side). The host-touching audio I/O already
# lives under TMPDIR_WORK (INBOX, inside the workspace); this guards the final
# output path too. Local macOS runs are exempt; fail-open if the marker is missing.
if [ "$IS_CONTAINER" = "true" ]; then
    WORKSPACE="$(cat "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.workspace" 2>/dev/null || true)"
    if [ -n "$WORKSPACE" ]; then
        OUTPUT_FILE="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$OUTPUT_FILE")"
        case "$OUTPUT_FILE/" in
            "$WORKSPACE"/*) ;;
            *)
                echo "Error: transcript output is confined to the workspace." >&2
                echo "  Workspace:      $WORKSPACE" >&2
                echo "  Offending path: $OUTPUT_FILE" >&2
                exit 1 ;;
        esac
    fi
fi

# --- Try subtitle download (manual first, then auto-generated) ---
SUBS_DOWNLOADED=false
VTT_FILE=""

# Try manual subtitles
echo "[yt-transcript] Trying manual subtitles ..."
if $YTDLP --write-sub --sub-langs "en.*" --skip-download \
    --output "$TMPDIR_WORK/subs" "$URL" 2>/dev/null; then
    VTT_FILE=$(find "$TMPDIR_WORK" -name "subs*.vtt" -o -name "subs*.srt" 2>/dev/null | head -1)
    if [[ -n "$VTT_FILE" && -s "$VTT_FILE" ]]; then
        SUBS_DOWNLOADED=true
        echo "[yt-transcript] Manual subtitles found."
    fi
fi

# Try auto-generated subtitles
if [[ "$SUBS_DOWNLOADED" != true ]]; then
    echo "[yt-transcript] No manual subs. Trying auto-generated ..."
    if $YTDLP --write-auto-sub --sub-langs "en.*" --skip-download \
        --output "$TMPDIR_WORK/subs" "$URL" 2>/dev/null; then
        VTT_FILE=$(find "$TMPDIR_WORK" -name "subs*.vtt" -o -name "subs*.srt" 2>/dev/null | head -1)
        if [[ -n "$VTT_FILE" && -s "$VTT_FILE" ]]; then
            SUBS_DOWNLOADED=true
            echo "[yt-transcript] Auto-generated subtitles found."
        fi
    fi
fi

# --- Process subtitles if downloaded ---
if [[ "$SUBS_DOWNLOADED" == true ]]; then
    echo "[yt-transcript] Cleaning subtitle text ..."

    # Parse VTT/SRT, deduplicate, strip tags and timestamps
    python3 -c "
import re
import sys

seen = []
seen_set = set()

with open(sys.argv[1], 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()

        # Skip VTT headers, timestamp lines, sequence numbers
        if not line:
            continue
        if line.startswith('WEBVTT'):
            continue
        if line.startswith('Kind:') or line.startswith('Language:'):
            continue
        if '-->' in line:
            continue
        if re.match(r'^\d+$', line):
            continue

        # Strip HTML/VTT tags like <c>, </c>, <00:01:02.345>
        clean = re.sub(r'<[^>]*>', '', line)
        # Decode HTML entities
        clean = clean.replace('&amp;', '&').replace('&gt;', '>').replace('&lt;', '<').replace('&nbsp;', ' ')
        clean = clean.strip()

        if clean and clean not in seen_set:
            seen.append(clean)
            seen_set.add(clean)

# Join into flowing text (one line per caption segment)
print('\n'.join(seen))
" "$VTT_FILE" > "$OUTPUT_FILE"

    LINES=$(wc -l < "$OUTPUT_FILE" | tr -d ' ')
    echo "[yt-transcript] Done. Saved $LINES lines to: $OUTPUT_FILE"
    exit 0
fi

# --- Audio fallback ---
if [[ "$AUDIO_FALLBACK" != true ]]; then
    echo "Error: No subtitles available for this video." >&2
    echo "Re-run with --audio-fallback to download audio and transcribe locally." >&2
    echo "(Requires ffmpeg and mlx-whisper)" >&2
    exit 2
fi

echo "[yt-transcript] No subtitles available. Falling back to audio transcription ..."

# Check audio fallback dependencies
if ! command -v ffmpeg &>/dev/null; then
    echo "Error: ffmpeg is required for audio fallback but not installed." >&2
    exit 1
fi
# In containers, mlx-whisper runs on the host via SSH — skip local check
if [ "$IS_CONTAINER" = "false" ]; then
    if ! command -v mlx_whisper &>/dev/null; then
        echo "Error: mlx_whisper is required for audio fallback but not installed." >&2
        echo "Install with: pip install mlx-whisper" >&2
        exit 1
    fi
fi

# Download audio
AUDIO_FILE="$TMPDIR_WORK/${VIDEO_ID}.m4a"
echo "[yt-transcript] Downloading audio ..."
$YTDLP -x --audio-format m4a --output "$AUDIO_FILE" "$URL" 2>/dev/null || {
    echo "Error: Failed to download audio." >&2
    exit 1
}

if [[ ! -f "$AUDIO_FILE" ]]; then
    # yt-dlp sometimes appends extra extensions
    AUDIO_FILE=$(find "$TMPDIR_WORK" -name "*.m4a" -o -name "*.mp3" -o -name "*.wav" 2>/dev/null | head -1)
    if [[ -z "$AUDIO_FILE" ]]; then
        echo "Error: Audio download produced no file." >&2
        exit 1
    fi
fi

AUDIO_SIZE=$(du -h "$AUDIO_FILE" | cut -f1)
echo "[yt-transcript] Audio downloaded ($AUDIO_SIZE). Transcribing ..."

# Select model based on language/task
if [[ "$TASK" == "translate" ]] || { [[ -n "$LANGUAGE" ]] && [[ "$LANGUAGE" != "en" ]] && [[ "$LANGUAGE" != "english" ]]; }; then
    MODEL="mlx-community/whisper-medium-mlx"
else
    MODEL="mlx-community/whisper-medium.en-mlx-8bit"
fi

# mlx_whisper's --output-name is a STEM (it appends .txt) and it writes to
# --output-dir (default: cwd). Passing the full "name.txt" as the stem with no
# output-dir produced "name.txt.txt" in $HOME.
#
# CRUCIAL: mlx_whisper runs on the HOST via run_on_host, so --output-dir resolves
# on the host filesystem — against the host ssh forced-command cwd, which is NOT
# the container cwd (verified 2026-07-09: the bridge even denies `pwd`, and its cwd
# differs from the container's). A relative or non-shared OUTPUT_DIR — and the
# default OUTPUT_FILE at line ~195 is a bare relative name — would therefore write
# host-side, invisible to the container, while we print "Saved to". So we stage the
# transcript into TMPDIR_WORK, which in a container lives under INBOX/ on the shared
# mount (identical absolute path on both sides — same handling as the audio input
# above), then move it to OUTPUT_FILE container-side (so OUTPUT_FILE itself need not
# be on the shared mount).
OUTPUT_STEM="$(basename "$OUTPUT_FILE")"
OUTPUT_STEM="${OUTPUT_STEM%.txt}"

# Build mlx_whisper command as an argv array (no shell string, no eval).
MLX_ARGS=(mlx_whisper --model "$MODEL")
[[ -n "$TASK" ]] && MLX_ARGS+=(--task "$TASK")
[[ -n "$LANGUAGE" ]] && MLX_ARGS+=(--language "$LANGUAGE")
MLX_ARGS+=(--output-format txt --output-dir "$TMPDIR_WORK" --output-name "$OUTPUT_STEM" "$AUDIO_FILE")

run_on_host "${MLX_ARGS[@]}" || {
    echo "Error: Transcription failed." >&2
    exit 1
}

# Bring the staged transcript back to the requested location.
GENERATED="$TMPDIR_WORK/$OUTPUT_STEM.txt"
if [[ ! -f "$GENERATED" ]]; then
    echo "Error: Transcription produced no output at $GENERATED." >&2
    exit 1
fi
OUTPUT_PARENT="$(dirname "$OUTPUT_FILE")"
mkdir -p "$OUTPUT_PARENT"
if [[ "$GENERATED" != "$OUTPUT_FILE" ]]; then
    mv "$GENERATED" "$OUTPUT_FILE"
fi

echo "[yt-transcript] Done. Saved to: $OUTPUT_FILE"
