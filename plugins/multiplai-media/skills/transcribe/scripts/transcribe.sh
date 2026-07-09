#!/bin/bash
# Transcribe audio using mlx-whisper
# Detects local (macOS) vs remote (container) and routes accordingly.
# In containers, bridges to macOS host via SSH for Metal GPU access.
# Usage: transcribe.sh <audio_file> [output_file] [--override] [--model <model_name>]

set -euo pipefail

# Default models
DEFAULT_MODEL_EN="mlx-community/whisper-medium.en-mlx-8bit"
DEFAULT_MODEL_MULTI="mlx-community/whisper-medium-mlx"

# --- SSH Configuration (for container → host bridge) ---
# Only consulted when running inside a container and the SSH bridge is actually
# used (see run_on_host). A fresh macOS user with mlx_whisper installed runs
# everything locally and never needs these.
TRANSCRIBE_HOST="${TRANSCRIBE_HOST:-host.docker.internal}"
# Default to the build-bridge user, not $USER ($USER is empty in-container, which
# produces a malformed "@host" ssh destination). Mirrors swift-host.sh.
TRANSCRIBE_USER="${TRANSCRIBE_USER:-${SSH_BUILD_USER:-}}"
TRANSCRIBE_KEY="${TRANSCRIBE_KEY:-${SSH_BUILD_KEY:-}}"

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

# Running locally, we need mlx_whisper on PATH. (mlx-whisper is Apple-Silicon-only;
# in a container it runs on the macOS host via the SSH bridge, checked below.)
if [ "$IS_CONTAINER" = "false" ] && ! command -v mlx_whisper &>/dev/null; then
  echo "Error: mlx_whisper is not installed (required to transcribe locally)." >&2
  echo "Install with: pip install mlx-whisper  (Apple Silicon macOS only)" >&2
  exit 1
fi

# Run a command given as an argv array (NOT a shell string). Locally we exec the
# argv directly — no eval, so an attacker-controlled path/filename cannot inject
# shell (CWE-78). Over SSH we shell-quote every arg with printf '%q' and let the
# remote shell re-parse it as literal data.
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

# Parse arguments
AUDIO_FILE=""
OUTPUT_FILE=""
OVERRIDE=false
MODEL=""
TASK=""
LANGUAGE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --override)
            OVERRIDE=true
            shift
            ;;
        --model)
            MODEL="$2"
            shift 2
            ;;
        --task)
            TASK="$2"
            shift 2
            ;;
        --language)
            LANGUAGE="$2"
            shift 2
            ;;
        *)
            if [[ -z "$AUDIO_FILE" ]]; then
                AUDIO_FILE="$1"
            elif [[ -z "$OUTPUT_FILE" ]]; then
                OUTPUT_FILE="$1"
            fi
            shift
            ;;
    esac
done

# Select model: if task is translate or language is non-English, use multilingual model
if [[ -z "$MODEL" ]]; then
    if [[ "$TASK" == "translate" ]] || { [[ -n "$LANGUAGE" ]] && [[ "$LANGUAGE" != "en" ]] && [[ "$LANGUAGE" != "english" ]]; }; then
        MODEL="$DEFAULT_MODEL_MULTI"
    else
        MODEL="$DEFAULT_MODEL_EN"
    fi
fi

# Validate audio file
if [[ -z "$AUDIO_FILE" ]]; then
    echo "Error: No audio file specified" >&2
    echo "Usage: transcribe.sh <audio_file> [output_file] [--override] [--model <model_name>]" >&2
    exit 1
fi

if [[ ! -f "$AUDIO_FILE" ]]; then
    echo "Error: Audio file not found: $AUDIO_FILE" >&2
    exit 1
fi

# Generate default output filename if not specified
if [[ -z "$OUTPUT_FILE" ]]; then
    # Remove extension and add .txt
    OUTPUT_FILE="${AUDIO_FILE%.*}-scribed.txt"
fi

# Check if output file exists
if [[ -f "$OUTPUT_FILE" ]] && [[ "$OVERRIDE" != true ]]; then
    echo "Error: Output file already exists: $OUTPUT_FILE" >&2
    echo "Use --override flag to overwrite existing file" >&2
    exit 1
fi

# Run transcription
echo "Transcribing: $AUDIO_FILE"
echo "Output: $OUTPUT_FILE"
echo "Model: $MODEL"
echo ""

# mlx_whisper expects --output-dir + --output-name (basename, no extension) + --output-format,
# not a single output path. Split $OUTPUT_FILE accordingly, then rename if the requested
# extension differs from .txt.
OUT_DIR=$(dirname "$OUTPUT_FILE")
OUT_BASENAME=$(basename "$OUTPUT_FILE")
OUT_STEM="${OUT_BASENAME%.*}"
OUT_EXT="${OUT_BASENAME##*.}"
[[ "$OUT_EXT" == "$OUT_BASENAME" ]] && OUT_EXT="txt"  # no extension → default .txt

# Build mlx_whisper command as an argv array (no shell string, no eval).
MLX_ARGS=(mlx_whisper --model "$MODEL" --output-dir "$OUT_DIR" --output-name "$OUT_STEM" --output-format txt)
[[ -n "$TASK" ]] && MLX_ARGS+=(--task "$TASK")
[[ -n "$LANGUAGE" ]] && MLX_ARGS+=(--language "$LANGUAGE")
MLX_ARGS+=("$AUDIO_FILE")

run_on_host "${MLX_ARGS[@]}"

# mlx_whisper always writes <stem>.txt; rename if user requested a different extension.
GENERATED="$OUT_DIR/$OUT_STEM.txt"
if [[ "$GENERATED" != "$OUTPUT_FILE" ]]; then
    mv "$GENERATED" "$OUTPUT_FILE"
fi

echo ""
echo "Transcription complete: $OUTPUT_FILE"
