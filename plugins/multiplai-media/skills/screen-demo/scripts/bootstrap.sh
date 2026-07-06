#!/usr/bin/env bash
# Bootstrap the screen-demo skill: build whisper.cpp + fetch model + install Python deps.
# Idempotent — safe to re-run.
set -euo pipefail

SKILL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR="$SKILL_ROOT/vendor"
mkdir -p "$VENDOR"

# 1. whisper.cpp build
if [ ! -d "$VENDOR/whisper.cpp" ]; then
  echo "→ cloning whisper.cpp"
  git clone --depth=1 https://github.com/ggml-org/whisper.cpp "$VENDOR/whisper.cpp"
fi
if [ ! -x "$VENDOR/whisper.cpp/build/bin/whisper-cli" ]; then
  echo "→ building whisper.cpp"
  command -v cmake >/dev/null || { echo "  need cmake; try: uv pip install cmake"; exit 1; }
  (cd "$VENDOR/whisper.cpp" && cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j)
fi
if [ ! -f "$VENDOR/whisper.cpp/models/ggml-small.en.bin" ]; then
  echo "→ downloading whisper small.en model (~466MB)"
  (cd "$VENDOR/whisper.cpp" && bash models/download-ggml-model.sh small.en)
fi

# 2. Python deps
if ! python3 -c "import scenedetect" 2>/dev/null; then
  echo "→ installing PySceneDetect"
  # Prefer a plain install; fall back to --user on PEP 668 externally-managed
  # environments. In a managed venv/uv env, activate it first and this just works.
  python3 -m pip install --quiet scenedetect opencv-python-headless \
    || python3 -m pip install --quiet --user scenedetect opencv-python-headless \
    || { echo "  pip install failed (PEP 668?). Install into a venv/uv env: uv pip install scenedetect opencv-python-headless"; exit 1; }
fi

echo "✓ bootstrap complete"
echo "  whisper-cli: $VENDOR/whisper.cpp/build/bin/whisper-cli"
echo "  model:       $VENDOR/whisper.cpp/models/ggml-small.en.bin"
