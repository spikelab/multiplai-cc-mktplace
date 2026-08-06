"""Tests for dream_triage.py — the split between what a human must read and
what need not be read.

The whole value of this module is that the split is *conservative*: a false
"needs review" costs one line of reading, a false "auto" writes something
nobody agreed to. Most of these tests exist to pin that asymmetry.
"""

from lib.dream_triage import (
    ADDITIVE_CHANGES,
    BEHAVIORAL_FILES,
    auto_slice,
    classify,
    flagged_by_routing,
    render_receipt,
)

PROPOSAL = """# Processed Learnings — 2026-08-06

**Sources:** 3 files, ~40 entries

---

## Updates for `technical-pref.md` (3 learnings)

### 1. uv workspace resolution
**Section:** Python Tooling
**Change:** add
> Run through the member dir, never `--project ../..` — two levels up does not
> exist on an installed plugin.

**Source:** 2026-08-05.md:12

### 2. Old pandoc invocation is wrong
**Section:** Python Tooling
**Change:** update
> Never run bare `pandoc -o x.pdf`; it defaults to pdflatex, which is absent.

**Source:** 2026-08-05.md:20

### 3. [RULE-PROPOSAL] Always check the diary first
**Section:** Python Tooling
**Change:** add
> Before starting work, read today's diary entry.

**Source:** 2026-08-05.md:33

---

## Updates for `CLAUDE.md` (1 learnings)

### 1. Never merge without approval
**Section:** Behavioral Rules
**Change:** add
> Never merge a PR unilaterally.

**Source:** 2026-08-05.md:41

---

## Updates for `projects.md` (2 learnings)

### 1. DolceBot logging is unimplemented
**Section:** DolceBot
**Change:** add
> VM logs → Cloud Logging is fully specified and zero implemented.

**Source:** 2026-08-05.md:50

### 2. Perfi has unpushed commits
**Section:** Perfi
**Change:** add
> Two unpushed commits as of 2026-08-06.

**Source:** 2026-08-05.md:55

---

## Routing Warnings

- `projects.md` #2 (Perfi has unpushed commits): section "Perfi" does not exist in
  `projects.md` but does in `workspace.md` — suggested reroute to `workspace.md`.
"""


class TestClassification:
    def test_plain_additive_item_is_auto(self):
        t = classify(PROPOSAL)
        autos = {(i.target, i.number) for i in t.auto}
        assert ("technical-pref.md", 1) in autos
        assert ("projects.md", 1) in autos

    def test_rule_proposal_needs_review(self):
        t = classify(PROPOSAL)
        item = next(i for i in t.review if i.target == "technical-pref.md" and i.number == 3)
        assert "rule-proposal" in item.reasons

    def test_behavioral_file_needs_review_even_when_additive(self):
        """A CLAUDE.md entry is a change to how the agent behaves. Being an
        additive, well-cited `add` does not make it a data entry."""
        t = classify(PROPOSAL)
        item = next(i for i in t.review if i.target == "CLAUDE.md")
        assert "behavioral-file" in item.reasons
        assert item.change == "add"

    def test_non_additive_needs_review(self):
        """An `update` can destroy a line that was right; an `add` cannot."""
        t = classify(PROPOSAL)
        item = next(i for i in t.review if i.target == "technical-pref.md" and i.number == 2)
        assert "not-additive" in item.reasons

    def test_routing_flagged_item_needs_review(self):
        t = classify(PROPOSAL)
        item = next(i for i in t.review if i.target == "projects.md" and i.number == 2)
        assert "routing-warning" in item.reasons

    def test_every_item_lands_in_exactly_one_bucket(self):
        t = classify(PROPOSAL)
        assert t.total == 6
        assert len(t.auto) == 2
        assert len(t.review) == 4
        assert not ({(i.target, i.number) for i in t.auto}
                    & {(i.target, i.number) for i in t.review})

    def test_low_confidence_marker_needs_review(self):
        proposal = PROPOSAL.replace(
            "### 1. DolceBot logging is unimplemented",
            "### 1. [warning low confidence] DolceBot logging is unimplemented",
        )
        item = next(i for i in classify(proposal).review
                    if i.target == "projects.md" and i.number == 1)
        assert "low-confidence" in item.reasons

    def test_missing_change_verb_needs_review(self):
        """Pessimistic on parse failure: a block that did not parse the way the
        format promises is not one to apply unattended."""
        proposal = PROPOSAL.replace("**Change:** add\n> VM logs", "> VM logs")
        item = next(i for i in classify(proposal).review
                    if i.target == "projects.md" and i.number == 1)
        assert "unparsed" in item.reasons

    def test_multiple_reasons_all_recorded(self):
        proposal = PROPOSAL.replace(
            "### 1. Never merge without approval",
            "### 1. [RULE-PROPOSAL] Never merge without approval",
        )
        item = next(i for i in classify(proposal).review if i.target == "CLAUDE.md")
        assert {"rule-proposal", "behavioral-file"} <= set(item.reasons)

    def test_processed_items_are_not_re_triaged(self):
        """Triaging a partly-reviewed proposal must be idempotent — a decided
        item lives under `## Processed` and is never pending again."""
        proposal = PROPOSAL + (
            "\n---\n\n## Processed\n\n"
            "### 9. Something already decided\n"
            "**Processed:** applied → technical-pref.md · 2026-08-06T10:00:00Z\n"
            "**Section:** Python Tooling\n**Change:** add\n> Decided text.\n"
        )
        t = classify(proposal)
        assert t.total == 6
        assert all(i.number != 9 for i in t.auto + t.review)

    def test_empty_proposal_is_empty_triage(self):
        t = classify("# Processed Learnings — 2026-08-06\n\nNothing.\n")
        assert t.total == 0

    def test_conflict_resolutions_are_counted_never_auto(self):
        proposal = PROPOSAL + (
            "\n---\n\n## Conflict Resolutions\n\n"
            "### `dolcebot.md` line 42\n\n- **Superseded** (was): old\n"
            "### `preferences.md` line 70\n\n- **Superseded** (was): old\n"
        )
        t = classify(proposal)
        assert t.conflict_resolutions == 2
        assert t.total == 6  # unchanged — they are not update entries


class TestRoutingWarningParsing:
    def test_extracts_target_and_number_pairs(self):
        assert flagged_by_routing(PROPOSAL) == {("projects.md", 2)}

    def test_clean_section_flags_nothing(self):
        proposal = PROPOSAL.split("## Routing Warnings")[0] + \
            "## Routing Warnings\n\n(none)\n"
        assert flagged_by_routing(proposal) == set()

    def test_missing_section_flags_nothing(self):
        """The gate not having run is not evidence of a clean proposal — but it
        is also not something this function can invent warnings from. The
        caller surfaces the missing section separately."""
        proposal = PROPOSAL.split("## Routing Warnings")[0]
        assert flagged_by_routing(proposal) == set()

    def test_stops_at_the_next_section(self):
        proposal = PROPOSAL + "\n## Action Items\n\n- `projects.md` #1 (not a warning)\n"
        assert flagged_by_routing(proposal) == {("projects.md", 2)}


class TestAutoSlice:
    def test_contains_only_the_auto_items(self):
        """The applier must never see a review item — it would write it."""
        t = classify(PROPOSAL)
        items = t.auto_by_file()["technical-pref.md"]
        rendered = auto_slice(items)
        assert "uv workspace resolution" in rendered
        assert "RULE-PROPOSAL" not in rendered
        assert "pandoc" not in rendered

    def test_preserves_section_and_text(self):
        t = classify(PROPOSAL)
        rendered = auto_slice(t.auto_by_file()["projects.md"])
        assert "**Section:** DolceBot" in rendered
        assert "> VM logs → Cloud Logging is fully specified and zero implemented." in rendered

    def test_empty_items_render_nothing(self):
        assert auto_slice([]) == ""


class TestReceipt:
    def test_names_every_applied_item_with_its_text(self):
        t = classify(PROPOSAL)
        receipt = render_receipt(
            t, proposal_name="p.md", applied=t.auto_by_file(),
            failed={}, generated="2026-08-06 10:00 UTC",
        )
        for item in t.auto:
            assert item.title in receipt
        assert "2 item(s) across 2 file(s)" in receipt

    def test_records_failures_as_still_pending(self):
        t = classify(PROPOSAL)
        receipt = render_receipt(
            t, proposal_name="p.md", applied={}, generated="2026-08-06 10:00 UTC",
            failed={"projects.md": "applier returned no safe content"},
        )
        assert "Failed to apply" in receipt
        assert "projects.md" in receipt

    def test_lists_what_was_left_for_the_human(self):
        t = classify(PROPOSAL)
        receipt = render_receipt(
            t, proposal_name="p.md", applied=t.auto_by_file(),
            failed={}, generated="2026-08-06 10:00 UTC",
        )
        assert "Left for you" in receipt
        assert "changes a behavioural rule" in receipt


class TestPolicyConstants:
    def test_add_is_the_only_additive_verb(self):
        assert ADDITIVE_CHANGES == {"add"}

    def test_claude_md_is_behavioral(self):
        assert "CLAUDE.md" in BEHAVIORAL_FILES
