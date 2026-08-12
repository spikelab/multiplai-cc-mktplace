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
    find_batch_near_duplicate_groups,
    find_duplicate_content,
    find_near_duplicate_line,
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


def _proposal(
    target: str,
    section_field: str,
    text: str = "Fresh insight about tooling.",
    change: str = "add",
) -> str:
    quoted = "\n".join(f"> {line}" for line in text.splitlines())
    return (
        "# Dream proposal\n\n"
        f"## Updates for `{target}`\n\n"
        "### 1. Some update\n"
        f"**Section:** {section_field}\n"
        f"**Change:** {change}\n"
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
        assert e["change"] == "add"
        assert e["text"] == "Line one.\nLine two."

    def test_change_field_lowercased(self):
        entries = parse_proposal_entries(_proposal("python.md", "Packaging", change="Update"))
        assert entries[0]["change"] == "update"

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

    def test_new_section_dash_variants_also_flagged(self):
        # The docstring promises "New section — Name" works; the em-dash /
        # hyphen separators must parse to the bare name, not "— Name".
        for field in ("New section — Worktrees", "New section - Worktrees"):
            warnings = validate_proposal(
                _proposal("python.md", field), _memory_contents()
            )
            assert len(warnings) == 1, field
            assert "collides" in warnings[0]
            assert '"Worktrees"' in warnings[0]

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

    def test_update_entry_exempt_from_target_file_dedup(self):
        # An update/replace revises existing text — overlap with its OWN
        # target file is expected, not a warning.
        for change in ("update", "replace"):
            warnings = validate_proposal(
                _proposal("git-policy.md", "Release Flow", _DUP_TEXT, change=change),
                _memory_contents(),
            )
            assert [w for w in warnings if "already present" in w] == [], change

    def test_update_entry_still_flagged_for_cross_file_duplicate(self):
        # The exemption is target-file only — text that already lives in a
        # DIFFERENT file stays a warning even for updates.
        warnings = validate_proposal(
            _proposal("python.md", "Packaging", _DUP_TEXT, change="update"),
            _memory_contents(),
        )
        dup = [w for w in warnings if "already present" in w]
        assert len(dup) == 1
        assert "ANOTHER file" in dup[0]
        assert "git-policy.md:" in dup[0]

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


# A rule stated one way in memory, and the same rule stated another way in a
# proposal item. They share no 8-gram, so the n-gram check is blind to the pair
# — this is the shape issue #195 measured, and what the word-overlap check is
# for.
_EXISTING_RULE = (
    "Worktrees for a project must live under the shared worktrees directory, "
    "never scattered inside project folders."
)
_RESTATEMENT = (
    "Never scatter worktrees inside project directories; every worktree lives "
    "under the shared worktrees directory."
)


def _memory_with_rule():
    contents = _memory_contents()
    contents["git-policy.md"] += f"\n## Worktree Location\n\n- {_EXISTING_RULE}\n"
    return contents


def _multi_entry_proposal(target: str, *texts: str, change: str = "add") -> str:
    out = ["# Dream proposal", "", f"## Updates for `{target}`", ""]
    for number, text in enumerate(texts, start=1):
        out += [
            f"### {number}. Entry {number}",
            "**Section:** Worktree Location",
            f"**Change:** {change}",
            f"> {text}",
            "",
        ]
    return "\n".join(out)


class TestNearDuplicateDetection:
    """Issue #195: the drafter is shown headers only, so it re-proposes rules
    that already exist. The n-gram check catches near-verbatim; this catches
    the restatement."""

    def test_reworded_restatement_is_flagged(self):
        warnings = validate_proposal(
            _proposal("git-policy.md", "Worktree Location", _RESTATEMENT),
            _memory_with_rule(),
        )
        near = [w for w in warnings if "near-duplicate" in w]
        assert len(near) == 1
        assert "git-policy.md:" in near[0]
        assert "target file" in near[0]

    def test_the_ngram_check_alone_would_have_missed_it(self):
        # Guards the premise: if this ever starts hitting, the new check is
        # measuring nothing the old one didn't.
        assert find_duplicate_content(_RESTATEMENT, _memory_with_rule()) == []

    def test_restatement_of_an_always_loaded_file_is_flagged(self):
        # The 12-of-17 case: items restating a rule that is already in an
        # always-loaded CLAUDE.md, which the drafter never sees at all.
        warnings = validate_proposal(
            _proposal("git-policy.md", "Worktree Location", _RESTATEMENT),
            _memory_contents(),
            dedup_extra={"CLAUDE.md": f"# Global\n\n- {_EXISTING_RULE}\n"},
        )
        near = [w for w in warnings if "near-duplicate" in w]
        assert len(near) == 1
        assert "ALWAYS-LOADED" in near[0]

    def test_a_verbatim_duplicate_is_reported_once(self):
        # Both checks fire on the same file; the reviewer gets one warning.
        warnings = validate_proposal(
            _proposal("python.md", "Packaging", _DUP_TEXT), _memory_contents()
        )
        assert len([w for w in warnings if "already present" in w]) == 1
        assert [w for w in warnings if "near-duplicate of an existing line" in w] == []

    def test_fresh_text_is_clean(self):
        warnings = validate_proposal(
            _proposal(
                "python.md", "Packaging",
                "Pin the interpreter version in the member pyproject so a fresh "
                "checkout resolves the same wheel set every time.",
            ),
            _memory_with_rule(),
        )
        assert [w for w in warnings if "near-duplicate" in w] == []

    def test_update_entry_exempt_in_its_own_target(self):
        for change in ("update", "replace"):
            warnings = validate_proposal(
                _proposal("git-policy.md", "Worktree Location", _RESTATEMENT,
                          change=change),
                _memory_with_rule(),
            )
            assert [w for w in warnings if "near-duplicate" in w] == [], change

    def test_short_text_never_flagged(self):
        assert find_near_duplicate_line("use uv", _memory_with_rule()) is None


class TestBatchNearDuplicates:
    """Secondary finding in #195: two items sourced from different dates said
    the same thing and survived both merge passes."""

    def test_two_items_restating_each_other_are_grouped(self):
        warnings = validate_proposal(
            _multi_entry_proposal("git-policy.md", _EXISTING_RULE, _RESTATEMENT),
            _memory_contents(),
        )
        groups = [w for w in warnings if "near-duplicates of each other" in w]
        assert len(groups) == 1
        assert "#1" in groups[0] and "#2" in groups[0]

    def test_distinct_items_are_not_grouped(self):
        warnings = validate_proposal(
            _multi_entry_proposal(
                "git-policy.md",
                _EXISTING_RULE,
                "Squash merges rewrite the commit hash, so a branch merged that "
                "way cannot be fast-forwarded afterwards.",
            ),
            _memory_contents(),
        )
        assert [w for w in warnings if "near-duplicates of each other" in w] == []

    def test_grouping_is_per_target_file(self):
        # Same text in two different files is a routing question, not a merge
        # question — the cross-file dedup check already owns that.
        proposal = (
            _multi_entry_proposal("git-policy.md", _EXISTING_RULE)
            + "\n"
            + _multi_entry_proposal("python.md", _RESTATEMENT)
        )
        assert find_batch_near_duplicate_groups(parse_proposal_entries(proposal)) == []

    def test_n_restatements_produce_one_warning_not_n_squared(self):
        """The output has to stay readable: a proposal restating one rule 20
        times has 190 duplicate pairs and must still yield one warning."""
        texts = [_EXISTING_RULE, _RESTATEMENT] * 10
        groups = find_batch_near_duplicate_groups(parse_proposal_entries(
            _multi_entry_proposal("git-policy.md", *texts)
        ))
        assert len(groups) == 1
        assert len(groups[0][0]) == 20

    def test_a_cluster_reports_its_strongest_overlap(self):
        groups = find_batch_near_duplicate_groups(parse_proposal_entries(
            _multi_entry_proposal("git-policy.md", _EXISTING_RULE, _RESTATEMENT)
        ))
        assert 0.35 <= groups[0][1] <= 1.0


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
