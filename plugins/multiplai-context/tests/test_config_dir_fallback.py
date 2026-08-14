"""Every CLAUDE_CONFIG_DIR consumer must fall back to ``~/.claude``.

Vanilla Claude Code exports ``CLAUDE_CONFIG_DIR`` only when the user has
overridden the config location; a standalone plugin install (rung 1: no kit,
no launcher) never sees the variable. ``lib.fsio.claude_config_dir`` is the
one shared resolver — these tests pin its contract and the behaviour of the
call sites that used to hand-roll (or skip) the fallback.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lib.fsio import claude_config_dir


class TestClaudeConfigDirHelper:
    def test_env_set_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        assert claude_config_dir() == tmp_path / "cfg"

    def test_env_is_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/custom")
        assert claude_config_dir() == tmp_path / "custom"

    def test_unset_falls_back_to_home_dot_claude(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert claude_config_dir() == tmp_path / ".claude"

    def test_set_but_empty_counts_as_unset(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "  ")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert claude_config_dir() == tmp_path / ".claude"

    def test_result_is_not_validated(self, monkeypatch, tmp_path):
        """The helper resolves, callers validate — a missing dir is a valid
        answer (e.g. before Claude Code ever ran)."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
        assert not claude_config_dir().is_dir()


class TestCallSitesFallBack:
    """The refactored call sites all resolve through the helper."""

    @pytest.fixture
    def vanilla(self, monkeypatch, tmp_path):
        """No CLAUDE_CONFIG_DIR, HOME pointing at a scratch dir."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        return tmp_path

    def test_costing_default_config_dir(self, vanilla):
        from lib.costing_collector import default_config_dir

        assert default_config_dir() == vanilla / ".claude"

    def test_plugin_skills_default_plugins_dir(self, vanilla):
        from lib.plugin_skills import default_plugins_dir

        assert default_plugins_dir() == vanilla / ".claude" / "plugins"

    def test_reference_dir(self, vanilla):
        from lib.reference_docs import reference_dir

        assert reference_dir() == vanilla / ".claude" / "reference" / "dev"

    def test_fleet_jobs_config_dir_requires_existing(self, vanilla):
        from lib.fleet_sources.jobs import config_dir

        assert config_dir() is None  # ~/.claude does not exist yet
        (vanilla / ".claude").mkdir()
        assert config_dir() == vanilla / ".claude"
