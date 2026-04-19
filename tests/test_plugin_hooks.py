"""Tests for plugin hook declarations and hook scripts."""

import json
import re
from pathlib import Path

import pytest

from conftest import PLUGIN_ROOT


class TestHookRegistration:
    """Verify hooks.json declares all required event types."""

    @pytest.fixture(autouse=True)
    def load_hooks(self):
        path = PLUGIN_ROOT / "hooks.json"
        assert path.is_file()
        self.hooks = json.loads(path.read_text())["hooks"]

    def test_session_start_registered(self):
        events = [h["event"] for h in self.hooks]
        assert "SessionStart" in events

    def test_user_prompt_submit_registered(self):
        events = [h["event"] for h in self.hooks]
        assert "UserPromptSubmit" in events

    def test_stop_registered(self):
        events = [h["event"] for h in self.hooks]
        assert "Stop" in events

    def test_session_end_registered(self):
        events = [h["event"] for h in self.hooks]
        assert "SessionEnd" in events

    def test_pre_compact_registered(self):
        events = [h["event"] for h in self.hooks]
        assert "PreCompact" in events

    def test_exactly_five_event_types(self):
        event_types = {h["event"] for h in self.hooks}
        assert len(event_types) == 5


class TestStopHookNoGit:
    """Verify Stop hook doesn't include git staging."""

    def test_no_git_stage_in_stop_script(self):
        hooks = json.loads((PLUGIN_ROOT / "hooks.json").read_text())["hooks"]
        stop_hooks = [h for h in hooks if h["event"] == "Stop"]
        for hook in stop_hooks:
            script_path = PLUGIN_ROOT / hook["script"]
            if script_path.exists():
                text = script_path.read_text()
                assert "git_stage" not in text
                assert "git add" not in text
                assert "git commit" not in text


class TestVenvPythonUsage:
    """Verify hook scripts use venv Python."""

    def _get_hook_scripts(self):
        hooks = json.loads((PLUGIN_ROOT / "hooks.json").read_text())["hooks"]
        return [PLUGIN_ROOT / h["script"] for h in hooks]

    def test_non_bootstrap_scripts_have_reexec(self):
        for script_path in self._get_hook_scripts():
            if "venv_bootstrap" in script_path.name:
                continue
            if script_path.exists():
                text = script_path.read_text()
                has_venv_ref = "venv" in text and ("python" in text or "execv" in text)
                assert has_venv_ref, f"{script_path.name} missing venv re-exec pattern"


class TestPathResolverImports:
    """Verify hook scripts import path resolver."""

    def test_hook_scripts_import_paths(self):
        hooks = json.loads((PLUGIN_ROOT / "hooks.json").read_text())["hooks"]
        for hook in hooks:
            script_path = PLUGIN_ROOT / hook["script"]
            if script_path.exists():
                text = script_path.read_text()
                has_paths_import = "from lib.paths" in text or "lib.paths" in text
                assert has_paths_import, f"{script_path.name} missing path resolver import"

    def test_no_hardcoded_paths_in_scripts(self):
        hooks = json.loads((PLUGIN_ROOT / "hooks.json").read_text())["hooks"]
        for hook in hooks:
            script_path = PLUGIN_ROOT / hook["script"]
            if script_path.exists():
                text = script_path.read_text()
                assert "~/.multiplai" not in text, f"{script_path.name} has hardcoded path"
                assert "/home/" not in text, f"{script_path.name} has hardcoded home path"


class TestModelClientUsage:
    """Verify hook scripts use model client, not direct SDK imports."""

    def test_no_direct_sdk_imports(self):
        hooks = json.loads((PLUGIN_ROOT / "hooks.json").read_text())["hooks"]
        for hook in hooks:
            script_path = PLUGIN_ROOT / hook["script"]
            if script_path.exists():
                text = script_path.read_text()
                assert "import claude_agent_sdk" not in text, \
                    f"{script_path.name} has direct SDK import"
                assert "from claude_agent_sdk" not in text, \
                    f"{script_path.name} has direct SDK import"
                # anthropic import allowed only in model_client.py
                if "model_client" not in script_path.name:
                    assert "import anthropic" not in text, \
                        f"{script_path.name} has direct anthropic import"


class TestHooksSchemaValidity:
    """Verify hooks.json schema is valid."""

    def test_valid_json(self):
        path = PLUGIN_ROOT / "hooks.json"
        json.loads(path.read_text())  # should not raise

    def test_all_entries_have_required_fields(self):
        hooks = json.loads((PLUGIN_ROOT / "hooks.json").read_text())["hooks"]
        for hook in hooks:
            assert "event" in hook, "Hook missing event field"
            assert "script" in hook, "Hook missing script field"

    def test_no_duplicate_event_script_pairs(self):
        hooks = json.loads((PLUGIN_ROOT / "hooks.json").read_text())["hooks"]
        pairs = [(h["event"], h["script"]) for h in hooks]
        assert len(pairs) == len(set(pairs))
