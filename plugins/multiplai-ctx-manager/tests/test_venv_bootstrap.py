"""Tests for venv bootstrap hook (scripts/venv_bootstrap.py).

Covers all scenarios from Block 4 — Venv Bootstrap Hook:
- Venv creation with --system-site-packages flag
- pip install -r requirements.txt within the created venv
- .bootstrap-complete marker file with SHA-256 hash of requirements.txt
- Idempotency check: skip if marker exists and hash matches
- Re-bootstrap on requirements.txt hash change
- Error handling: missing python, pip install failure, cleanup
- Reusable re-exec preamble (venv_guard module)

Related requirements:
- plugin-hooks.md: SessionStart venv bootstrap
- plugin-scaffold.md: hooks.json wiring
- D4 design decision: idempotent setup with marker hash
"""

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(content: bytes) -> str:
    """Compute SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# requirements hash computation
# ---------------------------------------------------------------------------


class TestRequirementsHash:
    """Requirement: .bootstrap-complete marker file with SHA-256 hash.

    The bootstrap script must compute a SHA-256 hash of requirements.txt
    and store it in the marker file for idempotency checks.
    """

    def test_hash_matches_sha256_of_file_content(self, tmp_path):
        """Scenario: Hash is a correct SHA-256 digest of requirements.txt bytes."""
        from venv_bootstrap import _requirements_hash

        req = tmp_path / "requirements.txt"
        content = b"anthropic>=0.40.0\npyyaml>=6.0\n"
        req.write_bytes(content)

        result = _requirements_hash(req)
        expected = _sha256(content)

        assert result == expected
        assert len(result) == 64  # SHA-256 hex is always 64 chars

    def test_hash_changes_when_content_changes(self, tmp_path):
        """Scenario: Different requirements.txt content produces different hash."""
        from venv_bootstrap import _requirements_hash

        req = tmp_path / "requirements.txt"

        req.write_bytes(b"anthropic>=0.40.0\n")
        hash1 = _requirements_hash(req)

        req.write_bytes(b"anthropic>=0.40.0\npyyaml>=6.0\n")
        hash2 = _requirements_hash(req)

        assert hash1 != hash2

    def test_hash_is_deterministic(self, tmp_path):
        """Scenario: Same content always produces same hash."""
        from venv_bootstrap import _requirements_hash

        req = tmp_path / "requirements.txt"
        content = b"anthropic>=0.40.0\npyyaml>=6.0\n"
        req.write_bytes(content)

        assert _requirements_hash(req) == _requirements_hash(req)


# ---------------------------------------------------------------------------
# Fresh bootstrap (first session)
# ---------------------------------------------------------------------------


class TestFreshBootstrap:
    """Requirement: SessionStart venv bootstrap — first session with no existing venv.

    WHEN the SessionStart hook fires and no venv directory exists,
    THEN a Python 3.12+ venv is created with --system-site-packages,
    pip install runs, and a marker file records the requirements hash.
    """

    @patch("venv_bootstrap._has_uv", return_value=False)
    @patch("venv_bootstrap.subprocess.run")
    def test_creates_venv_with_system_site_packages(
        self, mock_run, mock_has_uv, tmp_path, monkeypatch, reset_paths_cache
    ):
        """Scenario: Venv created with --system-site-packages flag."""
        data_dir = tmp_path / "data"
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        req = plugin_dir / "requirements.txt"
        req.write_text("anthropic>=0.40.0\npyyaml>=6.0\n")

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))

        from venv_bootstrap import bootstrap

        mock_run.return_value = MagicMock(returncode=0)

        bootstrap()

        # First subprocess call should be venv creation
        venv_call = mock_run.call_args_list[0]
        cmd = venv_call[0][0]  # positional args -> first arg is the command list
        assert "--system-site-packages" in cmd
        assert "-m" in cmd
        assert "venv" in cmd

    @patch("venv_bootstrap._has_uv", return_value=False)
    @patch("venv_bootstrap.subprocess.run")
    def test_runs_pip_install_with_requirements(
        self, mock_run, mock_has_uv, tmp_path, monkeypatch, reset_paths_cache
    ):
        """Scenario: pip install -r requirements.txt runs within the venv."""
        data_dir = tmp_path / "data"
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        req = plugin_dir / "requirements.txt"
        req.write_text("anthropic>=0.40.0\npyyaml>=6.0\n")

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))

        from venv_bootstrap import bootstrap

        mock_run.return_value = MagicMock(returncode=0)

        bootstrap()

        # Second subprocess call should be pip install
        assert len(mock_run.call_args_list) >= 2
        pip_call = mock_run.call_args_list[1]
        cmd = pip_call[0][0]
        assert "-m" in cmd
        assert "pip" in cmd
        assert "install" in cmd
        assert "-r" in cmd
        # Pip should use venv python, not system python
        venv_python = str(data_dir / "venv" / "bin" / "python")
        assert cmd[0] == venv_python

    @patch("venv_bootstrap.subprocess.run")
    def test_writes_marker_with_hash(
        self, mock_run, tmp_path, monkeypatch, reset_paths_cache
    ):
        """Scenario: .bootstrap-complete marker written with requirements hash."""
        data_dir = tmp_path / "data"
        venv_dir = data_dir / "venv"
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        req = plugin_dir / "requirements.txt"
        req_content = b"anthropic>=0.40.0\npyyaml>=6.0\n"
        req.write_bytes(req_content)

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))

        from venv_bootstrap import bootstrap

        mock_run.return_value = MagicMock(returncode=0)

        bootstrap()

        marker = venv_dir / ".bootstrap-complete"
        assert marker.exists()
        assert marker.read_text().strip() == _sha256(req_content)

    @patch("venv_bootstrap.subprocess.run")
    def test_creates_venv_directory(
        self, mock_run, tmp_path, monkeypatch, reset_paths_cache
    ):
        """Scenario: Venv directory is created if it doesn't exist."""
        data_dir = tmp_path / "data"
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        req = plugin_dir / "requirements.txt"
        req.write_text("anthropic>=0.40.0\n")

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))

        from venv_bootstrap import bootstrap

        mock_run.return_value = MagicMock(returncode=0)

        bootstrap()

        venv_dir = data_dir / "venv"
        assert venv_dir.exists()


# ---------------------------------------------------------------------------
# Idempotency — no-op on second run
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Requirement: Idempotency check — skip if marker exists and hash matches.

    WHEN the SessionStart hook fires and a venv directory already exists with
    all required packages installed, THEN the venv creation and pip install
    steps are skipped.
    """

    @patch("venv_bootstrap.subprocess.run")
    def test_skips_when_marker_matches(
        self, mock_run, tmp_path, monkeypatch, reset_paths_cache
    ):
        """Scenario: Subsequent session with existing venv — no subprocess calls."""
        data_dir = tmp_path / "data"
        venv_dir = data_dir / "venv"
        venv_dir.mkdir(parents=True)
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()

        req = plugin_dir / "requirements.txt"
        req_content = b"anthropic>=0.40.0\npyyaml>=6.0\n"
        req.write_bytes(req_content)

        # Write marker with matching hash
        marker = venv_dir / ".bootstrap-complete"
        marker.write_text(_sha256(req_content))

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))

        from venv_bootstrap import bootstrap

        bootstrap()

        # No subprocess calls should have been made
        mock_run.assert_not_called()

    @patch("venv_bootstrap.subprocess.run")
    def test_noop_completes_fast(
        self, mock_run, tmp_path, monkeypatch, reset_paths_cache
    ):
        """Scenario: No-op bootstrap completes without calling any subprocesses.

        D4 spec: exits in < 50ms when marker present and hash matches.
        """
        import time

        data_dir = tmp_path / "data"
        venv_dir = data_dir / "venv"
        venv_dir.mkdir(parents=True)
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()

        req = plugin_dir / "requirements.txt"
        req_content = b"anthropic>=0.40.0\npyyaml>=6.0\n"
        req.write_bytes(req_content)

        marker = venv_dir / ".bootstrap-complete"
        marker.write_text(_sha256(req_content))

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))

        from venv_bootstrap import bootstrap

        start = time.monotonic()
        bootstrap()
        elapsed_ms = (time.monotonic() - start) * 1000

        # Pure Python hash check + file read should be very fast
        assert elapsed_ms < 50, f"No-op bootstrap took {elapsed_ms:.1f}ms, expected < 50ms"
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Re-bootstrap on hash change
# ---------------------------------------------------------------------------


class TestReBootstrap:
    """Requirement: Re-bootstrap on requirements.txt hash change (R7).

    WHEN requirements.txt changes (hash mismatch), THEN venv is recreated
    and dependencies are reinstalled.
    """

    @patch("venv_bootstrap.subprocess.run")
    def test_rebootstraps_when_hash_differs(
        self, mock_run, tmp_path, monkeypatch, reset_paths_cache
    ):
        """Scenario: Hash change triggers full re-bootstrap."""
        data_dir = tmp_path / "data"
        venv_dir = data_dir / "venv"
        venv_dir.mkdir(parents=True)
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()

        # requirements.txt has new content
        req = plugin_dir / "requirements.txt"
        new_content = b"anthropic>=0.40.0\npyyaml>=6.0\nhttpx>=0.27.0\n"
        req.write_bytes(new_content)

        # Marker has OLD hash
        marker = venv_dir / ".bootstrap-complete"
        marker.write_text(_sha256(b"anthropic>=0.40.0\npyyaml>=6.0\n"))

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))

        from venv_bootstrap import bootstrap

        mock_run.return_value = MagicMock(returncode=0)

        bootstrap()

        # Should have called venv creation + pip install
        assert mock_run.call_count >= 2

        # Marker should now contain new hash
        assert marker.read_text().strip() == _sha256(new_content)

    @patch("venv_bootstrap.subprocess.run")
    def test_marker_updated_after_rebootstrap(
        self, mock_run, tmp_path, monkeypatch, reset_paths_cache
    ):
        """Scenario: Marker file updated to new hash after successful re-bootstrap."""
        data_dir = tmp_path / "data"
        venv_dir = data_dir / "venv"
        venv_dir.mkdir(parents=True)
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()

        req = plugin_dir / "requirements.txt"
        new_content = b"anthropic>=0.50.0\npyyaml>=6.0\n"
        req.write_bytes(new_content)

        marker = venv_dir / ".bootstrap-complete"
        marker.write_text("old-stale-hash-that-no-longer-matches")

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))

        from venv_bootstrap import bootstrap

        mock_run.return_value = MagicMock(returncode=0)

        bootstrap()

        # After re-bootstrap, marker should have the correct hash
        assert marker.read_text().strip() == _sha256(new_content)

    @patch("venv_bootstrap.subprocess.run")
    def test_subsequent_run_after_rebootstrap_is_noop(
        self, mock_run, tmp_path, monkeypatch, reset_paths_cache
    ):
        """Scenario: After re-bootstrap, a second run is idempotent."""
        data_dir = tmp_path / "data"
        venv_dir = data_dir / "venv"
        venv_dir.mkdir(parents=True)
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()

        req = plugin_dir / "requirements.txt"
        req_content = b"anthropic>=0.40.0\npyyaml>=6.0\n"
        req.write_bytes(req_content)

        # Simulate post-bootstrap state: marker matches requirements
        marker = venv_dir / ".bootstrap-complete"
        marker.write_text(_sha256(req_content))

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))

        from venv_bootstrap import bootstrap

        bootstrap()
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Requirement: Error handling during venv bootstrap.

    Venv creation or pip install failures must produce clear diagnostics
    and not leave an inconsistent state.
    """

    @patch("venv_bootstrap.subprocess.run")
    def test_venv_creation_failure_raises(
        self, mock_run, tmp_path, monkeypatch, reset_paths_cache
    ):
        """Scenario: Venv creation fails — error propagated, no marker written."""
        data_dir = tmp_path / "data"
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        req = plugin_dir / "requirements.txt"
        req.write_text("anthropic>=0.40.0\n")

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))

        mock_run.side_effect = subprocess.CalledProcessError(
            1, ["python3", "-m", "venv"], stderr="Python 3.12+ is required"
        )

        from venv_bootstrap import bootstrap

        with pytest.raises(subprocess.CalledProcessError):
            bootstrap()

        # Marker should NOT exist after failure
        marker = data_dir / "venv" / ".bootstrap-complete"
        assert not marker.exists()

    @patch("venv_bootstrap.subprocess.run")
    def test_pip_failure_cleans_marker(
        self, mock_run, tmp_path, monkeypatch, reset_paths_cache
    ):
        """Scenario: pip install fails — marker removed, venv not left incomplete.

        WHEN pip install fails, THEN the partially-created venv is not left
        in an inconsistent state (marker cleaned up).
        """
        data_dir = tmp_path / "data"
        venv_dir = data_dir / "venv"
        venv_dir.mkdir(parents=True)
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        req = plugin_dir / "requirements.txt"
        req.write_text("anthropic>=0.40.0\n")

        # Pre-create a stale marker (simulates re-bootstrap scenario)
        marker = venv_dir / ".bootstrap-complete"
        marker.write_text("stale-hash")

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "pip" in cmd:
                raise subprocess.CalledProcessError(
                    1, cmd, stderr="Network error: could not resolve host"
                )
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect

        from venv_bootstrap import bootstrap

        with pytest.raises(subprocess.CalledProcessError):
            bootstrap()

        # Marker must not exist — prevents false "up to date" on next run
        assert not marker.exists()

    @patch("venv_bootstrap.subprocess.run")
    def test_no_requirements_file_writes_fallback_marker(
        self, mock_run, tmp_path, monkeypatch, reset_paths_cache
    ):
        """Scenario: No requirements.txt — venv created, fallback marker written."""
        data_dir = tmp_path / "data"
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        # No requirements.txt created

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))

        from venv_bootstrap import bootstrap

        mock_run.return_value = MagicMock(returncode=0)

        bootstrap()

        marker = data_dir / "venv" / ".bootstrap-complete"
        assert marker.exists()
        assert marker.read_text().strip() == "no-requirements"


# ---------------------------------------------------------------------------
# Marker file details
# ---------------------------------------------------------------------------


class TestMarkerFile:
    """Requirement: .bootstrap-complete marker file correctness.

    The marker file must accurately reflect the state of the venv
    so that idempotency checks work reliably.
    """

    @patch("venv_bootstrap.subprocess.run")
    def test_marker_lives_in_venv_dir(
        self, mock_run, tmp_path, monkeypatch, reset_paths_cache
    ):
        """Scenario: Marker file located at $PLUGIN_DATA/venv/.bootstrap-complete."""
        data_dir = tmp_path / "data"
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        req = plugin_dir / "requirements.txt"
        req.write_text("anthropic>=0.40.0\n")

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))

        from venv_bootstrap import bootstrap

        mock_run.return_value = MagicMock(returncode=0)

        bootstrap()

        expected_path = data_dir / "venv" / ".bootstrap-complete"
        assert expected_path.exists()

    @patch("venv_bootstrap.subprocess.run")
    def test_marker_contains_only_hash(
        self, mock_run, tmp_path, monkeypatch, reset_paths_cache
    ):
        """Scenario: Marker file contains only the hex digest, no extra content."""
        data_dir = tmp_path / "data"
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        req = plugin_dir / "requirements.txt"
        req_content = b"anthropic>=0.40.0\n"
        req.write_bytes(req_content)

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))

        from venv_bootstrap import bootstrap

        mock_run.return_value = MagicMock(returncode=0)

        bootstrap()

        marker = data_dir / "venv" / ".bootstrap-complete"
        text = marker.read_text()
        # Should be a clean hex string, no JSON, no newline-separated metadata
        assert text == _sha256(req_content)

    def test_marker_absent_triggers_bootstrap(
        self, tmp_path, monkeypatch, reset_paths_cache
    ):
        """Scenario: Missing marker (venv dir exists but no marker) triggers bootstrap."""
        data_dir = tmp_path / "data"
        venv_dir = data_dir / "venv"
        venv_dir.mkdir(parents=True)
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        req = plugin_dir / "requirements.txt"
        req.write_text("anthropic>=0.40.0\n")

        # No marker file — venv exists but is incomplete

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_dir))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))

        from venv_bootstrap import bootstrap

        with patch("venv_bootstrap.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            bootstrap()
            # Should have made subprocess calls (not a no-op)
            assert mock_run.call_count >= 1


# ---------------------------------------------------------------------------
# Venv guard / re-exec preamble (D4 pattern)
# ---------------------------------------------------------------------------


class TestVenvGuard:
    """Requirement: Reusable re-exec preamble for other hook scripts (D4).

    All non-bootstrap hook scripts must use a guard that re-execs into
    the venv Python if not already running there.
    """

    def test_venv_guard_module_exists(self):
        """Scenario: venv_guard module is importable from lib."""
        from lib.venv_guard import ensure_venv_python

        assert callable(ensure_venv_python)

    def test_venv_guard_noop_when_already_in_venv(self, monkeypatch, tmp_path):
        """Scenario: No re-exec when sys.executable matches venv python."""
        data_dir = tmp_path / "data"
        venv_python = data_dir / "venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.touch()

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))
        monkeypatch.setattr("sys.executable", str(venv_python))

        from lib.venv_guard import ensure_venv_python

        # Should return without calling os.execv
        with patch("os.execv") as mock_execv:
            ensure_venv_python()
            mock_execv.assert_not_called()

    def test_venv_guard_execvs_when_not_in_venv(self, monkeypatch, tmp_path):
        """Scenario: Re-exec via os.execv when running system Python."""
        data_dir = tmp_path / "data"
        venv_python = data_dir / "venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.touch()

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))
        monkeypatch.setattr("sys.executable", "/usr/bin/python3")

        from lib.venv_guard import ensure_venv_python

        with patch("os.execv") as mock_execv:
            ensure_venv_python()
            mock_execv.assert_called_once()
            call_args = mock_execv.call_args[0]
            assert call_args[0] == str(venv_python)

    def test_venv_guard_bootstraps_when_venv_missing(self, monkeypatch, tmp_path):
        """Scenario: Venv is missing (deleted or first run) — guard bootstraps it.

        WHEN a hook fires and the venv Python does not exist, THEN the guard
        calls bootstrap() to recreate it before attempting re-exec.
        """
        data_dir = tmp_path / "data"
        # venv dir does NOT exist

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data_dir))
        monkeypatch.setattr("sys.executable", "/usr/bin/python3")

        from lib.venv_guard import ensure_venv_python

        with patch("lib.venv_guard._bootstrap_venv") as mock_bootstrap, \
             patch("os.execv"):
            ensure_venv_python()
            mock_bootstrap.assert_called_once()

    def test_venv_guard_uses_plugin_data_env(self, monkeypatch, tmp_path):
        """Scenario: Guard resolves venv from CLAUDE_PLUGIN_DATA env var."""
        custom_data = tmp_path / "custom-data"
        venv_python = custom_data / "venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.touch()

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(custom_data))
        monkeypatch.setattr("sys.executable", "/usr/bin/python3")

        from lib.venv_guard import ensure_venv_python

        with patch("os.execv") as mock_execv:
            ensure_venv_python()
            call_args = mock_execv.call_args[0]
            assert str(custom_data) in call_args[0]


# ---------------------------------------------------------------------------
# Path resolver integration — venv dir derives from plugin data
# ---------------------------------------------------------------------------


class TestVenvPathResolution:
    """Requirement: Venv path derived from plugin data directory (D2).

    The venv location must be derived from paths.plugin_data() / 'venv',
    not hardcoded.
    """

    def test_venv_dir_derived_from_plugin_data(self, monkeypatch, reset_paths_cache):
        """Scenario: paths.venv_dir() returns plugin_data/venv."""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugin")
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", "/custom/data")

        from lib.paths import Paths

        p = Paths.resolve()
        assert p.venv_dir() == Path("/custom/data/venv")

    def test_venv_dir_standalone_fallback(self, monkeypatch, reset_paths_cache):
        """Scenario: Standalone mode (no workspace) — venv at ~/.multiplai/data/venv."""
        for key in list(os.environ):
            if key.startswith("CLAUDE_PLUGIN"):
                monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("WORKSPACE", raising=False)

        from lib.paths import Paths

        p = Paths.resolve()
        expected = Path.home() / ".multiplai" / "data" / "venv"
        assert p.venv_dir() == expected

    def test_bootstrap_uses_paths_module(self):
        """Scenario: venv_bootstrap imports from lib.paths, not hardcoded paths."""
        import inspect

        from venv_bootstrap import bootstrap

        source = inspect.getsource(bootstrap)
        # Should use get_paths() or paths module, not hardcoded dirs
        assert "get_paths" in source or "paths" in source
        assert "~/.multiplai" not in source
        assert "/home/" not in source


# ---------------------------------------------------------------------------
# hooks.json wiring
# ---------------------------------------------------------------------------


class TestHooksJsonWiring:
    """Requirement: SessionStart hook registration in hooks.json.

    hooks.json must declare venv_bootstrap.py as a SessionStart hook.
    """

    def test_hooks_json_has_venv_bootstrap(self, plugin_root):
        """Scenario: SessionStart hook points to venv_bootstrap script."""
        from conftest import HOOKS_JSON, parse_hooks

        assert HOOKS_JSON.is_file(), "hooks/hooks.json must exist"

        session_start_hooks = [
            h for h in parse_hooks() if h["event"] == "SessionStart"
        ]
        assert len(session_start_hooks) >= 1, "Must have at least one SessionStart hook"

        scripts = [h["script"] for h in session_start_hooks]
        assert any(
            "venv_bootstrap" in s for s in scripts
        ), f"No venv_bootstrap in SessionStart scripts: {scripts}"

    def test_venv_bootstrap_script_exists(self, plugin_root):
        """Scenario: Referenced venv_bootstrap script file exists."""
        script_path = plugin_root / "scripts" / "venv_bootstrap.py"
        assert script_path.exists(), f"venv_bootstrap.py not found at {script_path}"

    def test_venv_bootstrap_is_valid_python(self, plugin_root):
        """Scenario: venv_bootstrap.py compiles without syntax errors."""
        import py_compile

        script_path = plugin_root / "scripts" / "venv_bootstrap.py"
        # Should not raise py_compile.PyCompileError
        py_compile.compile(str(script_path), doraise=True)


# ---------------------------------------------------------------------------
# No hardcoded paths / no direct SDK imports
# ---------------------------------------------------------------------------


class TestPortingConstraints:
    """Requirement: No hardcoded paths or direct SDK imports in bootstrap script.

    Per D8 porting strategy, all path resolution goes through lib.paths
    and no direct claude_agent_sdk imports are allowed.
    """

    def test_no_hardcoded_home_paths(self, plugin_root):
        """Scenario: No hardcoded home directory references in bootstrap."""
        script = (plugin_root / "scripts" / "venv_bootstrap.py").read_text()

        forbidden = ["~/.multiplai", "~/.claude", "/home/spike", "/Users/spike"]
        for pattern in forbidden:
            assert pattern not in script, f"Found hardcoded path '{pattern}' in venv_bootstrap.py"

    def test_no_direct_sdk_imports(self, plugin_root):
        """Scenario: No direct claude_agent_sdk or anthropic imports."""
        script = (plugin_root / "scripts" / "venv_bootstrap.py").read_text()

        assert "import claude_agent_sdk" not in script
        assert "from claude_agent_sdk" not in script
        # anthropic is fine as a string in requirements, but not as an import
        assert "import anthropic" not in script
        assert "from anthropic" not in script

    def test_no_shell_script_subprocess_calls(self, plugin_root):
        """Scenario: No subprocess calls to .sh/.bash wrapper scripts."""
        script = (plugin_root / "scripts" / "venv_bootstrap.py").read_text()

        assert ".sh" not in script or "venv_bootstrap.sh" not in script
        assert ".bash" not in script


# ---------------------------------------------------------------------------
# requirements.txt integration
# ---------------------------------------------------------------------------


class TestRequirementsFile:
    """Requirement: requirements.txt contains expected dependencies.

    The plugin's requirements.txt must declare anthropic, pyyaml, and
    claude-agent-sdk. (claude-agent-sdk used to be deliberately omitted
    — relying on host injection + --system-site-packages — but that
    left standalone / skill-invoked SDK scripts, e.g.
    /multiplai:refresh-catalogs, unable to import it. It is now a
    normal, pinned venv dependency installed into .multiplai/data/venv
    like everything else.)
    """

    def test_anthropic_in_requirements(self, plugin_root):
        """Scenario: anthropic is a declared dependency."""
        req_path = plugin_root / "requirements.txt"
        assert req_path.exists()
        content = req_path.read_text()
        assert "anthropic" in content

    def test_pyyaml_in_requirements(self, plugin_root):
        """Scenario: pyyaml is a declared dependency."""
        req_path = plugin_root / "requirements.txt"
        content = req_path.read_text()
        assert "pyyaml" in content.lower() or "PyYAML" in content

    def test_claude_agent_sdk_pinned_in_requirements(self, plugin_root):
        """Scenario: claude-agent-sdk is a pinned dependency.

        Installed into the plugin venv so SDK features work for
        standalone/skill invocations, not only host-injected hooks.
        Pinned (==) so it can't silently drift from the version the
        rest of the runtime expects.
        """
        req_path = plugin_root / "requirements.txt"
        content = req_path.read_text()
        assert "claude-agent-sdk==" in content
