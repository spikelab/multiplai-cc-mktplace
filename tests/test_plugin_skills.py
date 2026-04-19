"""Tests for plugin skill declarations and skill files."""

import json
import re
from pathlib import Path

import pytest

from conftest import PLUGIN_ROOT


class TestSkillManifest:
    """Verify skill declarations in plugin.json."""

    @pytest.fixture(autouse=True)
    def load_manifest(self):
        self.manifest = json.loads((PLUGIN_ROOT / "plugin.json").read_text())

    def test_has_skills_array(self):
        assert "skills" in self.manifest
        assert isinstance(self.manifest["skills"], list)

    def test_exactly_three_skills(self):
        assert len(self.manifest["skills"]) == 3

    def test_skill_names(self):
        names = {s["name"] for s in self.manifest["skills"]}
        assert names == {"setup", "dream", "health"}

    def test_skills_have_descriptions(self):
        for skill in self.manifest["skills"]:
            assert skill.get("description"), f"Skill {skill['name']} missing description"

    def test_skills_have_files(self):
        for skill in self.manifest["skills"]:
            assert "file" in skill
            assert skill["file"].endswith(".md")


class TestSkillFileExistence:
    """Verify skill files exist."""

    @pytest.mark.parametrize("skill_file", ["skills/setup.md", "skills/dream.md", "skills/health.md"])
    def test_skill_file_exists(self, skill_file):
        assert (PLUGIN_ROOT / skill_file).is_file(), f"Skill file missing: {skill_file}"

    @pytest.mark.parametrize("skill_file", ["skills/setup.md", "skills/dream.md", "skills/health.md"])
    def test_skill_file_nonempty(self, skill_file):
        text = (PLUGIN_ROOT / skill_file).read_text()
        assert len(text.strip()) > 0


class TestSetupSkillContent:
    """Verify setup skill markdown content."""

    @pytest.fixture(autouse=True)
    def load_skill(self):
        self.text = (PLUGIN_ROOT / "skills" / "setup.md").read_text()

    def test_has_heading(self):
        assert re.search(r"^#\s+", self.text, re.MULTILINE)

    def test_mentions_onboarding(self):
        assert re.search(r"(?i)(onboard|interview|setup)", self.text)

    def test_mentions_templates(self):
        assert re.search(r"(?i)template", self.text)

    def test_no_hardcoded_paths(self):
        assert "/home/" not in self.text
        assert "~/.claude/" not in self.text


class TestDreamSkillContent:
    """Verify dream skill markdown content."""

    @pytest.fixture(autouse=True)
    def load_skill(self):
        self.text = (PLUGIN_ROOT / "skills" / "dream.md").read_text()

    def test_has_heading(self):
        assert re.search(r"^#\s+", self.text, re.MULTILINE)

    def test_mentions_consolidation(self):
        assert re.search(r"(?i)(consolidat|dream|synthesi)", self.text)

    def test_no_hardcoded_paths(self):
        assert "/home/" not in self.text
        assert "~/.claude/" not in self.text


class TestHealthSkillContent:
    """Verify health skill markdown content."""

    @pytest.fixture(autouse=True)
    def load_skill(self):
        self.text = (PLUGIN_ROOT / "skills" / "health.md").read_text()

    def test_has_heading(self):
        assert re.search(r"^#\s+", self.text, re.MULTILINE)

    def test_mentions_audit(self):
        assert re.search(r"(?i)(audit|health|check)", self.text)

    def test_no_hardcoded_paths(self):
        assert "/home/" not in self.text
        assert "~/.claude/" not in self.text


class TestSkillsNoDirectSDK:
    """Verify skill files don't import SDK directly."""

    @pytest.mark.parametrize("skill_file", ["skills/setup.md", "skills/dream.md", "skills/health.md"])
    def test_no_direct_sdk_imports(self, skill_file):
        text = (PLUGIN_ROOT / skill_file).read_text()
        assert "import claude_agent_sdk" not in text
        assert "from claude_agent_sdk" not in text
