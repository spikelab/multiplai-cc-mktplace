"""Tests for the code-review prompt text and its vendored blocks.

Two things are pinned here. First, `CODE_REVIEW_PROMPT` is now composed from
several strings rather than written as one literal, so the placeholder contract
with `review_steps.run_code_review` is easy to break silently — a stray brace in
a vendored block raises `KeyError` at call time, in production, holding a
150k-char diff. Second, the vendored blocks come from someone else's repo under
Apache-2.0, and the adaptations that made them safe to use here (no stack
house rules, no internal names, no `model:` line) are invisible once merged into
a 13k-char prompt.
"""

import json
import re
import string
from pathlib import Path

import pytest

from build_pipeline.prompts import review
from build_pipeline.prompts.vendored import reviewer_blocks

VENDORED_DIR = Path(reviewer_blocks.__file__).parent


def _flat(text: str) -> str:
    """The prompt with its hard wraps removed, so assertions on a phrase do not
    depend on where the paragraph happened to break."""
    return re.sub(r"\s+", " ", text)


# Every placeholder `review_steps.run_code_review` supplies today.
EXPECTED_PLACEHOLDERS = {
    "diff",
    "rubric",
    "spec_context",
    "standards",
    "implementer_report",
}


def _block_constants() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(reviewer_blocks).items()
        if name.isupper() and isinstance(value, str) and not name.startswith("__")
    }


def _sources() -> list[dict]:
    return json.loads((VENDORED_DIR / "SOURCES.json").read_text())


class TestPlaceholderContract:
    def test_prompt_formats_with_every_existing_placeholder(self):
        rendered = review.CODE_REVIEW_PROMPT.format(
            diff="DIFF",
            rubric="RUBRIC",
            spec_context="SPEC",
            standards="STANDARDS",
            implementer_report="REPORT",
        )
        for token in ("DIFF", "RUBRIC", "SPEC", "STANDARDS", "REPORT"):
            assert token in rendered

    def test_prompt_requires_exactly_the_placeholders_the_call_site_supplies(self):
        fields = {
            name
            for _, name, _, _ in string.Formatter().parse(review.CODE_REVIEW_PROMPT)
            if name
        }
        assert fields == EXPECTED_PLACEHOLDERS

    def test_vendored_blocks_carry_no_brace(self):
        # A single unescaped brace anywhere in a composed block turns every
        # review call into a KeyError, and only at run time.
        for name, text in _block_constants().items():
            assert "{" not in text and "}" not in text, name


class TestConventionsAngle:
    def test_section_exists(self):
        assert "## Conventions" in review.CODE_REVIEW_PROMPT

    def test_carries_the_evidence_bar(self):
        prompt = _flat(review.CODE_REVIEW_PROMPT)
        assert "quote the exact rule and" in prompt
        assert "the exact line that breaks it" in prompt
        assert "spirit of the doc" in prompt
        assert "style preferences" in prompt

    def test_asks_for_the_claude_md_path_so_the_report_can_cite_it(self):
        assert "Name the `CLAUDE.md` path" in _flat(review.CODE_REVIEW_PROMPT)

    def test_empty_is_the_right_answer_when_no_claude_md_applies(self):
        assert (
            "return nothing for this angle" in _flat(review.CODE_REVIEW_PROMPT)
        ), "the no-CLAUDE.md case must be stated, or the reviewer invents rules"

    def test_offers_conventions_as_a_dimension_value(self):
        assert "`conventions`" in review.CODE_REVIEW_PROMPT


class TestRemovedBehaviourAngle:
    def test_section_exists(self):
        assert "## Removed Behavior" in review.CODE_REVIEW_PROMPT

    def test_review_method_step_follows_claim_verification(self):
        prompt = review.CODE_REVIEW_PROMPT
        claims = prompt.index("Verify the implementer's claims")
        removed = prompt.index("Account for what the diff removed")
        compliance = prompt.index("Judge spec compliance scenario by scenario")
        assert claims < removed < compliance

    def test_step_names_the_invariant_and_the_search_for_it(self):
        prompt = _flat(review.CODE_REVIEW_PROMPT)
        assert "**deletes or replaces**" in prompt
        assert "name the invariant or behavior it enforced" in prompt
        assert "where that invariant is re-established" in prompt

    def test_step_names_the_four_shapes_of_the_finding(self):
        prompt = _flat(review.CODE_REVIEW_PROMPT)
        assert (
            "a removed guard, a dropped error path, a narrowed validation, "
            "a deleted test that covered a real case" in prompt
        )

    def test_review_method_stays_numbered_in_order(self):
        steps = re.findall(r"^(\d+)\. ", review.CODE_REVIEW_PROMPT, re.MULTILINE)
        assert [int(s) for s in steps][:6] == [1, 2, 3, 4, 5, 6]

    def test_removed_behavior_is_an_offered_dimension_value(self):
        assert "`removed-behavior`" in review.CODE_REVIEW_PROMPT


class TestToolsSection:
    def test_section_exists(self):
        assert "## Tools" in review.CODE_REVIEW_PROMPT

    def test_diff_is_still_ground_truth_for_what_changed(self):
        assert (
            "The diff stays ground truth for *what changed*"
            in _flat(review.CODE_REVIEW_PROMPT)
        )

    def test_names_the_three_tools_it_actually_holds(self):
        prompt = _flat(review.CODE_REVIEW_PROMPT)
        assert "`Read`, `Grep` and `Glob`" in prompt

    def test_grants_no_tool_beyond_the_read_only_three(self):
        # The prompt must never imply a capability the SDK allow-list denies.
        tools_section = review.CODE_REVIEW_PROMPT[
            review.CODE_REVIEW_PROMPT.index("## Tools") : review.CODE_REVIEW_PROMPT.index(
                "## Diff (ground truth)"
            )
        ]
        for denied in ("`Write`", "`Edit`", "`Bash`", "`Task`", "`Agent`"):
            assert denied not in tools_section

    def test_says_it_reports_and_never_fixes(self):
        assert "you report, you never fix" in _flat(review.CODE_REVIEW_PROMPT)

    def test_tells_it_to_grep_for_callers(self):
        assert (
            "Grep for callers of every changed symbol"
            in _flat(review.CODE_REVIEW_PROMPT)
        )


class TestVendoredBlockAdaptations:
    # Cut, not translated: every one of these is one project's house style, and
    # against a different stack it is wrong rather than merely inert.
    HOUSE_RULE_TOKENS = [
        "logForDebugging",
        "logError",
        "logEvent",
        "errorIds",
        "Sentry",
        "Statsig",
        "ES modules",
        "arrow function",
        "React",
        "Props",
        "TypeScript",
        "constants/",
        ".ts",
    ]
    INTERNAL_NAMES = ["Daisy"]

    @pytest.mark.parametrize("token", HOUSE_RULE_TOKENS)
    def test_no_block_carries_a_stack_specific_house_rule(self, token):
        for name, text in _block_constants().items():
            assert token not in text, f"{name} still carries {token!r}"

    @pytest.mark.parametrize("token", HOUSE_RULE_TOKENS)
    def test_composed_prompt_carries_no_house_rule_either(self, token):
        assert token not in review.CODE_REVIEW_PROMPT

    @pytest.mark.parametrize("name", INTERNAL_NAMES)
    def test_no_block_names_an_internal_author(self, name):
        for const, text in _block_constants().items():
            assert name not in text, f"{const} still names {name}"
        assert name not in review.CODE_REVIEW_PROMPT

    def test_no_block_carries_a_model_line(self):
        # buildme resolves the model per step through conf_model(); a vendored
        # frontmatter `model:` would silently override it.
        source = (VENDORED_DIR / "reviewer_blocks.py").read_text()
        assert not re.search(r"^\s*model:", source, re.MULTILINE)
        for name, text in _block_constants().items():
            assert not re.search(r"^\s*model:", text, re.MULTILINE), name

    def test_no_block_carries_yaml_frontmatter(self):
        for name, text in _block_constants().items():
            assert not text.lstrip().startswith("---"), name
            for key in ("name:", "description:", "color:"):
                assert not re.search(rf"^{key}", text, re.MULTILINE), f"{name}: {key}"

    def test_one_rating_scale_survives_the_merge(self):
        # Three upstream files used three incompatible scales. Only
        # code-reviewer's 0-100 came with a suppression rule, so it is the one
        # kept; the 1-10 axes must not have come along.
        prompt = _flat(review.CODE_REVIEW_PROMPT)
        assert "Rate every issue 0-100" in prompt
        assert "X/10" not in prompt
        assert not re.search(r"\bRate 1-10\b", prompt)
        assert not re.search(r"criticality from 1-10", prompt)


class TestAttribution:
    REQUIRED_KEYS = {
        "repo",
        "path",
        "blob_sha",
        "tree_sha",
        "licence",
        "modified",
        "used_by",
    }

    def test_licence_ships_beside_the_blocks(self):
        licence = VENDORED_DIR / "LICENSE"
        assert licence.is_file()
        assert "Apache License" in licence.read_text()

    def test_sources_entries_carry_exactly_the_required_keys(self):
        for entry in _sources():
            assert set(entry) == self.REQUIRED_KEYS, entry.get("path")

    def test_sources_entries_are_well_formed(self):
        for entry in _sources():
            assert entry["repo"] == "anthropics/claude-plugins-official"
            assert entry["licence"] == "Apache-2.0"
            assert isinstance(entry["modified"], bool)
            assert re.fullmatch(r"[0-9a-f]{40}", entry["blob_sha"])
            assert re.fullmatch(r"[0-9a-f]{40}", entry["tree_sha"])
            assert isinstance(entry["used_by"], list)

    def test_every_sources_entry_names_a_constant_that_exists(self):
        blocks = _block_constants()
        for entry in _sources():
            for const in entry["used_by"]:
                assert const in blocks or hasattr(review, const), (
                    f"{entry['path']} claims {const}, which does not exist"
                )

    def test_every_block_is_accounted_for_in_sources(self):
        claimed = {c for entry in _sources() for c in entry["used_by"]}
        assert set(_block_constants()) == claimed

    def test_every_block_has_an_attribution_header_above_it(self):
        source = (VENDORED_DIR / "reviewer_blocks.py").read_text()
        for name in _block_constants():
            head = source[: source.index(f"{name} = ")]
            header = head[head.rindex("# Vendored from:") :]
            assert "anthropics/claude-plugins-official" in header
            assert "# Blob SHA:" in header
            assert "# Tree SHA:" in header
            assert "Apache-2.0" in header
            assert "# Modified:      yes" in header

    def test_every_block_reaches_the_composed_prompt(self):
        # A vendored block nobody uses is an attribution obligation with no
        # payoff — delete it rather than carry it.
        for name, text in _block_constants().items():
            assert text.strip() in review.CODE_REVIEW_PROMPT, name
