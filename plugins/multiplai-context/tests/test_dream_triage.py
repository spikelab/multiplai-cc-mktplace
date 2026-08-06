"""Tests for dream_triage.py — the split between what a human must read and
what need not be read.

The whole value of this module is that the split is *conservative*: a false
"needs review" costs one line of reading, a false "auto" writes something
nobody agreed to. Most of these tests exist to pin that asymmetry.

Filenames in the fixture are **real memory files** (see
``.multiplai/memory/``), because the file gate is an allowlist of real names —
a fixture full of invented ones would test nothing but the fallthrough.
"""

from lib.dream_triage import (
    ADDITIVE_CHANGES,
    auto_slice,
    classify,
    flagged_by_routing,
    has_routing_section,
    is_safe_target,
    render_receipt,
)
from lib.routing_validation import render_warnings_section, validate_proposal

PROPOSAL = """# Processed Learnings — 2026-08-06

**Sources:** 3 files, ~40 entries

---

## Updates for `python.md` (3 learnings)

### 1. uv workspace resolution
**Section:** Python Tooling
**Change:** add
> On an installed plugin there is no workspace root above the member, so uv
> resolves it standalone via its member-local `[tool.uv.sources]`.

**Source:** 2026-08-05.md:12

### 2. Old pandoc invocation is wrong
**Section:** Python Tooling
**Change:** update
> A bare `pandoc -o x.pdf` defaults to pdflatex, which is absent here.

**Source:** 2026-08-05.md:20

### 3. [RULE-PROPOSAL] Check the diary first
**Section:** Python Tooling
**Change:** add
> Before starting work, read today's diary entry.

**Source:** 2026-08-05.md:33

---

## Updates for `technical-pref.md` (1 learnings)

### 1. Bruno collections live under api/
**Section:** API Testing
**Change:** add
> Bruno collections for a service live under that service's `api/` directory.

**Source:** 2026-08-05.md:41

---

## Updates for `dolcebot.md` (2 learnings)

### 1. DolceBot logging is unimplemented
**Section:** DolceEngine
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

- `dolcebot.md` #2 (Perfi has unpushed commits): section "Perfi" does not exist in
  `dolcebot.md` but does in `personal-projects.md` — suggested reroute to
  `personal-projects.md`.
"""


class TestClassification:
    def test_plain_additive_item_is_auto(self):
        t = classify(PROPOSAL)
        autos = {(i.target, i.number) for i in t.auto}
        assert ("python.md", 1) in autos
        assert ("dolcebot.md", 1) in autos

    def test_rule_proposal_needs_review(self):
        t = classify(PROPOSAL)
        item = next(i for i in t.review if i.target == "python.md" and i.number == 3)
        assert "rule-proposal" in item.reasons

    def test_instructing_file_needs_review_even_when_additive(self):
        """`technical-pref.md` is read to decide how to act. Being an additive,
        well-cited `add` does not make it a data entry."""
        t = classify(PROPOSAL)
        item = next(i for i in t.review if i.target == "technical-pref.md")
        assert "not-recall-file" in item.reasons
        assert item.change == "add"

    def test_unknown_file_needs_review(self):
        """The gate is an allowlist, so a memory file added next month is not
        auto-appliable until someone classifies it. Fails closed."""
        proposal = PROPOSAL.replace("`dolcebot.md`", "`brand-new-file.md`")
        t = classify(proposal)
        assert all(i.target != "brand-new-file.md" for i in t.auto)
        item = next(i for i in t.review if i.target == "brand-new-file.md")
        assert "not-recall-file" in item.reasons

    def test_normative_text_needs_review_even_in_a_recall_file(self):
        """A rule can be carried into a recall file. The file gate alone would
        wave it through, so the item's own text is checked too."""
        proposal = PROPOSAL.replace(
            "> Two unpushed commits as of 2026-08-06.",
            "> Never push to DolceEngine main without running the migration check.",
        )
        item = next(i for i in classify(proposal).review
                    if i.target == "dolcebot.md" and i.number == 2)
        assert "normative-language" in item.reasons

    def test_normative_check_is_word_bounded(self):
        """`_NORMATIVE_RE` is trigger-happy on purpose, but not to the point of
        matching inside words — "mustard" and "shoulder" are not rules."""
        proposal = PROPOSAL.replace(
            "> VM logs → Cloud Logging is fully specified and zero implemented.",
            "> The mustard-coloured shoulder bar is the staging indicator.",
        )
        item = next(i for i in classify(proposal).auto
                    if i.target == "dolcebot.md" and i.number == 1)
        assert item.reasons == ()

    def test_traversal_target_needs_review(self):
        """`memory_dir / target` happily resolves `../../CLAUDE.md`, and the
        existing `.exists()` check is no guard — traversal targets exist."""
        proposal = PROPOSAL.replace("`dolcebot.md`", "`../../CLAUDE.md`")
        t = classify(proposal)
        assert all(i.target != "../../CLAUDE.md" for i in t.auto)
        item = next(i for i in t.review if i.target == "../../CLAUDE.md")
        assert "unsafe-target" in item.reasons

    def test_non_additive_needs_review(self):
        """An `update` can destroy a line that was right; an `add` cannot."""
        t = classify(PROPOSAL)
        item = next(i for i in t.review if i.target == "python.md" and i.number == 2)
        assert "not-additive" in item.reasons

    def test_routing_flagged_item_needs_review(self):
        t = classify(PROPOSAL)
        item = next(i for i in t.review if i.target == "dolcebot.md" and i.number == 2)
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
                    if i.target == "dolcebot.md" and i.number == 1)
        assert "low-confidence" in item.reasons

    def test_missing_change_verb_needs_review(self):
        """Pessimistic on parse failure: a block that did not parse the way the
        format promises is not one to apply unattended."""
        proposal = PROPOSAL.replace("**Change:** add\n> VM logs", "> VM logs")
        item = next(i for i in classify(proposal).review
                    if i.target == "dolcebot.md" and i.number == 1)
        assert "unparsed" in item.reasons

    def test_empty_body_needs_review(self):
        """A well-formed header with no text would apply nothing while being
        marked processed — the item would vanish without ever being written."""
        proposal = PROPOSAL.replace(
            "> VM logs → Cloud Logging is fully specified and zero implemented.\n",
            "",
        )
        item = next(i for i in classify(proposal).review
                    if i.target == "dolcebot.md" and i.number == 1)
        assert "unparsed" in item.reasons

    def test_multiple_reasons_all_recorded(self):
        proposal = PROPOSAL.replace(
            "### 1. Bruno collections live under api/",
            "### 1. [RULE-PROPOSAL] Bruno collections live under api/",
        )
        item = next(i for i in classify(proposal).review
                    if i.target == "technical-pref.md")
        assert {"rule-proposal", "not-recall-file"} <= set(item.reasons)

    def test_source_citation_is_captured(self):
        """The receipt claims to name each item's source. That claim is only
        true if the parser carries the field through."""
        item = next(i for i in classify(PROPOSAL).auto
                    if i.target == "python.md" and i.number == 1)
        assert item.source == "2026-08-05.md:12"

    def test_processed_items_are_not_re_triaged(self):
        """Triaging a partly-reviewed proposal must be idempotent — a decided
        item lives under `## Processed` and is never pending again."""
        proposal = PROPOSAL + (
            "\n---\n\n## Processed\n\n"
            "### 9. Something already decided\n"
            "**Processed:** applied → python.md · 2026-08-06T10:00:00Z\n"
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


class TestSafeTarget:
    def test_plain_memory_filename_is_safe(self):
        assert is_safe_target("dolcebot.md")

    def test_traversal_is_not(self):
        assert not is_safe_target("../../CLAUDE.md")

    def test_absolute_path_is_not(self):
        assert not is_safe_target("/etc/passwd.md")

    def test_subdirectory_is_not(self):
        assert not is_safe_target("sub/dir.md")

    def test_non_markdown_is_not(self):
        assert not is_safe_target("dolcebot.txt")


class TestRoutingWarningParsing:
    def test_extracts_target_and_number_pairs(self):
        assert flagged_by_routing(PROPOSAL) == {("dolcebot.md", 2)}

    def test_parses_what_the_renderer_actually_writes(self):
        """`flagged_by_routing` reads a rendered section, so it is coupled to
        `render_warnings_section`'s format. Generate the section rather than
        hand-writing it, so a change to the renderer breaks this test instead
        of silently emptying the flag set in production."""
        entry = (
            "# Proposal\n\n## Updates for `dolcebot.md` (1 learnings)\n\n"
            "### 2. Perfi has unpushed commits\n"
            "**Section:** Perfi\n**Change:** add\n"
            "> Two unpushed commits as of 2026-08-06.\n"
        )
        warnings = validate_proposal(
            entry, {"personal-projects.md": "## Perfi\n\nnotes\n", "dolcebot.md": "## DolceEngine\n"}
        )
        assert warnings, "fixture must actually produce a warning"
        rendered = entry + render_warnings_section(warnings)
        assert ("dolcebot.md", 2) in flagged_by_routing(rendered)

    def test_clean_section_flags_nothing(self):
        proposal = PROPOSAL.split("## Routing Warnings")[0] + \
            "## Routing Warnings\n\n(none)\n"
        assert flagged_by_routing(proposal) == set()

    def test_missing_section_flags_nothing(self):
        """The gate not having run is not evidence of a clean proposal — but it
        is also not something this function can invent warnings from. The
        caller distinguishes the two via `has_routing_section`."""
        proposal = PROPOSAL.split("## Routing Warnings")[0]
        assert flagged_by_routing(proposal) == set()

    def test_stops_at_the_next_section(self):
        proposal = PROPOSAL + "\n## Action Items\n\n- `dolcebot.md` #1 (not a warning)\n"
        assert flagged_by_routing(proposal) == {("dolcebot.md", 2)}


class TestRoutingSectionPresence:
    def test_present_when_the_gate_ran(self):
        assert has_routing_section(PROPOSAL)

    def test_present_when_the_gate_ran_and_found_nothing(self):
        """"clean" and "never ran" must be distinguishable — the renderer
        writes `(none)`, and this is what makes that distinction usable."""
        proposal = PROPOSAL.split("## Routing Warnings")[0] + \
            "## Routing Warnings\n\n(none)\n"
        assert has_routing_section(proposal)

    def test_absent_when_it_did_not(self):
        assert not has_routing_section(PROPOSAL.split("## Routing Warnings")[0])


class TestAutoSlice:
    def test_contains_only_the_auto_items(self):
        """The applier must never see a review item — it would write it."""
        t = classify(PROPOSAL)
        items = t.auto_by_file()["python.md"]
        rendered = auto_slice(items)
        assert "uv workspace resolution" in rendered
        assert "RULE-PROPOSAL" not in rendered
        assert "pandoc" not in rendered

    def test_preserves_section_and_text(self):
        t = classify(PROPOSAL)
        rendered = auto_slice(t.auto_by_file()["dolcebot.md"])
        assert "**Section:** DolceEngine" in rendered
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

    def test_cites_the_source_of_every_applied_item(self):
        t = classify(PROPOSAL)
        receipt = render_receipt(
            t, proposal_name="p.md", applied=t.auto_by_file(),
            failed={}, generated="2026-08-06 10:00 UTC",
        )
        assert "2026-08-05.md:12" in receipt
        assert "2026-08-05.md:50" in receipt

    def test_records_failures_as_still_pending(self):
        t = classify(PROPOSAL)
        receipt = render_receipt(
            t, proposal_name="p.md", applied={}, generated="2026-08-06 10:00 UTC",
            failed={"dolcebot.md": "applier returned no safe content"},
        )
        assert "Failed to apply" in receipt
        assert "dolcebot.md" in receipt

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

    def test_the_files_that_instruct_are_not_on_the_allowlist(self):
        """The allowlist's membership test is "does this file record, or does
        it instruct?". These four instruct, and each has been the target of a
        real proposal item — a regression here auto-applies behaviour changes."""
        from lib.dream_triage import RECALL_FILES

        for name in ("CLAUDE.md", "preferences.md", "git-policy.md",
                     "technical-pref.md", "writing-workflow.md"):
            assert name not in RECALL_FILES
