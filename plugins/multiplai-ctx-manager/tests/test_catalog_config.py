"""Tests for catalog config schema and plugin configuration.

Block 2: Catalog config schema and plugin configuration.

Covers all scenarios from requirements/catalog-config-surface.md:
- CatalogConfig dataclass with all fields and defaults
- Config loading from plugin settings with default fallbacks
- Validation for enum fields (reasoning_effort)
- Validation for numeric bounds (ttl_hours, diary_catalog_days)
- plugin.json userConfig schema entries
- Config values accessible to generators via config module
"""

import dataclasses
import json
import os
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# plugin.json userConfig Schema Entries
# ---------------------------------------------------------------------------


class TestPluginJsonUserConfigSchema:
    """Requirement: Config entries follow plugin.json userConfig schema.

    All new config entries MUST be declared in the userConfig section of
    plugin.json with proper type, default value, and description fields.
    """

    @pytest.fixture(autouse=True)
    def load_plugin_json(self):
        plugin_json_path = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
        with open(plugin_json_path) as f:
            self.plugin_config = json.load(f)
        self.user_config = self.plugin_config.get("userConfig", {})

    def test_catalog_model_entry_exists(self):
        """Scenario: plugin.json contains catalog_model entry."""
        assert "catalog_model" in self.user_config, (
            "plugin.json userConfig must contain 'catalog_model' entry"
        )

    def test_catalog_model_type_is_string(self):
        """Scenario: catalog_model has type string."""
        entry = self.user_config.get("catalog_model", {})
        assert entry.get("type") == "string", (
            f"catalog_model type must be 'string', got {entry.get('type')}"
        )

    def test_catalog_model_default_is_claude_sonnet(self):
        """Scenario: Default catalog model is claude-sonnet-4-6."""
        entry = self.user_config.get("catalog_model", {})
        assert entry.get("default") == "claude-sonnet-4-6", (
            f"catalog_model default must be 'claude-sonnet-4-6', got {entry.get('default')}"
        )

    def test_catalog_model_has_description(self):
        """Scenario: catalog_model has a description field."""
        entry = self.user_config.get("catalog_model", {})
        assert "description" in entry and entry["description"], (
            "catalog_model must have a non-empty description"
        )

    def test_catalog_reasoning_effort_entry_exists(self):
        """Scenario: plugin.json contains catalog_reasoning_effort entry."""
        assert "catalog_reasoning_effort" in self.user_config, (
            "plugin.json userConfig must contain 'catalog_reasoning_effort' entry"
        )

    def test_catalog_reasoning_effort_type_is_string(self):
        """Scenario: catalog_reasoning_effort has type string."""
        entry = self.user_config.get("catalog_reasoning_effort", {})
        assert entry.get("type") == "string", (
            f"catalog_reasoning_effort type must be 'string', got {entry.get('type')}"
        )

    def test_catalog_reasoning_effort_default_is_medium(self):
        """Scenario: Default reasoning effort is medium."""
        entry = self.user_config.get("catalog_reasoning_effort", {})
        assert entry.get("default") == "medium", (
            f"catalog_reasoning_effort default must be 'medium', got {entry.get('default')}"
        )

    def test_catalog_reasoning_effort_has_description(self):
        """Scenario: catalog_reasoning_effort has a description field."""
        entry = self.user_config.get("catalog_reasoning_effort", {})
        assert "description" in entry and entry["description"], (
            "catalog_reasoning_effort must have a non-empty description"
        )

    def test_catalog_ttl_hours_entry_exists(self):
        """Scenario: plugin.json contains catalog_ttl_hours entry."""
        assert "catalog_ttl_hours" in self.user_config, (
            "plugin.json userConfig must contain 'catalog_ttl_hours' entry"
        )

    def test_catalog_ttl_hours_type_is_number(self):
        """Scenario: catalog_ttl_hours has type number."""
        entry = self.user_config.get("catalog_ttl_hours", {})
        assert entry.get("type") == "number", (
            f"catalog_ttl_hours type must be 'number', got {entry.get('type')}"
        )

    def test_catalog_ttl_hours_default_is_168(self):
        """Scenario: Default TTL is 168 hours (7 days)."""
        entry = self.user_config.get("catalog_ttl_hours", {})
        assert entry.get("default") == 168, (
            f"catalog_ttl_hours default must be 168, got {entry.get('default')}"
        )

    def test_catalog_ttl_hours_has_description(self):
        """Scenario: catalog_ttl_hours has a description field."""
        entry = self.user_config.get("catalog_ttl_hours", {})
        assert "description" in entry and entry["description"], (
            "catalog_ttl_hours must have a non-empty description"
        )

    def test_diary_catalog_days_entry_exists(self):
        """Scenario: plugin.json contains diary_catalog_days entry."""
        assert "diary_catalog_days" in self.user_config, (
            "plugin.json userConfig must contain 'diary_catalog_days' entry"
        )

    def test_diary_catalog_days_type_is_number(self):
        """Scenario: diary_catalog_days has type number."""
        entry = self.user_config.get("diary_catalog_days", {})
        assert entry.get("type") == "number", (
            f"diary_catalog_days type must be 'number', got {entry.get('type')}"
        )

    def test_diary_catalog_days_default_is_7(self):
        """Scenario: Default diary window is 7 days."""
        entry = self.user_config.get("diary_catalog_days", {})
        assert entry.get("default") == 7, (
            f"diary_catalog_days default must be 7, got {entry.get('default')}"
        )

    def test_diary_catalog_days_has_description(self):
        """Scenario: diary_catalog_days has a description field."""
        entry = self.user_config.get("diary_catalog_days", {})
        assert "description" in entry and entry["description"], (
            "diary_catalog_days must have a non-empty description"
        )

    def test_enable_skills_entry_exists(self):
        """Scenario: plugin.json contains enable_skills entry."""
        assert "enable_skills" in self.user_config, (
            "plugin.json userConfig must contain 'enable_skills' entry"
        )

    def test_enable_skills_type_is_boolean(self):
        """Scenario: enable_skills has type boolean."""
        entry = self.user_config.get("enable_skills", {})
        assert entry.get("type") == "boolean", (
            f"enable_skills type must be 'boolean', got {entry.get('type')}"
        )

    def test_enable_skills_default_is_false(self):
        """Scenario: Skills catalog disabled by default."""
        entry = self.user_config.get("enable_skills", {})
        assert entry.get("default") is False, (
            f"enable_skills default must be false, got {entry.get('default')}"
        )

    def test_enable_skills_has_description(self):
        """Scenario: enable_skills has a description field."""
        entry = self.user_config.get("enable_skills", {})
        assert "description" in entry and entry["description"], (
            "enable_skills must have a non-empty description"
        )

    def test_enable_resources_entry_exists(self):
        """Scenario: plugin.json contains enable_resources entry."""
        assert "enable_resources" in self.user_config, (
            "plugin.json userConfig must contain 'enable_resources' entry"
        )

    def test_enable_resources_type_is_boolean(self):
        """Scenario: enable_resources has type boolean."""
        entry = self.user_config.get("enable_resources", {})
        assert entry.get("type") == "boolean", (
            f"enable_resources type must be 'boolean', got {entry.get('type')}"
        )

    def test_enable_resources_default_is_false(self):
        """Scenario: Resources catalog disabled by default."""
        entry = self.user_config.get("enable_resources", {})
        assert entry.get("default") is False, (
            f"enable_resources default must be false, got {entry.get('default')}"
        )

    def test_enable_resources_has_description(self):
        """Scenario: enable_resources has a description field."""
        entry = self.user_config.get("enable_resources", {})
        assert "description" in entry and entry["description"], (
            "enable_resources must have a non-empty description"
        )

    def test_all_config_entries_present(self):
        """Scenario: All config entries present in plugin.json."""
        expected = {
            "catalog_model",
            "catalog_model_diary",
            "catalog_reasoning_effort",
            "catalog_ttl_hours",
            "diary_catalog_days",
            "enable_skills",
            "skills_dir",
            "enable_resources",
            "resources_dir",
        }
        actual = set(self.user_config.keys())
        missing = expected - actual
        assert not missing, (
            f"Missing config entries in plugin.json userConfig: {missing}"
        )

    def test_config_types_are_correct(self):
        """Scenario: Config types are correct per spec."""
        type_map = {
            "catalog_model": "string",
            "catalog_model_diary": "string",
            "catalog_reasoning_effort": "string",
            "catalog_ttl_hours": "number",
            "diary_catalog_days": "number",
            "enable_skills": "boolean",
            "skills_dir": "string",
            "enable_resources": "boolean",
            "resources_dir": "string",
        }
        for key, expected_type in type_map.items():
            entry = self.user_config.get(key, {})
            actual_type = entry.get("type")
            assert actual_type == expected_type, (
                f"{key} type must be '{expected_type}', got '{actual_type}'"
            )


# ---------------------------------------------------------------------------
# CatalogConfig Dataclass
# ---------------------------------------------------------------------------


class TestCatalogConfigDataclass:
    """Requirement: CatalogConfig dataclass in scripts/generators/config.py.

    CatalogConfig must be a dataclass with all catalog config fields,
    sensible defaults, and validation logic.
    """

    def test_catalog_config_module_importable(self):
        """CatalogConfig must be importable from scripts/generators/config.py."""
        from generators.config import CatalogConfig

        assert CatalogConfig is not None

    def test_catalog_config_is_dataclass(self):
        """CatalogConfig must be a dataclass."""
        from generators.config import CatalogConfig

        assert dataclasses.is_dataclass(CatalogConfig), (
            "CatalogConfig must be a dataclass"
        )

    def test_catalog_config_has_model_field(self):
        """CatalogConfig must have a model field."""
        from generators.config import CatalogConfig

        fields = {f.name for f in dataclasses.fields(CatalogConfig)}
        assert "model" in fields, "CatalogConfig must have a 'model' field"

    def test_catalog_config_has_reasoning_effort_field(self):
        """CatalogConfig must have a reasoning_effort field."""
        from generators.config import CatalogConfig

        fields = {f.name for f in dataclasses.fields(CatalogConfig)}
        assert "reasoning_effort" in fields, (
            "CatalogConfig must have a 'reasoning_effort' field"
        )

    def test_catalog_config_has_ttl_hours_field(self):
        """CatalogConfig must have a ttl_hours field."""
        from generators.config import CatalogConfig

        fields = {f.name for f in dataclasses.fields(CatalogConfig)}
        assert "ttl_hours" in fields, (
            "CatalogConfig must have a 'ttl_hours' field"
        )

    def test_catalog_config_has_diary_catalog_days_field(self):
        """CatalogConfig must have a diary_catalog_days field."""
        from generators.config import CatalogConfig

        fields = {f.name for f in dataclasses.fields(CatalogConfig)}
        assert "diary_catalog_days" in fields, (
            "CatalogConfig must have a 'diary_catalog_days' field"
        )

    def test_catalog_config_has_enable_skills_field(self):
        """CatalogConfig must have an enable_skills field."""
        from generators.config import CatalogConfig

        fields = {f.name for f in dataclasses.fields(CatalogConfig)}
        assert "enable_skills" in fields, (
            "CatalogConfig must have an 'enable_skills' field"
        )

    def test_catalog_config_has_enable_resources_field(self):
        """CatalogConfig must have an enable_resources field."""
        from generators.config import CatalogConfig

        fields = {f.name for f in dataclasses.fields(CatalogConfig)}
        assert "enable_resources" in fields, (
            "CatalogConfig must have an 'enable_resources' field"
        )

    def test_catalog_config_has_resources_dir_field(self):
        """CatalogConfig must have a resources_dir field."""
        from generators.config import CatalogConfig

        fields = {f.name for f in dataclasses.fields(CatalogConfig)}
        assert "resources_dir" in fields, (
            "CatalogConfig must have a 'resources_dir' field"
        )


# ---------------------------------------------------------------------------
# CatalogConfig Default Values
# ---------------------------------------------------------------------------


class TestCatalogConfigDefaults:
    """Requirement: CatalogConfig provides sensible defaults matching plugin.json.

    When constructed with no arguments (or loaded from empty config),
    all fields must have the default values specified in the design doc.
    """

    def test_default_model(self):
        """Scenario: Default catalog model is claude-sonnet-4-6."""
        from generators.config import CatalogConfig

        config = CatalogConfig()
        assert config.model == "claude-sonnet-4-6"

    def test_default_reasoning_effort(self):
        """Scenario: Default reasoning effort is medium."""
        from generators.config import CatalogConfig

        config = CatalogConfig()
        assert config.reasoning_effort == "medium"

    def test_default_ttl_hours(self):
        """Scenario: Default TTL is 168 hours (7 days)."""
        from generators.config import CatalogConfig

        config = CatalogConfig()
        assert config.ttl_hours == 168

    def test_default_diary_catalog_days(self):
        """Scenario: Default diary window is 7 days."""
        from generators.config import CatalogConfig

        config = CatalogConfig()
        assert config.diary_catalog_days == 7

    def test_default_enable_skills(self):
        """Scenario: Skills catalog disabled by default."""
        from generators.config import CatalogConfig

        config = CatalogConfig()
        assert config.enable_skills is False

    def test_default_enable_resources(self):
        """Scenario: Resources catalog disabled by default."""
        from generators.config import CatalogConfig

        config = CatalogConfig()
        assert config.enable_resources is False

    def test_default_resources_dir(self):
        """Scenario: Resources dir defaults to empty string."""
        from generators.config import CatalogConfig

        config = CatalogConfig()
        assert config.resources_dir == ""

    def test_default_recommend_cooldown_turns(self):
        """Scenario: Re-recommendation cooldown defaults to 4 turns."""
        from generators.config import CatalogConfig

        config = CatalogConfig()
        assert config.recommend_cooldown_turns == 4

    def test_negative_cooldown_resets_to_default(self):
        """Scenario: A negative cooldown is clamped back to the default."""
        from generators.config import CatalogConfig

        config = CatalogConfig(recommend_cooldown_turns=-3)
        assert config.recommend_cooldown_turns == 4

    def test_zero_cooldown_preserved(self):
        """Scenario: 0 is a valid value (disables the cooldown)."""
        from generators.config import CatalogConfig

        config = CatalogConfig(recommend_cooldown_turns=0)
        assert config.recommend_cooldown_turns == 0


# ---------------------------------------------------------------------------
# CatalogConfig Validation — Reasoning Effort
# ---------------------------------------------------------------------------


class TestReasoningEffortValidation:
    """Requirement: Validation for enum fields (reasoning_effort).

    reasoning_effort must be one of "low", "medium", "high".
    Invalid values must be rejected and the default used instead.
    """

    def test_valid_low_reasoning_effort(self):
        """Scenario: reasoning_effort 'low' is accepted."""
        from generators.config import CatalogConfig

        config = CatalogConfig(reasoning_effort="low")
        assert config.reasoning_effort == "low"

    def test_valid_medium_reasoning_effort(self):
        """Scenario: reasoning_effort 'medium' is accepted."""
        from generators.config import CatalogConfig

        config = CatalogConfig(reasoning_effort="medium")
        assert config.reasoning_effort == "medium"

    def test_valid_high_reasoning_effort(self):
        """Scenario: reasoning_effort 'high' is accepted."""
        from generators.config import CatalogConfig

        config = CatalogConfig(reasoning_effort="high")
        assert config.reasoning_effort == "high"

    def test_invalid_reasoning_effort_uses_default(self):
        """Scenario: Invalid reasoning effort value falls back to 'medium'."""
        from generators.config import CatalogConfig

        config = CatalogConfig(reasoning_effort="extreme")
        assert config.reasoning_effort == "medium", (
            "Invalid reasoning_effort should fall back to 'medium'"
        )

    def test_empty_reasoning_effort_uses_default(self):
        """Scenario: Empty reasoning effort value falls back to 'medium'."""
        from generators.config import CatalogConfig

        config = CatalogConfig(reasoning_effort="")
        assert config.reasoning_effort == "medium", (
            "Empty reasoning_effort should fall back to 'medium'"
        )

    def test_case_sensitive_reasoning_effort(self):
        """Scenario: Reasoning effort validation is case-sensitive (or handles case)."""
        from generators.config import CatalogConfig

        # "Medium" (capitalized) is not one of "low", "medium", "high"
        # so it should either be lowered or rejected. The spec says the
        # valid values are lowercase. If rejected, default is used.
        config = CatalogConfig(reasoning_effort="Medium")
        assert config.reasoning_effort in ("medium", "Medium"), (
            "Capitalized reasoning_effort should be normalized or rejected to default"
        )


# ---------------------------------------------------------------------------
# CatalogConfig Validation — TTL Hours
# ---------------------------------------------------------------------------


class TestTtlHoursValidation:
    """Requirement: Validation for numeric bounds (ttl_hours).

    ttl_hours must be >= 0. Negative values must be rejected with default used.
    Zero is valid (forces always-regenerate).
    """

    def test_positive_ttl_hours(self):
        """Scenario: Custom TTL of 12 is accepted."""
        from generators.config import CatalogConfig

        config = CatalogConfig(ttl_hours=12)
        assert config.ttl_hours == 12

    def test_zero_ttl_hours_is_valid(self):
        """Scenario: TTL of zero forces always-regenerate."""
        from generators.config import CatalogConfig

        config = CatalogConfig(ttl_hours=0)
        assert config.ttl_hours == 0

    def test_negative_ttl_hours_uses_default(self):
        """Scenario: Negative TTL is rejected, default 168 used instead."""
        from generators.config import CatalogConfig

        config = CatalogConfig(ttl_hours=-1)
        assert config.ttl_hours == 168, (
            "Negative ttl_hours should fall back to default 168"
        )

    def test_large_negative_ttl_hours_uses_default(self):
        """Scenario: Large negative TTL is rejected."""
        from generators.config import CatalogConfig

        config = CatalogConfig(ttl_hours=-100)
        assert config.ttl_hours == 168

    def test_large_positive_ttl_hours_accepted(self):
        """Scenario: Large positive TTL is accepted."""
        from generators.config import CatalogConfig

        config = CatalogConfig(ttl_hours=168)  # 1 week
        assert config.ttl_hours == 168


# ---------------------------------------------------------------------------
# CatalogConfig Validation — Diary Lookback Days
# ---------------------------------------------------------------------------


class TestDiaryLookbackDaysValidation:
    """Requirement: Validation for numeric bounds (diary_catalog_days).

    diary_catalog_days must be >= 0. Negative values must be rejected.
    Zero is valid (no entries processed, empty catalog).
    """

    def test_custom_diary_catalog_days(self):
        """Scenario: Custom diary window of 7 days is accepted."""
        from generators.config import CatalogConfig

        config = CatalogConfig(diary_catalog_days=7)
        assert config.diary_catalog_days == 7

    def test_zero_diary_catalog_days_is_valid(self):
        """Scenario: Diary window of zero means no entries processed."""
        from generators.config import CatalogConfig

        config = CatalogConfig(diary_catalog_days=0)
        assert config.diary_catalog_days == 0

    def test_negative_diary_catalog_days_uses_default(self):
        """Scenario: Negative diary window is rejected, default 7 used."""
        from generators.config import CatalogConfig

        config = CatalogConfig(diary_catalog_days=-5)
        assert config.diary_catalog_days == 7, (
            "Negative diary_catalog_days should fall back to default 7"
        )

    def test_large_negative_diary_catalog_days_uses_default(self):
        """Scenario: Large negative diary window is rejected."""
        from generators.config import CatalogConfig

        config = CatalogConfig(diary_catalog_days=-999)
        assert config.diary_catalog_days == 7

    def test_large_positive_diary_catalog_days_accepted(self):
        """Scenario: Large diary window is accepted."""
        from generators.config import CatalogConfig

        config = CatalogConfig(diary_catalog_days=365)
        assert config.diary_catalog_days == 365


# ---------------------------------------------------------------------------
# CatalogConfig Validation — Model
# ---------------------------------------------------------------------------


class TestModelValidation:
    """Requirement: Catalog model configuration validation.

    Empty or whitespace-only model values should be rejected.
    Non-empty string values should be accepted as-is.
    """

    def test_custom_model_accepted(self):
        """Scenario: User overrides catalog model."""
        from generators.config import CatalogConfig

        config = CatalogConfig(model="claude-haiku-4-5")
        assert config.model == "claude-haiku-4-5"

    def test_empty_model_rejected(self):
        """Scenario: Empty model value is rejected.

        Config validation should reject empty string and either use
        default or raise an error.
        """
        from generators.config import CatalogConfig

        # Empty model should either raise or use default
        try:
            config = CatalogConfig(model="")
            # If it doesn't raise, it should use the default
            assert config.model == "claude-sonnet-4-6", (
                "Empty model should fall back to default 'claude-sonnet-4-6'"
            )
        except (ValueError, TypeError):
            pass  # Raising is also acceptable behavior per spec


# ---------------------------------------------------------------------------
# Config Loading from Plugin Settings
# ---------------------------------------------------------------------------


class TestConfigLoadingFromPluginSettings:
    """Requirement: Config values are accessible to generators via config module.

    The config module must load catalog config from plugin settings
    (environment variables CLAUDE_PLUGIN_OPTION_*) with default fallbacks.
    """

    def test_load_catalog_config_function_exists(self):
        """Config module must expose a function to load CatalogConfig."""
        from generators.config import load_catalog_config

        assert callable(load_catalog_config)

    def test_load_catalog_config_returns_catalog_config(self):
        """load_catalog_config must return a CatalogConfig instance."""
        from generators.config import CatalogConfig, load_catalog_config

        config = load_catalog_config()
        assert isinstance(config, CatalogConfig)

    def test_load_returns_defaults_when_no_env_vars(self, monkeypatch):
        """Scenario: Config module returns defaults for unset values."""
        # Clear any catalog-related env vars
        for key in list(os.environ):
            if "catalog" in key.lower() or key.startswith("CLAUDE_PLUGIN_OPTION_"):
                monkeypatch.delenv(key, raising=False)

        from generators.config import load_catalog_config

        config = load_catalog_config()
        assert config.model == "claude-sonnet-4-6"
        assert config.reasoning_effort == "medium"
        assert config.ttl_hours == 168
        assert config.diary_catalog_days == 7
        assert config.enable_skills is False
        assert config.enable_resources is False
        assert config.resources_dir == ""

    def test_load_respects_model_override(self, monkeypatch):
        """Scenario: Config module exposes catalog settings from env vars."""
        monkeypatch.setenv(
            "CLAUDE_PLUGIN_OPTION_catalog_model", "claude-haiku-4-5"
        )
        from generators.config import load_catalog_config

        config = load_catalog_config()
        assert config.model == "claude-haiku-4-5"

    def test_load_respects_reasoning_effort_override(self, monkeypatch):
        """Scenario: Config module reflects user overrides for reasoning_effort."""
        monkeypatch.setenv(
            "CLAUDE_PLUGIN_OPTION_catalog_reasoning_effort", "high"
        )
        from generators.config import load_catalog_config

        config = load_catalog_config()
        assert config.reasoning_effort == "high"

    def test_load_respects_ttl_hours_override(self, monkeypatch):
        """Scenario: Config module reflects user overrides for ttl_hours."""
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_catalog_ttl_hours", "12")
        from generators.config import load_catalog_config

        config = load_catalog_config()
        assert config.ttl_hours == 12

    def test_load_respects_diary_catalog_days_override(self, monkeypatch):
        """Scenario: Config module reflects user overrides for diary_catalog_days."""
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_diary_catalog_days", "14")
        from generators.config import load_catalog_config

        config = load_catalog_config()
        assert config.diary_catalog_days == 14

    def test_load_respects_enable_skills_override(self, monkeypatch):
        """Scenario: Skills catalog enabled via config."""
        monkeypatch.setenv(
            "CLAUDE_PLUGIN_OPTION_enable_skills", "true"
        )
        from generators.config import load_catalog_config

        config = load_catalog_config()
        assert config.enable_skills is True

    def test_load_respects_enable_resources_override(self, monkeypatch):
        """Scenario: Resources catalog enabled via config."""
        monkeypatch.setenv(
            "CLAUDE_PLUGIN_OPTION_enable_resources", "true"
        )
        from generators.config import load_catalog_config

        config = load_catalog_config()
        assert config.enable_resources is True

    def test_load_validates_invalid_reasoning_effort(self, monkeypatch):
        """Scenario: Invalid reasoning effort from env is rejected, default used."""
        monkeypatch.setenv(
            "CLAUDE_PLUGIN_OPTION_catalog_reasoning_effort", "ultra"
        )
        from generators.config import load_catalog_config

        config = load_catalog_config()
        assert config.reasoning_effort == "medium", (
            "Invalid reasoning_effort from env should fall back to 'medium'"
        )

    def test_load_validates_negative_ttl_hours(self, monkeypatch):
        """Scenario: Negative ttl_hours from env is rejected, default used."""
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_catalog_ttl_hours", "-5")
        from generators.config import load_catalog_config

        config = load_catalog_config()
        assert config.ttl_hours == 168, (
            "Negative ttl_hours from env should fall back to 24"
        )

    def test_load_validates_negative_diary_catalog(self, monkeypatch):
        """Scenario: Negative diary_catalog_days from env is rejected, default used."""
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_diary_catalog_days", "-10")
        from generators.config import load_catalog_config

        config = load_catalog_config()
        assert config.diary_catalog_days == 7, (
            "Negative diary_catalog_days from env should fall back to 30"
        )

    def test_load_handles_non_numeric_ttl_hours(self, monkeypatch):
        """Edge case: Non-numeric ttl_hours from env should use default."""
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_catalog_ttl_hours", "abc")
        from generators.config import load_catalog_config

        config = load_catalog_config()
        assert config.ttl_hours == 168, (
            "Non-numeric ttl_hours should fall back to default 168"
        )

    def test_load_handles_non_numeric_diary_catalog(self, monkeypatch):
        """Edge case: Non-numeric diary_catalog_days from env should use default."""
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_diary_catalog_days", "abc")
        from generators.config import load_catalog_config

        config = load_catalog_config()
        assert config.diary_catalog_days == 7, (
            "Non-numeric diary_catalog_days should fall back to default 7"
        )


# ---------------------------------------------------------------------------
# Config Loading — Boolean Parsing Edge Cases
# ---------------------------------------------------------------------------


class TestBooleanConfigParsing:
    """Edge cases for boolean config parsing from environment variables.

    Plugin settings come through as strings via CLAUDE_PLUGIN_OPTION_* env vars.
    Boolean parsing must handle "true", "false", "1", "0", etc.
    """

    def test_enable_skills_false_string(self, monkeypatch):
        """Scenario: 'false' string maps to False."""
        monkeypatch.setenv(
            "CLAUDE_PLUGIN_OPTION_enable_skills", "false"
        )
        from generators.config import load_catalog_config

        config = load_catalog_config()
        assert config.enable_skills is False

    def test_enable_skills_true_string(self, monkeypatch):
        """Scenario: 'true' string maps to True."""
        monkeypatch.setenv(
            "CLAUDE_PLUGIN_OPTION_enable_skills", "true"
        )
        from generators.config import load_catalog_config

        config = load_catalog_config()
        assert config.enable_skills is True

    def test_enable_resources_false_string(self, monkeypatch):
        """Scenario: 'false' string maps to False for resources."""
        monkeypatch.setenv(
            "CLAUDE_PLUGIN_OPTION_enable_resources", "false"
        )
        from generators.config import load_catalog_config

        config = load_catalog_config()
        assert config.enable_resources is False

    def test_enable_resources_empty_string_is_false(self, monkeypatch):
        """Scenario: Empty string for boolean config is treated as False."""
        monkeypatch.setenv(
            "CLAUDE_PLUGIN_OPTION_enable_resources", ""
        )
        from generators.config import load_catalog_config

        config = load_catalog_config()
        assert config.enable_resources is False


# ---------------------------------------------------------------------------
# Config Loading — Resources Dir
# ---------------------------------------------------------------------------


class TestResourcesDirConfig:
    """Requirement: Resources catalog gate configuration.

    Resources catalog requires both enable_resources=true AND a non-empty
    resources_dir. Config loading must make resources_dir accessible.
    """

    def test_resources_dir_from_env(self, monkeypatch):
        """Scenario: Resources dir loaded from environment."""
        monkeypatch.setenv(
            "CLAUDE_PLUGIN_OPTION_enable_resources", "true"
        )
        monkeypatch.setenv(
            "CLAUDE_PLUGIN_OPTION_resources_dir", "/path/to/resources"
        )
        from generators.config import load_catalog_config

        config = load_catalog_config()
        assert config.resources_dir == "/path/to/resources"

    def test_resources_enabled_but_dir_empty(self, monkeypatch):
        """Scenario: Resources enabled but resources_dir not set.

        Config should load successfully; the dispatcher/generator is
        responsible for skipping when dir is empty.
        """
        monkeypatch.setenv(
            "CLAUDE_PLUGIN_OPTION_enable_resources", "true"
        )
        # resources_dir not set or empty
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_resources_dir", raising=False)
        from generators.config import load_catalog_config

        config = load_catalog_config()
        assert config.enable_resources is True
        assert config.resources_dir == ""


# ---------------------------------------------------------------------------
# CatalogConfig Construction with All Fields
# ---------------------------------------------------------------------------


class TestCatalogConfigConstruction:
    """CatalogConfig construction with explicit values.

    Verify that all fields can be set explicitly and are stored correctly.
    """

    def test_all_fields_explicit(self):
        """All CatalogConfig fields can be set explicitly."""
        from generators.config import CatalogConfig

        config = CatalogConfig(
            model="claude-haiku-4-5",
            reasoning_effort="low",
            ttl_hours=12,
            diary_catalog_days=7,
            enable_skills=True,
            enable_resources=True,
            resources_dir="/my/resources",
        )
        assert config.model == "claude-haiku-4-5"
        assert config.reasoning_effort == "low"
        assert config.ttl_hours == 12
        assert config.diary_catalog_days == 7
        assert config.enable_skills is True
        assert config.enable_resources is True
        assert config.resources_dir == "/my/resources"

    def test_partial_override_preserves_other_defaults(self):
        """Setting some fields preserves defaults for unset fields."""
        from generators.config import CatalogConfig

        config = CatalogConfig(model="claude-haiku-4-5")
        assert config.model == "claude-haiku-4-5"
        assert config.reasoning_effort == "medium"  # default
        assert config.ttl_hours == 168  # default
        assert config.diary_catalog_days == 7  # default
        assert config.enable_skills is False  # default
        assert config.enable_resources is False  # default
        assert config.resources_dir == ""  # default


# ---------------------------------------------------------------------------
# Config Module Path — generators/config.py exists
# ---------------------------------------------------------------------------


class TestConfigModulePath:
    """Requirement: CatalogConfig lives at scripts/generators/config.py.

    The generators package must exist and config.py must be importable.
    """

    def test_generators_package_exists(self):
        """scripts/generators/ must be a Python package."""
        generators_dir = PLUGIN_ROOT / "scripts" / "generators"
        assert generators_dir.exists(), (
            f"scripts/generators/ directory must exist at {generators_dir}"
        )
        init_file = generators_dir / "__init__.py"
        assert init_file.exists(), (
            f"scripts/generators/__init__.py must exist for package import"
        )

    def test_generators_config_module_exists(self):
        """scripts/generators/config.py must exist."""
        config_file = PLUGIN_ROOT / "scripts" / "generators" / "config.py"
        assert config_file.exists(), (
            f"scripts/generators/config.py must exist at {config_file}"
        )


# ---------------------------------------------------------------------------
# CatalogConfig — TTL zero semantics
# ---------------------------------------------------------------------------


class TestTtlZeroSemantics:
    """Requirement: TTL of zero forces always-regenerate.

    A ttl_hours of 0 means catalogs are always considered stale.
    """

    def test_ttl_zero_is_valid_and_distinct_from_negative(self):
        """Zero TTL is valid; negative is rejected.

        This verifies that validation distinguishes between zero (valid,
        meaning always stale) and negative (invalid, rejected to default).
        """
        from generators.config import CatalogConfig

        zero_config = CatalogConfig(ttl_hours=0)
        neg_config = CatalogConfig(ttl_hours=-1)

        assert zero_config.ttl_hours == 0, "Zero TTL should be accepted"
        assert neg_config.ttl_hours == 168, "Negative TTL should use default"
        assert zero_config.ttl_hours != neg_config.ttl_hours


# ---------------------------------------------------------------------------
# CatalogConfig — Diary window zero semantics
# ---------------------------------------------------------------------------


class TestDiaryWindowZeroSemantics:
    """Requirement: Diary window of zero means no entries processed.

    A diary_catalog_days of 0 means no diary entries are processed.
    """

    def test_diary_catalog_zero_is_valid_and_distinct_from_negative(self):
        """Zero window is valid; negative is rejected."""
        from generators.config import CatalogConfig

        zero_config = CatalogConfig(diary_catalog_days=0)
        neg_config = CatalogConfig(diary_catalog_days=-1)

        assert zero_config.diary_catalog_days == 0, (
            "Zero diary window should be accepted"
        )
        assert neg_config.diary_catalog_days == 7, (
            "Negative diary window should use default"
        )
