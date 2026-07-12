"""Tripwire tests for the uv guard wrapping every hook command (C1).

Claude Code does not bundle uv. Without the guard, a missing uv makes every
hook spawn-fail silently and the plugin just "doesn't work". The guard must:
exit 0 (never break the session), emit one clear install pointer on stdout
(surfaced as hook context) and stderr, and rate-limit repeats via a marker
file. Verified by actually running each hooks.json command with uv masked
off PATH.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import HOOKS_JSON, parse_hooks


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


class TestUvGuard:
    def test_every_hook_command_is_guarded(self):
        for hook in parse_hooks():
            assert "command -v uv" in hook["command"], (
                f"{hook['event']} command missing the uv guard: {hook['command']}"
            )
            assert "exec uv run --no-project" in hook["command"]

    @pytest.mark.parametrize("hook", parse_hooks(), ids=lambda h: f"{h['event']}:{h['script']}")
    def test_missing_uv_warns_once_and_exits_zero(self, hook, tmp_path):
        env = _masked_env(tmp_path)
        res = _run(hook["command"], env)
        assert res.returncode == 0, res.stderr
        assert "uv not found" in res.stdout
        assert "docs.astral.sh/uv" in res.stdout
        assert "uv not found" in res.stderr

    def test_warning_is_rate_limited_by_marker(self, tmp_path):
        env = _masked_env(tmp_path)
        first = _run(parse_hooks()[0]["command"], env)
        assert "uv not found" in first.stdout
        marker = Path(env["CLAUDE_CONFIG_DIR"]) / ".multiplai-context-uv-warned"
        assert marker.exists()
        for hook in parse_hooks():
            again = _run(hook["command"], env)
            assert again.returncode == 0
            assert again.stdout == "" and again.stderr == "", (
                f"{hook['event']} warned despite fresh marker"
            )

    def test_with_uv_present_command_reaches_uv(self, tmp_path):
        """With a fake `uv` first on PATH, the guard must exec through to it."""
        env = _masked_env(tmp_path)
        fake_uv = Path(env["PATH"]) / "uv"
        fake_uv.write_text("#!/bin/sh\necho UV_CALLED \"$@\"\nexit 0\n")
        fake_uv.chmod(0o755)
        res = _run(parse_hooks()[0]["command"], env)
        assert res.returncode == 0
        assert "UV_CALLED run --no-project" in res.stdout
        assert "uv not found" not in res.stdout
