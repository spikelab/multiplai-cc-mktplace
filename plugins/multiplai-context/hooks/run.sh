#!/bin/sh
# Shared launcher for every hook command in hooks.json.
#
# Invoked as:  sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" "${CLAUDE_PLUGIN_ROOT}/scripts/<hook>.py"
# (always through `sh`, so nothing depends on an executable bit surviving
# the marketplace install).
#
# One job: run the hook script under uv against the plugin's scripts/
# member — and when uv is missing, degrade to a single, rate-limited
# warning instead of seven silent spawn failures. This block used to be
# duplicated byte-identically in all seven hooks.json commands; fixing
# anything in it meant seven edits inside JSON string escaping.
#
# Warning routing (P1): hook stdout is CONTEXT — for prompt-phase hooks it
# reaches the model, which is exactly where "tell the user to install uv"
# belongs. PreCompact is the exception: its stdout is appended to the
# compaction prompt as custom instructions, so for pre_compact.py the
# warning goes to stderr ONLY.
#
# uv discovery (P2): `command -v uv` sees only PATH, and hooks run under a
# non-login sh whose PATH often lacks ~/.local/bin — uv's default install
# location. Before giving up, use that path directly; a uv that is
# installed but not on PATH must not read as "uv missing".

script="$1"

if command -v uv >/dev/null 2>&1; then
    exec uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "$script"
fi

if [ -x "$HOME/.local/bin/uv" ]; then
    exec "$HOME/.local/bin/uv" run --project "${CLAUDE_PLUGIN_ROOT}/scripts" "$script"
fi

# No uv anywhere: warn at most once per 24h (marker file), then exit 0 —
# a missing tool must never break the user's session.
d="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
if [ -d "$d" ] && [ -w "$d" ]; then
    m="$d/.multiplai-context-uv-warned"
else
    m="${TMPDIR:-/tmp}/.multiplai-context-uv-warned-${USER:-unknown}"
fi
if [ ! -f "$m" ] || [ -n "$(find "$m" -mmin +1440 2>/dev/null)" ]; then
    case "$script" in
        *pre_compact.py)
            # PreCompact stdout feeds the compaction summarizer as
            # instructions — stderr only (P1).
            ;;
        *)
            echo "[multiplai-context] uv not found - the plugin hooks are disabled until it is installed. Tell the user to install uv: https://docs.astral.sh/uv"
            ;;
    esac
    echo "[multiplai-context] uv not found - install from https://docs.astral.sh/uv (hooks disabled until then)" >&2
    touch "$m" 2>/dev/null
fi
exit 0
