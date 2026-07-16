"""Tests for lib/routing_validation.py — deterministic post-proposal gate.

Covers:
- build_section_registry() maps H2 names to owning files
- parse_proposal_entries() extracts targets/sections/text, skips action items
- validate_proposal() flags misrouted sections (section owned by another file)
- validate_proposal() flags new-section name collisions
- validate_proposal() flags cross-file n-gram duplicates (planted duplicate)
- append_routing_warnings() always appends the section — "(none)" when clean
- dream.py wires the gate into proposal generation (fail-open + loud)
- dream-remember SKILL.md consults the Routing Warnings section
"""

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.routing_validation import (  # noqa: E402
    append_routing_warnings,
    build_section_registry,
    find_duplicate_content,
    parse_proposal_entries,
    render_warnings_section,
    validate_proposal,
)


# A paragraph long enough to form many 8-grams, reused as the planted duplicate.
_DUP_TEXT = (
    "Always run the release script from the runtime checkout because setup.sh "
    "resolves every path relative to its own script directory and running it "
    "from a dev clone silently provisions a second runtime."
)


def _memory_contents():
    return {
        "python.md": (
            "# Python\n\n## Asyncio Patterns\n\n- use TaskGroup\n\n"
            "## Packaging\n\n- uv only\n"
        ),
        "git-policy.md": (
            "# Git\n\n## Worktrees\n\n- under $WORKSPACE/.worktrees/\n\n"
            f"## Release Flow\n\n{_DUP_TEXT}\n"
        ),
    }


def _proposal(target: str, section_field: str, text: str = "Fresh insight about tooling.") -> str:
    quoted = "\n".join(f"> {line}" for line in text.splitlines())
    return (
        "# Dream proposal\n\n"
        f"## Updates for `{target}`\n\n"
        "### 1. Some update\n"
        f"**Section:** {section_field}\n"
        f"{quoted}\n"
    )


class TestBuildSectionRegistry:
    def test_maps_sections_to_files(self, tmp_path):
        (tmp_path / "a.md").write_text("# A\n\n## Alpha\n\nx\n\n## Beta\n\ny\n")
        (tmp_path / "b.md").write_text("# B\n\n## Gamma\n\nz\n")
        registry = build_section_registry(tmp_path)
        assert registry["Alpha"] == ["a.md"]
        assert registry["Gamma"] == ["b.md"]

    def test_duplicate_section_lists_both_files(self, tmp_path):
        (tmp_path / "a.md").write_text("## Shared\n")
        (tmp_path / "b.md").write_text("## Shared\n")
        assert sorted(build_section_registry(tmp_path)["Shared"]) == ["a.md", "b.md"]

    def test_missing_dir_returns_empty(self, tmp_path):
        assert build_section_registry(tmp_path / "nope") == {}


class TestParseProposalEntries:
    def test_extracts_target_section_and_text(self):
        entries = parse_proposal_entries(_proposal("python.md", "Asyncio Patterns", "Line one.\nLine two."))
        assert len(entries) == 1
        e = entries[0]
        assert e["target"] == "python.md"
        assert e["number"] == "1"
        assert e["section"] == "Asyncio Patterns"
        assert e["text"] == "Line one.\nLine two."

    def test_skips_action_items(self):
        proposal = (
            "## Updates for `python.md`\n\n"
            "### 1. Real update\n**Section:** Packaging\n> text here\n\n"
            "## Action Items\n\n### A1. Do a thing\n> not a memory update\n"
        )
        entries = parse_proposal_entries(proposal)
        assert [e["number"] for e in entries] == ["1"]

    def test_other_h2_ends_file_block(self):
        # A trailing "## Routing Warnings" (or "## Filtered Out") section must
        # not be parsed as content belonging to the last file.
        proposal = _proposal("python.md", "Packaging") + "\n## Routing Warnings\n\n- bogus\n"
        entries = parse_proposal_entries(proposal)
        assert len(entries) == 1
        assert "bogus" not in entries[0]["text"]


class TestSectionChecks:
    def test_misrouted_section_flagged_with_reroute(self):
        # "Worktrees" lives in git-policy.md, proposal targets python.md.
        warnings = validate_proposal(_proposal("python.md", "Worktrees"), _memory_contents())
        assert len(warnings) == 1
        assert "Worktrees" in warnings[0]
        assert "reroute to `git-policy.md`" in warnings[0]

    def test_correctly_routed_section_clean(self):
        warnings = validate_proposal(_proposal("python.md", "Packaging"), _memory_contents())
        assert warnings == []

    def test_new_section_collision_flagged(self):
        warnings = validate_proposal(
            _proposal("python.md", 'New section: "Worktrees"'), _memory_contents()
        )
        assert len(warnings) == 1
        assert "collides" in warnings[0]
        assert "git-policy.md" in warnings[0]

    def test_new_unique_section_clean(self):
        warnings = validate_proposal(
            _proposal("python.md", 'New section: "Typing Discipline"'), _memory_contents()
        )
        assert warnings == []

    def test_new_section_in_own_file_not_a_collision(self):
        # Re-declaring a section the target file already owns is not cross-file.
        warnings = validate_proposal(
            _proposal("python.md", 'New section: "Packaging"'), _memory_contents()
        )
        assert warnings == []


class TestDuplicateDetection:
    def test_planted_duplicate_in_another_file_flagged(self):
        warnings = validate_proposal(
            _proposal("python.md", "Packaging", _DUP_TEXT), _memory_contents()
        )
        dup = [w for w in warnings if "already present" in w]
        assert len(dup) == 1
        assert "ANOTHER file" in dup[0]
        assert "git-policy.md:" in dup[0]

    def test_duplicate_in_target_file_labeled_target(self):
        warnings = validate_proposal(
            _proposal("git-policy.md", "Release Flow", _DUP_TEXT), _memory_contents()
        )
        dup = [w for w in warnings if "already present" in w]
        assert len(dup) == 1
        assert "target file" in dup[0]

    def test_short_text_never_flagged(self):
        # Below one 8-gram there is no signal — must not warn.
        assert find_duplicate_content("use TaskGroup", _memory_contents()) == []

    def test_fresh_text_clean(self):
        hits = find_duplicate_content(
            "Entirely novel guidance about a subsystem no memory file mentions "
            "anywhere in its current content today.",
            _memory_contents(),
        )
        assert hits == []


class TestAppendRoutingWarnings:
    def test_clean_proposal_gets_none_marker(self):
        out = append_routing_warnings(_proposal("python.md", "Packaging"), _memory_contents())
        assert "## Routing Warnings" in out
        assert "(none)" in out.split("## Routing Warnings")[1]

    def test_dirty_proposal_gets_bullets(self):
        out = append_routing_warnings(_proposal("python.md", "Worktrees"), _memory_contents())
        tail = out.split("## Routing Warnings")[1]
        assert "- " in tail
        assert "(none)" not in tail

    def test_render_section_shape(self):
        assert render_warnings_section([]).endswith("(none)\n")
        assert "- w1" in render_warnings_section(["w1"])


class TestDreamWiring:
    """The gate must be wired into dream.py's proposal generation, fail-open."""

    def setup_method(self):
        self.source = (SCRIPTS_DIR / "dream.py").read_text()

    def test_generate_proposal_appends_warnings(self):
        assert "_with_routing_warnings" in self.source
        assert "render_warnings_section" in self.source

    def test_gate_is_fail_open_and_loud(self):
        # A gate crash must never lose the proposal, and must be logged.
        assert "WITHOUT a Routing Warnings section" in self.source


class TestSkillConsultsWarnings:
    def test_dream_remember_skill_mentions_routing_warnings(self):
        text = (PLUGIN_ROOT / "skills" / "dream-remember" / "SKILL.md").read_text()
        assert "## Routing Warnings" in text
        assert "Never silently apply" in text
