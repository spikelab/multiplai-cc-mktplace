"""Tests for plugin skill declarations and skill files."""

import json
import re
from pathlib import Path

import pytest

from conftest import PLUGIN_ROOT


def _skill_frontmatter(skill_file: str) -> dict:
    """Extract frontmatter key/value pairs from a skill markdown file."""
    text = (PLUGIN_ROOT / skill_file).read_text()
    match = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
    if not match:
        return {}
    fm = {}
    for line in match.group(1).splitlines():
        if ':' in line:
            k, _, v = line.partition(':')
            fm[k.strip()] = v.strip().strip('"')
    return fm


_EXPECTED_SKILLS = {
    "setup", "dream", "dream-remember", "health",
    "refresh-catalogs", "memory-health-audit", "backfill", "now",
}

_SKILL_FILES = [f"skills/{name}/SKILL.md" for name in sorted(_EXPECTED_SKILLS)]


class TestSkillFrontmatter:
    """Verify skill files have required YAML frontmatter for CC auto-discovery."""

    def test_skill_count_matches_expected(self):
        skill_files = list((PLUGIN_ROOT / "skills").glob("*/SKILL.md"))
        actual_names = {p.parent.name for p in skill_files}
        assert actual_names == _EXPECTED_SKILLS, (
            f"Skill mismatch. Extra: {actual_names - _EXPECTED_SKILLS!r}, "
            f"Missing: {_EXPECTED_SKILLS - actual_names!r}"
        )

    @pytest.mark.parametrize("skill_file", _SKILL_FILES)
    def test_has_frontmatter(self, skill_file):
        fm = _skill_frontmatter(skill_file)
        assert fm, f"{skill_file} missing YAML frontmatter"

    @pytest.mark.parametrize("skill_file", _SKILL_FILES)
    def test_has_name(self, skill_file):
        fm = _skill_frontmatter(skill_file)
        assert fm.get("name"), f"{skill_file} frontmatter missing 'name'"

    @pytest.mark.parametrize("skill_file", _SKILL_FILES)
    def test_has_description(self, skill_file):
        fm = _skill_frontmatter(skill_file)
        assert fm.get("description"), f"{skill_file} frontmatter missing 'description'"

    def test_skill_names_match_expected(self):
        names = {_skill_frontmatter(f"skills/{p.parent.name}/SKILL.md").get("name")
                 for p in (PLUGIN_ROOT / "skills").glob("*/SKILL.md")}
        names.discard(None)
        assert names == _EXPECTED_SKILLS


class TestSkillFileExistence:
    """Verify skill files exist."""

    @pytest.mark.parametrize("skill_file", ["skills/setup/SKILL.md", "skills/dream/SKILL.md", "skills/health/SKILL.md"])
    def test_skill_file_exists(self, skill_file):
        assert (PLUGIN_ROOT / skill_file).is_file(), f"Skill file missing: {skill_file}"

    @pytest.mark.parametrize("skill_file", ["skills/setup/SKILL.md", "skills/dream/SKILL.md", "skills/health/SKILL.md"])
    def test_skill_file_nonempty(self, skill_file):
        text = (PLUGIN_ROOT / skill_file).read_text()
        assert len(text.strip()) > 0


class TestSetupSkillContent:
    """Verify setup skill markdown content."""

    @pytest.fixture(autouse=True)
    def load_skill(self):
        self.text = (PLUGIN_ROOT / "skills" / "setup" / "SKILL.md").read_text()

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
        self.text = (PLUGIN_ROOT / "skills" / "dream" / "SKILL.md").read_text()

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
        self.text = (PLUGIN_ROOT / "skills" / "health" / "SKILL.md").read_text()

    def test_has_heading(self):
        assert re.search(r"^#\s+", self.text, re.MULTILINE)

    def test_mentions_audit(self):
        assert re.search(r"(?i)(audit|health|check)", self.text)

    def test_no_hardcoded_paths(self):
        assert "/home/" not in self.text
        assert "~/.claude/" not in self.text


class TestSkillsNoDirectSDK:
    """Verify skill files don't import SDK directly."""

    @pytest.mark.parametrize("skill_file", ["skills/setup/SKILL.md", "skills/dream/SKILL.md", "skills/health/SKILL.md"])
    def test_no_direct_sdk_imports(self, skill_file):
        text = (PLUGIN_ROOT / skill_file).read_text()
        assert "import claude_agent_sdk" not in text
        assert "from claude_agent_sdk" not in text
