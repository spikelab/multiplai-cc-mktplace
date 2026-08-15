"""Tripwire tests for the shared hook launcher ``hooks/run.sh`` (C1, P1, P2).

Claude Code does not bundle uv. Without the guard, a missing uv makes every
hook spawn-fail silently and the plugin just "doesn't work". The guard —
now factored into one ``hooks/run.sh`` instead of seven byte-identical
inline preambles — must: exit 0 (never break the session), emit one clear
install pointer, rate-limit repeats via a marker file, fall back to uv's
default install dir when PATH lacks it (P2), and keep the PreCompact hook's
stdout empty (its stdout feeds the compaction summarizer as instructions —
P1). Verified by actually running each hooks.json command with uv masked
off PATH.

``--warm`` (the Setup hook) is the one command held to the OPPOSITE
contract: it is an explicit maintenance flow whose entire job is to leave a
working environment behind, so a missing uv must fail it. Exiting 0 there
would pass the Setup gate of ``claude --init-only`` over an install that
cannot run a single hook.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from conftest import HOOKS_JSON, parse_hooks, script_hooks

RUN_SH = HOOKS_JSON.parent / "run.sh"


def _masked_env(tmp_path: Path) -> dict[str, str]:
    """Env whose PATH has sh/find/touch but no uv, with a scratch config dir."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("sh", "find", "touch"):
        real = shutil.which(tool)
        assert real, f"test needs {tool}"
        link = bin_dir / tool
        if not link.exists():
            link.symlink_to(real)
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        "PATH": str(bin_dir),
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(cfg),
        "CLAUDE_PLUGIN_ROOT": str(HOOKS_JSON.parent.parent),
    }


def _run(command: str, env: dict[str, str], stdin: str = ""):
    """Run a hooks.json command.

    ``stdin`` is always a pipe that closes immediately (Claude Code writes
    the JSON payload and closes it). run.sh reads stdin on the cold path to
    stash a lifecycle hook's payload for replay, so leaving stdin inherited
    from pytest would make these tests depend on how pytest was launched.
    """
    return subprocess.run(
        command, shell=True, env=env, input=stdin,
        capture_output=True, text=True, timeout=30,
    )


def _script_hooks():
    """The hooks that launch a Python script (excludes run.sh mode flags)."""
    return script_hooks()


class TestUvGuard:
    def test_every_hook_command_routes_through_run_sh(self):
        assert RUN_SH.is_file(), "hooks/run.sh must ship with the plugin"
        for hook in parse_hooks():
            # Invoked via `sh` so nothing depends on an executable bit
            # surviving the marketplace install.
            assert hook["command"].startswith(
                'sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" '
            ), f"{hook['event']} command must invoke run.sh: {hook['command']}"

    def test_run_sh_carries_the_guard_and_the_member_project(self):
        src = RUN_SH.read_text()
        assert "command -v uv" in src, "run.sh lost the uv guard"
        # Readiness is uv's answer, not a path guess: where the environment
        # lives is uv's decision (a workspace root moves it,
        # UV_PROJECT_ENVIRONMENT relocates it outright), so run.sh asks the
        # same question its consumer asks.
        assert "--frozen --no-sync" in src, (
            "run.sh must ASK uv whether the environment is usable; guessing "
            "the venv path reads a relocated environment as permanently cold"
        )
        # Member-dir --project: "${CLAUDE_PLUGIN_ROOT}/scripts" exists both
        # in-repo (resolves via the workspace root) and on an installed
        # copy (resolves standalone). The old ../.. workspace-root form
        # does not exist on installs — ../.. may appear only in the
        # read-only dev-checkout probe, never as a --project value.
        assert 'sdir="${CLAUDE_PLUGIN_ROOT}/scripts"' in src
        assert 'run --project "$sdir"' in src
        assert '--project "${CLAUDE_PLUGIN_ROOT}/../..' not in src

    @pytest.mark.parametrize(
        "hook", script_hooks(), ids=lambda h: f"{h['event']}:{h['script']}"
    )
    def test_missing_uv_warns_once_and_exits_zero(self, hook, tmp_path):
        env = _masked_env(tmp_path)
        res = _run(hook["command"], env)
        assert res.returncode == 0, res.stderr
        if hook["script"].endswith("pre_compact.py"):
            # P1: PreCompact stdout reaches the compaction summarizer as
            # custom instructions — the warning must be stderr-only.
            assert res.stdout == "", (
                "pre_compact's uv warning leaked to stdout, where the "
                "compaction summarizer reads it as instructions"
            )
        else:
            assert "uv not found" in res.stdout
            assert "docs.astral.sh/uv" in res.stdout
        assert "uv not found" in res.stderr

    def test_missing_uv_fails_the_setup_hook(self, tmp_path):
        """Setup is not a session hook and is not held to "never break the
        session". Its whole job is to leave a working environment behind, so
        with no uv it must FAIL — otherwise `claude --init-only` passes its
        gate in CI or a scripted install over an environment that does not
        exist. It must also leave the warn-once marker alone: touching it
        would silence the next session's SessionStart warning, which is the
        only other signal the user gets."""
        env = _masked_env(tmp_path)
        warm_cmd = next(h["command"] for h in parse_hooks() if h["event"] == "Setup")
        res = _run(warm_cmd, env)
        assert res.returncode != 0, (
            "run.sh --warm reported success with no uv and nothing built"
        )
        assert "docs.astral.sh/uv" in res.stderr
        assert not list(
            Path(env["CLAUDE_CONFIG_DIR"]).glob(".multiplai-context-uv-warned*")
        ), "--warm must not consume the session hooks' warn-once budget"

    def test_warning_is_rate_limited_by_marker(self, tmp_path):
        env = _masked_env(tmp_path)
        first = _run(_script_hooks()[0]["command"], env)
        assert "uv not found" in first.stdout
        marker = Path(env["CLAUDE_CONFIG_DIR"]) / ".multiplai-context-uv-warned"
        assert marker.exists()
        for hook in _script_hooks():
            again = _run(hook["command"], env)
            assert again.returncode == 0
            assert again.stdout == "" and again.stderr == "", (
                f"{hook['event']} warned despite fresh marker"
            )

    def test_unwritable_config_dir_falls_back_to_tmp_marker(self, tmp_path):
        """An unwritable $CLAUDE_CONFIG_DIR must not turn the warning into a
        per-event repeat: the marker degrades to $TMPDIR and still
        rate-limits (warn once, then silent)."""
        env = _masked_env(tmp_path)
        cfg = Path(env["CLAUDE_CONFIG_DIR"])
        scratch_tmp = tmp_path / "scratch-tmp"
        scratch_tmp.mkdir()
        env["TMPDIR"] = str(scratch_tmp)
        cfg.chmod(0o555)  # readable, unwritable
        try:
            first = _run(_script_hooks()[0]["command"], env)
            assert first.returncode == 0
            assert "uv not found" in first.stdout
            assert not list(cfg.glob(".multiplai-context-uv-warned*")), (
                "no marker can land in the unwritable config dir"
            )
            fallback = list(scratch_tmp.glob(".multiplai-context-uv-warned*"))
            assert fallback, "marker must fall back to TMPDIR"
            again = _run(_script_hooks()[0]["command"], env)
            assert again.returncode == 0
            assert again.stdout == "" and again.stderr == "", (
                "warned again despite the TMPDIR fallback marker"
            )
        finally:
            cfg.chmod(0o755)

    def test_with_uv_present_command_reaches_uv(self, tmp_path):
        """With a fake `uv` first on PATH, the guard must exec through to it."""
        env = _masked_env(tmp_path)
        fake_uv = Path(env["PATH"]) / "uv"
        fake_uv.write_text("#!/bin/sh\necho UV_CALLED \"$@\"\nexit 0\n")
        fake_uv.chmod(0o755)
        res = _run(_script_hooks()[0]["command"], env)
        assert res.returncode == 0
        assert "UV_CALLED run --project" in res.stdout
        assert "uv not found" not in res.stdout

    def test_uv_off_path_but_in_default_install_dir_is_found(self, tmp_path):
        """P2: a non-login sh often lacks ~/.local/bin on PATH — uv's default
        install dir. That must read as "uv present", not as seven dead hooks."""
        env = _masked_env(tmp_path)
        local_bin = Path(env["HOME"]) / ".local" / "bin"
        local_bin.mkdir(parents=True)
        fake_uv = local_bin / "uv"
        fake_uv.write_text("#!/bin/sh\necho UV_CALLED \"$@\"\nexit 0\n")
        fake_uv.chmod(0o755)
        res = _run(_script_hooks()[0]["command"], env)
        assert res.returncode == 0
        assert "UV_CALLED run --project" in res.stdout
        assert "uv not found" not in res.stdout
        assert "uv not found" not in res.stderr
        assert not list(
            Path(env["CLAUDE_CONFIG_DIR"]).glob(".multiplai-context-uv-warned*")
        ), "the fallback path must not leave a warned marker"


# --- cold-start pre-warm (C3) -----------------------------------------------
# On an installed copy the first `uv run` does the FULL dependency resolution
# (including a git clone of multiplai-core) inline, and the hook timeouts
# (60s SessionStart, 30s/10s UserPromptSubmit) can kill it mid-flight. run.sh
# must instead detach the build, guard concurrent spawns with an atomic
# marker, inject one context line, and exit 0.

_FAKE_UV = """#!/bin/sh
# Recording stand-in for uv. It answers the two questions run.sh asks:
#
#   run --project <member> --frozen --no-sync python -c 'import ...'
#       the readiness probe -> exit 0 iff the environment uv would use for
#       <member> exists. Prints nothing of consequence (run.sh discards it).
#   sync --project <member>
#       builds that environment.
#
# Environment location follows uv's own precedence closely enough for these
# tests: $UV_PROJECT_ENVIRONMENT wins outright; else a workspace root two
# levels above the member owns it; else it is <member>/.venv.
printf 'UV_CALLED %s\\n' "$*" >> "$UV_LOG"

proj=""
probe=""
prev=""
for a in "$@"; do
    [ "$prev" = "--project" ] && proj="$a"
    [ "$a" = "--no-sync" ] && probe=1
    prev="$a"
done
if [ -n "$UV_PROJECT_ENVIRONMENT" ]; then
    venv="$UV_PROJECT_ENVIRONMENT"
elif [ -f "$proj/../../../uv.lock" ]; then
    venv="$proj/../../../.venv"
else
    venv="$proj/.venv"
fi

if [ -n "$probe" ]; then
    [ -d "$venv" ] || exit 1
    exit 0
fi
echo "UV_CALLED $@"
if [ "$1" = "sync" ]; then
    [ -n "$UV_SYNC_HANG" ] && sleep "$UV_SYNC_HANG"
    [ -n "$UV_SYNC_FAIL" ] && exit 7
    mkdir -p "$venv"
elif [ -n "$UV_RECORD_STDIN" ]; then
    # Prove a replayed hook still receives its payload.
    cat >> "$UV_LOG"
fi
exit 0
"""


def _prewarm_env(tmp_path: Path):
    """An installed-copy layout: plugin subtree only, no workspace above it.

    Real system PATH (sh/find/mkdir/rmdir/sleep) with a recording fake uv
    first; HOME points at an empty dir so ~/.local/bin/uv stays out of play.
    """
    plugin_root = tmp_path / "inst" / "multiplai-context"
    (plugin_root / "hooks").mkdir(parents=True)
    (plugin_root / "scripts").mkdir()
    shutil.copy(RUN_SH, plugin_root / "hooks" / "run.sh")
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(_FAKE_UV)
    fake_uv.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(home),
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "UV_LOG": str(tmp_path / "uv-calls.log"),
    }
    return env, plugin_root, tmp_path / "uv-calls.log"


def _cmd(script_name: str) -> str:
    for h in parse_hooks():
        if h["script"].endswith(script_name):
            return h["command"]
    raise AssertionError(f"no hook launches {script_name}")


def _sync_calls(uv_log: Path) -> int:
    return uv_log.read_text().count("UV_CALLED sync") if uv_log.exists() else 0


def _text(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class TestColdStartPrewarm:
    def test_cold_fire_detaches_build_and_notifies(self, tmp_path):
        env, root, uv_log = _prewarm_env(tmp_path)
        env["UV_SYNC_HANG"] = "8"
        t0 = time.monotonic()
        res = _run(_cmd("context_manager.py"), env)
        elapsed = time.monotonic() - t0
        assert res.returncode == 0
        assert elapsed < 5, "hook must return while the build continues detached"
        assert "building" in res.stdout, "model gets the one-line notice"
        assert (root / "scripts" / ".warmup").is_dir(), "in-flight marker missing"
        assert _wait_for(lambda: _sync_calls(uv_log) == 1)

    def test_concurrent_cold_fires_spawn_one_build(self, tmp_path):
        env, root, uv_log = _prewarm_env(tmp_path)
        env["UV_SYNC_HANG"] = "8"
        _run(_cmd("context_manager.py"), env)
        second = _run(_cmd("checkpoint_nudge.py"), env)
        assert second.returncode == 0
        assert second.stdout == "", (
            "only context_manager/session_start narrate the build — a second "
            "notice on the same prompt is noise"
        )
        third = _run(_cmd("session_start.py"), env)
        assert "building" in third.stdout
        assert _wait_for(lambda: _sync_calls(uv_log) >= 1)
        assert _sync_calls(uv_log) == 1, "the mkdir marker must gate concurrent spawns"

    def test_pre_compact_cold_keeps_stdout_empty(self, tmp_path):
        env, _root, _uv_log = _prewarm_env(tmp_path)
        env["UV_SYNC_HANG"] = "8"
        res = _run(_cmd("pre_compact.py"), env)
        assert res.returncode == 0
        assert res.stdout == "", (
            "PreCompact stdout feeds the compaction summarizer as instructions"
        )

    def test_build_completes_detached_and_clears_marker(self, tmp_path):
        env, root, uv_log = _prewarm_env(tmp_path)
        res = _run(_cmd("context_manager.py"), env)
        assert res.returncode == 0
        venv = root / "scripts" / ".venv"
        warm = root / "scripts" / ".warmup"
        assert _wait_for(lambda: venv.is_dir() and not warm.exists()), (
            "detached build must create .venv and clear the marker"
        )
        res2 = _run(_cmd("context_manager.py"), env)
        assert "UV_CALLED run --project" in res2.stdout, "warm path must exec the hook"

    def test_failed_build_leaves_marker_and_rate_limits(self, tmp_path):
        env, root, uv_log = _prewarm_env(tmp_path)
        env["UV_SYNC_FAIL"] = "1"
        _run(_cmd("context_manager.py"), env)
        assert _wait_for(lambda: _sync_calls(uv_log) == 1)
        warm = root / "scripts" / ".warmup"
        assert warm.is_dir(), (
            "failed build keeps the marker: it rate-limits the retry"
        )
        assert _wait_for(lambda: (warm / "status").is_file())
        assert (warm / "status").read_text().strip() == "7", (
            "the marker must record uv's exit code — without it the next fire "
            "cannot tell a failed build from one in flight"
        )
        _run(_cmd("context_manager.py"), env)
        assert _sync_calls(uv_log) == 1, "no rebuild until the marker goes stale"

    def test_failed_build_says_failed_and_names_the_log(self, tmp_path):
        """A kept marker means "retry rate-limited", not "still building".
        Telling the user for 15 minutes that a build is in progress and that
        hooks go live on the next prompt is three false claims, and never
        names .warmup.log, where the error actually is."""
        env, root, uv_log = _prewarm_env(tmp_path)
        env["UV_SYNC_FAIL"] = "1"
        _run(_cmd("context_manager.py"), env)
        assert _wait_for(lambda: (root / "scripts" / ".warmup" / "status").is_file())
        res = _run(_cmd("context_manager.py"), env)
        assert res.returncode == 0
        assert "FAILED" in res.stdout
        assert ".warmup.log" in res.stdout, "the message must name the error log"
        assert "in the background" not in res.stdout, (
            "a build that already failed is not in progress"
        )

    def test_stale_marker_triggers_retry(self, tmp_path):
        env, root, uv_log = _prewarm_env(tmp_path)
        warm = root / "scripts" / ".warmup"
        warm.mkdir()
        dead = subprocess.Popen(["sh", "-c", "exit 0"])
        dead.wait()  # reaped: kill -0 on this pid now fails
        (warm / "pid").write_text(str(dead.pid))
        old = time.time() - 16 * 60
        os.utime(warm, (old, old))
        res = _run(_cmd("context_manager.py"), env)
        assert res.returncode == 0
        assert _wait_for(lambda: _sync_calls(uv_log) == 1), (
            "a dead builder past the window means the build died — retry"
        )

    def test_live_builder_outlives_the_staleness_window(self, tmp_path):
        """The marker's mtime is stamped once and nothing refreshes it, so a
        first resolution slower than the window (it clones multiplai-core —
        a slow link makes that ordinary) used to get its marker reaped out
        from under it: a second uv sync spawned against the same lock, and
        builder 2's redirect truncated builder 1's .warmup.log mid-write.
        Liveness, not age, decides."""
        env, root, uv_log = _prewarm_env(tmp_path)
        warm = root / "scripts" / ".warmup"
        warm.mkdir()
        builder = subprocess.Popen(["sh", "-c", "sleep 30"])
        try:
            (warm / "pid").write_text(str(builder.pid))
            old = time.time() - 16 * 60
            os.utime(warm, (old, old))
            res = _run(_cmd("context_manager.py"), env)
            assert res.returncode == 0
            assert "building" in res.stdout
            assert warm.is_dir(), "a running builder's marker must survive"
            assert (warm / "pid").read_text().strip() == str(builder.pid), (
                "the marker was reaped and re-taken while its builder was "
                "still running — that is the second concurrent uv sync"
            )
            assert not _wait_for(lambda: _sync_calls(uv_log) > 0, timeout=2), (
                "no second build against the same lock"
            )
        finally:
            builder.kill()
            builder.wait()

    def test_ready_environment_beats_a_leftover_marker(self, tmp_path):
        """The marker is kept after a failed build to rate-limit retries —
        but the documented recovery is to run `uv sync` by hand, which
        leaves it in place. Consulting the marker first would gate a
        perfectly working environment for another 15 minutes."""
        env, root, uv_log = _prewarm_env(tmp_path)
        (root / "scripts" / ".venv").mkdir()
        warm = root / "scripts" / ".warmup"
        warm.mkdir()
        (warm / "status").write_text("7")
        res = _run(_cmd("context_manager.py"), env)
        assert "UV_CALLED run --project" in res.stdout, (
            "a usable environment must win over a marker left by an earlier "
            "failure"
        )
        assert _sync_calls(uv_log) == 0

    def test_relocated_environment_is_not_a_permanent_cold_start(self, tmp_path):
        """UV_PROJECT_ENVIRONMENT moves the environment somewhere no path
        guess can find. Guessing (`../../uv.lock` ? `../../.venv` :
        `scripts/.venv`) read that as cold on EVERY fire, forever: each one
        won the mkdir, detached another uv sync, and exited before reaching
        the hook — hooks dead indefinitely behind a "first run" banner on
        every prompt."""
        env, root, uv_log = _prewarm_env(tmp_path)
        relocated = tmp_path / "elsewhere" / "env"
        env["UV_PROJECT_ENVIRONMENT"] = str(relocated)
        res = _run(_cmd("context_manager.py"), env)
        assert res.returncode == 0
        assert "building" in res.stdout
        warm = root / "scripts" / ".warmup"
        assert _wait_for(lambda: relocated.is_dir() and not warm.exists())
        assert not (root / "scripts" / ".venv").exists(), (
            "test premise: the environment is NOT where the old probe guessed"
        )
        res2 = _run(_cmd("context_manager.py"), env)
        assert "UV_CALLED run --project" in res2.stdout, (
            "the relocated environment is ready — this fire must run the hook"
        )
        assert _sync_calls(uv_log) == 1, "exactly one build, not one per fire"

    def test_repo_checkout_with_root_venv_skips_prewarm(self, tmp_path):
        env, root, uv_log = _prewarm_env(tmp_path)
        # A repo checkout (directory-source marketplace, or the dev repo):
        # the workspace root two levels up owns the environment. With its
        # .venv present, hooks are already fast — no pre-warm.
        (root.parent.parent / "uv.lock").write_text("")
        (root.parent.parent / ".venv").mkdir()
        res = _run(_cmd("context_manager.py"), env)
        assert "UV_CALLED run --project" in res.stdout
        assert _sync_calls(uv_log) == 0

    def test_fresh_repo_checkout_prewarms_the_workspace_root(self, tmp_path):
        env, root, uv_log = _prewarm_env(tmp_path)
        # A fresh clone added as a directory-source marketplace: workspace
        # root exists but its .venv does not — the first uv run would do
        # the full workspace resolution inline. Must pre-warm like any
        # other cold start, with the build landing at the workspace root.
        (root.parent.parent / "uv.lock").write_text("")
        res = _run(_cmd("context_manager.py"), env)
        assert res.returncode == 0
        assert "building" in res.stdout
        root_venv = root.parent.parent / ".venv"
        warm = root / "scripts" / ".warmup"
        assert _wait_for(lambda: root_venv.is_dir() and not warm.exists())
        res2 = _run(_cmd("context_manager.py"), env)
        assert "UV_CALLED run --project" in res2.stdout

    def test_existing_venv_execs_normally(self, tmp_path):
        env, root, uv_log = _prewarm_env(tmp_path)
        (root / "scripts" / ".venv").mkdir()
        res = _run(_cmd("context_manager.py"), env)
        assert "UV_CALLED run --project" in res.stdout
        assert _sync_calls(uv_log) == 0


class TestColdStartLosesNothing:
    """Install the plugin, ask one question, close the tab inside the first
    minute: SessionEnd fires during the build window. Exiting 0 there means
    session_end.py never runs, no deferred-extraction marker is enqueued —
    no diary entry, no learnings, and nothing anywhere recording that the
    session was dropped. Same for a PreCompact in that window."""

    def _deferred_log(self, root: Path) -> Path:
        return root / "scripts" / ".warmup-deferred.log"

    def test_session_end_runs_once_the_build_lands(self, tmp_path):
        env, root, uv_log = _prewarm_env(tmp_path)
        env["UV_SYNC_HANG"] = "3"
        env["UV_RECORD_STDIN"] = "1"
        payload = json.dumps(
            {"hook_event_name": "SessionEnd", "session_id": "sess-abc123"}
        )
        t0 = time.monotonic()
        res = _run(_cmd("session_end.py"), env, stdin=payload)
        assert res.returncode == 0
        assert time.monotonic() - t0 < 3, "the hook itself must still return at once"
        assert res.stdout == "", (
            "only context_manager/session_start narrate the build"
        )
        assert _wait_for(
            lambda: "session_end.py" in _text(uv_log), timeout=25
        ), "session_end.py was dropped instead of re-run after the build"
        assert "sess-abc123" in _text(uv_log), (
            "the replayed hook must receive its original payload"
        )

    def test_pre_compact_runs_once_the_build_lands(self, tmp_path):
        env, root, uv_log = _prewarm_env(tmp_path)
        env["UV_SYNC_HANG"] = "3"
        res = _run(_cmd("pre_compact.py"), env, stdin="{}")
        assert res.stdout == "", (
            "PreCompact stdout feeds the compaction summarizer as instructions"
        )
        assert _wait_for(
            lambda: "pre_compact.py" in _text(uv_log), timeout=25
        )

    def test_a_dropped_lifecycle_hook_is_recorded(self, tmp_path):
        """When the build never lands there is nothing to re-run — but the
        loss must be written down somewhere, not swallowed."""
        env, root, uv_log = _prewarm_env(tmp_path)
        env["UV_SYNC_FAIL"] = "1"
        _run(_cmd("session_end.py"), env, stdin="{}")
        log = self._deferred_log(root)
        assert _wait_for(
            lambda: "dropped session_end.py" in _text(log), timeout=25
        ), f"nothing recorded the dropped session: {_text(log)!r}"

    def test_prompt_hooks_are_not_replayed(self, tmp_path):
        """context_manager/checkpoint_nudge belong to a prompt that has
        already been answered — re-running them a minute later would inject
        context into nothing."""
        env, root, uv_log = _prewarm_env(tmp_path)
        env["UV_SYNC_HANG"] = "2"
        _run(_cmd("context_manager.py"), env, stdin="{}")
        assert _wait_for(lambda: not (root / "scripts" / ".warmup").exists())
        time.sleep(2.5)  # longer than the waiter's poll interval
        assert "context_manager.py" not in _text(uv_log)


class TestWarmMode:
    """`run.sh --warm` — the Setup hook (`claude --init-only`, or `--init` /
    `--maintenance` in -p mode) and the manual pre-warm: a synchronous build
    that propagates uv's exit status."""

    def _warm_cmd(self) -> str:
        for h in parse_hooks():
            if h["event"] == "Setup":
                return h["command"]
        raise AssertionError("no Setup hook registered")

    def test_warm_syncs_synchronously(self, tmp_path):
        env, root, _uv_log = _prewarm_env(tmp_path)
        res = _run(self._warm_cmd(), env)
        assert res.returncode == 0
        assert "UV_CALLED sync --project" in res.stdout
        assert (root / "scripts" / ".venv").is_dir()

    def test_warm_propagates_failure(self, tmp_path):
        env, _root, _uv_log = _prewarm_env(tmp_path)
        env["UV_SYNC_FAIL"] = "1"
        res = _run(self._warm_cmd(), env)
        assert res.returncode == 7, "Setup is an explicit maintenance flow — report failure"

    def test_warm_clears_inflight_marker_on_success(self, tmp_path):
        env, root, _uv_log = _prewarm_env(tmp_path)
        (root / "scripts" / ".warmup").mkdir()
        res = _run(self._warm_cmd(), env)
        assert res.returncode == 0
        assert not (root / "scripts" / ".warmup").exists(), (
            "a completed warm-up must not leave hooks gated behind the marker"
        )

    def test_warm_takes_the_marker_it_releases(self, tmp_path):
        """`claude --init-only` starts a synchronous build. A SessionStart or
        UserPromptSubmit firing in that window used to see no marker, win the
        mkdir and detach a SECOND uv sync — serialized by uv's project lock,
        but truncating the first's .warmup.log and diverging marker ownership
        between the two paths."""
        env, root, uv_log = _prewarm_env(tmp_path)
        env["UV_SYNC_HANG"] = "5"
        warm = root / "scripts" / ".warmup"
        setup = subprocess.Popen(
            self._warm_cmd(), shell=True, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        try:
            assert _wait_for(warm.is_dir), "--warm must claim the in-flight marker"
            res = _run(_cmd("context_manager.py"), env)
            assert res.returncode == 0
            assert "building" in res.stdout
            setup.wait(timeout=30)
        finally:
            setup.kill()
        assert _sync_calls(uv_log) == 1, (
            "the session hook must not detach a second build alongside Setup's"
        )
        assert not warm.exists(), "--warm still releases the marker it took"
