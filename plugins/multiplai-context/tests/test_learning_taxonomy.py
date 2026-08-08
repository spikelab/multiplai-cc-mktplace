"""Tests for the two-axis learning taxonomy — provenance × kind.

The taxonomy's whole value is that a label means what it says. So the tests
that matter most here are the ones asserting what the code will NOT do:

- it does not coerce an unrecognised provenance to the nearest legal one,
- it does not reprint a legacy record wearing a label the record never had,
- it does not infer a record's real origin from how confidently it is worded,

and the one asserting the pair survives the whole way to a proposal item, since
a classifier downstream cannot read a field that stopped at the learnings file.
"""

import pytest

from lib import taxonomy
from lib.extraction import _format_learning_entry, _parse_learning
from lib.learnings_ledger import (
    LEDGER_VERSION,
    block_key,
    legacy_block_key,
    parse_blocks,
    record,
    unprocessed,
)
from lib.learnings_ledger import migrate as taxonomy_migrate
from lib.routing_validation import parse_proposal_entries


# ---------------------------------------------------------------------------
# The vocabulary itself
# ---------------------------------------------------------------------------

class TestValueSets:
    def test_the_five_provenances(self):
        assert set(taxonomy.PROVENANCES) == {
            "RESEARCH", "EMPIRICAL", "CORRECTION", "DECLARATION", "INFERENCE",
        }

    def test_the_four_kinds(self):
        assert set(taxonomy.KINDS) == {"FACT", "RULE", "DECISION", "INTENTION"}

    def test_unclear_answers_route_toward_review(self):
        """Both defaults are the cautious end of their axis.

        `INFERENCE` is the provenance nothing can re-verify and `RULE` is the
        kind with the widest blast radius, so an extractor that cannot tell
        produces an item a human looks at rather than one that slips past.
        """
        assert taxonomy.UNCLEAR_PROVENANCE == "INFERENCE"
        assert taxonomy.UNCLEAR_KIND == "RULE"

    @pytest.mark.parametrize("value", ["correction", " Correction ", "CORRECTION"])
    def test_case_and_whitespace_are_forgiven(self, value):
        assert taxonomy.normalize_provenance(value) == "CORRECTION"

    @pytest.mark.parametrize("value", ["OBSERVATION", "VERIFIED", "", None, "FACT"])
    def test_out_of_set_provenance_is_rejected(self, value):
        assert taxonomy.normalize_provenance(value) is None

    @pytest.mark.parametrize("value", ["PREFERENCE", "CORRECTION", "", None])
    def test_out_of_set_kind_is_rejected(self, value):
        assert taxonomy.normalize_kind(value) is None


# ---------------------------------------------------------------------------
# Case 2 — legacy records map without inventing labels
# ---------------------------------------------------------------------------

class TestNormalizeLegacy:
    @pytest.mark.parametrize("ltype,expected", [
        ("CORRECTION", ("CORRECTION", "FACT")),
        ("PREFERENCE", ("DECLARATION", "FACT")),
        ("RULE-PROPOSAL", (None, "RULE")),
        ("INTENTION", ("DECLARATION", "INTENTION")),
        ("OBSERVATION", ("INFERENCE", "FACT")),
        ("PATTERN", ("INFERENCE", "FACT")),
    ])
    def test_the_six_old_values(self, ltype, expected):
        assert taxonomy.normalize_legacy({"type": ltype}) == expected

    def test_rule_proposal_carries_no_provenance(self):
        """The old vocabulary never recorded where a proposed rule came from.

        `None` says so. Substituting `INFERENCE` here would look harmless and
        would be a claim about origin that no record ever made — the consumer
        that needs a value is the one entitled to make that substitution.
        """
        provenance, kind = taxonomy.normalize_legacy({"type": "RULE-PROPOSAL"})
        assert provenance is None
        assert kind == "RULE"

    @pytest.mark.parametrize("record", [{}, {"type": ""}, {"type": "NONSENSE"}])
    def test_unknown_type_yields_no_labels(self, record):
        assert taxonomy.normalize_legacy(record) == (None, None)

    def test_it_never_reads_the_description(self):
        """Two records with the same `type` and opposite wording map alike.

        Guessing that a confidently-worded OBSERVATION was "really empirical"
        is the exact fabrication the taxonomy exists to prevent.
        """
        confident = {"type": "OBSERVATION", "description": "Verified by running the suite."}
        hedged = {"type": "OBSERVATION", "description": "Probably true, unchecked."}
        assert taxonomy.normalize_legacy(confident) == taxonomy.normalize_legacy(hedged)


# ---------------------------------------------------------------------------
# Case 1, 3, 4 — parsing a learning record
# ---------------------------------------------------------------------------

class TestParseLearning:
    def test_a_record_with_both_axes_parses(self):
        entry = _parse_learning(
            "trust: verified\n"
            "provenance: CORRECTION\n"
            "kind: RULE\n"
            "target: dev.md\n"
            "description: Stage with a pathspec.\n"
            "action: Add to the git section.\n"
        )
        assert entry["provenance"] == "CORRECTION"
        assert entry["kind"] == "RULE"
        assert entry["trust"] == "verified"

    def test_lowercase_values_are_normalized(self):
        entry = _parse_learning(
            "provenance: empirical\nkind: fact\ndescription: It works now.\n"
        )
        assert (entry["provenance"], entry["kind"]) == ("EMPIRICAL", "FACT")

    def test_out_of_set_provenance_is_rejected_not_coerced(self):
        """Case 3. The description survives; the doubtful label does not.

        Dropping the whole learning would throw away the only part nobody is
        unsure about, and keeping a coerced label would put a fabricated origin
        into the file.
        """
        entry = _parse_learning(
            "provenance: VERIFIED\nkind: FACT\ndescription: Something happened.\n"
        )
        assert "provenance" not in entry
        assert entry["kind"] == "FACT"
        assert entry["description"] == "Something happened."

    def test_out_of_set_kind_is_rejected_not_coerced(self):
        entry = _parse_learning(
            "provenance: RESEARCH\nkind: PREFERENCE\ndescription: The docs say X.\n"
        )
        assert "kind" not in entry
        assert entry["provenance"] == "RESEARCH"

    def test_a_legacy_record_still_parses(self):
        """Case 2. 227 pending records use the old shape and are never rewritten."""
        entry = _parse_learning(
            "trust: high\ntype: OBSERVATION\ntarget: dev.md\n"
            "description: The router caps at 40 KB.\naction: Note it.\n"
        )
        assert entry["type"] == "OBSERVATION"
        assert "provenance" not in entry
        assert "kind" not in entry

    def test_a_missing_provenance_surfaces_as_absent(self):
        """Case 4. Absent is a state the record can be in, not a hole to fill."""
        entry = _parse_learning("type: OBSERVATION\ndescription: A thing.\n")
        assert entry.get("provenance") is None
        assert taxonomy.pair(entry) == ("INFERENCE", "FACT")  # read-time only


class TestPairResolution:
    def test_explicit_fields_win_over_legacy_type(self):
        record = {"type": "OBSERVATION", "provenance": "EMPIRICAL", "kind": "RULE"}
        assert taxonomy.pair(record) == ("EMPIRICAL", "RULE")

    def test_a_rejected_half_falls_back_to_what_legacy_can_say(self):
        record = {"type": "CORRECTION", "provenance": "NONSENSE", "kind": "RULE"}
        assert taxonomy.pair(record) == ("CORRECTION", "RULE")

    def test_has_taxonomy_is_false_for_a_legacy_record(self):
        assert not taxonomy.has_taxonomy({"type": "CORRECTION"})
        assert not taxonomy.has_taxonomy({"provenance": "NOT-A-PROVENANCE"})
        assert taxonomy.has_taxonomy({"kind": "RULE"})

    def test_format_and_parse_round_trip(self):
        assert taxonomy.format_pair("CORRECTION", "FACT") == "CORRECTION/FACT"
        assert taxonomy.parse_pair("CORRECTION/FACT") == ("CORRECTION", "FACT")

    def test_an_unknown_half_renders_as_a_question_mark(self):
        assert taxonomy.format_pair(None, "RULE") == "?/RULE"
        assert taxonomy.parse_pair("?/RULE") == (None, "RULE")

    def test_parsing_junk_yields_no_labels(self):
        assert taxonomy.parse_pair("") == (None, None)
        assert taxonomy.parse_pair("nonsense/garbage") == (None, None)


# ---------------------------------------------------------------------------
# Case 1 and 7 — rendering
# ---------------------------------------------------------------------------

class TestRendering:
    def test_a_taxonomy_record_renders_the_pair(self):
        line = _format_learning_entry({
            "provenance": "CORRECTION",
            "kind": "RULE",
            "description": "Stage with a pathspec.",
            "target": "dev.md",
            "action": "Add to the git section.",
        })
        assert line == (
            "- **[CORRECTION/RULE]** Stage with a pathspec. "
            "→ Target: dev.md — Add to the git section."
        )

    def test_trust_is_dropped_from_the_new_line(self):
        """Two confidence-ish markers on one line is what made the old format
        ambiguous. `trust` stays in the record, off the line."""
        line = _format_learning_entry({
            "trust": "verified", "provenance": "EMPIRICAL", "kind": "FACT",
            "description": "It works now.",
        })
        assert "trust" not in line
        assert line.startswith("- **[EMPIRICAL/FACT]**")

    def test_a_legacy_record_renders_in_the_legacy_form(self):
        line = _format_learning_entry({
            "trust": "high", "type": "OBSERVATION",
            "description": "The router caps at 40 KB.", "target": "dev.md",
        })
        assert line.startswith("- **[trust: high]** OBSERVATION ")

    def test_a_legacy_line_claims_no_provenance_it_does_not_have(self):
        """Case 7. `normalize_legacy` is a reading, not a fact about the record.

        Printing its output into the file would turn a conservative guess into
        a written-down claim that survives every later review.
        """
        line = _format_learning_entry({
            "trust": "high", "type": "PATTERN", "description": "A pattern.",
        })
        assert "INFERENCE" not in line
        assert "/FACT" not in line

    def test_a_half_labelled_record_renders_the_unknown_half(self):
        line = _format_learning_entry({"kind": "RULE", "description": "A rule."})
        assert line.startswith("- **[?/RULE]**")


# ---------------------------------------------------------------------------
# Case 6 — the ledger survives the format change
# ---------------------------------------------------------------------------

LEGACY_LEARNINGS = """\
---
## Session Learnings — 2026-08-06T10:00:00+00:00
Session: abc-123
- **[trust: verified]** CORRECTION Stage with a pathspec. → Target: dev.md — Add it.
- **[trust: high]** OBSERVATION The router caps at 40 KB. → Target: dev.md — Note it.

---
"""

# The same two learnings, re-rendered under the taxonomy. Same knowledge, same
# targets, same actions — only the label markers differ.
TAXONOMY_LEARNINGS = """\
---
## Session Learnings — 2026-08-06T10:00:00+00:00
Session: abc-123
- **[CORRECTION/RULE]** Stage with a pathspec. → Target: dev.md — Add it.
- **[INFERENCE/FACT]** The router caps at 40 KB. → Target: dev.md — Note it.

---
"""


class TestLedgerCompatibility:
    def test_the_render_change_is_hash_invisible(self):
        """The property that makes the whole change safe to ship.

        Keys are hashed from what a learning SAYS, so relabelling it is not a
        new record. Without this, adding the marker would have re-keyed the
        entire pending backlog at once and re-proposed work already reviewed.
        """
        [legacy] = parse_blocks("2026-08-06.md", LEGACY_LEARNINGS)
        [new] = parse_blocks("2026-08-06.md", TAXONOMY_LEARNINGS)
        assert legacy.key == new.key

    def test_a_pre_change_ledger_reports_its_records_consumed(self):
        """Case 6. A ledger written by the previous version, verbatim."""
        blocks = parse_blocks("2026-08-06.md", LEGACY_LEARNINGS)
        pre_change_ledger = {
            "version": 1,
            "processed": {
                legacy_block_key(b.text): {
                    "file": b.file, "proposal": "old.md", "at": "2026-08-06T10:00:00Z",
                }
                for b in blocks
            },
        }
        assert unprocessed(blocks, pre_change_ledger) == []

    def test_migration_rekeys_a_pre_change_ledger(self):
        """The one-time conversion, run against the records it was computed from.

        A legacy key is a hash of raw text and cannot be converted without that
        text, which is why migration takes the blocks and not just the ledger.
        """
        blocks = parse_blocks("2026-08-06.md", LEGACY_LEARNINGS)
        ledger = {
            "version": 1,
            "processed": {legacy_block_key(b.text): {"file": b.file} for b in blocks},
        }
        assert taxonomy_migrate(ledger, blocks) == len(blocks)
        assert set(ledger["processed"]) == {b.key for b in blocks}
        assert ledger["version"] == LEDGER_VERSION

    def test_migration_is_idempotent(self):
        blocks = parse_blocks("2026-08-06.md", LEGACY_LEARNINGS)
        ledger = {
            "version": 1,
            "processed": {legacy_block_key(b.text): {"file": b.file} for b in blocks},
        }
        taxonomy_migrate(ledger, blocks)
        assert taxonomy_migrate(ledger, blocks) == 0

    def test_after_migration_a_relabelled_record_is_still_consumed(self):
        """Once re-keyed, the ledger no longer cares how a record is rendered."""
        blocks = parse_blocks("2026-08-06.md", LEGACY_LEARNINGS)
        ledger = {
            "version": 1,
            "processed": {legacy_block_key(b.text): {"file": b.file} for b in blocks},
        }
        taxonomy_migrate(ledger, blocks)
        relabelled = parse_blocks("2026-08-06.md", TAXONOMY_LEARNINGS)
        assert unprocessed(relabelled, ledger) == []

    def test_migration_leaves_an_unknown_key_alone(self):
        """A key whose learnings file is gone stays put for `prune` to remove."""
        blocks = parse_blocks("2026-08-06.md", LEGACY_LEARNINGS)
        ledger = {"version": 1, "processed": {"deadbeefdeadbeef": {"file": "gone.md"}}}
        assert taxonomy_migrate(ledger, blocks) == 0
        assert "deadbeefdeadbeef" in ledger["processed"]

    def test_genuinely_new_content_is_still_new(self):
        """The compatibility shim must not swallow real input.

        Changing what a learning says changes its key, under either scheme.
        """
        changed = TAXONOMY_LEARNINGS.replace("40 KB", "80 KB")
        blocks = parse_blocks("2026-08-06.md", LEGACY_LEARNINGS)
        ledger = {
            "processed": {legacy_block_key(b.text): {"file": b.file} for b in blocks}
        }
        assert len(unprocessed(parse_blocks("2026-08-06.md", changed), ledger)) == 1

    def test_recording_skips_a_block_already_held_under_its_legacy_key(self, tmp_path):
        """Otherwise every upgrade doubles the ledger and `prune` never catches up."""
        [block] = parse_blocks("2026-08-06.md", LEGACY_LEARNINGS)
        path = tmp_path / "ledger.json"
        import json
        path.write_text(json.dumps({
            "version": 1,
            "processed": {legacy_block_key(block.text): {"file": block.file}},
        }))
        assert record(path, [block], "new.md") == 0

    def test_the_version_marks_the_new_key_scheme(self):
        assert LEDGER_VERSION == 2

    def test_keys_still_survive_a_whitespace_reformat(self):
        """The property the projection had to preserve, not merely not break.

        A stray editor save must not orphan every key in a file.
        """
        trailing_space = "\n".join(
            line + "   " for line in LEGACY_LEARNINGS.splitlines()
        )
        assert block_key(LEGACY_LEARNINGS) == block_key(trailing_space)


# ---------------------------------------------------------------------------
# Case 5 — the pair reaches the proposal item (contract C1)
# ---------------------------------------------------------------------------

PROPOSAL = """\
# Processed Learnings — 2026-08-08

---

## Updates for `dev.md`

### 1. Stage with a pathspec
**Section:** Git
**Change:** add
**Provenance:** CORRECTION/RULE
> Always stage with an explicit pathspec.

**Source:** 2026-08-06.md:4

### 2. Router injection cap
**Section:** Routing
**Change:** add
**Provenance:** INFERENCE/FACT
> The router caps injection at 40 KB.

**Source:** 2026-08-06.md:5

---
"""


class TestPairReachesTheProposal:
    def test_parse_proposal_entries_exposes_both_halves(self):
        """Case 5, and the end of contract C1's path.

        A pair that stops at the learnings file is a pair no classifier can
        read, which is the failure mode this assertion exists to catch.
        """
        entries = parse_proposal_entries(PROPOSAL)
        assert [(e["provenance"], e["kind"]) for e in entries] == [
            ("CORRECTION", "RULE"), ("INFERENCE", "FACT"),
        ]

    def test_the_taxonomy_survives_the_whole_hop(self):
        """A CORRECTION/RULE learning still reads CORRECTION/RULE at the far end."""
        learning = {
            "provenance": "CORRECTION", "kind": "RULE",
            "description": "Always stage with an explicit pathspec.",
            "target": "dev.md", "action": "Add to the Git section.",
        }
        line = _format_learning_entry(learning)
        rendered_pair = line.split("**[")[1].split("]**")[0]
        [entry] = parse_proposal_entries(
            "## Updates for `dev.md`\n\n"
            "### 1. Stage with a pathspec\n"
            "**Section:** Git\n**Change:** add\n"
            f"**Provenance:** {rendered_pair}\n"
            "> Always stage with an explicit pathspec.\n"
        )
        assert (entry["provenance"], entry["kind"]) == ("CORRECTION", "RULE")

    def test_a_proposal_without_the_field_parses_with_empty_halves(self):
        """Every proposal drafted before this change. Absent is not a failure."""
        [entry] = parse_proposal_entries(
            "## Updates for `dev.md`\n\n### 1. A title\n"
            "**Section:** Git\n**Change:** add\n> Some text.\n"
        )
        assert entry["provenance"] == ""
        assert entry["kind"] == ""

    def test_an_unrecognised_pair_comes_back_empty(self):
        """A consumer never sees a label the vocabulary does not define."""
        [entry] = parse_proposal_entries(
            "## Updates for `dev.md`\n\n### 1. A title\n"
            "**Section:** Git\n**Change:** add\n"
            "**Provenance:** VERIFIED/PREFERENCE\n> Some text.\n"
        )
        assert (entry["provenance"], entry["kind"]) == ("", "")

    def test_the_provenance_line_never_enters_memory_text(self):
        """Only `> ` lines are the insert. A label must not land in a memory file."""
        entries = parse_proposal_entries(PROPOSAL)
        assert all("Provenance" not in e["text"] for e in entries)
