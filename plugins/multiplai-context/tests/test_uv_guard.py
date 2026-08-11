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

import shutil
import subprocess
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
        # does not exist on installs.
        assert 'exec uv run --project "${CLAUDE_PLUGIN_ROOT}/scripts"' in src
        assert "/../.." not in src

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
            first = _run(parse_hooks()[0]["command"], env)
            assert first.returncode == 0
            assert "uv not found" in first.stdout
            assert not list(cfg.glob(".multiplai-context-uv-warned*")), (
                "no marker can land in the unwritable config dir"
            )
            fallback = list(scratch_tmp.glob(".multiplai-context-uv-warned*"))
            assert fallback, "marker must fall back to TMPDIR"
            again = _run(parse_hooks()[0]["command"], env)
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
        res = _run(parse_hooks()[0]["command"], env)
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
        res = _run(parse_hooks()[0]["command"], env)
        assert res.returncode == 0
        assert "UV_CALLED run --project" in res.stdout
        assert "uv not found" not in res.stdout
        assert "uv not found" not in res.stderr
        assert not list(
            Path(env["CLAUDE_CONFIG_DIR"]).glob(".multiplai-context-uv-warned*")
        ), "the fallback path must not leave a warned marker"
