#!/usr/bin/env bash
# Bootstrap the screen-demo skill.
#
# Transcription is NOT built here — it runs on the macOS host via mlx_whisper
# (Metal GPU) over the SSH bridge. There is no in-container whisper build and no
# compiler toolchain requirement. This script only verifies ffmpeg, ensures the
# scene-detection Python deps are importable, and preflights the host bridge.
# Idempotent — safe to re-run.
set -euo pipefail

SKILL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# 1. ffmpeg (proxy / audio extract / composite) must be present. It ships in
#    the container image; we never install it here.
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "✗ ffmpeg not found on PATH. It should be baked into the container image." >&2
  exit 1
fi
echo "✓ ffmpeg: $(command -v ffmpeg)"

# 2. Scene-detection deps (PySceneDetect + OpenCV) must be importable. Prefer
#    them baked into the image. If missing, install into a uv venv — NEVER a
#    bare `pip install` into system Python (PEP 668 externally-managed error).
if python3 -c "import scenedetect, cv2" 2>/dev/null; then
  echo "✓ scenedetect + opencv already importable"
else
  echo "→ scenedetect/opencv not importable; installing into a uv venv"
  if ! command -v uv >/dev/null 2>&1; then
    echo "✗ uv not found and scenedetect is missing." >&2
    echo "  Bake scenedetect + opencv-python-headless into the container image," >&2
    echo "  or install uv (https://docs.astral.sh/uv/) so this can create a venv." >&2
    exit 1
  fi
  VENV="$SKILL_ROOT/.venv"
  [ -d "$VENV" ] || uv venv "$VENV"
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
  uv pip install --quiet scenedetect opencv-python-headless
  python3 -c "import scenedetect, cv2" \
    || { echo "✗ scenedetect still not importable after install" >&2; exit 1; }
  echo "✓ scenedetect + opencv installed into $VENV (activate it before running the pipeline)"
fi

# 3. Preflight the host transcription bridge: confirm mlx_whisper is reachable on
#    the macOS host. Non-fatal warning if unreachable (a Mac-native run doesn't
#    need it) — but on a container this is what makes transcription work.
SSH_KEY="${TRANSCRIBE_KEY:-${SSH_BUILD_KEY:-/home/agent/.ssh/build_key}}"
SSH_HOST="${TRANSCRIBE_HOST:-host.docker.internal}"
SSH_USER="${TRANSCRIBE_USER:-${SSH_BUILD_USER:-}}"

if [ "$(uname -s)" = "Darwin" ] && command -v mlx_whisper >/dev/null 2>&1; then
  echo "✓ transcription: mlx_whisper local (Mac): $(command -v mlx_whisper)"
elif [ -n "$SSH_USER" ] && [ -f "$SSH_KEY" ]; then
  echo "→ preflight host bridge: ${SSH_USER}@${SSH_HOST}"
  if REMOTE_MLX=$(ssh -q -o StrictHostKeyChecking=accept-new -o BatchMode=yes \
        -o ConnectTimeout=10 -i "$SSH_KEY" "${SSH_USER}@${SSH_HOST}" \
        'command -v mlx_whisper' 2>/dev/null); then
    echo "✓ transcription: host mlx_whisper via SSH bridge → $REMOTE_MLX"
  else
    echo "⚠ host bridge preflight FAILED — mlx_whisper not reachable on ${SSH_HOST}." >&2
    echo "  Ensure the host has mlx_whisper (pip install mlx-whisper) and the gateway" >&2
    echo "  allowlists 'mlx_whisper'. Transcription will fail until this passes." >&2
  fi
else
  echo "⚠ no transcription backend configured: not on a Mac, and no bridge" >&2
  echo "  (need SSH_BUILD_USER/TRANSCRIBE_USER + key at $SSH_KEY)." >&2
fi

echo "✓ bootstrap complete"
