"""Tests for memory_lint.

The expired check is deterministic and the tests for it are boring. The
unmarked check is a heuristic, and its tests are the interesting half: the
first draft fired 195 times on the real memory tree and was wrong essentially
every time. Every line in `TestRealCorpusFalsePositives` is copied from that
run. They are the specification — a heuristic lint is defined by what it stays
quiet about, not by what it catches.
"""

from datetime import date
from pathlib import Path

import pytest

from scripts.lib.memory_lint import (AS_OF_RE, Finding, _parse_stamp, classify,
                                     lint_dir, lint_text, main, summarize)

TODAY = date(2026, 7, 26)


def findings_for(line: str, today: date = TODAY) -> list[Finding]:
    return lint_text(line + "\n", Path("mem.md"), today)


def kinds(line: str, today: date = TODAY) -> list[str]:
    return [f.kind for f in findings_for(line, today)]


class TestExpiry:
    def test_passed_review_date_is_expired(self):
        line = "- Opus is the top tier (as of 2026-01, review by 2026-03)"
        assert kinds(line) == ["expired"]

    def test_future_review_date_is_clean(self):
        line = "- Opus is the top tier (as of 2026-07, review by 2026-12)"
        assert kinds(line) == []

    def test_a_recent_as_of_without_review_by_is_clean(self):
        """The author dated the fact but claimed no expiry. While the stamp is
        recent that's a complete annotation, not a half-finished one."""
        line = "- Angela works at DRBU (as of 2026-07)"
        assert kinds(line) == []

    def test_an_ancient_as_of_with_no_review_by_is_reported_as_undated(self):
        """`(as of 2019-01)` with no review date was permanently clean —
        nothing could ever expire it — while the premise of the whole linter is
        that facts rot on their own schedule."""
        assert kinds("- Angela works at DRBU (as of 2019-01)") == ["undated"]

    def test_undated_is_its_own_kind_not_expired(self):
        """Nothing has passed, so calling it expired would misdescribe it and
        blur a missed review date with a missing annotation."""
        [f] = lint_text("- x (as of 2019-01)\n", Path("a.md"), TODAY)
        assert f.kind == "undated"
        assert "no 'review by'" in f.detail

    def test_the_undated_threshold_is_a_year(self):
        # 2025-07 resolves to 2025-07-31 (end of month), 360 days before TODAY.
        assert kinds("- x (as of 2025-07)") == []
        # 2025-06 resolves to 2025-06-30, 391 days before TODAY.
        assert kinds("- x (as of 2025-06)") == ["undated"]
        assert kinds("- x (as of 2024-01)") == ["undated"]

    def test_an_ancient_as_of_WITH_a_future_review_by_stays_clean(self):
        """An old stamp is fine when the author said when to re-check it."""
        assert kinds("- x (as of 2019-01, review by 2027-01)") == []

    def test_expired_only_does_not_report_undated(self):
        """`--expired-only` means "something has passed". Nothing has."""
        findings = lint_text("- x (as of 2019-01)\n", Path("a.md"), TODAY)
        assert [f for f in findings if f.kind == "expired"] == []

    def test_day_precision_is_accepted(self):
        assert kinds("x (as of 2026-01-05, review by 2026-03-04)") == ["expired"]
        assert kinds("x (as of 2026-01-05, review by 2026-12-31)") == []

    @pytest.mark.parametrize("stamp,expected", [
        ("2026-03", date(2026, 3, 31)),
        ("2026-12", date(2026, 12, 31)),
        ("2026-02", date(2026, 2, 28)),
        ("2024-02", date(2024, 2, 29)),
        ("2026-03-04", date(2026, 3, 4)),
    ])
    def test_month_stamps_mean_end_of_month(self, stamp, expected):
        """`review by 2026-10` is not overdue on 2026-10-01.

        Treating a month as its 1st would make every month-granular annotation
        fire up to 31 days early — steady early noise, and a lint that is
        wrong on purpose gets switched off.
        """
        assert _parse_stamp(stamp) == expected

    def test_review_due_this_month_is_not_yet_expired(self):
        line = "- x (as of 2026-01, review by 2026-07)"
        assert kinds(line, today=date(2026, 7, 1)) == []
        assert kinds(line, today=date(2026, 8, 1)) == ["expired"]

    def test_case_insensitive_annotation(self):
        assert kinds("- x (As Of 2026-01, Review By 2026-03)") == ["expired"]


class TestUnmarkedRequiresBothHalves:
    """A volatile fact claims something about *now* AND names a changeable value."""

    def test_currency_plus_version_fires(self):
        line = "Claude 4.6 models are the current generation, replacing 4.5."
        assert kinds(line) == ["unmarked"]
        assert classify(line)[0][0] == "version"

    def test_version_alone_is_silent(self):
        """A permanent technical fact that happens to name a version."""
        assert kinds("Swift 6.3 rejects covariant `Self` in stored properties.") == []

    def test_currency_alone_is_silent(self):
        assert kinds("Currently we prefer small, reviewable commits.") == []

    def test_currency_plus_price_fires(self):
        assert kinds("Current rate is $400/hr for advisory work.") == ["unmarked"]

    def test_employer_fires_on_its_own_phrase(self):
        assert kinds("- Works at DRBU at the City of Ten Thousand Buddhas") == ["unmarked"]


class TestDeadlinesAreTheException:
    def test_dated_deadline_fires_without_currency_language(self):
        """A deadline is volatile by definition and carries its own date."""
        assert kinds("- Filing deadline 2026-09-30 for the Italian return") == ["unmarked"]

    def test_iso_and_prose_dates_both_match(self):
        assert kinds("- Renewal on Sep 30, 2026") == ["unmarked"]

    def test_deadline_word_without_a_date_is_silent(self):
        """"Don't automate end-to-end on day one" names no deadline."""
        assert kinds("Progressive trust: don't automate end-to-end on day one.") == []


class TestHistoricalFactsAreLeftAlone:
    def test_explicitly_historical_line_is_silent(self):
        """Copied verbatim from life.md — the line disclaims its own currency,
        and the lint was firing on the word "current" inside that disclaimer."""
        line = ("**US cost base (historical, no longer current):** ~$4,500 USD/month "
                "expenses in Ukiah.")
        assert kinds(line) == []

    @pytest.mark.parametrize("marker", [
        "formerly", "previously", "deprecated", "superseded", "obsolete"])
    def test_past_tense_markers_suppress(self, marker):
        assert kinds(f"The {marker} current default was v1.2 at $50/mo") == []


class TestRealCorpusFalsePositives:
    """Every line here fired in the first run against the live memory tree.

    195 findings, essentially all wrong. The fix was making detection a
    conjunction instead of a disjunction; these pin that it stays one.
    """

    @pytest.mark.parametrize("line", [
        "**Coding agent inflection point:** November 2025 (Claude Opus 4.5 + GPT 5.1)",
        "- **Swift 6.3 rejects covariant `Self` in stored-property initializers.**",
        "**Retrieval scaling thresholds:** Grep-over-files wins at <200 files.",
        "**Example:** Knowledge graph triple extraction from 280 transcripts",
        "**Sonnet vs Haiku for routing:** For short-output tasks (1-22 tokens)",
        "**Split memory files by retrieval domain, not topic affinity:**",
        "**Pre-check for existing entries:** Batch-apply workflows should pre-check.",
        "OAuth tokens expire hourly (not for long-lived connections).",
        "- **Royal Roads MGM** — deadline passed, no longer actionable.",
        "- **Xcode 26.3:** Ships native Claude Agent SDK support.",
    ])
    def test_stays_quiet(self, line):
        assert findings_for(line) == [], f"false positive on: {line}"


class TestStructureIsNotFacts:
    def test_headings_are_skipped(self):
        assert kinds("## Current tooling") == []

    def test_fenced_code_is_skipped(self):
        """Code blocks are full of versions and amounts that are examples."""
        text = "```\nCURRENT_VERSION = 4.5  # the current model\n```\n"
        assert lint_text(text, Path("m.md"), TODAY) == []

    def test_last_updated_stamp_is_skipped(self):
        assert kinds("**Last Updated:** 2026-07-26") == []

    def test_fence_reopens_correctly(self):
        text = ("```\nv1.2 is current\n```\n"
                "Claude 4.6 is the current generation.\n")
        assert [f.lineno for f in lint_text(text, Path("m.md"), TODAY)] == [4]


class TestDirectoryAndOutput:
    def test_claude_md_index_is_skipped(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("Claude 4.6 is the current model.\n")
        assert lint_dir(tmp_path, TODAY) == []

    def test_findings_are_sorted_by_file(self, tmp_path):
        (tmp_path / "b.md").write_text("x (as of 2026-01, review by 2026-02)\n")
        (tmp_path / "a.md").write_text("y (as of 2026-01, review by 2026-02)\n")
        names = [f.path.name for f in lint_dir(tmp_path, TODAY)]
        assert names == ["a.md", "b.md"]

    def test_summary_groups_by_kind(self, tmp_path):
        (tmp_path / "a.md").write_text(
            "x (as of 2026-01, review by 2026-02)\n"
            "Claude 4.6 is the current generation.\n")
        out = summarize(lint_dir(tmp_path, TODAY), root=tmp_path)
        assert "## Expired (1)" in out
        assert "## Missing validity annotation (1)" in out
        assert "1 expired, 0 undated, 1 unmarked" in out

    def test_clean_tree_says_clean(self, tmp_path):
        (tmp_path / "a.md").write_text("Nothing volatile here.\n")
        assert summarize(lint_dir(tmp_path, TODAY)) == "memory_lint: clean"

    def test_long_lines_are_truncated_in_output(self, tmp_path):
        (tmp_path / "a.md").write_text(
            "x " * 200 + "(as of 2026-01, review by 2026-02)\n")
        rendered = lint_dir(tmp_path, TODAY)[0].render()
        assert "..." in rendered and len(rendered.splitlines()[1]) <= 104


class TestCLI:
    def test_exit_1_on_findings(self, tmp_path, capsys):
        (tmp_path / "a.md").write_text("x (as of 2026-01, review by 2026-02)\n")
        assert main([str(tmp_path), "--today", "2026-07-26"]) == 1
        assert "expired" in capsys.readouterr().out

    def test_exit_0_on_clean(self, tmp_path, capsys):
        (tmp_path / "a.md").write_text("Nothing here.\n")
        assert main([str(tmp_path), "--today", "2026-07-26"]) == 0

    def test_expired_only_suppresses_unmarked(self, tmp_path, capsys):
        (tmp_path / "a.md").write_text("Claude 4.6 is the current generation.\n")
        assert main([str(tmp_path), "--today", "2026-07-26", "--expired-only"]) == 0
        assert "clean" in capsys.readouterr().out

    def test_missing_directory_is_an_error_not_a_traceback(self, tmp_path, capsys):
        assert main([str(tmp_path / "nope"), "--today", "2026-07-26"]) == 1
        assert "not a directory" in capsys.readouterr().err


class TestAnnotationRegex:
    def test_matches_both_forms(self):
        assert AS_OF_RE.search("x (as of 2026-07)").group("as_of") == "2026-07"
        m = AS_OF_RE.search("x (as of 2026-07, review by 2026-10)")
        assert m.group("review_by") == "2026-10"

    def test_annotated_line_is_never_checked_for_volatility(self):
        """An annotated fact has already been dated; re-flagging it as
        'unmarked' would make annotating a line strictly worse than not."""
        assert kinds("Claude 4.6 is the current generation (as of 2026-07)") == []
