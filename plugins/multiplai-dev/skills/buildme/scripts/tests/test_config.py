"""Tests for config — tier detection, test command discovery, config loading."""

import os
import pytest
from pathlib import Path
from unittest.mock import patch

from build_pipeline.config import detect_tier, BuildConfig, GateToggles


class TestTierDetection:
    def test_opus_46_is_advanced(self):
        with patch.dict(os.environ, {"CLAUDE_MODEL": "claude-opus-4-6"}):
            tier, name = detect_tier()
            assert tier == "advanced"
            assert "opus-4-6" in name

    def test_opus_45_is_advanced(self):
        with patch.dict(os.environ, {"CLAUDE_MODEL": "claude-opus-4-5"}):
            tier, _ = detect_tier()
            assert tier == "advanced"

    def test_sonnet_is_standard(self):
        with patch.dict(os.environ, {"CLAUDE_MODEL": "claude-sonnet-4-6"}):
            tier, _ = detect_tier()
            assert tier == "standard"

    def test_haiku_is_standard(self):
        with patch.dict(os.environ, {"CLAUDE_MODEL": "claude-haiku-4-5"}):
            tier, _ = detect_tier()
            assert tier == "standard"

    def test_empty_model_defaults_standard(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CLAUDE_MODEL", None)
            tier, name = detect_tier()
            assert tier == "standard"
            assert name == "unknown"

    def test_unknown_model_defaults_standard(self):
        with patch.dict(os.environ, {"CLAUDE_MODEL": "gpt-4-turbo"}):
            tier, _ = detect_tier()
            assert tier == "standard"

    def test_future_opus_5_is_advanced(self):
        with patch.dict(os.environ, {"CLAUDE_MODEL": "claude-opus-5-0"}):
            tier, _ = detect_tier()
            assert tier == "advanced"


class TestTestCommandDiscovery:
    def test_discovers_pytest(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
        config = BuildConfig(project_dir=tmp_path)
        config._discover_test_command()
        assert config.test_command == "pytest -xvs"

    def test_discovers_swift_test(self, tmp_path):
        (tmp_path / "Package.swift").write_text("// swift-tools-version:5.9\n")
        config = BuildConfig(project_dir=tmp_path)
        config._discover_test_command()
        assert config.test_command == "swift test"

    def test_discovers_npm_test(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name":"test"}\n')
        config = BuildConfig(project_dir=tmp_path)
        config._discover_test_command()
        assert config.test_command == "npm test"

    def test_no_test_command_if_no_markers(self, tmp_path):
        config = BuildConfig(project_dir=tmp_path)
        config._discover_test_command()
        assert config.test_command == ""


class TestGateToggles:
    def test_defaults_all_enabled(self):
        g = GateToggles()
        assert g.code_review_per_block
        assert g.security_review_per_block
        assert g.test_quality_enabled
        assert g.e2e_test_entry_point_check

    def test_toggle_off(self):
        g = GateToggles(security_review_per_block=False)
        assert not g.security_review_per_block


class TestTierProperties:
    def test_advanced_task_granularity(self):
        c = BuildConfig(tier="advanced")
        assert c.task_granularity == "blocks"

    def test_standard_task_granularity(self):
        c = BuildConfig(tier="standard")
        assert c.task_granularity == "checkboxes"

    def test_advanced_agent_scope(self):
        c = BuildConfig(tier="advanced")
        assert c.agent_scope == "per_block"

    def test_standard_agent_scope(self):
        c = BuildConfig(tier="standard")
        assert c.agent_scope == "per_task"

    def test_advanced_no_refactor_phase(self):
        c = BuildConfig(tier="advanced")
        assert not c.refactor_phase
        assert c.tdd_phases == ["test", "implement"]

    def test_standard_has_refactor_phase(self):
        c = BuildConfig(tier="standard")
        assert c.refactor_phase
        assert c.tdd_phases == ["test", "implement", "refactor"]

    def test_advanced_implementer_prompt_clean(self):
        c = BuildConfig(tier="advanced")
        assert c.implementer_prompt_style == "clean"

    def test_standard_implementer_prompt_minimum(self):
        c = BuildConfig(tier="standard")
        assert c.implementer_prompt_style == "minimum"


class TestConfigPaths:
    def test_change_dir(self, tmp_path):
        config = BuildConfig(project_dir=tmp_path, change_name="my-feature")
        config.specs_dir = tmp_path / "specs"
        assert config.change_dir == tmp_path / "specs" / "changes" / "my-feature"

    def test_tasks_path(self, tmp_path):
        config = BuildConfig(project_dir=tmp_path, change_name="feat")
        config.specs_dir = tmp_path / "specs"
        assert config.tasks_path == tmp_path / "specs" / "changes" / "feat" / "tasks.md"
