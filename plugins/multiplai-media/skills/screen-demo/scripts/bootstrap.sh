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

case "${1:-}" in
  -h|--help)
    cat <<'EOF'
bootstrap.sh — preflight the screen-demo skill. Installs nothing.

Checks, in order:
  1. ffmpeg is on PATH (hint names the fix for the actual platform)
  2. scenedetect + opencv are importable — they come from the scripts/
     project (in-repo: the root uv workspace), so run the pipeline via
     `uv run --project <skill>/scripts`
  3. the host transcription bridge reaches mlx_whisper (warning only)

Exit 0 if usable, 1 with a fix-it message otherwise. Idempotent.
EOF
    exit 0
    ;;
esac

# 1. ffmpeg (proxy / audio extract / composite) must be present. We never
#    install it here — the hint names the right fix for the actual platform.
if ! command -v ffmpeg >/dev/null 2>&1; then
  if [ "$(uname -s)" = "Darwin" ]; then
    echo "✗ ffmpeg not found on PATH. Install it with: brew install ffmpeg" >&2
  # MULTIPLAI_CONTAINER: 1 = multiplai container, 0 = explicitly not a
  # container, unset = fall back to /.dockerenv.
  elif [ "${MULTIPLAI_CONTAINER:-}" = "1" ] \
       || { [ "${MULTIPLAI_CONTAINER:-}" != "0" ] && [ -f /.dockerenv ]; }; then
    echo "✗ ffmpeg not found on PATH. It should be baked into the container image." >&2
  else
    echo "✗ ffmpeg not found on PATH. Install it with your package manager (e.g. apt install ffmpeg)." >&2
  fi
  exit 1
fi
echo "✓ ffmpeg: $(command -v ffmpeg)"

# 2. Scene-detection deps (PySceneDetect + OpenCV) must be importable. This
#    script used to create a skill-local `.venv` here when they were not. It no
#    longer does: that venv reached 229MB, was gitignored so nothing ever
#    flagged it, and was one of four such environments in this repo. The deps
#    are now declared in scripts/pyproject.toml, a member of the repo-root uv
#    workspace, so `uv run` provisions them from the single shared environment.
#    We verify and point at the fix rather than silently building a second one.
if python3 -c "import scenedetect, cv2" 2>/dev/null; then
  echo "✓ scenedetect + opencv importable"
else
  echo "✗ scenedetect/opencv not importable." >&2
  echo "  Run the pipeline through the skill scripts project, which provisions them:" >&2
  echo "    uv run --project \"$SKILL_ROOT/scripts\" python3 $SKILL_ROOT/scripts/pipeline.py …" >&2
  echo "  (or bake scenedetect + opencv-python-headless into the container image)." >&2
  exit 1
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
