"""Declaration-level checks for this plugin's own skills.

Restored from the pre-0.31.1 ``test_plugin_skills.py``, which now tests
``lib/plugin_skills.py`` instead. Frontmatter shape (name, description,
name-matches-dir) moved to the ``scripts/lint_skills.py`` gate long ago, but
three checks lived only here and nowhere else:

- the skill roster (an accidentally added or dropped skill directory),
- no hardcoded home paths (``lint_skills.py`` deliberately does not flag
  ``/home/`` because that is the container's own home),
- no direct SDK imports from skill markdown.

The path and SDK checks now cover every skill, not just the original three.
"""

import pytest

from conftest import PLUGIN_ROOT

_EXPECTED_SKILLS = {
    "setup", "dream", "dream-remember", "health",
    "refresh-catalogs", "memory-health-audit", "backfill", "now",
    "log-doctor", "qmd-search", "costs", "config-audit", "fleet-status",
    "memory-bank",
}

_SKILL_FILES = [f"skills/{name}/SKILL.md" for name in sorted(_EXPECTED_SKILLS)]


class TestSkillRoster:
    """The set of shipped skills is intentional, not accidental."""

    def test_skill_count_matches_expected(self):
        skill_files = list((PLUGIN_ROOT / "skills").glob("*/SKILL.md"))
        actual_names = {p.parent.name for p in skill_files}
        assert actual_names == _EXPECTED_SKILLS, (
            f"Skill mismatch. Extra: {actual_names - _EXPECTED_SKILLS!r}, "
            f"Missing: {_EXPECTED_SKILLS - actual_names!r}"
        )


class TestSkillContentHygiene:
    """Skill bodies stay portable and SDK-free."""

    @pytest.mark.parametrize("skill_file", _SKILL_FILES)
    def test_no_hardcoded_paths(self, skill_file):
        text = (PLUGIN_ROOT / skill_file).read_text()
        assert "/home/spike" not in text, f"{skill_file} hardcodes a host home path"
        assert "/Users/" not in text, f"{skill_file} hardcodes a host home path"
        assert "~/.claude/" not in text, (
            f"{skill_file} hardcodes ~/.claude/ — use $CLAUDE_CONFIG_DIR"
        )

    @pytest.mark.parametrize("skill_file", _SKILL_FILES)
    def test_no_direct_sdk_imports(self, skill_file):
        text = (PLUGIN_ROOT / skill_file).read_text()
        assert "import claude_agent_sdk" not in text
        assert "from claude_agent_sdk" not in text
