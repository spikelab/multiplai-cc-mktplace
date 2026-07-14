"""Tests for the /multiplai-context:config-audit skill — subtractive config review.

Covers: skill definition (frontmatter for CC auto-discovery) and prompt
content — the config surface it must enumerate, the three-way rule
classification, the removals-first proposal to .multiplai/dreams/, the
propose-don't-apply contract, and the state-stamp step that closes the
90-day SessionStart nudge gate (delegated to the deterministic
scripts/config_audit.py --stamp entry point — never hand-written YAML,
which broke on installs whose data dir comes from an env override).
"""

import re

from conftest import PLUGIN_ROOT

SKILL_FILE = PLUGIN_ROOT / "skills" / "config-audit" / "SKILL.md"


def _frontmatter() -> dict:
    text = SKILL_FILE.read_text()
    m = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            if ':' in line:
                k, _, v = line.partition(':')
                fm[k.strip()] = v.strip().strip('"')
    return fm


class TestConfigAuditSkillFrontmatter:
    """Verify config-audit skill frontmatter for CC auto-discovery."""

    def test_skill_file_exists(self):
        assert SKILL_FILE.is_file()

    def test_has_frontmatter(self):
        assert _frontmatter(), "config-audit SKILL.md missing YAML frontmatter"

    def test_skill_name(self):
        assert _frontmatter().get("name") == "config-audit"

    def test_has_description(self):
        assert _frontmatter().get("description", "").strip()

    def test_description_states_no_apply(self):
        """The description itself must advertise the propose-only contract."""
        assert re.search(
            r"(?i)does not apply|NOT apply", _frontmatter().get("description", "")
        )


class TestConfigAuditSkillPrompt:
    """Verify the SKILL.md instructions meet the config-audit design."""

    @classmethod
    def setup_class(cls):
        cls.text = SKILL_FILE.read_text()

    def test_enumerates_config_surface(self):
        """Must name every layer of the active config surface."""
        assert "$CLAUDE_CONFIG_DIR/CLAUDE.md" in self.text
        assert re.search(r"(?i)workspace `?CLAUDE\.md`?", self.text)
        assert "settings.json" in self.text
        assert re.search(r"(?i)permissions", self.text)
        assert re.search(r"(?i)hook registrations", self.text)
        assert re.search(r"(?i)memory-file standing rules", self.text)

    def test_three_way_classification(self):
        assert "still-serving" in self.text
        assert "obsolete" in self.text
        assert "model-constraining" in self.text

    def test_proposal_path(self):
        """Proposal goes to .multiplai/dreams/config-audit-YYYY-MM-DD.md."""
        assert ".multiplai/dreams/config-audit-YYYY-MM-DD.md" in self.text

    def test_subtractive_ordering(self):
        """Removals first, edits second, additions only if a removal needs one."""
        assert re.search(r"(?i)removals first", self.text)
        i_rem = self.text.lower().index("removals")
        i_edt = self.text.lower().index("edits")
        i_add = self.text.lower().index("additions")
        assert i_rem < i_edt < i_add
        assert re.search(r"(?i)additions.*only.*removal", self.text, re.DOTALL)

    def test_never_instructs_applying_changes(self):
        """Every 'apply' occurrence must sit in a negation — the skill
        proposes; it never applies."""
        for m in re.finditer(r"(?i)\bappl(y|ies|ied|ying)\b", self.text):
            window = self.text[max(0, m.start() - 80):m.start()]
            assert re.search(r"(?i)\b(not|never|nor)\b", window), (
                f"non-negated 'apply' at offset {m.start()}: "
                f"...{self.text[max(0, m.start() - 60):m.end() + 20]}..."
            )

    def test_motivating_examples_present(self):
        """The three canonical decay cases must be in the prompt."""
        assert re.search(r"(?i)single-file", self.text)      # refactor rule
        assert re.search(r"(?i)perforce", self.text)          # redundant hook
        assert "2026-07-08" in self.text                       # kit env-var regression
        assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" in self.text

    def test_stamps_state_file(self):
        """Final step must stamp config_audit_state.yaml beside the dream state."""
        assert "config_audit_state.yaml" in self.text
        assert "dream_state.yaml" in self.text
        assert "last_run" in self.text

    def test_stamp_uses_deterministic_script(self):
        """Step 6 must invoke config_audit.py --stamp — never hand-write YAML.

        The gate reads paths.data_dir()/config_audit_state.yaml (a 4-way
        env cascade); a hand-located path misses it on CLAUDE_PLUGIN_DATA /
        option-override installs and the nudge then fires forever.
        """
        assert '${CLAUDE_PLUGIN_ROOT}/scripts/config_audit.py" --stamp' in self.text
        # No hand-rolled timestamp command and no YAML block to copy out.
        assert "date -u +" not in self.text
        assert "<UTC ISO-8601 timestamp>" not in self.text

    def test_stamp_script_exists(self):
        """The entry point step 6 points at must actually ship."""
        assert (PLUGIN_ROOT / "scripts" / "config_audit.py").is_file()

    def test_stamp_survives_clean_audit(self):
        """The stamp must be required even when nothing is found to remove."""
        assert re.search(
            r"(?i)even (on a clean audit|when the audit found nothing)", self.text
        )
