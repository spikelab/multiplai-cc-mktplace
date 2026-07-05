#!/bin/bash
# Transcribe audio using mlx-whisper
# Detects local (macOS) vs remote (container) and routes accordingly.
# In containers, bridges to macOS host via SSH for Metal GPU access.
# Usage: transcribe.sh <audio_file> [output_file] [--override] [--model <model_name>]

set -e

# Default models
DEFAULT_MODEL_EN="mlx-community/whisper-medium.en-mlx-8bit"
DEFAULT_MODEL_MULTI="mlx-community/whisper-medium-mlx"

# --- SSH Configuration (for container → host bridge) ---
TRANSCRIBE_HOST="${TRANSCRIBE_HOST:-host.docker.internal}"
# Default to the build-bridge user, not $USER ($USER is empty in-container, which
# produces a malformed "@host" ssh destination). Mirrors swift-host.sh.
TRANSCRIBE_USER="${TRANSCRIBE_USER:-${SSH_BUILD_USER:-}}"
if [ -z "$TRANSCRIBE_USER" ]; then
  echo "Error: no SSH user for the container→host bridge." >&2
  echo "  Set SSH_BUILD_USER (or TRANSCRIBE_USER) in .env — see .env.example." >&2
  exit 1
fi
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

run_on_host() {
  if [ "$IS_CONTAINER" = "false" ]; then
    eval "$1"
  else
    if [ -z "$TRANSCRIBE_KEY" ]; then
      echo "ERROR: No SSH key found. Set TRANSCRIBE_KEY or place key at ~/.ssh/build_key" >&2
      echo "MLX Whisper requires macOS Metal GPU — cannot run in container." >&2
      exit 1
    fi
    ssh -q -o StrictHostKeyChecking=no -o BatchMode=yes \
      -i "$TRANSCRIBE_KEY" \
      "${TRANSCRIBE_USER}@${TRANSCRIBE_HOST}" \
      "$1"
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
    echo "Error: No audio file specified"
    echo "Usage: transcribe.sh <audio_file> [output_file] [--override] [--model <model_name>]"
    exit 1
fi

if [[ ! -f "$AUDIO_FILE" ]]; then
    echo "Error: Audio file not found: $AUDIO_FILE"
    exit 1
fi

# Generate default output filename if not specified
if [[ -z "$OUTPUT_FILE" ]]; then
    # Remove extension and add .txt
    OUTPUT_FILE="${AUDIO_FILE%.*}-scribed.txt"
fi

# Check if output file exists
if [[ -f "$OUTPUT_FILE" ]] && [[ "$OVERRIDE" != true ]]; then
    echo "Error: Output file already exists: $OUTPUT_FILE"
    echo "Use --override flag to overwrite existing file"
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

# Build mlx_whisper command with optional flags
MLX_CMD="mlx_whisper --model \"$MODEL\" --output-dir \"$OUT_DIR\" --output-name \"$OUT_STEM\" --output-format txt"
[[ -n "$TASK" ]] && MLX_CMD="$MLX_CMD --task \"$TASK\""
[[ -n "$LANGUAGE" ]] && MLX_CMD="$MLX_CMD --language \"$LANGUAGE\""
MLX_CMD="$MLX_CMD \"$AUDIO_FILE\""

run_on_host "$MLX_CMD"

# mlx_whisper always writes <stem>.txt; rename if user requested a different extension.
GENERATED="$OUT_DIR/$OUT_STEM.txt"
if [[ "$GENERATED" != "$OUTPUT_FILE" ]]; then
    mv "$GENERATED" "$OUTPUT_FILE"
fi

echo ""
echo "Transcription complete: $OUTPUT_FILE"
