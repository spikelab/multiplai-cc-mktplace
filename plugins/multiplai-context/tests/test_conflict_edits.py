"""Tests for conflict_edits.

The matcher's job is to put an edit at the top of a proposal saying "this
existing memory line is wrong". That is a claim with teeth: a wrong match asks
the reviewer to supersede a true fact, wrapped in a diff that looks reasonable.
So most of these tests are about what it *declines* to match.
"""

from datetime import date
from pathlib import Path

import pytest

from scripts.lib.conflict_edits import (CONFLICT_PROVENANCES, Learning,
                                        conflict_section_for,
                                        detect_conflicts,
                                        find_contradicted_line, overlap,
                                        parse_learnings, render_section)

TODAY = date(2026, 7, 26)


def learning_line(desc, *, trust="verified", ltype="CORRECTION",
                  target="dev.md", action="update it") -> str:
    return (f"- **[trust: {trust}]** {ltype} {desc} "
            f"→ Target: {target} — {action}")


def taxonomy_line(desc, *, provenance="EMPIRICAL", kind="FACT",
                  target="dev.md", action="update it") -> str:
    """The form ``extraction._format_learning_entry`` writes today."""
    return f"- **[{provenance}/{kind}]** {desc} → Target: {target} — {action}"


class TestParsing:
    def test_parses_a_full_line(self):
        [l] = parse_learnings(learning_line("Opus is the default model"))
        assert l.trust == "verified"
        assert l.type == "CORRECTION"
        assert l.description == "Opus is the default model"
        assert l.target == "dev.md"
        assert l.action == "update it"

    def test_parses_a_line_without_a_target(self):
        [l] = parse_learnings(
            "- **[trust: high]** OBSERVATION Something happened")
        assert l.target == "" and l.description == "Something happened"

    def test_ignores_prose_and_headings(self):
        text = ("## Session Learnings — 2026-07-26\n"
                "Some narrative prose.\n"
                + learning_line("A real one about model defaults") + "\n")
        assert len(parse_learnings(text)) == 1

    def test_hyphenated_type_is_parsed(self):
        [l] = parse_learnings(learning_line("x y z q", ltype="RULE-PROPOSAL"))
        assert l.type == "RULE-PROPOSAL"

    def test_real_corpus_line_parses(self):
        """Verbatim from .multiplai/learnings/ — the format this must track."""
        line = ("- **[trust: high]** RULE-PROPOSAL Apple Team IDs must be redacted "
                "to a placeholder in committed docs. → Target: apple.md — Add rule: "
                "never commit real Apple Team IDs anywhere in the repo.")
        [l] = parse_learnings(line)
        assert l.target == "apple.md" and l.type == "RULE-PROPOSAL"

    def test_parses_the_two_axis_form(self):
        [l] = parse_learnings(taxonomy_line("Opus is the default model"))
        assert l.provenance == "EMPIRICAL" and l.kind == "FACT"
        assert l.description == "Opus is the default model"
        assert l.target == "dev.md" and l.action == "update it"
        assert l.trust == "" and l.type == ""

    @pytest.mark.parametrize(
        "provenance,kind",
        [("RULE-PROPOSAL", "FACT"), ("EMPIRICAL", "RULE-ISH"), ("?", "RULE")],
    )
    def test_a_hyphenated_or_unknown_label_is_parsed_not_dropped(self, provenance, kind):
        """``[A-Z?]+`` excluded ``-``, so the whole line vanished from the scan.

        A detector that silently stops seeing a line is worse than one that
        misjudges it, and ``learnings_ledger`` already accepted hyphens — two
        parsers disagreeing about what a learning *is* is how this goes quiet.
        """
        parsed = parse_learnings(taxonomy_line("x y z q", provenance=provenance, kind=kind))
        assert len(parsed) == 1
        assert parsed[0].description == "x y z q"

    def test_basis_names_the_pair_for_the_reviewer(self):
        [l] = parse_learnings(taxonomy_line("x y z q", provenance="CORRECTION"))
        assert l.basis == "CORRECTION/FACT"


class TestCandidateSelection:
    def test_corrections_always_qualify(self):
        l = Learning("medium", "CORRECTION", "d", "dev.md", "a")
        assert l.is_conflict_candidate

    def test_verified_non_corrections_qualify(self):
        assert Learning("verified", "OBSERVATION", "d", "dev.md", "a").is_conflict_candidate

    @pytest.mark.parametrize("trust", ["high", "medium"])
    def test_unverified_observations_do_not(self, trust):
        """An inference that touches an existing line is not evidence the line
        is wrong. Superseding on it lets a guess overwrite a confirmed fact."""
        assert not Learning(trust, "OBSERVATION", "d", "dev.md", "a").is_conflict_candidate

    # --- the two-axis form: this is the decision, pinned ------------------
    #
    # The narrowing this PR could have shipped silently was "CORRECTION only",
    # which would have taken `## Conflict Resolutions` from firing on most
    # verified learnings to firing on almost none: the extractor returns
    # EMPIRICAL for roughly five records in six. These tests exist so the rule
    # is a decision rather than an accident of which arm the parser took.

    @pytest.mark.parametrize("provenance", sorted(CONFLICT_PROVENANCES))
    def test_observed_provenances_qualify(self, provenance):
        [l] = parse_learnings(taxonomy_line("x y z q", provenance=provenance))
        assert l.is_conflict_candidate, f"{provenance} should be a candidate"

    @pytest.mark.parametrize("provenance", ["RESEARCH", "DECLARATION", "INFERENCE"])
    def test_unobserved_provenances_do_not(self, provenance):
        """Read somewhere, asserted, or reasoned to — none of them observed here."""
        [l] = parse_learnings(taxonomy_line("x y z q", provenance=provenance))
        assert not l.is_conflict_candidate

    def test_the_two_forms_of_one_learning_agree(self):
        """The same knowledge, relabelled, must not change whether it fires.

        This is the equivalence the taxonomy migration is supposed to preserve,
        and the one the review found broken: `trust: verified` OBSERVATION was
        a candidate, and its EMPIRICAL/FACT rendering was not.
        """
        legacy = parse_learnings(
            learning_line("the router caches picks per session", ltype="OBSERVATION")
        )[0]
        new = parse_learnings(
            taxonomy_line("the router caches picks per session", provenance="EMPIRICAL")
        )[0]
        assert legacy.is_conflict_candidate == new.is_conflict_candidate is True

    def test_an_unknown_provenance_does_not_qualify(self):
        """Fail closed: an unreadable label is not evidence of anything."""
        assert not Learning("", "", "d", "dev.md", "a", provenance="?", kind="FACT").is_conflict_candidate


class TestOverlap:
    def test_restatement_scores_high(self):
        a = "git worktree add with a relative path resolves against the repo root"
        b = "`git worktree add <path>` resolves the path relative to the repo dir"
        assert overlap(a, b) >= 0.35

    def test_unrelated_lines_score_low(self):
        a = "Opus is the default model for routing decisions"
        b = "Trenitalia autocomplete needs a partial name then arrow-down"
        assert overlap(a, b) < 0.35

    def test_short_lines_never_match(self):
        """Two three-word lines sharing two words score 0.66 by accident."""
        assert overlap("use uv", "use uv now") == 0.0

    def test_stopwords_do_not_create_matches(self):
        a = "the and or but if then that this is are was were be"
        b = "the and or but if then that this is are was were do"
        assert overlap(a, b) == 0.0

    def test_code_spans_are_stripped(self):
        """Identical boilerplate in code spans shouldn't manufacture overlap."""
        a = "`uv run script.py` handles the alpha subsystem cleanly"
        b = "`uv run script.py` handles the beta pipeline instead"
        assert overlap(a, b) < 0.6


class TestFindingTheContradictedLine:
    MEMORY = (
        "# Dev\n\n"
        "## Models\n\n"
        "- The default model for routing decisions is Sonnet at medium effort.\n"
        "- Trenitalia autocomplete needs a partial name then arrow-down.\n"
    )

    def test_finds_the_matching_line(self):
        l = Learning("verified", "CORRECTION",
                     "The default model for routing decisions is Opus at high effort",
                     "dev.md", "update it")
        found = find_contradicted_line(l, self.MEMORY)
        assert found is not None
        assert found[0] == 5 and "Sonnet" in found[1]

    def test_returns_none_when_nothing_is_close(self):
        l = Learning("verified", "CORRECTION",
                     "Postgres connection pooling should use pgbouncer in transaction mode",
                     "dev.md", "add it")
        assert find_contradicted_line(l, self.MEMORY) is None

    def test_headings_are_never_matched(self):
        l = Learning("verified", "CORRECTION", "Models models models models",
                     "dev.md", "x")
        found = find_contradicted_line(l, self.MEMORY)
        assert found is None or not found[1].lstrip().startswith("#")

    def test_only_the_best_match_is_returned(self):
        """A correction supersedes one fact. Three 'maybe this one' options
        turn a targeted edit back into generic consolidation."""
        memory = ("- The default model for routing is Sonnet at medium effort.\n"
                  "- The default model for routing is Sonnet, medium effort, mostly.\n")
        l = Learning("verified", "CORRECTION",
                     "The default model for routing is Opus at high effort",
                     "dev.md", "x")
        found = find_contradicted_line(l, memory)
        assert found is not None and isinstance(found[0], int)


class TestDetectConflicts:
    MEMORY = {"dev.md": "- The default model for routing decisions is Sonnet at medium effort.\n"}

    def test_end_to_end_detection(self):
        text = learning_line(
            "The default model for routing decisions is Opus at high effort")
        [edit] = detect_conflicts(parse_learnings(text), self.MEMORY)
        assert edit.target == "dev.md" and edit.lineno == 1

    def test_learning_without_a_target_is_skipped(self):
        text = ("- **[trust: verified]** CORRECTION The default model for "
                "routing decisions is Opus at high effort")
        assert detect_conflicts(parse_learnings(text), self.MEMORY) == []

    def test_target_file_that_does_not_exist_is_skipped(self):
        """Routed to a file we don't have — the generic pass handles it, and
        guessing another file is exactly the wrong-match failure mode."""
        text = learning_line(
            "The default model for routing decisions is Opus at high effort",
            target="nonexistent.md")
        assert detect_conflicts(parse_learnings(text), self.MEMORY) == []

    def test_unverified_learning_is_skipped(self):
        text = learning_line(
            "The default model for routing decisions is Opus at high effort",
            trust="medium", ltype="OBSERVATION")
        assert detect_conflicts(parse_learnings(text), self.MEMORY) == []

    def test_results_are_ordered_by_confidence(self):
        memory = {
            "a.md": "- The default model for routing decisions is Sonnet at medium effort.\n",
            "b.md": "- The default model for routing decisions is Sonnet at medium effort exactly.\n",
        }
        text = "\n".join([
            learning_line("The default model for routing decisions is Opus at high effort",
                          target="a.md"),
            learning_line("The default model for routing decisions is Opus at high effort",
                          target="b.md"),
        ])
        edits = detect_conflicts(parse_learnings(text), memory)
        assert [e.overlap for e in edits] == sorted(
            [e.overlap for e in edits], reverse=True)


class TestRendering:
    def test_empty_when_no_conflicts(self):
        """An empty section in every proposal trains the reader to scroll past
        the place conflicts appear."""
        assert render_section([]) == ""

    def test_section_names_both_readings(self):
        memory = {"dev.md": "- The default model for routing decisions is Sonnet at medium effort.\n"}
        text = learning_line(
            "The default model for routing decisions is Opus at high effort")
        out = conflict_section_for(text, memory, TODAY)
        assert "## Conflict Resolutions" in out
        assert "contradicts" in out and "restates" in out

    def test_edit_shows_old_and_new(self):
        memory = {"dev.md": "- The default model for routing decisions is Sonnet at medium effort.\n"}
        text = learning_line(
            "The default model for routing decisions is Opus at high effort")
        out = conflict_section_for(text, memory, TODAY)
        assert "Sonnet" in out and "Opus" in out
        assert "superseded 2026-07-26" in out

    def test_no_conflicts_yields_empty_string_end_to_end(self):
        memory = {"dev.md": "- Something entirely unrelated to any learning here.\n"}
        assert conflict_section_for(learning_line("alpha beta gamma delta epsilon"),
                                    memory, TODAY) == ""


class TestDreamIntegration:
    def test_section_is_prepended_not_appended(self):
        """The model's headings are what a reviewer skims; a conflict block
        below them reads as a footnote."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from dream import _with_conflict_resolutions

        memory = {"dev.md": "- The default model for routing decisions is Sonnet at medium effort.\n"}
        text = learning_line(
            "The default model for routing decisions is Opus at high effort")
        out = _with_conflict_resolutions("## Updates for `dev.md`\n", text, memory)
        assert out.index("Conflict Resolutions") < out.index("Updates for")

    def test_failure_never_loses_the_proposal(self, monkeypatch):
        """Patch the module `dream` actually imports from.

        The first version of this test patched `scripts.lib.conflict_edits`
        while dream imports `lib.conflict_edits` — two module objects, so the
        patch never applied and the test passed on the no-conflicts path
        instead of the failure path it claimed to cover.
        """
        import lib.conflict_edits as conflict_edits
        import dream

        def boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(conflict_edits, "conflict_section_for", boom)

        # Inputs that WOULD produce a section, so the only way to reach the
        # return is through the exception handler.
        memory = {"dev.md": "- The default model for routing decisions is Sonnet at medium effort.\n"}
        text = learning_line(
            "The default model for routing decisions is Opus at high effort")
        assert conflict_section_for(text, memory, TODAY)  # sanity: real path works

        proposal = "## Updates for `dev.md`\n"
        assert dream._with_conflict_resolutions(proposal, text, memory) == proposal
