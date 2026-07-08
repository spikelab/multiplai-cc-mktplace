#!/usr/bin/env bash
# One-shot setup for qmd-based resources retrieval (resources_retrieval=qmd).
#
# RUN THIS WHERE QMD WILL EXECUTE: on the machine itself for native installs
# (qmd_mode=local), or on the Mac HOST for container setups (qmd_mode=ssh —
# llama.cpp needs Metal; container CPU is ~50x slower).
#
#   bash setup_qmd.sh --workspace <root> --resources-dir <dir> [--collection resources]
#
# Idempotent. Does, in order:
#   1. install bun + qmd if missing
#   2. create the project-local .qmd index at the workspace root
#   3. add the resources collection and index + embed it (with retry passes —
#      qmd embed can die mid-run with "LLM session expired" but is incremental)
#   4. run a smoke query
#
# Container setups additionally need the qmd allowlist in the host SSH-bridge
# gateway (container-build-gateway.sh from multiplai-container) — deploy that
# separately on the host:  cp container-build-gateway.sh ~/.local/bin/
set -euo pipefail

WORKSPACE=""
RESOURCES_DIR=""
COLLECTION="resources"

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace)     WORKSPACE="$2"; shift 2 ;;
    --resources-dir) RESOURCES_DIR="$2"; shift 2 ;;
    --collection)    COLLECTION="$2"; shift 2 ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done
[ -n "$WORKSPACE" ] && [ -n "$RESOURCES_DIR" ] || {
  echo "usage: setup_qmd.sh --workspace <root> --resources-dir <dir> [--collection <name>]"
  exit 1
}
[ -d "$RESOURCES_DIR" ] || { echo "ERROR: resources dir not found: $RESOURCES_DIR"; exit 1; }

# 1. bun + qmd
export PATH="$HOME/.bun/bin:$PATH"
command -v bun >/dev/null || {
  echo "Installing bun..."
  curl -fsSL https://bun.sh/install | bash
  export PATH="$HOME/.bun/bin:$PATH"
}
if ! command -v qmd >/dev/null; then
  echo "Installing qmd..."
  bun install -g @tobilu/qmd
  bun pm -g trust --all
fi
echo "qmd: $(command -v qmd) ($(qmd --version))"

# SSH-bridge sessions exec through `zsh -lc`; make sure qmd resolves there
# even if ~/.bun/bin isn't on the login-shell PATH (macOS hosts only).
if [ "$(uname)" = "Darwin" ] && ! zsh -lc 'command -v qmd' >/dev/null 2>&1; then
  ln -sf "$(command -v qmd)" /opt/homebrew/bin/qmd
  echo "symlinked qmd into /opt/homebrew/bin"
fi

# 2. Project-local index (for ssh mode: host + container see the same
# absolute path, so the container-side hook cwd resolves the same index)
cd "$WORKSPACE"
[ -d .qmd ] || qmd init

# 3. Collection + index + embed (retry loop: embed is incremental)
qmd collection list 2>/dev/null | grep -q "$COLLECTION" || \
  qmd collection add "$RESOURCES_DIR" --name "$COLLECTION"
qmd update
prev=-1
for i in 1 2 3 4 5; do
  left="$(qmd status 2>/dev/null | grep -oE '[0-9]+ need embedding' | grep -oE '[0-9]+' || echo 0)"
  [ "$left" -eq 0 ] && break
  [ "$left" = "$prev" ] && { echo "WARNING: embed stalled at $left pending"; break; }
  prev="$left"
  echo "embed pass $i ($left pending)"
  qmd embed
done
qmd status | head -12

# 4. Smoke query
echo; echo "--- smoke query ---"
qmd search "the" -c "$COLLECTION" -n 3 2>/dev/null | head -8 || true
qmd vsearch "setup and configuration" -c "$COLLECTION" -n 3 | head -12
echo; echo "Done. Set plugin options: enable_resources=true resources_retrieval=qmd"
echo "(and qmd_mode=ssh for container setups)."
