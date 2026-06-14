"""Tests for one-time project-state injection at SessionStart.

The per-project ``now`` snapshot is injected once here (not per-prompt by
context_manager). These cover the SessionStart helper and lock in the removal
of the old per-prompt path.
"""

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _make_ws(tmp_path, monkeypatch, *, detection="basename"):
    ws = tmp_path / "ws"
    (ws / ".multiplai" / "now").mkdir(parents=True)
    (ws / ".multiplai" / "project-map.yaml").write_text(f"detection: {detection}\n")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_workspace_dir", str(ws))
    return ws


def _capture_inject(now_dir, cwd):
    import session_start

    out = io.StringIO()
    with redirect_stdout(out):
        result = session_start._inject_project_state(now_dir, cwd)
    return result, out.getvalue()


class TestInjectProjectState:
    def test_injects_matching_project(self, tmp_path, monkeypatch, reset_paths_cache):
        ws = _make_ws(tmp_path, monkeypatch)
        (ws / ".multiplai" / "now" / "foo.md").write_text(
            "# Project Status: foo\n\n- shipped the thing\n"
        )
        from multiplai_core.paths import get_paths

        result, text = _capture_inject(get_paths().now_dir(), "/a/b/foo")
        assert result is True
        assert "--- PROJECT STATE ---" in text
        assert "shipped the thing" in text

    def test_empty_cwd_no_output(self, tmp_path, monkeypatch, reset_paths_cache):
        _make_ws(tmp_path, monkeypatch)
        from multiplai_core.paths import get_paths

        result, text = _capture_inject(get_paths().now_dir(), "")
        assert result is False
        assert text == ""

    def test_missing_now_file_no_output(self, tmp_path, monkeypatch, reset_paths_cache):
        _make_ws(tmp_path, monkeypatch)
        from multiplai_core.paths import get_paths

        result, text = _capture_inject(get_paths().now_dir(), "/a/b/nope")
        assert result is False
        assert text == ""

    def test_workspace_umbrella_injects_workspace_file(
        self, tmp_path, monkeypatch, reset_paths_cache
    ):
        ws = tmp_path / "ws"
        (ws / ".multiplai" / "now").mkdir(parents=True)
        (ws / ".multiplai" / "project-map.yaml").write_text(
            f"umbrella_roots:\n  - {ws}\n"
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_workspace_dir", str(ws))
        (ws / ".multiplai" / "now" / "workspace.md").write_text(
            "# Project Status: workspace\n\n- cross-project work\n"
        )
        from multiplai_core.paths import get_paths

        result, text = _capture_inject(get_paths().now_dir(), str(ws))
        assert result is True
        assert "cross-project work" in text


class TestCwdCapture:
    """SessionStart must record cwd in session_state so SessionEnd can tag
    the diary entry with the project (the live capture that was missing)."""

    def test_session_start_records_cwd(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        env = os.environ.copy()
        for k in list(env):
            if k.startswith("CLAUDE_PLUGIN") or k == "WORKSPACE":
                del env[k]
        env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
        env["CLAUDE_PLUGIN_DATA"] = str(data_dir)

        subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "session_start.py")],
            input=json.dumps({"cwd": "/some/proj", "session_id": "zzz"}),
            text=True,
            capture_output=True,
            env=env,
            timeout=30,
        )
        state = json.loads((data_dir / "session_state.json").read_text())
        assert state.get("cwd") == "/some/proj"

    def test_session_start_uses_real_session_id(self, tmp_path):
        """The Claude Code session id from the hook input must be recorded
        verbatim, so every hook logs under one id and the activity stream
        is followable end-to-end (no random per-hook ids)."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        env = os.environ.copy()
        for k in list(env):
            if k.startswith("CLAUDE_PLUGIN") or k == "WORKSPACE":
                del env[k]
        env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
        env["CLAUDE_PLUGIN_DATA"] = str(data_dir)

        real_id = "abc12345-dead-beef-0000-111122223333"
        subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "session_start.py")],
            input=json.dumps({"cwd": "/some/proj", "session_id": real_id}),
            text=True,
            capture_output=True,
            env=env,
            timeout=30,
        )
        state = json.loads((data_dir / "session_state.json").read_text())
        assert state.get("session_id") == real_id


class TestPerPromptInjectionRemoved:
    """Regression: context_manager must no longer inject project state."""

    def test_context_manager_has_no_project_state_path(self):
        source = (SCRIPTS_DIR / "context_manager.py").read_text()
        assert "_load_project_state" not in source
        assert "PROJECT STATE" not in source

    def test_session_start_injects(self):
        source = (SCRIPTS_DIR / "session_start.py").read_text()
        assert "_inject_project_state" in source
        assert "PROJECT STATE" in source
