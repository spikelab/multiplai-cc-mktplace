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
        assert {b.key for b in blocks} <= set(ledger["processed"])
        assert ledger["version"] == LEDGER_VERSION
        # The legacy key stays as an alias — see TestMigrationKeepsLegacyAliases
        # for the staged-draft loss that popping it caused.
        assert {legacy_block_key(b.text) for b in blocks} <= set(ledger["processed"])

    def test_the_alias_and_the_new_key_share_one_entry(self):
        """So `prune` removes both together and neither can go stale."""
        blocks = parse_blocks("2026-08-06.md", LEGACY_LEARNINGS)
        ledger = {
            "version": 1,
            "processed": {legacy_block_key(b.text): {"file": b.file} for b in blocks},
        }
        taxonomy_migrate(ledger, blocks)
        for b in blocks:
            assert (
                ledger["processed"][b.key]
                is ledger["processed"][legacy_block_key(b.text)]
            )

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


class TestMigrationKeepsLegacyAliases:
    """Requirement: a crashed run straddling the upgrade loses nothing.

    Staged-draft sidecars store **raw key strings** — a pre-upgrade run wrote
    ``[b.key for b in chunk.blocks]``, which under a v1 ledger are legacy keys —
    and ``dream._resume_staged_drafts`` tests raw membership rather than going
    through ``lookup``. Popping the legacy key made that test fail, which
    deleted the staged draft *while* ``unprocessed`` correctly still saw the
    record as consumed. The drafted content was destroyed and the learning
    stayed marked consolidated forever: precisely the "silent learning loss"
    ``_enforce_ledger_coverage`` exists to prevent, and the same
    crash-then-resume worked before version 2.
    """

    def _v1_ledger(self, blocks):
        return {
            "version": 1,
            "processed": {
                legacy_block_key(b.text): {"file": b.file, "proposal": "old.md"}
                for b in blocks
            },
        }

    def test_a_staged_draft_written_under_v1_keys_survives(self):
        blocks = parse_blocks("2026-08-06.md", LEGACY_LEARNINGS)
        ledger = self._v1_ledger(blocks)
        # What the crashed pre-upgrade run persisted beside its draft: `b.key`
        # as that version computed it, which is the raw-text hash.
        staged_keys = [legacy_block_key(b.text) for b in blocks]

        taxonomy_migrate(ledger, blocks)

        processed = ledger["processed"]
        # The exact membership test _resume_staged_drafts performs. False here
        # means it unlinks the draft and its sidecar.
        assert all(k in processed for k in staged_keys)

    def test_the_record_is_still_consumed_under_the_new_key(self):
        """Both halves have to hold, or the fix just moves the loss."""
        blocks = parse_blocks("2026-08-06.md", LEGACY_LEARNINGS)
        ledger = self._v1_ledger(blocks)
        taxonomy_migrate(ledger, blocks)
        assert unprocessed(blocks, ledger) == []

    def test_prune_removes_the_alias_too(self, tmp_path):
        """The alias must not outlive the record it aliases."""
        import json

        from lib.learnings_ledger import prune

        blocks = parse_blocks("2026-08-06.md", LEGACY_LEARNINGS)
        ledger = self._v1_ledger(blocks)
        taxonomy_migrate(ledger, blocks)
        path = tmp_path / "ledger.json"
        path.write_text(json.dumps(ledger))

        removed = prune(path, set())
        assert removed == 2 * len(blocks)
        assert load_json(path)["processed"] == {}

    def test_migration_is_still_idempotent_with_aliases_present(self):
        blocks = parse_blocks("2026-08-06.md", LEGACY_LEARNINGS)
        ledger = self._v1_ledger(blocks)
        assert taxonomy_migrate(ledger, blocks) == len(blocks)
        assert taxonomy_migrate(ledger, blocks) == 0
        before = dict(ledger["processed"])
        taxonomy_migrate(ledger, blocks)
        assert ledger["processed"] == before


class TestLookup:
    """New public function, one production call site, previously no test."""

    def test_finds_an_entry_under_the_new_key(self):
        from lib.learnings_ledger import lookup

        [block] = parse_blocks("2026-08-06.md", LEGACY_LEARNINGS)[:1]
        entry = {"file": block.file, "proposal": "p.md"}
        assert lookup({block.key: entry}, block) is entry

    def test_finds_an_entry_under_the_legacy_key(self):
        """A ledger that has not migrated must not read as 'never consolidated'."""
        from lib.learnings_ledger import lookup

        [block] = parse_blocks("2026-08-06.md", LEGACY_LEARNINGS)[:1]
        entry = {"file": block.file, "proposal": "p.md"}
        assert lookup({legacy_block_key(block.text): entry}, block) is entry

    def test_returns_none_when_absent(self):
        from lib.learnings_ledger import lookup

        [block] = parse_blocks("2026-08-06.md", LEGACY_LEARNINGS)[:1]
        assert lookup({}, block) is None

    def test_a_mixed_ledger_resolves_every_record(self):
        """Some v1, some v2, one whose file is gone — all through one read."""
        from lib.learnings_ledger import lookup

        blocks = parse_blocks("2026-08-06.md", LEGACY_LEARNINGS)
        processed = {"deadbeefdeadbeef": {"file": "gone.md"}}
        for i, b in enumerate(blocks):
            key = b.key if i % 2 == 0 else legacy_block_key(b.text)
            processed[key] = {"file": b.file, "proposal": f"p{i}.md"}
        for b in blocks:
            assert lookup(processed, b) is not None
        assert unprocessed(blocks, {"processed": processed}) == []


def load_json(path):
    import json

    return json.loads(path.read_text())


class TestProjectionCollisions:
    """Requirement: two different learnings never share a key.

    The optional legacy-type word was stripped after *both* regex arms, so a
    new-form learning whose description happened to begin with one of six caps
    words projected to the same string as the same learning without it — and the
    second was silently treated as already consolidated.
    """

    @pytest.mark.parametrize(
        "word",
        ["OBSERVATION", "PREFERENCE", "CORRECTION", "PATTERN", "INTENTION"],
    )
    def test_a_description_opening_with_a_legacy_type_word_keeps_its_own_key(self, word):
        with_word = (
            "---\n## Session Learnings — 2026-08-06T10:00:00Z\n"
            f"- **[EMPIRICAL/FACT]** {word} about the router. → Target: dev.md\n"
        )
        without = (
            "---\n## Session Learnings — 2026-08-06T10:00:00Z\n"
            "- **[EMPIRICAL/FACT]** about the router. → Target: dev.md\n"
        )
        assert block_key(with_word) != block_key(without)

    def test_the_legacy_arm_still_strips_its_type_word(self):
        """The strip is what makes the two renderings of one learning agree."""
        legacy = (
            "---\n## Session Learnings — 2026-08-06T10:00:00Z\n"
            "- **[trust: verified]** CORRECTION the cache is per-session. "
            "→ Target: dev.md — fix\n"
        )
        taxonomy_form = (
            "---\n## Session Learnings — 2026-08-06T10:00:00Z\n"
            "- **[CORRECTION/FACT]** the cache is per-session. "
            "→ Target: dev.md — fix\n"
        )
        assert block_key(legacy) == block_key(taxonomy_form)


class TestRenderInvarianceIsDrivenByTheRenderer:
    """The PR's core invariant, asserted against the real writer.

    Every other test in this file uses hand-written string fixtures, so an edit
    to ``_format_learning_entry`` could break render-invariance without failing
    anything.
    """

    @pytest.mark.parametrize(
        "legacy_type,provenance,kind",
        [
            ("CORRECTION", "CORRECTION", "FACT"),
            ("PREFERENCE", "DECLARATION", "FACT"),
            ("INTENTION", "DECLARATION", "INTENTION"),
            ("OBSERVATION", "INFERENCE", "FACT"),
            ("RULE-PROPOSAL", None, "RULE"),
        ],
    )
    def test_both_renderings_of_one_learning_hash_alike(
        self, legacy_type, provenance, kind
    ):
        desc = "the router caches picks per session"
        legacy = _format_learning_entry({
            "trust": "verified", "type": legacy_type, "description": desc,
            "target": "dev.md", "action": "note it",
        })
        new = _format_learning_entry({
            "provenance": provenance, "kind": kind, "description": desc,
            "target": "dev.md", "action": "note it",
        })
        wrap = "---\n## Session Learnings — 2026-08-06T10:00:00Z\n{}\n"
        assert block_key(wrap.format(legacy)) == block_key(wrap.format(new))


class TestRankingsHaveOneHome:
    """Requirement: the ordering is not written down twice.

    ``taxonomy`` used to say ranking "belongs to whatever makes that decision",
    and then the drafting prompt shipped exactly this ordering as prose while a
    downstream rubric was about to encode it in code. Two sources of truth that
    can silently disagree is the failure this guards.
    """

    def test_both_rankings_cover_their_whole_value_set(self):
        assert set(taxonomy.PROVENANCE_STRENGTH) == set(taxonomy.PROVENANCES)
        assert set(taxonomy.KIND_BREADTH) == set(taxonomy.KINDS)

    def test_weakest_and_broadest_win_a_merge(self):
        assert taxonomy.weakest_provenance(["CORRECTION", "INFERENCE"]) == "INFERENCE"
        assert taxonomy.weakest_provenance(["EMPIRICAL", "DECLARATION"]) == "EMPIRICAL"
        assert taxonomy.broadest_kind(["FACT", "RULE"]) == "RULE"
        assert taxonomy.broadest_kind(["FACT", "DECISION"]) == "DECISION"

    def test_they_are_total_on_junk(self):
        assert taxonomy.weakest_provenance([]) is None
        assert taxonomy.weakest_provenance([None, "", "NOPE"]) is None
        assert taxonomy.broadest_kind(["NOPE"]) is None

    def test_the_drafting_prompt_renders_them_rather_than_restating_them(self):
        import dream

        prompt = dream._PROPOSAL_SYSTEM
        assert "@PROVENANCE_STRENGTH@" not in prompt
        assert "@KIND_BREADTH@" not in prompt
        assert taxonomy.render_ranking(taxonomy.PROVENANCE_STRENGTH) in prompt
        assert taxonomy.render_ranking(taxonomy.KIND_BREADTH) in prompt


class TestParsePairKeepsBothHalves:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("CORRECTION/RULE", ("CORRECTION", "RULE")),
            ("CORRECTION/RULE extra words", ("CORRECTION", "RULE")),
            ("CORRECTION/RULE (from the earlier session)", ("CORRECTION", "RULE")),
            (" EMPIRICAL / FACT ", ("EMPIRICAL", "FACT")),
            ("?/RULE", (None, "RULE")),
            ("EMPIRICAL/?", ("EMPIRICAL", None)),
            ("EMPIRICAL", ("EMPIRICAL", None)),
            ("junk", (None, None)),
            ("", (None, None)),
            (None, (None, None)),
        ],
    )
    def test_a_trailing_parenthetical_does_not_eat_the_kind(self, text, expected):
        """The drafter is a model writing free text; a parenthetical is likely,
        and the half it silently lost is the one carrying blast radius."""
        assert taxonomy.parse_pair(text) == expected

    def test_it_round_trips_format_pair(self):
        for provenance in taxonomy.PROVENANCES:
            for kind in taxonomy.KINDS:
                rendered = taxonomy.format_pair(provenance, kind)
                assert taxonomy.parse_pair(rendered) == (provenance, kind)


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
