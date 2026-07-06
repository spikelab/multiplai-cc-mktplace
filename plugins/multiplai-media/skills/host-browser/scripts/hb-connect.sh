#!/usr/bin/env bash
# hb-connect.sh — attach the agent-browser daemon to the user's REAL host Chrome.
#
# The whole point of this skill: drive the persistent, logged-in Chrome on the
# Mac — NOT the ephemeral "Chrome for Testing" that agent-browser launches by
# default. Attaching (connect) to a normally-launched Chrome is also what keeps
# navigator.webdriver === false, the single biggest anti-bot tell.
#
# It is idempotent: safe to run at the start of every browser session.
#
#   ${CLAUDE_PLUGIN_ROOT}/skills/host-browser/scripts/hb-connect.sh [PORT]
#
# Default PORT is 9222 (the `chrome-agent` alias on the Mac). Exit 0 = the
# daemon is attached to a real Chrome with webdriver=false.
set -euo pipefail

PORT="${1:-9222}"
HOST="${AB_HOST:-host.docker.internal}"

ab() { command ab "$@"; }

fail() { printf '✗ %s\n' "$*" >&2; exit 1; }

# 0. `ab` (agent-browser bridge) must be installed. Without it there is no way to
#    reach the host browser — bail with a clear message, not an opaque
#    "command not found" three lines down.
if ! command -v ab >/dev/null 2>&1; then
  cat >&2 <<'EOF'
✗ `ab` (agent-browser bridge) is not on PATH.

  This skill drives the host Chrome through agent-browser over the
  container→host SSH bridge. Install/expose `ab` first — see this skill's
  SKILL.md "Prerequisites" section — then re-run.
EOF
  exit 3
fi

# 1. Is a real Chrome exposing CDP on PORT? Probe the DevTools endpoint.
#    Prefer the container→host SSH bridge; fall back to a direct local probe
#    (the case where this runs on the Mac itself). If neither answers, the
#    bridge is down OR Chrome isn't up — say which, don't leak a raw ssh error.
ver_json="$(ssh -q -o BatchMode=yes "$HOST" "curl -s --max-time 5 http://127.0.0.1:${PORT}/json/version" 2>/dev/null || true)"
if [ -z "$ver_json" ]; then
  # Fallback: maybe CDP is reachable locally (running directly on the host).
  ver_json="$(curl -s --max-time 5 "http://127.0.0.1:${PORT}/json/version" 2>/dev/null || true)"
fi
if [ -z "$ver_json" ]; then
  cat >&2 <<EOF
✗ No Chrome DevTools endpoint reachable on 127.0.0.1:${PORT}
  (tried the container→host SSH bridge via ${HOST}, then a direct local probe).

  Two possible causes:
    1. The host Chrome isn't exposing CDP. On the Mac, run ONCE:
         chrome-agent                # alias: launch real Chrome + CDP on ${PORT}
       (launches your normal profile, so your logins carry over.)
    2. The container→host SSH bridge isn't configured/reachable. Verify you can
         ssh ${HOST}
       and that agent-browser is installed on the host.

  Fix whichever applies, then re-run this script.
EOF
  exit 2
fi

browser="$(printf '%s' "$ver_json" | sed -n 's/.*"Browser": *"\([^"]*\)".*/\1/p')"
[ -n "$browser" ] && printf '• Host CDP %s reachable: %s\n' "$PORT" "$browser"

# 2. Bind the persistent daemon to that Chrome.
ab connect "$PORT" >/dev/null 2>&1 || fail "agent-browser connect $PORT failed (daemon issue — try: ab doctor)"

# 3. Assert we're driving a non-automated, real browser.
probe="$(printf '({wd:navigator.webdriver, ua:navigator.userAgent, lang:navigator.languages.join(",")})' | ab eval --stdin 2>/dev/null || true)"
wd="$(printf '%s' "$probe" | sed -n 's/.*"wd": *\([a-z]*\).*/\1/p')"
[ "$wd" = "false" ] || fail "navigator.webdriver=$wd — attached to an automated/launched Chrome, not the real one. Aborting."

url="$(ab get url 2>/dev/null || echo '?')"
printf '✓ Attached to real Chrome on %s (webdriver=false). Current tab: %s\n' "$PORT" "$url"
printf '  Tabs:\n'
ab tab 2>/dev/null | sed 's/^/    /'
