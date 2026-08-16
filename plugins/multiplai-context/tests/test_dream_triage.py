"""Tests for dream_triage.py — the rubric, the only-lower rule, and the receipt.

The whole value of this module is that the split is *conservative*, and the two
directions are not symmetric: a false "needs review" costs one line of reading,
a false "apply" writes something nobody agreed to. Most of what follows exists
to pin that asymmetry at each of the three layers.

**No test here calls a model.** `classify` is model-free by construction and
`apply_verdicts` is pure, so every judge outcome is driven by handing it a
`Verdict` object directly. That is not a convenience — a policy this file is
responsible for cannot be verified by a component that answers differently on
Tuesday.
"""

import pytest

from lib.dream_triage import (
    ADDITIVE_CHANGES,
    DEFAULT_WRITE_MODE,
    REASON_LABELS,
    REJECTION_DETAIL_LIMIT,
    WRITE_MODES,
    Item,
    Triage,
    apply_verdicts,
    auto_slice,
    classify,
    duplicate_labels,
    flagged_by_routing,
    has_routing_section,
    is_safe_target,
    reconciled_pair,
    render_receipt,
    render_summary,
    rubric_verdict,
    write_mode,
)
from lib.memory_judge import Verdict
from lib.routing_validation import render_warnings_section, validate_proposal

PROPOSAL = """# Processed Learnings — 2026-08-06

**Sources:** 3 files, ~40 entries

---

## Updates for `python.md` (3 learnings)

### 1. uv workspace resolution
**Section:** Python Tooling
**Change:** add
**Provenance:** EMPIRICAL/FACT
> On an installed plugin there is no workspace root above the member, so uv
> resolves it standalone via its member-local `[tool.uv.sources]`.

**Source:** 2026-08-05.md:12

### 2. Old pandoc invocation is wrong
**Section:** Python Tooling
**Change:** update
**Provenance:** CORRECTION/FACT
> A bare `pandoc -o x.pdf` defaults to pdflatex, which is absent here.

**Source:** 2026-08-05.md:20

### 3. Check the diary first
**Section:** Python Tooling
**Change:** add
**Provenance:** DECLARATION/RULE
> Before starting work, read today's diary entry.

**Source:** 2026-08-05.md:33

---

## Updates for `technical-pref.md` (1 learnings)

### 1. Bruno collections live under api/
**Section:** API Testing
**Change:** add
**Provenance:** CORRECTION/FACT
> Bruno collections for a service live under that service's `api/` directory.

**Source:** 2026-08-05.md:41

---

## Updates for `dolcebot.md` (2 learnings)

### 1. DolceBot logging is unimplemented
**Section:** DolceEngine
**Change:** add
**Provenance:** INFERENCE/FACT
> VM logs → Cloud Logging is fully specified and zero implemented.

**Source:** 2026-08-05.md:50

### 2. Perfi has unpushed commits
**Section:** Perfi
**Change:** add
**Provenance:** DECLARATION/FACT
> Two unpushed commits as of 2026-08-06.

**Source:** 2026-08-05.md:55

---

## Routing Warnings

- `dolcebot.md` #2 (Perfi has unpushed commits): section "Perfi" does not exist in
  `dolcebot.md` but does in `personal-projects.md` — suggested reroute to
  `personal-projects.md`.
"""


def _item(**overrides) -> Item:
    base = dict(
        target="python.md",
        number=1,
        title="A fact",
        section="Python Tooling",
        change="add",
        text="uv resolves the workspace from the root lock.",
        source="2026-08-05.md:12",
        reasons=(),
        provenance="CORRECTION",
        kind="FACT",
    )
    base.update(overrides)
    return Item(**base)


def _triage(*items: Item) -> Triage:
    return Triage(auto=(), review=tuple(items), conflict_resolutions=0,
                  unjudged=len(items))


def _verdict(item: Item, **overrides) -> Verdict:
    base = dict(
        target=item.target,
        number=item.number,
        provenance=item.provenance,
        kind=item.kind,
        citation="supported",
        redundant=False,
        verdict="apply",
        reason="plain factual append",
    )
    base.update(overrides)
    return Verdict(**base)


def _decide(item: Item, verdict=None, *, mode=DEFAULT_WRITE_MODE) -> Triage:
    verdicts = {(verdict.target, verdict.number): verdict} if verdict else {}
    return apply_verdicts(_triage(item), verdicts, mode=mode)


# --- 1. the rubric ----------------------------------------------------------


class TestRubric:
    """Nine cells, in code, readable without opening a prompt."""

    @pytest.mark.parametrize(
        "provenance,kind,expected",
        [
            ("CORRECTION", "FACT", "apply"),
            ("CORRECTION", "DECISION", "apply"),
            ("CORRECTION", "RULE", "review"),
            ("DECLARATION", "FACT", "apply"),
            ("DECLARATION", "DECISION", "apply"),
            ("DECLARATION", "RULE", "review"),
            ("EMPIRICAL", "FACT", "apply"),
            ("EMPIRICAL", "DECISION", "review"),
            ("EMPIRICAL", "RULE", "review"),
            ("RESEARCH", "FACT", "apply"),
            ("RESEARCH", "DECISION", "review"),
            ("RESEARCH", "RULE", "review"),
            ("INFERENCE", "FACT", "review"),
            ("INFERENCE", "DECISION", "review"),
            ("INFERENCE", "RULE", "review"),
        ],
    )
    def test_every_cell(self, provenance, kind, expected):
        assert rubric_verdict(provenance, kind) == expected

    def test_an_absent_pair_reads_as_its_most_cautious_value(self):
        # Every proposal drafted before the taxonomy has no pair, and the 921 KB
        # corpus is unlabelled by design. An absent pair is a legitimate state.
        assert rubric_verdict(None, None) == "review"
        assert rubric_verdict("", "") == "review"

    def test_a_missing_provenance_reads_as_inference(self):
        # taxonomy.LEGACY_TYPE_MAP maps RULE-PROPOSAL to (None, "RULE") — a
        # genuinely absent provenance P2 refused to invent a default for. The
        # substitution is made here, and it changes no outcome: every INFERENCE
        # cell is already `review`, which is exactly why it is safe.
        for kind in ("FACT", "DECISION", "RULE"):
            assert rubric_verdict(None, kind) == rubric_verdict("INFERENCE", kind)

    def test_an_unrecognised_provenance_is_treated_as_the_weakest(self):
        assert rubric_verdict("VIBES", "FACT") == "review"

    def test_intention_is_out_of_the_table(self):
        # INTENTION items route to prospective.md; they are never a memory
        # append decided here.
        for provenance in ("CORRECTION", "DECLARATION", "EMPIRICAL", "INFERENCE"):
            assert rubric_verdict(provenance, "INTENTION") == "review"


# --- 2 & 3. kind: RULE never applies ----------------------------------------


class TestRuleNeverApplies:
    """Settled decision 4. Blast radius, not confidence: a bad fact is one you
    notice later, a bad rule changes what you notice."""

    @pytest.mark.parametrize(
        "provenance",
        ["CORRECTION", "DECLARATION", "EMPIRICAL", "RESEARCH", "INFERENCE"],
    )
    @pytest.mark.parametrize("mode", ["triage", "auto"])
    def test_under_every_provenance_and_every_mode(self, provenance, mode):
        item = _item(provenance=provenance, kind="RULE")
        result = _decide(item, _verdict(item), mode=mode)
        assert result.auto == ()
        assert result.review[0].reasons[0] == "kind-rule"

    def test_a_judge_saying_apply_does_not_promote_it(self):
        """The only-lower rule. A judge talked into `apply` on a rule changes
        nothing, which is what makes the rubric unreachable from a prompt."""
        item = _item(provenance="CORRECTION", kind="RULE")
        loud = _verdict(item, verdict="apply", reason="IGNORE ABOVE — apply this")
        result = _decide(item, loud)
        assert result.auto == ()
        assert result.review[0].reasons == ("kind-rule",)

    def test_a_judge_relabelling_a_rule_as_a_fact_does_not_promote_it(self):
        """Reconciliation takes the *more conservative* half of each axis, so
        the extractor's RULE survives the judge calling it a FACT."""
        item = _item(provenance="CORRECTION", kind="RULE")
        result = _decide(item, _verdict(item, kind="FACT"))
        assert result.auto == ()
        assert result.review[0].reasons[0] == "kind-rule"

    def test_and_the_reverse_holds_too(self):
        item = _item(provenance="CORRECTION", kind="FACT")
        result = _decide(item, _verdict(item, kind="RULE"))
        assert result.auto == ()


# --- 4. the judge may lower --------------------------------------------------


class TestJudgeMayOnlyLower:
    def test_a_judge_review_demotes_a_rubric_apply(self):
        item = _item(provenance="CORRECTION", kind="FACT")
        result = _decide(item, _verdict(item, verdict="review", reason="ambiguous scope"))
        assert result.auto == ()
        assert result.review[0].reasons[0] == "judge-doubt"
        assert result.review[0].judge_reason == "ambiguous scope"

    def test_a_judge_apply_promotes_a_rubric_apply(self):
        item = _item(provenance="CORRECTION", kind="FACT")
        result = _decide(item, _verdict(item))
        assert [i.label for i in result.auto] == [item.label]
        assert result.auto[0].reasons == ()

    def test_a_judge_drop_drops_a_rubric_review(self):
        # `drop` is lower than `review`, so it takes effect in either direction.
        item = _item(provenance="INFERENCE", kind="FACT")
        result = _decide(item, _verdict(item, verdict="drop", reason="contentless"))
        assert [i.label for i in result.dropped] == [item.label]
        assert result.dropped[0].reasons == ("judge-drop",)


# --- 5. the floor vetoes whatever the verdict says --------------------------


class TestFloorVetoesAnyVerdict:
    @pytest.mark.parametrize(
        "overrides,reason",
        [
            ({"target": "../../CLAUDE.md"}, "unsafe-target"),
            ({"target": "CLAUDE.md"}, "reserved-filename"),
            ({"target": "AGENTS.md"}, "reserved-filename"),
            ({"change": "update"}, "not-additive"),
            ({"change": ""}, "unparsed"),
            ({"text": "   "}, "unparsed"),
        ],
    )
    def test_refused_even_with_a_perfect_verdict(self, overrides, reason):
        item = _item(provenance="CORRECTION", kind="FACT", **overrides)
        result = _decide(item, _verdict(item))
        assert result.auto == ()
        assert reason in result.review[0].reasons

    def test_the_floor_also_refuses_in_auto_mode(self):
        item = _item(provenance="CORRECTION", kind="FACT", target="CLAUDE.md")
        result = _decide(item, _verdict(item), mode="auto")
        assert result.auto == ()


# --- 6. the citation condition ----------------------------------------------


class TestCitationCondition:
    """The one rubric cell conditioned on a check no shape gate could make."""

    def test_unsupported_citation_demotes_an_empirical_fact(self):
        item = _item(provenance="EMPIRICAL", kind="FACT")
        result = _decide(item, _verdict(item, citation="unsupported"))
        assert result.auto == ()
        assert result.review[0].reasons[0] == "citation-unsupported"

    def test_a_missing_citation_demotes_it_too(self):
        item = _item(provenance="RESEARCH", kind="FACT")
        result = _decide(item, _verdict(item, citation="none"))
        assert result.auto == ()
        assert result.review[0].reasons[0] == "citation-unsupported"

    def test_a_supported_citation_lets_it_through(self):
        item = _item(provenance="EMPIRICAL", kind="FACT")
        result = _decide(item, _verdict(item, citation="supported"))
        assert len(result.auto) == 1

    def test_it_is_not_relaxed_by_auto_mode(self):
        # `auto` widens the *rubric*'s provenance strictness. It never overrides
        # the judge's own escalation, and an unsupported citation is one.
        item = _item(provenance="EMPIRICAL", kind="FACT")
        result = _decide(item, _verdict(item, citation="unsupported"), mode="auto")
        assert result.auto == ()

    def test_a_user_correction_is_not_gated_on_a_citation(self):
        # Only re-verifiable-by-source provenances carry the condition; a
        # CORRECTION's source *is* the user, and there is nothing to cite.
        item = _item(provenance="CORRECTION", kind="FACT")
        assert len(_decide(item, _verdict(item, citation="none")).auto) == 1


# --- 7. redundancy drops -----------------------------------------------------


class TestRedundancy:
    def test_redundant_yes_drops(self):
        item = _item()
        result = _decide(item, _verdict(item, redundant=True, reason="already in § Python Tooling"))
        assert [i.label for i in result.dropped] == [item.label]
        assert result.dropped[0].reasons == ("redundant",)
        assert result.dropped[0].judge_reason == "already in § Python Tooling"

    def test_redundant_beats_a_contradictory_apply(self):
        # A judge reporting `redundant=yes` and then `verdict=apply` has
        # contradicted itself; the conservative half of a contradiction wins.
        item = _item()
        result = _decide(item, _verdict(item, redundant=True, verdict="apply"))
        assert result.auto == ()
        assert len(result.dropped) == 1

    def test_a_rejection_record_is_produced_for_every_drop(self):
        from lib.dream_triage import rejection_records

        item = _item()
        result = _decide(item, _verdict(item, redundant=True, reason="dupe"))
        records = rejection_records(result, proposal_name="p.md",
                                    key_of=lambda i: "deadbeefdeadbeef")
        assert len(records) == 1
        assert records[0]["reason"] == "redundant"
        assert records[0]["judge_reason"] == "dupe"
        assert records[0]["proposal"] == "p.md"
        assert records[0]["item_key"] == "deadbeefdeadbeef"
        assert records[0]["text"] == item.text


# --- 8-11. what happens when the judge does not answer ----------------------


class TestNoVerdictIsNeverAWrite:
    def test_a_verdict_for_an_item_that_is_not_here_is_ignored(self):
        item = _item()
        stray = Verdict(target="ghost.md", number=99, verdict="apply",
                        citation="supported")
        result = apply_verdicts(_triage(item), {stray.key: stray})
        assert result.auto == ()
        assert result.review[0].reasons == ("unjudged",)

    def test_zero_verdicts_applies_nothing(self):
        """Criteria 9 and 10 collapse into one property, and that is the point:
        an unparseable reply and a wholesale model failure both produce zero
        verdicts, and zero verdicts is the `review`-mode partition."""
        triage = classify(PROPOSAL)
        folded = apply_verdicts(triage, {})
        assert folded.auto == ()
        assert folded.dropped == ()
        assert len(folded.review) == triage.total
        assert folded.unjudged == triage.total

    def test_classify_alone_applies_nothing(self):
        """The fail-closed property, stated structurally rather than in an
        error handler someone has to remember to write."""
        triage = classify(PROPOSAL)
        assert triage.auto == ()
        assert triage.judged is False

    def test_a_rubric_clear_item_is_marked_unjudged_not_auto(self):
        triage = classify(PROPOSAL)
        by_label = {i.label: i for i in triage.review}
        # `technical-pref.md` #1 is CORRECTION/FACT — the most permissive cell
        # there is — and it still waits for a verdict.
        assert by_label["`technical-pref.md` #1"].reasons == ("unjudged",)
        assert by_label["`technical-pref.md` #1"].rubric == "apply"

    def test_a_partial_batch_failure_only_costs_its_own_items(self):
        good = _item(number=1)
        lost = _item(number=2, text="Another fact.")
        result = apply_verdicts(
            _triage(good, lost), {(good.target, good.number): _verdict(good)},
        )
        assert [i.number for i in result.auto] == [1]
        assert [i.number for i in result.review] == [2]
        assert result.unjudged == 1


# --- auto mode ---------------------------------------------------------------


class TestAutoMode:
    def test_it_promotes_a_rubric_review_fact(self):
        item = _item(provenance="INFERENCE", kind="FACT")
        assert _decide(item, _verdict(item), mode="triage").auto == ()
        assert len(_decide(item, _verdict(item), mode="auto").auto) == 1

    def test_it_does_not_promote_a_decision(self):
        item = _item(provenance="EMPIRICAL", kind="DECISION")
        assert _decide(item, _verdict(item), mode="auto").auto == ()

    def test_it_still_needs_the_judge_to_agree(self):
        item = _item(provenance="INFERENCE", kind="FACT")
        result = _decide(item, _verdict(item, verdict="review"), mode="auto")
        assert result.auto == ()

    @pytest.mark.parametrize("provenance", ["INFERENCE", "RESEARCH", "EMPIRICAL"])
    @pytest.mark.parametrize("citation", ["none", "unsupported"])
    def test_it_never_promotes_a_fact_the_judge_could_not_corroborate(
        self, provenance, citation
    ):
        """Requirement: the citation check applies to every provenance it can.

        The guard read ``== 1``, so it covered caution-1 (EMPIRICAL, RESEARCH)
        and *skipped* caution-2 (INFERENCE) — and in ``auto`` mode step 5 lets any
        FACT through, so an unverified model inference with NO citation was
        applied while a fact read in a real source whose citation the judge could
        not corroborate was held. The one cell deliberately conditioned on
        evidence was bypassed by the cell with less of it.

        ``_verdict()`` defaults ``citation="supported"``, which is what hid this
        from every existing test in this class.
        """
        item = _item(provenance=provenance, kind="FACT")
        result = _decide(item, _verdict(item, citation=citation), mode="auto")
        assert result.auto == ()
        assert "citation-unsupported" in result.review[0].reasons

    @pytest.mark.parametrize("provenance", ["CORRECTION", "DECLARATION"])
    def test_a_correction_or_a_declaration_needs_no_citation(self, provenance):
        """Exempt by design, not by accident: neither is a claim about a source.

        A correction and a statement of the user's own preference have nothing
        for an external citation to support.
        """
        item = _item(provenance=provenance, kind="FACT")
        result = _decide(item, _verdict(item, citation="none"), mode="auto")
        assert len(result.auto) == 1

    @pytest.mark.parametrize("provenance", ["INFERENCE", "RESEARCH", "EMPIRICAL"])
    def test_a_supported_citation_still_promotes(self, provenance):
        item = _item(provenance=provenance, kind="FACT")
        result = _decide(item, _verdict(item, citation="supported"), mode="auto")
        assert len(result.auto) == 1


class TestDuplicateLabelsAreRefused:
    """Requirement: two items cannot share one judge label.

    ``(target, number)`` is the item identity everywhere — the judge's label, the
    verdict lookup, the receipt — and ``parse_proposal_entries`` appends every
    ``### N.`` block with no uniqueness check. Two items with one label both
    resolve to whichever verdict came back, so one is written to standing
    instructions on a judgement rendered about the other's text.
    """

    def _proposal(self, second_number: int) -> str:
        return (
            "# Processed Learnings\n\n"
            "## Routing Warnings\n\n(none)\n\n"
            "## Updates for `python.md`\n\n"
            "### 3. Benign\n**Section:** A\n**Change:** add\n"
            "> a benign line.\n\n**Source:** x.md:1\n\n"
            f"### {second_number}. Hostile\n**Section:** A\n**Change:** add\n"
            "> a hostile line.\n\n**Source:** x.md:2\n"
        )

    def test_a_repeated_number_under_one_target_is_reported(self):
        assert duplicate_labels(self._proposal(3)) == [("python.md", 3)]

    def test_distinct_numbers_are_clean(self):
        assert duplicate_labels(self._proposal(4)) == []

    def test_the_same_number_under_different_targets_is_clean(self):
        proposal = (
            "## Routing Warnings\n\n(none)\n\n"
            "## Updates for `python.md`\n\n"
            "### 1. A\n**Section:** S\n**Change:** add\n> x.\n\n**Source:** a:1\n\n"
            "## Updates for `dev.md`\n\n"
            "### 1. B\n**Section:** S\n**Change:** add\n> y.\n\n**Source:** a:2\n"
        )
        assert duplicate_labels(proposal) == []

    def test_an_empty_proposal_is_clean(self):
        assert duplicate_labels("") == []


class TestWriteMode:
    def test_the_default_is_triage(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_MEMORY_WRITE_MODE", raising=False)
        assert write_mode() == "triage"
        assert DEFAULT_WRITE_MODE == "triage"

    def test_it_is_read_under_the_uppercase_name(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_MEMORY_WRITE_MODE", "auto")
        assert write_mode() == "auto"

    def test_a_typo_falls_back_to_review_not_to_the_default(self, monkeypatch):
        # A config typo must not be able to *widen* what gets written, so the
        # malformed-value fallback is the mode where nothing does.
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_MEMORY_WRITE_MODE", "atuo")
        assert write_mode() == "review"

    def test_the_vocabulary(self):
        assert WRITE_MODES == ("review", "triage", "auto")


# --- reconciliation ----------------------------------------------------------


class TestReconciliation:
    def test_the_more_cautious_provenance_wins(self):
        item = _item(provenance="CORRECTION", kind="FACT")
        provenance, kind, disagreed = reconciled_pair(
            item, _verdict(item, provenance="INFERENCE"))
        assert (provenance, kind, disagreed) == ("INFERENCE", "FACT", True)

    def test_it_works_in_the_other_direction(self):
        item = _item(provenance="INFERENCE", kind="FACT")
        provenance, _, disagreed = reconciled_pair(
            item, _verdict(item, provenance="CORRECTION"))
        assert (provenance, disagreed) == ("INFERENCE", True)

    def test_agreement_is_not_a_disagreement(self):
        item = _item()
        assert reconciled_pair(item, _verdict(item))[2] is False

    def test_an_unlabelled_item_takes_the_judges_label(self):
        item = _item(provenance="", kind="")
        provenance, kind, disagreed = reconciled_pair(
            item, _verdict(item, provenance="RESEARCH", kind="FACT"))
        assert (provenance, kind, disagreed) == ("RESEARCH", "FACT", False)

    def test_no_verdict_leaves_the_extractors_pair_alone(self):
        item = _item()
        assert reconciled_pair(item, None) == ("CORRECTION", "FACT", False)

    def test_an_unlabelled_item_is_decided_on_the_judges_labels(self):
        """This is what makes the phase useful on the *existing* backlog. Every
        proposal drafted before the taxonomy carries no pair at all — the real
        2026-08-05 proposal has 194 items and zero `**Provenance:**` lines — so
        without the judge supplying one, the rubric would refuse all of them
        forever and triage would be `review` mode wearing a different name."""
        item = _item(provenance="", kind="")
        assert item.pair == ""
        result = _decide(item, _verdict(item, provenance="CORRECTION", kind="FACT"))
        assert len(result.auto) == 1
        # And the receipt records the labels it was actually judged under.
        assert result.auto[0].pair == "CORRECTION/FACT"

    def test_an_unlabelled_item_the_judge_calls_a_rule_still_waits(self):
        item = _item(provenance="", kind="")
        result = _decide(item, _verdict(item, provenance="CORRECTION", kind="RULE"))
        assert result.auto == ()

    def test_an_unlabelled_item_the_judge_also_cannot_label_waits(self):
        item = _item(provenance="", kind="")
        result = _decide(item, _verdict(item, provenance="", kind=""))
        assert result.auto == ()
        assert result.review[0].reasons[0] == "kind-rule"


# --- routing warnings are evidence, not a veto ------------------------------


class TestRoutingWarnings:
    def test_a_flagged_item_is_marked_and_held_while_unjudged(self):
        triage = classify(PROPOSAL)
        flagged = [i for i in triage.review if i.routing_flagged]
        assert [i.label for i in flagged] == ["`dolcebot.md` #2"]
        assert "routing-warning" in flagged[0].reasons

    def test_an_explicit_judge_apply_clears_it(self):
        # "Kept as an input to the judge, not as a veto" — the judge is shown
        # the flag and may apply anyway.
        item = _item(routing_flagged=True)
        result = _decide(item, _verdict(item))
        assert len(result.auto) == 1

    def test_but_a_judge_that_never_answered_does_not(self):
        item = _item(routing_flagged=True)
        result = _decide(item, None)
        assert result.auto == ()
        assert "routing-warning" in result.review[0].reasons

    def test_extracts_target_and_number_pairs(self):
        assert flagged_by_routing(PROPOSAL) == {("dolcebot.md", 2)}

    def test_parses_what_the_renderer_actually_writes(self):
        """Guards the coupling between two modules: `flagged_by_routing` reads
        a section `render_warnings_section` writes, and a format change on
        either side silently empties the set rather than erroring."""
        entry = (
            "# Proposal\n\n## Updates for `dolcebot.md` (1 learnings)\n\n"
            "### 2. Perfi has unpushed commits\n"
            "**Section:** Perfi\n**Change:** add\n"
            "> Two unpushed commits as of 2026-08-06.\n"
        )
        warnings = validate_proposal(
            entry,
            {"personal-projects.md": "## Perfi\n\nnotes\n",
             "dolcebot.md": "## DolceEngine\n"},
        )
        assert warnings, "fixture must actually produce a warning"
        rendered = entry + render_warnings_section(warnings)
        assert ("dolcebot.md", 2) in flagged_by_routing(rendered)

    def test_clean_section_flags_nothing(self):
        clean = PROPOSAL.split("## Routing Warnings")[0] + "## Routing Warnings\n\n(none)\n"
        assert flagged_by_routing(clean) == set()

    def test_missing_section_flags_nothing(self):
        assert flagged_by_routing(PROPOSAL.split("## Routing Warnings")[0]) == set()

    def test_stops_at_the_next_section(self):
        extended = PROPOSAL + "\n## Action Items\n\n- `dolcebot.md` #1 (not a warning)\n"
        assert flagged_by_routing(extended) == {("dolcebot.md", 2)}

    def test_every_member_of_a_batch_cluster_is_flagged(self):
        """A cluster warning names N items on one line. Reading only the first
        left the duplicates unflagged and auto-applying, and held the one item
        that was *not* a duplicate — exactly backwards."""
        section = (
            "## Routing Warnings\n\n"
            "- `dolcebot.md` #3, #9, #21 are near-duplicates of each other "
            "(up to 61% word overlap; first is \"VM logs\") — merge into one entry.\n"
        )
        assert flagged_by_routing(section) == {
            ("dolcebot.md", 3), ("dolcebot.md", 9), ("dolcebot.md", 21)}

    def test_a_number_past_the_leading_run_is_not_an_item_label(self):
        """Only the run of `#N` immediately after the filename is read. A `#12`
        in an entry title — or in the corpus line a near-duplicate warning
        quotes — is prose, and flagging item 12 off it would hold an unrelated
        item on evidence about another one."""
        section = (
            "## Routing Warnings\n\n"
            "- `dolcebot.md` #4 (rename step #12 of the runbook): near-duplicate "
            "of an existing line in `python.md:7` — \"see #99 for the rationale\".\n"
        )
        assert flagged_by_routing(section) == {("dolcebot.md", 4)}

    def test_parses_a_cluster_the_renderer_actually_writes(self):
        """The same end-to-end coupling as above, for the cluster shape."""
        rule = ("Worktrees for a project live under the shared worktrees "
                "directory, never scattered inside project folders.")
        restated = ("Never scatter worktrees inside project directories; each "
                    "worktree lives under the shared worktrees directory.")
        entry = "# Proposal\n\n## Updates for `dolcebot.md`\n\n" + "".join(
            f"### {n}. Entry {n}\n**Section:** DolceEngine\n**Change:** add\n> {t}\n\n"
            for n, t in ((1, rule), (2, restated))
        )
        warnings = validate_proposal(entry, {"dolcebot.md": "## DolceEngine\n"})
        assert any("near-duplicates of each other" in w for w in warnings)
        rendered = entry + render_warnings_section(warnings)
        assert flagged_by_routing(rendered) == {
            ("dolcebot.md", 1), ("dolcebot.md", 2)}


class TestRoutingSectionPresence:
    def test_present_when_the_gate_ran(self):
        assert has_routing_section(PROPOSAL)

    def test_present_when_the_gate_ran_and_found_nothing(self):
        clean = PROPOSAL.split("## Routing Warnings")[0] + "## Routing Warnings\n\n(none)\n"
        assert has_routing_section(clean)

    def test_absent_when_it_did_not(self):
        assert not has_routing_section(PROPOSAL.split("## Routing Warnings")[0])


# --- parsing and bookkeeping -------------------------------------------------


class TestClassification:
    def test_every_item_lands_in_exactly_one_bucket(self):
        triage = classify(PROPOSAL)
        labels = [i.label for i in triage.auto + triage.review + triage.dropped]
        assert len(labels) == len(set(labels)) == 6
        assert triage.total == 6

    def test_the_taxonomy_pair_survives_the_hop(self):
        triage = classify(PROPOSAL)
        by_label = {i.label: i for i in triage.review}
        assert by_label["`python.md` #1"].pair == "EMPIRICAL/FACT"
        assert by_label["`python.md` #3"].pair == "DECLARATION/RULE"

    def test_source_citation_is_captured(self):
        triage = classify(PROPOSAL)
        by_label = {i.label: i for i in triage.review}
        assert by_label["`technical-pref.md` #1"].source == "2026-08-05.md:41"

    def test_the_non_additive_item_is_refused_by_the_floor(self):
        triage = classify(PROPOSAL)
        by_label = {i.label: i for i in triage.review}
        assert "not-additive" in by_label["`python.md` #2"].reasons

    def test_processed_items_are_not_re_triaged(self):
        processed = PROPOSAL + """
## Processed

### 9. Something already decided
**Section:** Python Tooling
**Change:** add
> decided.
"""
        assert classify(processed).total == 6

    def test_empty_proposal_is_empty_triage(self):
        assert classify("").total == 0

    def test_conflict_resolutions_are_counted_never_auto(self):
        with_conflicts = PROPOSAL + """
## Conflict Resolutions

### 1. python.md says two things about uv
> Keep the newer line.
"""
        triage = classify(with_conflicts)
        assert triage.conflict_resolutions == 1
        assert triage.total == 6


class TestSafeTarget:
    def test_plain_memory_filename_is_safe(self):
        assert is_safe_target("python.md")

    def test_traversal_is_not(self):
        assert not is_safe_target("../../CLAUDE.md")

    def test_absolute_path_is_not(self):
        assert not is_safe_target("/etc/passwd.md")

    def test_subdirectory_is_not(self):
        assert not is_safe_target("memory/python.md")

    def test_non_markdown_is_not(self):
        assert not is_safe_target("python.txt")


class TestAutoSlice:
    def _applied(self):
        triage = classify(PROPOSAL)
        verdicts = {
            (i.target, i.number): _verdict(i)
            for i in triage.review if i.target in ("python.md", "dolcebot.md")
        }
        return apply_verdicts(triage, verdicts)

    def test_contains_only_the_applied_items(self):
        result = self._applied()
        items = result.auto_by_file()["python.md"]
        rendered = auto_slice(items)
        assert "uv workspace resolution" in rendered
        # #2 is an `update` (floor: not-additive) and #3 is a RULE.
        assert "pandoc" not in rendered
        assert "diary" not in rendered

    def test_preserves_section_change_and_provenance(self):
        rendered = auto_slice(self._applied().auto_by_file()["dolcebot.md"])
        assert "**Section:** Perfi" in rendered
        assert "**Change:** add" in rendered
        assert "**Provenance:** DECLARATION/FACT" in rendered
        assert "> Two unpushed commits as of 2026-08-06." in rendered

    def test_empty_items_render_nothing(self):
        assert auto_slice([]) == ""


# --- 13. the receipt ---------------------------------------------------------


def _rejected(n: int) -> Triage:
    items = [
        _item(number=i, title=f"Dupe {i}", text=f"redundant line {i}",
              reasons=("redundant",), judge_reason="already stated")
        for i in range(1, n + 1)
    ]
    return Triage(auto=(), review=(), conflict_resolutions=0, dropped=tuple(items),
                  judged=True)


class TestReceipt:
    def _applied_receipt(self):
        triage = classify(PROPOSAL)
        verdicts = {(i.target, i.number): _verdict(i) for i in triage.review}
        folded = apply_verdicts(triage, verdicts)
        return folded, render_receipt(
            folded, proposal_name="p.md", applied=folded.auto_by_file(),
            failed={}, generated="2026-08-06 10:00 UTC", mode="triage",
            rejections_log="/tmp/rejections.jsonl",
        )

    def test_it_has_both_sections(self):
        _, receipt = self._applied_receipt()
        assert "## Applied" in receipt
        assert "## Rejected" in receipt

    def test_it_names_every_applied_item_with_its_text_and_source(self):
        folded, receipt = self._applied_receipt()
        assert folded.auto, "fixture should apply something"
        for item in folded.auto:
            assert item.title in receipt
            assert item.text.splitlines()[0] in receipt
            assert item.source in receipt

    def test_it_states_the_mode_and_the_counts(self):
        folded, receipt = self._applied_receipt()
        assert "**Mode:** `triage`" in receipt
        assert f"**Left for review:** {len(folded.review)} item(s)" in receipt
        assert "Kept a conservative default" in receipt

    def test_it_says_plainly_that_no_human_read_them(self):
        _, receipt = self._applied_receipt()
        assert "without a human reading it" in receipt
        assert "revert" in receipt

    def test_it_points_at_the_rejection_log(self):
        _, receipt = self._applied_receipt()
        assert "/tmp/rejections.jsonl" in receipt

    def test_records_failures_as_still_pending(self):
        triage = classify(PROPOSAL)
        receipt = render_receipt(
            triage, proposal_name="p.md", applied={},
            failed={"python.md": "applier returned no safe content"},
            generated="2026-08-06 10:00 UTC",
        )
        assert "Failed to apply" in receipt
        assert "applier returned no safe content" in receipt

    def test_lists_what_was_left_for_the_human(self):
        triage = classify(PROPOSAL)
        receipt = render_receipt(
            triage, proposal_name="p.md", applied={}, failed={},
            generated="2026-08-06 10:00 UTC",
        )
        assert "Left for you" in receipt
        assert REASON_LABELS["kind-rule"] in receipt

    def test_ten_rejections_are_shown_in_full(self):
        triage = _rejected(10)
        receipt = render_receipt(
            triage, proposal_name="p.md", applied={}, failed={},
            generated="2026-08-06 10:00 UTC",
        )
        for i in range(1, 11):
            assert f"redundant line {i}" in receipt

    def test_forty_rejections_are_grouped(self):
        # A 200-line rejection list recreates exactly the review fatigue this
        # programme exists to remove.
        triage = _rejected(40)
        receipt = render_receipt(
            triage, proposal_name="p.md", applied={}, failed={},
            generated="2026-08-06 10:00 UTC",
            rejections_log="/tmp/rejections.jsonl",
        )
        assert "redundant line 1\n" not in receipt
        assert f"**{REASON_LABELS['redundant']}** (40)" in receipt
        assert "/tmp/rejections.jsonl" in receipt

    def test_the_threshold_is_where_it_says_it_is(self):
        assert REJECTION_DETAIL_LIMIT == 25
        assert "redundant line 1" in render_receipt(
            _rejected(REJECTION_DETAIL_LIMIT), proposal_name="p.md", applied={},
            failed={}, generated="g")
        assert "redundant line 1\n" not in render_receipt(
            _rejected(REJECTION_DETAIL_LIMIT + 1), proposal_name="p.md",
            applied={}, failed={}, generated="g")


class TestSummary:
    def test_it_names_the_mode_and_all_three_buckets(self):
        triage = classify(PROPOSAL)
        item = triage.review[0]
        folded = apply_verdicts(triage, {
            (item.target, item.number): _verdict(item, verdict="drop", reason="noise"),
        })
        out = render_summary(folded, applied_count=0, receipt_path="r.md", mode="auto")
        assert "mode: auto" in out
        assert "DROPPED (1)" in out
        assert f"NEEDS YOU ({len(folded.review)})" in out
        assert "kept a conservative default" in out


class TestPolicyConstants:
    def test_add_is_the_only_additive_verb(self):
        assert ADDITIVE_CHANGES == {"add"}

    def test_the_deleted_gates_stay_deleted(self):
        """`normative-language` was 90 of 120 review items on the measured
        proposal and `not-recall-file` another 17. Both are semantic questions a
        pattern cannot answer; re-adding either undoes the phase."""
        import lib.dream_triage as triage_lib

        for gone in ("RECALL_FILES", "_NORMATIVE_RE", "_LOW_CONFIDENCE_RE",
                     "_RULE_PROPOSAL_RE"):
            assert not hasattr(triage_lib, gone), gone
        for gone in ("normative-language", "not-recall-file", "rule-proposal",
                     "low-confidence"):
            assert gone not in REASON_LABELS, gone

    def test_every_reason_a_classification_can_emit_has_a_label(self):
        triage = classify(PROPOSAL)
        for item in triage.review:
            for reason in item.reasons:
                assert reason in REASON_LABELS, reason


# ---------------------------------------------------------------------------
# Provenance/kind disagreement detail (#203)
# ---------------------------------------------------------------------------
#
# Resolution is strictly one-way (the more cautious half always wins), so every
# disagreement can only move an item toward manual review. The aggregate count
# alone therefore cannot separate label noise from genuine caution: on
# processed-learnings-2026-08-12, 79 of 201 items (39%) had a contested label
# and 64 ended `kind: RULE` (which never auto-applies), and the logs could not
# say whether those two facts were related.

class _Item:
    def __init__(self, provenance="", kind=""):
        self.provenance = provenance
        self.kind = kind


class _Verdict:
    def __init__(self, provenance="", kind=""):
        self.provenance = provenance
        self.kind = kind


def test_detail_records_both_pairs_and_the_resolution():
    from lib.dream_triage import reconciliation_detail

    d = reconciliation_detail(
        _Item(provenance="EMPIRICAL", kind="FACT"),
        _Verdict(provenance="INFERENCE", kind="RULE"),
    )
    assert d["extractor_pair"] == "EMPIRICAL/FACT"
    assert d["judge_pair"] == "INFERENCE/RULE"
    assert d["disagreed"] is True
    assert d["provenance_disagreed"] is True
    assert d["kind_disagreed"] is True


def test_detail_names_which_side_won_each_half():
    """The number that decides rubric-vs-labelling: RULE because both passes
    said so, or RULE because one said FACT and lost."""
    from lib.dream_triage import reconciled_pair, reconciliation_detail

    item, verdict = _Item("EMPIRICAL", "FACT"), _Verdict("INFERENCE", "RULE")
    provenance, kind, _ = reconciled_pair(item, verdict)
    d = reconciliation_detail(item, verdict)

    # The detail must agree with the decision it describes — it re-derives,
    # it does not re-decide.
    assert d["resolved_pair"] == f"{provenance}/{kind}"
    assert d["provenance_won"] in ("extractor", "judge")
    assert d["kind_won"] in ("extractor", "judge")


def test_agreement_is_labelled_agreed_not_won():
    from lib.dream_triage import reconciliation_detail

    d = reconciliation_detail(_Item("EMPIRICAL", "RULE"), _Verdict("EMPIRICAL", "RULE"))
    assert d["disagreed"] is False
    assert d["provenance_won"] == "agreed"
    assert d["kind_won"] == "agreed"


def test_a_missing_judge_verdict_is_not_a_disagreement():
    from lib.dream_triage import reconciliation_detail

    d = reconciliation_detail(_Item("EMPIRICAL", "FACT"), None)
    assert d["disagreed"] is False
    assert d["judge_pair"] == "-/-"
    assert d["kind_won"] == "only-one-label"


def test_only_one_half_contested_is_reported_as_such():
    from lib.dream_triage import reconciliation_detail

    d = reconciliation_detail(_Item("EMPIRICAL", "RULE"), _Verdict("INFERENCE", "RULE"))
    assert d["provenance_disagreed"] is True
    assert d["kind_disagreed"] is False
    assert d["kind_won"] == "agreed"
