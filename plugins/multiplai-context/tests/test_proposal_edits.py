"""Tests for lib/proposal_edits.py — the critic's directives, applied in code.

Three invariants, in order of how much damage breaking them does:

1. **Provenance is not editable.** Anything that would rewrite or lose a
   ``**Source:**`` line is refused, with the sole exception of ``MERGE``'s
   append (both citations survive) and ``DROP``, which removes an entry whole
   and leaves a one-line record in ``## Filtered Out``.
2. **Nothing raises.** The input is model output; garbage must apply as a
   byte-identical no-op, not an exception that loses the proposal.
3. **Directive order cannot change the result.** Ranges are resolved against
   the input document and applied back-to-front, so an unsorted directive list
   produces exactly the sorted list's document.
"""

from lib.proposal_edits import Directive, apply_directives, parse_directives

PROPOSAL = """\
# Processed Learnings — 2026-07-31

**Sources:** 2 files

---

## Updates for `dev.md`

### 1. Clean clone before open-sourcing
**Section:** Debugging Methodology
**Change:** add
> Simulate a clean clone before publishing a repo.

**Source:** 2026-07-27.md:12

### 2. Decision (2026-06-15): repos go public
**Section:** Git Workflow
**Change:** add
> Decision (2026-06-15): make repos A/B/C public, committed as abc1234.

**Source:** 2026-07-27.md:40

### 3. Split the dream script
**Section:** Tooling
**Change:** add
> dream.py should be split into a pipeline module and a CLI.

**Source:** 2026-07-27.md:55

### 4. Clean clone before publishing (again)
**Section:** Debugging Methodology
**Change:** add
> Always simulate a clean clone first.

**Source:** 2026-07-28.md:9

---

## Updates for `python.md`

### 1. Atomic writes
**Section:** Data Patterns
**Change:** add
> Write state files temp-then-rename.

**Source:** 2026-07-27.md:70

---

## Action Items (1 items)

### A1. Lower the dream timeout
**What:** set MULTIPLAI_SDK_CALL_TIMEOUT_S to 900
**Why:** chunks are sized against it
**Source:** 2026-07-27.md:88

---

## Filtered Out (1 items)

- renamed a branch — task residue (Source: 2026-07-27.md:3)
"""


def sources(text: str) -> list[str]:
    return [l for l in text.splitlines() if l.startswith("**Source:**")]


def headings(text: str) -> list[str]:
    return [l for l in text.splitlines() if l.startswith("### ")]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class TestParseDirectives:
    def test_replace(self):
        (d,), rejected = parse_directives("REPLACE dev.md#2 Before making a repo public, scrub secrets.")
        assert (d.op, d.target_file, d.index) == ("REPLACE", "dev.md", 2)
        assert d.arg == "Before making a repo public, scrub secrets."
        assert rejected == []

    def test_move(self):
        (d,), _ = parse_directives("MOVE dev.md#1 -> python.md")
        assert (d.op, d.target_file, d.index, d.arg) == ("MOVE", "dev.md", 1, "python.md")

    def test_to_action(self):
        (d,), _ = parse_directives("TO-ACTION dev.md#3")
        assert (d.op, d.target_file, d.index, d.arg) == ("TO-ACTION", "dev.md", 3, None)

    def test_drop_carries_its_reason(self):
        (d,), _ = parse_directives("DROP dev.md#2 past event, no durable rule")
        assert (d.op, d.index, d.arg) == ("DROP", 2, "past event, no durable rule")

    def test_merge_carries_both_refs(self):
        (d,), _ = parse_directives("MERGE dev.md#1 <- dev.md#4")
        assert (d.op, d.target_file, d.index) == ("MERGE", "dev.md", 1)
        assert (d.src_file, d.src_index) == ("dev.md", 4)

    def test_noop_parses_but_yields_no_directive(self):
        parsed, rejected = parse_directives("NOOP")
        assert parsed == [] and rejected == []

    def test_code_fences_and_blanks_are_ignored_not_rejected(self):
        parsed, rejected = parse_directives("```\n\nDROP dev.md#1 x\n\n```")
        assert len(parsed) == 1 and rejected == []

    def test_garbage_is_returned_verbatim(self):
        parsed, rejected = parse_directives(
            "I've reviewed the proposal and it looks good.\nDROP dev.md#1 x"
        )
        assert len(parsed) == 1
        assert rejected == ["I've reviewed the proposal and it looks good."]

    def test_malformed_reference_is_rejected(self):
        parsed, rejected = parse_directives("DROP dev.md 2 no hash")
        assert parsed == [] and len(rejected) == 1

    def test_empty_input_parses_to_nothing(self):
        assert parse_directives("") == ([], [])

    def test_never_raises_on_arbitrary_text(self):
        parsed, rejected = parse_directives("### 1.\n> quoted\n**Source:** x.md:1\n\x00\x01")
        assert isinstance(parsed, list) and isinstance(rejected, list)


# ---------------------------------------------------------------------------
# Applying — the five operations
# ---------------------------------------------------------------------------

class TestReplace:
    def test_swaps_the_quoted_text_only(self):
        new, applied, refused = apply_directives(
            PROPOSAL,
            [Directive("REPLACE", "dev.md", 2, "Before making a repo public, scrub and rotate secrets.")],
        )
        assert applied == ["REPLACE dev.md#2"] and refused == []
        assert "> Before making a repo public, scrub and rotate secrets." in new
        assert "committed as abc1234" not in new
        assert "**Section:** Git Workflow" in new

    def test_keeps_the_source_line(self):
        new, _, _ = apply_directives(
            PROPOSAL, [Directive("REPLACE", "dev.md", 2, "A durable rule.")]
        )
        assert "**Source:** 2026-07-27.md:40" in new
        assert sources(new) == sources(PROPOSAL)

    def test_text_containing_a_source_line_is_refused(self):
        new, applied, refused = apply_directives(
            PROPOSAL, [Directive("REPLACE", "dev.md", 2, "A rule. **Source:** made-up.md:1")]
        )
        assert new == PROPOSAL and applied == []
        assert "**Source:**" in refused[0]

    def test_empty_replacement_is_refused(self):
        new, applied, refused = apply_directives(
            PROPOSAL, [Directive("REPLACE", "dev.md", 2, "")]
        )
        assert new == PROPOSAL and applied == [] and refused


class TestMove:
    def test_entry_changes_target_file(self):
        new, applied, refused = apply_directives(
            PROPOSAL, [Directive("MOVE", "dev.md", 3, "python.md")]
        )
        assert applied == ["MOVE dev.md#3 -> python.md"] and refused == []
        dev = new.split("## Updates for `python.md`")[0]
        assert "Split the dream script" not in dev
        assert "Split the dream script" in new

    def test_source_line_travels_with_the_entry(self):
        new, _, _ = apply_directives(PROPOSAL, [Directive("MOVE", "dev.md", 3, "python.md")])
        assert sorted(sources(new)) == sorted(sources(PROPOSAL))

    def test_creates_the_destination_section_when_absent(self):
        new, applied, _ = apply_directives(
            PROPOSAL, [Directive("MOVE", "dev.md", 3, "claude-code-tools.md")]
        )
        assert applied and "## Updates for `claude-code-tools.md`" in new
        assert new.index("## Updates for `claude-code-tools.md`") < new.index("## Action Items")

    def test_move_to_the_same_file_is_refused(self):
        new, applied, refused = apply_directives(
            PROPOSAL, [Directive("MOVE", "dev.md", 3, "dev.md")]
        )
        assert new == PROPOSAL and applied == [] and refused


class TestToAction:
    def test_becomes_an_action_item(self):
        new, applied, refused = apply_directives(
            PROPOSAL, [Directive("TO-ACTION", "dev.md", 3, "the pipeline is untestable as one file")]
        )
        assert applied == ["TO-ACTION dev.md#3"] and refused == []
        assert "### A2. Split the dream script" in new
        assert "**What:** dream.py should be split" in new
        assert "**Why:** the pipeline is untestable as one file" in new

    def test_leaves_the_memory_section(self):
        new, _, _ = apply_directives(PROPOSAL, [Directive("TO-ACTION", "dev.md", 3)])
        dev = new.split("## Updates for `python.md`")[0]
        assert "Split the dream script" not in dev

    def test_source_line_survives_the_reformat(self):
        new, _, _ = apply_directives(PROPOSAL, [Directive("TO-ACTION", "dev.md", 3)])
        assert "**Source:** 2026-07-27.md:55" in new
        assert sorted(sources(new)) == sorted(sources(PROPOSAL))

    def test_action_item_count_is_restated(self):
        new, _, _ = apply_directives(PROPOSAL, [Directive("TO-ACTION", "dev.md", 3)])
        assert "## Action Items (2 items)" in new

    def test_an_action_item_cannot_be_moved_to_actions(self):
        new, applied, refused = apply_directives(
            PROPOSAL, [Directive("TO-ACTION", "action-items", 1)]
        )
        assert new == PROPOSAL and applied == [] and refused


class TestDrop:
    def test_removes_the_entry(self):
        new, applied, refused = apply_directives(
            PROPOSAL, [Directive("DROP", "dev.md", 2, "past event, no durable rule")]
        )
        assert refused == [] and applied == ["DROP dev.md#2 (past event, no durable rule)"]
        assert "### 2. Decision (2026-06-15): repos go public" not in new
        assert "> Decision (2026-06-15): make repos A/B/C public" not in new
        assert "**Source:** 2026-07-27.md:40" not in new

    def test_leaves_a_one_line_audit_record(self):
        """A dropped entry's block is still marked consolidated in the ledger, so
        a silent drop is permanent and invisible."""
        new, _, _ = apply_directives(
            PROPOSAL, [Directive("DROP", "dev.md", 2, "past event, no durable rule")]
        )
        assert (
            "- Decision (2026-06-15): repos go public — past event, no durable rule "
            "(Source: 2026-07-27.md:40)" in new
        )
        assert "## Filtered Out (2 items)" in new

    def test_remaining_entries_are_renumbered_without_holes(self):
        new, _, _ = apply_directives(PROPOSAL, [Directive("DROP", "dev.md", 2, "x")])
        dev = new.split("## Updates for `python.md`")[0]
        assert [h.split(".")[0] for h in headings(dev)] == ["### 1", "### 2", "### 3"]

    def test_other_entries_are_untouched(self):
        new, _, _ = apply_directives(PROPOSAL, [Directive("DROP", "dev.md", 2, "x")])
        assert "> Simulate a clean clone before publishing a repo." in new
        assert "**Source:** 2026-07-27.md:12" in new


class TestMerge:
    def test_keeps_both_source_lines(self):
        """The one sanctioned provenance operation — nothing is lost."""
        new, applied, refused = apply_directives(
            PROPOSAL, [Directive("MERGE", "dev.md", 1, src_file="dev.md", src_index=4)]
        )
        assert applied == ["MERGE dev.md#1 <- dev.md#4"] and refused == []
        assert "**Source:** 2026-07-27.md:12" in new
        assert "**Source:** 2026-07-28.md:9" in new
        assert sorted(sources(new)) == sorted(sources(PROPOSAL))

    def test_deletes_the_absorbed_entry(self):
        new, _, _ = apply_directives(
            PROPOSAL, [Directive("MERGE", "dev.md", 1, src_file="dev.md", src_index=4)]
        )
        assert "Clean clone before publishing (again)" not in new
        assert "> Always simulate a clean clone first." not in new

    def test_keeps_the_surviving_entrys_text(self):
        new, _, _ = apply_directives(
            PROPOSAL, [Directive("MERGE", "dev.md", 1, src_file="dev.md", src_index=4)]
        )
        assert "> Simulate a clean clone before publishing a repo." in new

    def test_merges_across_target_files(self):
        new, applied, refused = apply_directives(
            PROPOSAL, [Directive("MERGE", "dev.md", 1, src_file="python.md", src_index=1)]
        )
        assert applied and refused == []
        assert sorted(sources(new)) == sorted(sources(PROPOSAL))
        assert "> Write state files temp-then-rename." not in new

    def test_merging_an_entry_into_itself_is_refused(self):
        new, applied, refused = apply_directives(
            PROPOSAL, [Directive("MERGE", "dev.md", 1, src_file="dev.md", src_index=1)]
        )
        assert new == PROPOSAL and applied == [] and refused

    def test_unknown_absorbed_entry_is_refused(self):
        new, applied, refused = apply_directives(
            PROPOSAL, [Directive("MERGE", "dev.md", 1, src_file="dev.md", src_index=99)]
        )
        assert new == PROPOSAL and applied == [] and refused


# ---------------------------------------------------------------------------
# Ordering, refusals, degenerate input
# ---------------------------------------------------------------------------

class TestOrdering:
    DIRECTIVES = [
        Directive("DROP", "dev.md", 2, "past event"),
        Directive("REPLACE", "dev.md", 4, "Always simulate a clean clone first."),
        Directive("TO-ACTION", "dev.md", 3),
        Directive("DROP", "python.md", 1, "already applied"),
    ]

    def test_unsorted_directives_match_the_sorted_order(self):
        """A DROP of #2 must not shift what #3 and #4 mean."""
        shuffled = [self.DIRECTIVES[i] for i in (1, 3, 0, 2)]
        sorted_first = sorted(self.DIRECTIVES, key=lambda d: (d.target_file, -d.index))
        assert apply_directives(PROPOSAL, shuffled)[0] == apply_directives(
            PROPOSAL, sorted_first
        )[0]

    def test_each_directive_hits_the_entry_it_named(self):
        new, applied, refused = apply_directives(PROPOSAL, self.DIRECTIVES)
        assert refused == [] and len(applied) == 4
        assert "**Source:** 2026-07-27.md:40" not in new    # DROP dev.md#2
        assert "> Always simulate a clean clone first." in new  # REPLACE dev.md#4
        assert "### A2. Split the dream script" in new      # TO-ACTION dev.md#3
        assert "> Write state files temp-then-rename." not in new  # DROP python.md#1

    def test_two_directives_on_one_entry_refuse_the_second(self):
        new, applied, refused = apply_directives(
            PROPOSAL,
            [Directive("DROP", "dev.md", 2, "x"), Directive("REPLACE", "dev.md", 2, "y")],
        )
        assert len(applied) == 1 and len(refused) == 1
        assert "already edited" in refused[0]


class TestRefusalsAndNoOps:
    def test_no_directives_returns_the_proposal_byte_identical(self):
        assert apply_directives(PROPOSAL, []) == (PROPOSAL, [], [])

    def test_garbage_input_is_a_no_op(self):
        parsed, rejected = parse_directives("Looks good to me!\nNothing to change.")
        assert parsed == [] and len(rejected) == 2
        assert apply_directives(PROPOSAL, parsed)[0] == PROPOSAL

    def test_unknown_entry_is_refused_and_changes_nothing(self):
        new, applied, refused = apply_directives(
            PROPOSAL, [Directive("DROP", "nope.md", 1, "x")]
        )
        assert new == PROPOSAL and applied == []
        assert "no such entry" in refused[0]

    def test_all_refused_leaves_the_document_untouched(self):
        new, applied, refused = apply_directives(
            PROPOSAL,
            [Directive("DROP", "nope.md", 1, "x"), Directive("MOVE", "dev.md", 99, "python.md")],
        )
        assert new == PROPOSAL and applied == [] and len(refused) == 2

    def test_applying_to_an_empty_document_never_raises(self):
        new, applied, refused = apply_directives("", [Directive("DROP", "dev.md", 1, "x")])
        assert new == "" and applied == [] and refused

    def test_end_to_end_from_raw_critic_output(self):
        response = (
            "```\n"
            "DROP dev.md#2 past event with no durable rule\n"
            "MERGE dev.md#1 <- dev.md#4\n"
            "TO-ACTION dev.md#3\n"
            "Everything else looks clean.\n"
            "```\n"
        )
        parsed, rejected = parse_directives(response)
        new, applied, refused = apply_directives(PROPOSAL, parsed)
        assert rejected == ["Everything else looks clean."]
        assert len(applied) == 3 and refused == []
        # One entry dropped (its Source goes with it); every other Source survives.
        assert "**Source:** 2026-07-27.md:40" not in new
        assert set(sources(PROPOSAL)) - set(sources(new)) == {"**Source:** 2026-07-27.md:40"}
