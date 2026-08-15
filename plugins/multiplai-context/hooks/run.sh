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
#   1. Find uv. When it is missing, a *session* hook degrades to a single,
#      rate-limited "install uv" warning and exits 0 — a missing tool must
#      never break the session. `--warm` is the exception and fails: see
#      job 2.
#   2. `--warm` (the Setup hook: `claude --init-only`, or `--init` /
#      `--maintenance` in -p mode): build the scripts/ environment
#      synchronously from the lock and exit with uv's status. One-time
#      preparation with a generous timeout — the right place for the
#      full first resolution in CI or scripted installs. Its whole job is
#      to leave a working environment behind, so with no uv it reports
#      failure rather than passing a gate over an install that cannot run.
#   3. Cold start (uv cannot run anything against scripts/ yet): a first
#      `uv run` would do the full dependency resolution inline — including
#      a git clone of multiplai-core — and the hook timeouts (60s
#      SessionStart, 30s/10s UserPromptSubmit) can kill it mid-flight,
#      leaving a half-built environment and a silently dead hook. So spawn
#      the build DETACHED (it survives this hook exiting), guard against
#      concurrent spawns with an atomic mkdir marker (SessionStart + two
#      UserPromptSubmit hooks all fire cold at once), tell the model what
#      is happening in one context line, and exit 0. Hooks whose work has
#      no second chance are re-run when the build lands (see "Nothing is
#      dropped" below).
#   4. Warm path: exec the hook script under uv against the plugin's
#      scripts/ member.
#
# Readiness is uv's answer, not a path guess. Where the environment lives
# is uv's decision — a workspace root above the plugin moves it to that
# root, and UV_PROJECT_ENVIRONMENT relocates it anywhere at all — so this
# script asks uv the same question its consumer asks:
#
#     uv run --project "$sdir" --frozen --no-sync python -c 'import multiplai_core'
#
# `--frozen --no-sync` neither re-resolves nor installs, so the probe costs
# ~50ms, writes nothing, and does not block on a build already holding uv's
# project lock. The import is the load-bearing part: `python -c ''` succeeds
# inside the empty venv uv creates on the spot, which would read every cold
# start as warm. Guessing the path instead (the previous shape) mis-read a
# relocated environment as permanently cold: every fire spawned another
# detached build and no hook ever ran.
#
# The marker (`scripts/.warmup/`) records who is building and how it ended,
# because its mtime alone answers neither. See "Marker triage" below.
#
# Nothing is dropped (P3): a cold fire cannot run session_end.py or
# pre_compact.py, and those enqueue the deferred-extraction marker that is
# the only record of a session's diary and learnings. Losing them because
# the environment was a minute from ready loses the session with nothing
# anywhere to say so. Such a hook stashes its payload and re-runs detached
# once the build lands; if the build never lands, it says so in
# `scripts/.warmup-deferred.log`.
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
warm="$sdir/.warmup"
build_log="$sdir/.warmup.log"
deferred_log="$sdir/.warmup-deferred.log"

# --- no uv anywhere ---------------------------------------------------------
if [ -z "$UV" ]; then
    if [ "$script" = "--warm" ]; then
        # Setup is an explicit maintenance flow whose entire job is to
        # leave a working environment behind. Exiting 0 would let
        # `claude --init-only` pass its gate in CI or a scripted install
        # over an environment that does not exist — and, by touching the
        # warn-once marker below, would suppress the very SessionStart
        # warning that is the user's only other signal. Fail loudly.
        echo "[multiplai-context] uv not found - cannot build the plugin environment. Install uv (https://docs.astral.sh/uv) and re-run: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
        exit 1
    fi
    # Warn at most once per 24h (marker file), then exit 0 — a missing tool
    # must never break the user's session.
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
fi

# Can uv run the plugin's scripts right now? See "Readiness" above.
env_ready() {
    "$UV" run --project "$sdir" --frozen --no-sync \
        python -c 'import multiplai_core' >/dev/null 2>&1
}

# Is the builder recorded in the marker still running?
builder_alive() {
    _pid=$(cat "$warm/pid" 2>/dev/null)
    [ -n "$_pid" ] || return 1
    kill -0 "$_pid" 2>/dev/null
}

# --- synchronous warm-up (Setup hook / manual pre-warm) ---------------------
if [ "$script" = "--warm" ]; then
    # Take the marker so a SessionStart or UserPromptSubmit firing during
    # this build sees one and does not detach a second uv sync against the
    # same lock. If a detached build already owns it, uv's project lock
    # makes this wait for it and then no-op.
    owned=""
    if mkdir "$warm" 2>/dev/null; then
        echo "$$" >"$warm/pid" 2>/dev/null
        owned=1
    fi
    # uv's output belongs to whoever ran Setup — stdout/stderr, not
    # .warmup.log, so this never truncates a detached builder's log.
    "$UV" sync --project "$sdir"
    rc=$?
    if [ "$rc" -eq 0 ]; then
        # The environment is ready. Clear the gate whoever owns the marker:
        # leaving it would keep every hook gated behind a build that has
        # already happened.
        rm -f "$warm/pid" "$warm/status" 2>/dev/null
        rmdir "$warm" 2>/dev/null
    elif [ -n "$owned" ]; then
        echo "$rc" >"$warm/status" 2>/dev/null
    fi
    exit "$rc"
fi

# --- warm path --------------------------------------------------------------
# Checked before the marker: a marker left behind by a failed build must not
# gate an environment that works. The PR's own recovery advice is to run
# `uv sync` by hand, and that leaves the marker in place — testing it first
# would keep hooks dead for 15 more minutes with a healthy environment.
if env_ready; then
    exec "$UV" run --project "$sdir" "$script"
fi

# --- marker triage ----------------------------------------------------------
# The marker's mtime cannot answer "is a build in flight?" on its own —
# nothing refreshes it. A first resolution slower than the window (it clones
# multiplai-core; a slow link makes that ordinary) would have its marker
# reaped out from under it, a second uv sync spawned, and the first
# builder's .warmup.log truncated mid-write. So the marker carries two
# files, and this asks those:
#
#   status present  -> the build FAILED, with uv's exit code. Keep the
#                      marker for 15 minutes to rate-limit the retry, and
#                      say "failed" rather than "in progress".
#   pid alive       -> in flight, however long it has been running.
#   pid dead/absent -> the builder died (network drop, reboot, a kill
#                      mid-resolution) or has not recorded itself yet.
#                      Reap after 15 minutes and retry.
#   over an hour    -> reap regardless. No first build takes an hour, and
#                      across a reboot a recycled pid can read as alive —
#                      this bounds that to one duplicate build rather than
#                      hooks gated forever.
build_state=""
if [ -d "$warm" ]; then
    build_state=inflight
    stale=""
    if [ -f "$warm/status" ]; then
        build_state=failed
        [ -n "$(find "$warm/status" -mmin +15 2>/dev/null)" ] && stale=1
    elif ! builder_alive; then
        [ -n "$(find "$warm" -maxdepth 0 -mmin +15 2>/dev/null)" ] && stale=1
    fi
    [ -n "$(find "$warm" -maxdepth 0 -mmin +60 2>/dev/null)" ] && stale=1
    if [ -n "$stale" ]; then
        rm -f "$warm/pid" "$warm/status" 2>/dev/null
        rmdir "$warm" 2>/dev/null
        [ -d "$warm" ] || build_state=""
    fi
fi

# --- cold start: detach the first environment build -------------------------
if [ -z "$build_state" ]; then
    if mkdir "$warm" 2>/dev/null; then
        # Winner of the atomic mkdir: build detached. All fds are detached
        # from the hook's pipes (Claude Code would wait on them);
        # diagnostics go to .warmup.log. On success the marker is removed;
        # on failure its exit code is recorded there, which both
        # rate-limits the retry and lets the next fire say "failed"
        # instead of "in progress".
        (
            "$UV" sync --project "$sdir" >"$build_log" 2>&1
            rc=$?
            if [ "$rc" -eq 0 ]; then
                rm -f "$warm/pid" "$warm/status" 2>/dev/null
                rmdir "$warm" 2>/dev/null
            else
                echo "$rc" >"$warm/status" 2>/dev/null
            fi
        ) </dev/null >/dev/null 2>&1 &
        # $! is the detached subshell's pid; it stays alive for the whole
        # build as uv's parent, so `kill -0` on it answers "still building".
        echo "$!" >"$warm/pid" 2>/dev/null
        build_state=inflight
    elif [ -d "$warm" ]; then
        build_state=inflight  # a concurrent hook won the mkdir race
    fi
    # mkdir failed with no marker present (e.g. unwritable plugin dir):
    # fall through to `uv run`, which names the real error.
fi

if [ -n "$build_state" ]; then
    if [ "$build_state" = failed ]; then
        notice="[multiplai-context] The plugin's Python environment FAILED to build - the error is in $build_log. Memory hooks stay off until it is fixed; tell the user, and that \`claude --init-only\` retries the build now (otherwise it retries by itself within 15 minutes)."
    else
        notice="[multiplai-context] First run: the plugin is building its Python environment in the background (~1 min). Memory hooks go live on the next prompt after it finishes - mention this briefly if the user wonders where their memory context is."
    fi
    case "$script" in
        *context_manager.py|*session_start.py)
            echo "$notice"
            ;;
    esac
    echo "$notice" >&2

    # Hooks whose work has no second chance: session_end.py and
    # pre_compact.py enqueue the deferred-extraction marker that carries
    # the session's diary and learnings, and session_stop.py /
    # session_notification.py record session state. Stash the payload and
    # re-run detached once the build lands, so closing a tab inside the
    # first minute does not silently drop the session.
    case "$script" in
        *session_end.py|*pre_compact.py|*session_stop.py|*session_notification.py)
            payload=""
            # Claude Code writes the JSON payload and closes stdin; the tty
            # guard is for a hand-run invocation, where there is no payload
            # and `cat` would block.
            [ -t 0 ] || payload=$(cat)
            (
                i=0
                while [ -d "$warm" ] && [ ! -f "$warm/status" ] && [ "$i" -lt 300 ]
                do
                    sleep 2
                    i=$((i + 1))
                done
                if env_ready; then
                    printf '%s' "$payload" | "$UV" run --project "$sdir" "$script"
                else
                    echo "[multiplai-context] $(date -u '+%Y-%m-%dT%H:%M:%SZ') dropped ${script##*/}: the environment never finished building - see $build_log"
                fi
            ) </dev/null >>"$deferred_log" 2>&1 &
            ;;
    esac
    exit 0
fi

exec "$UV" run --project "$sdir" "$script"
