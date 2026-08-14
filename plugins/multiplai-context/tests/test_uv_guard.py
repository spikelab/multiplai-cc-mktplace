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
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from conftest import HOOKS_JSON, parse_hooks

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


def _run(command: str, env: dict[str, str]):
    return subprocess.run(
        command, shell=True, env=env, capture_output=True, text=True, timeout=30,
    )


def _script_hooks():
    """The hooks that launch a Python script (excludes run.sh mode flags)."""
    return [h for h in parse_hooks() if h["script"].endswith(".py")]


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
        # Member-dir --project: "${CLAUDE_PLUGIN_ROOT}/scripts" exists both
        # in-repo (resolves via the workspace root) and on an installed
        # copy (resolves standalone). The old ../.. workspace-root form
        # does not exist on installs — ../.. may appear only in the
        # read-only dev-checkout probe, never as a --project value.
        assert 'sdir="${CLAUDE_PLUGIN_ROOT}/scripts"' in src
        assert 'run --project "$sdir"' in src
        assert '--project "${CLAUDE_PLUGIN_ROOT}/../..' not in src

    @pytest.mark.parametrize("hook", parse_hooks(), ids=lambda h: f"{h['event']}:{h['script']}")
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

    def test_warning_is_rate_limited_by_marker(self, tmp_path):
        env = _masked_env(tmp_path)
        first = _run(_script_hooks()[0]["command"], env)
        assert "uv not found" in first.stdout
        marker = Path(env["CLAUDE_CONFIG_DIR"]) / ".multiplai-context-uv-warned"
        assert marker.exists()
        for hook in parse_hooks():
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
printf 'UV_CALLED %s\\n' "$*" >> "$UV_LOG"
echo "UV_CALLED $@"
if [ "$1" = "sync" ]; then
    [ -n "$UV_SYNC_HANG" ] && sleep "$UV_SYNC_HANG"
    [ -n "$UV_SYNC_FAIL" ] && exit 7
    mkdir -p "$3/.venv"
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
        assert (root / "scripts" / ".warmup").is_dir(), (
            "failed build keeps the marker: it rate-limits the retry"
        )
        _run(_cmd("context_manager.py"), env)
        assert _sync_calls(uv_log) == 1, "no rebuild until the marker goes stale"

    def test_stale_marker_triggers_retry(self, tmp_path):
        env, root, uv_log = _prewarm_env(tmp_path)
        warm = root / "scripts" / ".warmup"
        warm.mkdir()
        old = time.time() - 16 * 60
        os.utime(warm, (old, old))
        res = _run(_cmd("context_manager.py"), env)
        assert res.returncode == 0
        assert _wait_for(lambda: _sync_calls(uv_log) == 1), (
            "a marker older than any plausible build means the builder died — retry"
        )

    def test_dev_checkout_skips_prewarm(self, tmp_path):
        env, root, uv_log = _prewarm_env(tmp_path)
        # A workspace root two levels up owns the environment in the dev
        # repo; scripts/.venv never exists there and uv run is already fast.
        (root.parent.parent / "uv.lock").write_text("")
        res = _run(_cmd("context_manager.py"), env)
        assert "UV_CALLED run --project" in res.stdout
        assert _sync_calls(uv_log) == 0

    def test_existing_venv_execs_normally(self, tmp_path):
        env, root, uv_log = _prewarm_env(tmp_path)
        (root / "scripts" / ".venv").mkdir()
        res = _run(_cmd("context_manager.py"), env)
        assert "UV_CALLED run --project" in res.stdout
        assert _sync_calls(uv_log) == 0


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
