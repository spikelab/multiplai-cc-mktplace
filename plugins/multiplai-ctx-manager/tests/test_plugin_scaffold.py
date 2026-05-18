"""Tests for plugin repository scaffold and manifests."""

import json
import re
from pathlib import Path

import pytest

from conftest import (
    PLUGIN_ROOT,
    REPO_ROOT,
    HOOKS_JSON,
    MARKETPLACE_JSON,
    EXPECTED_HOOK_SCRIPTS,
    parse_hooks,
)


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
        path = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
        assert path.is_file(), ".claude-plugin/plugin.json does not exist"
        self.manifest = json.loads(path.read_text())

    def test_required_fields_present(self):
        for field in ("name", "version", "description", "author", "license"):
            assert field in self.manifest, f"Missing field: {field}"

    def test_name_is_multiplai(self):
        assert self.manifest["name"] == "multiplai"

    def test_version_is_semver(self):
        assert re.match(r"^\d+\.\d+\.\d+$", self.manifest["version"])

    def test_author_is_object(self):
        assert isinstance(self.manifest["author"], dict), "author must be an object"
        assert "name" in self.manifest["author"]

    def test_user_config_memory_dir(self):
        cfg = self.manifest["userConfig"]
        assert "memory_dir" in cfg
        # Empty default delegates to the workspace_dir cascade in lib.paths;
        # see test_user_config_workspace_dir.
        assert cfg["memory_dir"]["default"] == ""

    def test_user_config_diary_dir(self):
        cfg = self.manifest["userConfig"]
        assert "diary_dir" in cfg
        assert cfg["diary_dir"]["default"] == ""

    def test_user_config_workspace_dir(self):
        """workspace_dir is the anchor for memory/diary/now/learnings defaults."""
        cfg = self.manifest["userConfig"]
        assert "workspace_dir" in cfg
        assert cfg["workspace_dir"]["default"] == ""

    def test_user_config_now_dir(self):
        cfg = self.manifest["userConfig"]
        assert "now_dir" in cfg
        assert cfg["now_dir"]["default"] == ""

    def test_user_config_learnings_dir(self):
        cfg = self.manifest["userConfig"]
        assert "learnings_dir" in cfg
        assert cfg["learnings_dir"]["default"] == ""

    def test_user_config_api_key_sensitive(self):
        cfg = self.manifest["userConfig"]
        assert "anthropic_api_key" in cfg
        assert cfg["anthropic_api_key"].get("sensitive") is True

    def test_userconfig_fields_have_title(self):
        for key, cfg in self.manifest.get("userConfig", {}).items():
            assert "title" in cfg, f"userConfig.{key} missing title"
            assert isinstance(cfg["title"], str), f"userConfig.{key}.title must be string"


# ---------------------------------------------------------------------------
# marketplace.json
# ---------------------------------------------------------------------------

class TestMarketplaceJson:
    """Verify marketplace.json metadata (Claude Code marketplace schema)."""

    @pytest.fixture(autouse=True)
    def load_manifest(self):
        assert MARKETPLACE_JSON.is_file(), "marketplace.json does not exist"
        self.manifest = json.loads(MARKETPLACE_JSON.read_text())

    def test_required_fields_present(self):
        for field in ("name", "owner", "description", "plugins"):
            assert field in self.manifest, f"Missing field: {field}"

    def test_owner_is_object_with_name(self):
        owner = self.manifest["owner"]
        assert isinstance(owner, dict), "owner must be an object"
        assert owner.get("name"), "owner.name must be present"

    def test_plugins_non_empty(self):
        plugins = self.manifest["plugins"]
        assert isinstance(plugins, list)
        assert len(plugins) > 0, "marketplace.json must declare at least one plugin"

    def test_first_plugin_repository_is_github(self):
        plugin = self.manifest["plugins"][0]
        assert "repository" in plugin, "plugins[0] missing repository"
        assert re.match(r"https://github\.com/.+/.+", plugin["repository"])

    def test_first_plugin_has_keywords_list(self):
        plugin = self.manifest["plugins"][0]
        assert "keywords" in plugin, "plugins[0] missing keywords"
        assert isinstance(plugin["keywords"], list)
        assert len(plugin["keywords"]) > 0


# ---------------------------------------------------------------------------
# hooks.json
# ---------------------------------------------------------------------------

class TestHooksJson:
    """Verify hooks/hooks.json declarations (official nested CC schema)."""

    @pytest.fixture(autouse=True)
    def load_hooks(self):
        assert HOOKS_JSON.is_file(), "hooks/hooks.json does not exist"
        self.hooks = parse_hooks()

    def test_hooks_key_exists(self):
        assert "hooks" in json.loads(HOOKS_JSON.read_text())

    def test_all_event_types_registered(self):
        events = {h["event"] for h in self.hooks}
        assert events == set(EXPECTED_HOOK_SCRIPTS.keys())

    def test_no_unexpected_events(self):
        allowed = set(EXPECTED_HOOK_SCRIPTS.keys())
        for hook in self.hooks:
            assert hook["event"] in allowed, f"Unexpected event: {hook['event']}"

    def test_each_hook_has_script(self):
        for hook in self.hooks:
            assert hook["script"], f"Hook {hook['event']} missing script"

    def test_hook_scripts_exist(self):
        for hook in self.hooks:
            script_path = PLUGIN_ROOT / hook["script"]
            assert script_path.is_file(), f"Hook script missing: {hook['script']}"

    def test_no_duplicate_event_script_pairs(self):
        pairs = [(h["event"], h["script"]) for h in self.hooks]
        assert len(pairs) == len(set(pairs)), "Duplicate event-script pairs found"


# ---------------------------------------------------------------------------
# LICENSE, README, CHANGELOG, requirements.txt
# ---------------------------------------------------------------------------

class TestSupportFiles:

    # In the marketplace monorepo a single LICENSE covers the whole repo;
    # it lives at REPO_ROOT (fall back to the plugin dir if a per-plugin
    # LICENSE is ever added).
    LICENSE_PATH = (
        REPO_ROOT / "LICENSE"
        if (REPO_ROOT / "LICENSE").is_file()
        else PLUGIN_ROOT / "LICENSE"
    )

    def test_license_exists_and_nonempty(self):
        assert self.LICENSE_PATH.is_file()
        lines = self.LICENSE_PATH.read_text().strip().splitlines()
        assert len(lines) >= 10

    def test_license_matches_plugin_json(self):
        manifest = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())
        license_text = self.LICENSE_PATH.read_text()
        if manifest["license"] == "MIT":
            assert "MIT" in license_text

    def test_readme_exists(self):
        assert (PLUGIN_ROOT / "README.md").is_file()

    def test_readme_has_installation(self):
        text = (PLUGIN_ROOT / "README.md").read_text()
        assert re.search(r"(?i)install", text)
        # Marketplace-based install — accept any documented install path.
        assert any(marker in text for marker in (
            "/plugin marketplace add",
            "/plugin install",
            "plugin-dir",
        )), "README must document marketplace/plugin installation"

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
        manifest = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())
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

    def test_requirements_pins_claude_agent_sdk(self):
        # Installed into the plugin venv (so standalone/skill SDK runs
        # work, not only host-injected hooks); pinned to avoid drift.
        text = (PLUGIN_ROOT / "requirements.txt").read_text()
        assert "claude-agent-sdk==" in text.lower()


# ---------------------------------------------------------------------------
# No hardcoded paths
# ---------------------------------------------------------------------------

class TestNoHardcodedPaths:
    """Verify no user-specific paths in scaffold files."""

    # marketplace.json lives at REPO_ROOT/.claude-plugin/, hooks.json at
    # PLUGIN_ROOT/hooks/ — use explicit Path objects rather than PLUGIN_ROOT / name.
    SCAFFOLD_FILES = [
        PLUGIN_ROOT / ".claude-plugin" / "plugin.json",
        MARKETPLACE_JSON,
        HOOKS_JSON,
        PLUGIN_ROOT / "README.md",
        PLUGIN_ROOT / "CHANGELOG.md",
        PLUGIN_ROOT / "requirements.txt",
    ]

    @pytest.mark.parametrize("path", SCAFFOLD_FILES, ids=lambda p: p.name)
    def test_no_hardcoded_home_paths(self, path):
        if not path.exists():
            pytest.skip(f"{path.name} does not exist")
        text = path.read_text()
        assert "/home/spike" not in text
        assert "/Users/spike" not in text

    def test_user_config_defaults_use_tilde(self):
        manifest = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())
        for key, cfg in manifest.get("userConfig", {}).items():
            if "default" in cfg and "/" in str(cfg["default"]):
                assert str(cfg["default"]).startswith("~"), \
                    f"userConfig.{key}.default should use ~ prefix"
