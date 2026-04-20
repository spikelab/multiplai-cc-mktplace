"""Tests for plugin repository scaffold and manifests."""

import json
import re
from pathlib import Path

import pytest

from conftest import PLUGIN_ROOT


# ---------------------------------------------------------------------------
# Directory structure
# ---------------------------------------------------------------------------

class TestDirectoryStructure:
    """Verify the D1 directory layout exists."""

    def test_plugin_root_exists(self):
        assert PLUGIN_ROOT.is_dir(), f"multiplai-plugin/ does not exist at {PLUGIN_ROOT}"

    def test_scripts_dir_exists(self):
        assert (PLUGIN_ROOT / "scripts").is_dir()

    def test_scripts_lib_dir_exists(self):
        assert (PLUGIN_ROOT / "scripts" / "lib").is_dir()

    def test_skills_dir_exists(self):
        assert (PLUGIN_ROOT / "skills").is_dir()

    def test_templates_dir_exists(self):
        assert (PLUGIN_ROOT / "templates").is_dir()

    def test_lib_init_exists(self):
        assert (PLUGIN_ROOT / "scripts" / "lib" / "__init__.py").is_file()

    @pytest.mark.parametrize("module", ["paths.py", "model_client.py", "log_utils.py", "config.py"])
    def test_lib_modules_exist(self, module):
        assert (PLUGIN_ROOT / "scripts" / "lib" / module).is_file()


# ---------------------------------------------------------------------------
# plugin.json
# ---------------------------------------------------------------------------

class TestPluginJson:
    """Verify plugin.json manifest."""

    @pytest.fixture(autouse=True)
    def load_manifest(self):
        path = PLUGIN_ROOT / "plugin.json"
        assert path.is_file(), "plugin.json does not exist"
        self.manifest = json.loads(path.read_text())

    def test_required_fields_present(self):
        for field in ("name", "version", "description", "author", "license", "engines", "entrypoints"):
            assert field in self.manifest, f"Missing field: {field}"

    def test_name_is_multiplai(self):
        assert self.manifest["name"] == "multiplai"

    def test_version_is_semver(self):
        assert re.match(r"^\d+\.\d+\.\d+$", self.manifest["version"])

    def test_engines_claude_code(self):
        assert "claude-code" in self.manifest["engines"]

    def test_user_config_memory_dir(self):
        cfg = self.manifest["userConfig"]
        assert "memory_dir" in cfg
        assert cfg["memory_dir"]["default"] == "~/.multiplai/memory"

    def test_user_config_diary_dir(self):
        cfg = self.manifest["userConfig"]
        assert "diary_dir" in cfg
        assert cfg["diary_dir"]["default"] == "~/.multiplai/diary"

    def test_user_config_api_key_sensitive(self):
        cfg = self.manifest["userConfig"]
        assert "anthropic_api_key" in cfg
        assert cfg["anthropic_api_key"].get("sensitive") is True

    def test_skills_declared(self):
        assert "skills" in self.manifest
        names = {s["name"] for s in self.manifest["skills"]}
        assert names == {"setup", "dream", "health", "refresh-catalogs"}

    def test_skills_have_files(self):
        for skill in self.manifest["skills"]:
            assert "file" in skill
            skill_path = PLUGIN_ROOT / skill["file"]
            assert skill_path.is_file(), f"Skill file missing: {skill['file']}"

    def test_skills_have_descriptions(self):
        for skill in self.manifest["skills"]:
            assert skill.get("description"), f"Skill {skill['name']} missing description"


# ---------------------------------------------------------------------------
# marketplace.json
# ---------------------------------------------------------------------------

class TestMarketplaceJson:
    """Verify marketplace.json metadata."""

    @pytest.fixture(autouse=True)
    def load_manifest(self):
        path = PLUGIN_ROOT / "marketplace.json"
        assert path.is_file(), "marketplace.json does not exist"
        self.manifest = json.loads(path.read_text())

    def test_required_fields_present(self):
        for field in ("name", "displayName", "description", "author", "repository", "categories", "keywords"):
            assert field in self.manifest, f"Missing field: {field}"

    def test_repository_is_github(self):
        assert re.match(r"https://github\.com/.+/.+", self.manifest["repository"])

    def test_categories_non_empty(self):
        assert isinstance(self.manifest["categories"], list)
        assert len(self.manifest["categories"]) > 0


# ---------------------------------------------------------------------------
# hooks.json
# ---------------------------------------------------------------------------

class TestHooksJson:
    """Verify hooks.json declarations."""

    @pytest.fixture(autouse=True)
    def load_hooks(self):
        path = PLUGIN_ROOT / "hooks.json"
        assert path.is_file(), "hooks.json does not exist"
        self.hooks = json.loads(path.read_text())

    def test_hooks_key_exists(self):
        assert "hooks" in self.hooks

    def test_all_event_types_registered(self):
        events = {h["event"] for h in self.hooks["hooks"]}
        expected = {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "PreCompact"}
        assert events == expected

    def test_no_unexpected_events(self):
        allowed = {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "PreCompact"}
        for hook in self.hooks["hooks"]:
            assert hook["event"] in allowed, f"Unexpected event: {hook['event']}"

    def test_each_hook_has_script(self):
        for hook in self.hooks["hooks"]:
            assert "script" in hook, f"Hook {hook['event']} missing script"

    def test_hook_scripts_exist(self):
        for hook in self.hooks["hooks"]:
            script_path = PLUGIN_ROOT / hook["script"]
            assert script_path.is_file(), f"Hook script missing: {hook['script']}"

    def test_no_duplicate_event_script_pairs(self):
        pairs = [(h["event"], h["script"]) for h in self.hooks["hooks"]]
        assert len(pairs) == len(set(pairs)), "Duplicate event-script pairs found"


# ---------------------------------------------------------------------------
# LICENSE, README, CHANGELOG, requirements.txt
# ---------------------------------------------------------------------------

class TestSupportFiles:

    def test_license_exists_and_nonempty(self):
        path = PLUGIN_ROOT / "LICENSE"
        assert path.is_file()
        lines = path.read_text().strip().splitlines()
        assert len(lines) >= 10

    def test_license_matches_plugin_json(self):
        manifest = json.loads((PLUGIN_ROOT / "plugin.json").read_text())
        license_text = (PLUGIN_ROOT / "LICENSE").read_text()
        if manifest["license"] == "MIT":
            assert "MIT" in license_text

    def test_readme_exists(self):
        assert (PLUGIN_ROOT / "README.md").is_file()

    def test_readme_has_installation(self):
        text = (PLUGIN_ROOT / "README.md").read_text()
        assert re.search(r"(?i)install", text)
        assert "claude --plugin-dir" in text

    def test_readme_documents_config(self):
        text = (PLUGIN_ROOT / "README.md").read_text()
        for field in ("memory_dir", "diary_dir", "anthropic_api_key"):
            assert field in text, f"README missing config field: {field}"

    def test_readme_lists_skills(self):
        text = (PLUGIN_ROOT / "README.md").read_text()
        for skill in ("multiplai:setup", "multiplai:dream", "multiplai:health"):
            assert skill in text, f"README missing skill: {skill}"

    def test_changelog_exists(self):
        assert (PLUGIN_ROOT / "CHANGELOG.md").is_file()

    def test_changelog_has_version_entry(self):
        manifest = json.loads((PLUGIN_ROOT / "plugin.json").read_text())
        version = manifest["version"]
        text = (PLUGIN_ROOT / "CHANGELOG.md").read_text()
        assert version in text

    def test_requirements_exists(self):
        assert (PLUGIN_ROOT / "requirements.txt").is_file()

    def test_requirements_has_anthropic(self):
        text = (PLUGIN_ROOT / "requirements.txt").read_text()
        assert "anthropic" in text

    def test_requirements_has_pyyaml(self):
        text = (PLUGIN_ROOT / "requirements.txt").read_text()
        assert "pyyaml" in text.lower()

    def test_requirements_no_claude_agent_sdk(self):
        text = (PLUGIN_ROOT / "requirements.txt").read_text()
        assert "claude-agent-sdk" not in text.lower()
        assert "claude_agent_sdk" not in text.lower()


# ---------------------------------------------------------------------------
# No hardcoded paths
# ---------------------------------------------------------------------------

class TestNoHardcodedPaths:
    """Verify no user-specific paths in scaffold files."""

    SCAFFOLD_FILES = [
        "plugin.json", "marketplace.json", "hooks.json",
        "README.md", "CHANGELOG.md", "requirements.txt",
    ]

    @pytest.mark.parametrize("filename", SCAFFOLD_FILES)
    def test_no_hardcoded_home_paths(self, filename):
        path = PLUGIN_ROOT / filename
        if not path.exists():
            pytest.skip(f"{filename} does not exist")
        text = path.read_text()
        assert "/home/spike" not in text
        assert "/Users/spike" not in text

    def test_user_config_defaults_use_tilde(self):
        manifest = json.loads((PLUGIN_ROOT / "plugin.json").read_text())
        for key, cfg in manifest.get("userConfig", {}).items():
            if "default" in cfg and "/" in str(cfg["default"]):
                assert str(cfg["default"]).startswith("~"), \
                    f"userConfig.{key}.default should use ~ prefix"
