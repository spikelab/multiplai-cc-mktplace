#!/bin/sh
# Shared launcher for every hook command in hooks.json.
#
# Invoked as:  sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" "${CLAUDE_PLUGIN_ROOT}/scripts/<hook>.py"
#         or:  sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" --warm
# (always through `sh`, so nothing depends on an executable bit surviving
# the marketplace install).
#
# Jobs, in order:
#
#   1. Find uv. When it is missing entirely, degrade to a single,
#      rate-limited "install uv" warning instead of seven silent spawn
#      failures, and exit 0 — a missing tool must never break the session.
#   2. `--warm` (the Setup hook: `claude --init-only`, or `--init` /
#      `--maintenance` in -p mode): build the scripts/ environment
#      synchronously from the lock and exit with uv's status. One-time
#      preparation with a generous timeout — the right place for the
#      full first resolution in CI or scripted installs.
#   3. Cold start on an installed copy (scripts/.venv missing): a first
#      `uv run` would do the full dependency resolution inline — including
#      a git clone of multiplai-core — and the hook timeouts (60s
#      SessionStart, 30s/10s UserPromptSubmit) can kill it mid-flight,
#      leaving a half-built environment and a silently dead hook. So spawn
#      the build DETACHED (it survives this hook exiting), guard against
#      concurrent spawns with an atomic mkdir marker (SessionStart + two
#      UserPromptSubmit hooks all fire cold at once), tell the model what
#      is happening in one context line, and exit 0. Once the venv exists,
#      hooks run normally.
#   4. Warm path: exec the hook script under uv against the plugin's
#      scripts/ member.
#
# Warning routing (P1): hook stdout is CONTEXT — for prompt-phase hooks it
# reaches the model, which is exactly where "tell the user to install uv"
# and "the environment is still building" belong. PreCompact is the
# exception: its stdout is appended to the compaction prompt as custom
# instructions, so for pre_compact.py warnings go to stderr ONLY.
#
# uv discovery (P2): `command -v uv` sees only PATH, and hooks run under a
# non-login sh whose PATH often lacks ~/.local/bin — uv's default install
# location. Before giving up, use that path directly; a uv that is
# installed but not on PATH must not read as "uv missing".

script="$1"

UV=""
if command -v uv >/dev/null 2>&1; then
    UV="uv"
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
fi

sdir="${CLAUDE_PLUGIN_ROOT}/scripts"

if [ -n "$UV" ]; then
    # --- synchronous warm-up (Setup hook / manual pre-warm) --------------
    if [ "$script" = "--warm" ]; then
        # If a detached cold-start build is already running, uv's own
        # project lock makes this wait for it and then no-op. Clear the
        # in-flight marker on success so hooks go live immediately.
        "$UV" sync --project "$sdir"
        rc=$?
        [ "$rc" -eq 0 ] && rmdir "$sdir/.warmup" 2>/dev/null
        exit "$rc"
    fi

    # --- cold start: detach the first environment build ------------------
    # Only an installed copy needs this. In the dev repo checkout the
    # workspace root two levels up owns the environment (scripts/.venv
    # never exists there), so the pre-warm must not trigger.
    warm="$sdir/.warmup"
    build_in_flight=""
    if [ ! -f "${CLAUDE_PLUGIN_ROOT}/../../uv.lock" ]; then
        # A marker much older than any plausible build means the builder
        # died (network drop, reboot). Clear it so this fire retries —
        # and at most one retry per 15 minutes.
        if [ -d "$warm" ] && [ -n "$(find "$warm" -maxdepth 0 -mmin +15 2>/dev/null)" ]; then
            rmdir "$warm" 2>/dev/null
        fi
        if [ -d "$warm" ]; then
            build_in_flight=1
        elif [ ! -d "$sdir/.venv" ]; then
            if mkdir "$warm" 2>/dev/null; then
                # Winner of the atomic mkdir: build detached. All fds are
                # detached from the hook's pipes (Claude Code would wait on
                # them); diagnostics go to .warmup.log. On success the
                # marker is removed; on failure it stays and rate-limits
                # the retry to the staleness window above.
                (
                    "$UV" sync --project "$sdir" >"$sdir/.warmup.log" 2>&1 \
                        && rmdir "$warm"
                ) </dev/null >/dev/null 2>&1 &
                build_in_flight=1
            elif [ -d "$warm" ]; then
                build_in_flight=1  # a concurrent hook won the mkdir race
            fi
            # mkdir failed with no marker present (e.g. unwritable plugin
            # dir): fall through to `uv run`, which names the real error.
        fi
    fi
    if [ -n "$build_in_flight" ]; then
        case "$script" in
            *context_manager.py|*session_start.py)
                echo "[multiplai-context] First run: the plugin is building its Python environment in the background (~1 min). Memory hooks go live on the next prompt after it finishes — mention this briefly if the user wonders where their memory context is."
                ;;
        esac
        echo "[multiplai-context] environment build in progress (first run, ~1 min); this hook activates when it completes" >&2
        exit 0
    fi

    exec "$UV" run --project "$sdir" "$script"
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
