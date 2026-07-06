#!/bin/bash
# swift-host.sh — Bridge for running Swift/Xcode commands on macOS host.
# Detects local (macOS) vs remote (container) and routes accordingly.
# All commands are compatible with the SSH gateway allowlist.
#
# Usage: swift-host.sh <command> [args...]
#   build              Build the project
#   test [--filter X]  Run tests (optionally filtered)
#   sim list           List available simulators
#   sim boot <name>    Boot a simulator
#   sim screenshot [p] Take screenshot of booted simulator

set -euo pipefail

# --- Configuration ---
SWIFT_BUILD_HOST="${SWIFT_BUILD_HOST:-host.docker.internal}"
SWIFT_BUILD_USER="${SWIFT_BUILD_USER:-${SSH_BUILD_USER:-}}"
if [ -z "$SWIFT_BUILD_USER" ]; then
  echo "Error: no SSH user for the container→host bridge." >&2
  echo "  Set SSH_BUILD_USER (or SWIFT_BUILD_USER) in your kit root .env." >&2
  exit 1
fi
SWIFT_BUILD_KEY="${SWIFT_BUILD_KEY:-}"

# Key discovery
if [ -z "$SWIFT_BUILD_KEY" ]; then
  for candidate in /home/agent/.ssh/build_key "$HOME/.ssh/build_key"; do
    if [ -f "$candidate" ]; then
      SWIFT_BUILD_KEY="$candidate"
      break
    fi
  done
fi

# Shell-quote a value for safe interpolation into a command string that
# run_on_host later re-parses (via `eval` locally or the remote ssh shell).
# Without this, a scheme/path/filter containing spaces or shell metacharacters
# would break the command or inject (CWE-78).
q() { printf '%q' "$1"; }

# --- Environment detection ---
run_on_host() {
  if [ "$(uname -s)" = "Darwin" ]; then
    # Local macOS — run directly
    eval "$1"
  else
    # Remote (container) — SSH to host
    if [ -z "$SWIFT_BUILD_KEY" ]; then
      echo "ERROR: No SSH key found. Set SWIFT_BUILD_KEY or place key at ~/.ssh/build_key" >&2
      exit 1
    fi
    ssh -q -o StrictHostKeyChecking=accept-new -o BatchMode=yes \
      -i "$SWIFT_BUILD_KEY" \
      "${SWIFT_BUILD_USER}@${SWIFT_BUILD_HOST}" \
      "$1"
  fi
}

# --- Project detection ---
PROJECT_TYPE=""
detect_project() {
  if [ -f "Package.swift" ] && ls *.xcodeproj &>/dev/null; then
    PROJECT_TYPE="hybrid"
  elif [ -f "Package.swift" ]; then
    PROJECT_TYPE="swiftpm"
  elif ls *.xcodeproj &>/dev/null || ls *.xcworkspace &>/dev/null; then
    PROJECT_TYPE="xcode"
  else
    echo "ERROR: No Package.swift, .xcodeproj, or .xcworkspace found in $(pwd)" >&2
    exit 1
  fi
}

# --- Xcode scheme discovery ---
discover_scheme() {
  local list_output
  list_output=$(run_on_host "cd $(q "$(pwd)") && xcodebuild -list -quiet 2>/dev/null") || true
  echo "$list_output" | sed -n '/Schemes:/,/^$/p' | grep -v 'Schemes:' | head -1 | xargs
}

# --- xcsift wrapping ---
# Pipes output through xcsift if available; falls back to raw output.
# Uses TOON format (30-60% fewer tokens) with --quiet (suppress clean passes).
pipe_xcsift() {
  if command -v xcsift &>/dev/null; then
    xcsift --format toon --quiet
  elif [ "$(uname -s)" != "Darwin" ]; then
    # Remote: check if host has xcsift
    if run_on_host "command -v xcsift" &>/dev/null; then
      # xcsift is on host — caller should pipe on host side
      cat
    else
      echo "WARNING: xcsift not installed — showing raw output" >&2
      cat
    fi
  else
    echo "WARNING: xcsift not installed — showing raw output" >&2
    cat
  fi
}

# Build the command string with optional xcsift piping on the host side.
# For SwiftPM commands, uses --package-path to avoid cd (gateway-friendly).
# For Xcode commands, uses cd (xcodebuild has no --package-path equivalent).
build_remote_cmd() {
  local base_cmd="$1"
  local use_cd="${2:-false}"  # true for xcodebuild commands that need cd
  local xcsift_suffix=""

  # Check if host has xcsift (cache result for session)
  if run_on_host "command -v xcsift" &>/dev/null 2>&1; then
    xcsift_suffix=" 2>&1 | xcsift --format toon --quiet"
  fi

  if [ "$use_cd" = "true" ]; then
    echo "cd $(q "$(pwd)") && ${base_cmd}${xcsift_suffix}"
  else
    echo "${base_cmd}${xcsift_suffix}"
  fi
}

# --- Commands ---
cmd_build() {
  detect_project
  local build_cmd
  local use_cd="false"
  case "$PROJECT_TYPE" in
    swiftpm|hybrid)
      if [ "$(uname -s)" = "Darwin" ]; then
        build_cmd="swift build"
      else
        build_cmd="swift build --package-path $(q "$(pwd)")"
      fi
      ;;
    xcode)
      use_cd="true"
      local scheme
      scheme=$(discover_scheme)
      if [ -z "$scheme" ]; then
        echo "ERROR: Could not discover Xcode scheme" >&2
        exit 1
      fi
      build_cmd="xcodebuild -scheme $(q "$scheme") -sdk iphonesimulator build"
      ;;
  esac

  if [ "$(uname -s)" = "Darwin" ]; then
    eval "$build_cmd" 2>&1 | pipe_xcsift
  else
    run_on_host "$(build_remote_cmd "$build_cmd" "$use_cd")"
  fi
}

cmd_test() {
  detect_project
  local filter="$1"
  local test_cmd
  local use_cd="false"

  case "$PROJECT_TYPE" in
    swiftpm|hybrid)
      if [ "$(uname -s)" = "Darwin" ]; then
        test_cmd="swift test"
        if [ -n "$filter" ]; then
          test_cmd="swift test --filter $(q "$filter")"
        fi
      else
        test_cmd="swift test --package-path $(q "$(pwd)")"
        if [ -n "$filter" ]; then
          test_cmd="swift test --package-path $(q "$(pwd)") --filter $(q "$filter")"
        fi
      fi
      ;;
    xcode)
      use_cd="true"
      local scheme
      scheme=$(discover_scheme)
      if [ -z "$scheme" ]; then
        echo "ERROR: Could not discover Xcode scheme" >&2
        exit 1
      fi
      test_cmd="xcodebuild -scheme $(q "$scheme") -sdk iphonesimulator test"
      if [ -n "$filter" ]; then
        test_cmd="$test_cmd -only-testing:$(q "$filter")"
      fi
      ;;
  esac

  if [ "$(uname -s)" = "Darwin" ]; then
    eval "$test_cmd" 2>&1 | pipe_xcsift
  else
    run_on_host "$(build_remote_cmd "$test_cmd" "$use_cd")"
  fi
}

cmd_sim() {
  local subcmd="${1:-}"
  shift || true

  case "$subcmd" in
    list)
      run_on_host "xcrun simctl list devices available"
      ;;
    boot)
      local name="$1"
      if [ -z "$name" ]; then
        echo "ERROR: sim boot requires a device name" >&2
        exit 1
      fi
      run_on_host "xcrun simctl boot $(q "$name")"
      ;;
    screenshot)
      local path="${1:-/tmp/simulator-screenshot.png}"
      run_on_host "xcrun simctl io booted screenshot $(q "$path")"
      echo "Screenshot saved to: $path"
      ;;
    open)
      # Open Simulator.app GUI (so user can see/interact with the device)
      run_on_host "open -a Simulator"
      echo "Simulator.app opened"
      ;;
    install)
      local app_path="$1"
      if [ -z "$app_path" ]; then
        echo "ERROR: sim install requires an app path (.app bundle)" >&2
        exit 1
      fi
      run_on_host "xcrun simctl install booted $(q "$app_path")"
      echo "Installed: $app_path"
      ;;
    launch)
      local bundle_id="$1"
      if [ -z "$bundle_id" ]; then
        echo "ERROR: sim launch requires a bundle identifier" >&2
        exit 1
      fi
      run_on_host "xcrun simctl launch booted $(q "$bundle_id")"
      echo "Launched: $bundle_id"
      ;;
    shutdown)
      local name="${1:-booted}"
      run_on_host "xcrun simctl shutdown $(q "$name")"
      echo "Shutdown: $name"
      ;;
    *)
      echo "ERROR: Unknown sim subcommand: $subcmd" >&2
      echo "Usage: swift-host.sh sim {list|boot|shutdown|open|install|launch|screenshot}" >&2
      exit 1
      ;;
  esac
}

# --- Main ---

# Parse global flags (before command)
PACKAGE_PATH=""
while [ $# -gt 0 ]; do
  case "$1" in
    --package-path)
      PACKAGE_PATH="$2"
      shift 2
      ;;
    *)
      break
      ;;
  esac
done

# If --package-path given, cd to it for project detection
if [ -n "$PACKAGE_PATH" ]; then
  if [ ! -d "$PACKAGE_PATH" ]; then
    echo "ERROR: --package-path directory does not exist: $PACKAGE_PATH" >&2
    exit 1
  fi
  cd "$PACKAGE_PATH"
fi

COMMAND="${1:-}"
shift || true

case "$COMMAND" in
  build)      cmd_build ;;
  test)
    FILTER=""
    # Parse test-specific flags
    while [ $# -gt 0 ]; do
      case "$1" in
        --filter) FILTER="${2:-}"; shift 2 || break ;;
        --package-path)
          # Also accept --package-path after the command (convenience)
          if [ -z "$PACKAGE_PATH" ]; then
            PACKAGE_PATH="$2"
            cd "$PACKAGE_PATH"
          fi
          shift 2
          ;;
        *) break ;;
      esac
    done
    cmd_test "$FILTER"
    ;;
  sim)        cmd_sim "$@" ;;
  *)
    echo "Usage: swift-host.sh [--package-path <dir>] {build|test|sim} [args...]"
    echo ""
    echo "Commands:"
    echo "  build                        Build the project"
    echo "  test [--filter X]            Run tests (optionally filtered)"
    echo "  sim list                     List available simulators"
    echo "  sim boot <name>              Boot a simulator"
    echo "  sim shutdown [name]          Shutdown a simulator (default: booted)"
    echo "  sim open                     Open Simulator.app GUI window"
    echo "  sim install <path>           Install .app bundle on booted simulator"
    echo "  sim launch <bundle-id>       Launch app by bundle identifier"
    echo "  sim screenshot [path]        Take screenshot of booted simulator"
    echo ""
    echo "Options:"
    echo "  --package-path <dir>         Path to Swift package (default: cwd)"
    exit 1
    ;;
esac
