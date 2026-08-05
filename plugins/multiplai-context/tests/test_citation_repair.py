"""Citation repair: fix the filename, never anything else, never on a guess.

The failure being repaired is narrow and was measured (issue #104): a record
whose session ran past midnight lands in the next day's file, and the model
sometimes cites it under the date in its own `## Session Learnings — <stamp>`
header instead of the file it was rendered from. The line number stays correct,
which is the whole basis for a deterministic fix.

So the tests that matter are not "does it repair the happy case" — they are the
ones pinning what it must NOT do. A wrong repair is worse than a wrong citation,
because a wrong citation looks wrong and a wrong repair looks right.
"""

import pytest

from lib.citation_repair import render_findings, repair_citations
from lib.learnings_ledger import parse_blocks


def block(stamp: str, body: str) -> str:
    return f"## Session Learnings — {stamp}\nSession: s\n{body}\n\n---\n"


# `2026-07-29.md` opens with 3 lines of padding so its second record sits well
# past the end of the short `2026-07-28.md` — the shape that produces the bug.
FILES = {
    "2026-07-28.md": (
        block("2026-07-28T09:00:00+00:00", "- **[trust: verified]** Early lesson.")
    ),
    "2026-07-29.md": (
        "# Learnings\n\n\n"
        + block("2026-07-29T09:00:00+00:00", "- **[trust: verified]** Same-day lesson.")
        + block("2026-07-28T20:54:22+00:00", "- **[trust: verified]** Past-midnight lesson.")
    ),
}


@pytest.fixture
def corpus():
    blocks = [b for name, text in FILES.items() for b in parse_blocks(name, text)]
    return blocks, FILES


@pytest.fixture
def past_midnight_line(corpus):
    """The line of the record that lives in 07-29 but is stamped 07-28."""
    blocks, _ = corpus
    match = [
        b for b in blocks
        if b.file == "2026-07-29.md" and "20:54:22" in b.text
    ]
    assert len(match) == 1, "fixture no longer has exactly one past-midnight record"
    return match[0].start_line


class TestTheMeasuredFailure:
    def test_repairs_a_filename_taken_from_the_block_timestamp(
        self, corpus, past_midnight_line
    ):
        blocks, learnings = corpus
        line = past_midnight_line
        text = f"### 1. Thing\n**Source:** 2026-07-28.md:{line}\n"

        out, findings = repair_citations(text, blocks, learnings)

        assert f"2026-07-29.md:{line}" in out
        assert [(f.cited_file, f.resolved_file) for f in findings] == [
            ("2026-07-28.md", "2026-07-29.md")
        ]

    def test_repairs_the_inline_filtered_out_form_too(self, corpus, past_midnight_line):
        """`(Source: f.md:12)` is the same provenance in a different shape.

        Fixing one form and not the other would leave a proposal citing the same
        record two ways.
        """
        blocks, learnings = corpus
        text = f"- Dropped it — event-only (Source: 2026-07-28.md:{past_midnight_line})"

        out, findings = repair_citations(text, blocks, learnings)

        assert f"(Source: 2026-07-29.md:{past_midnight_line})" in out
        assert findings[0].repaired

    def test_a_line_range_moves_as_one(self, corpus, past_midnight_line):
        blocks, learnings = corpus
        lo = past_midnight_line
        text = f"**Source:** 2026-07-28.md:{lo}-{lo + 1}\n"

        out, _ = repair_citations(text, blocks, learnings)

        assert f"**Source:** 2026-07-29.md:{lo}-{lo + 1}" in out


class TestWhatItMustNotDo:
    def test_a_citation_that_resolves_is_left_alone(self, corpus):
        """In-range citations are not findings, so a clean run says nothing."""
        blocks, learnings = corpus
        text = "**Source:** 2026-07-28.md:1\n"

        out, findings = repair_citations(text, blocks, learnings)

        assert out == text
        assert findings == []

    def test_an_unresolvable_citation_is_reported_not_invented(self, corpus):
        """Out of range, and no record anywhere carries that date."""
        blocks, learnings = corpus
        text = "**Source:** 2020-01-01.md:9999\n"

        out, findings = repair_citations(text, blocks, learnings)

        assert out == text, "must never rewrite what it cannot prove"
        assert len(findings) == 1
        assert not findings[0].repaired
        assert "2020-01-01" in findings[0].reason

    def test_an_ambiguous_match_is_refused(self, corpus, past_midnight_line):
        """Two files carrying the same date at the same line: refuse, don't pick.

        Guessing here would be a coin flip that reads as a verified correction.
        """
        stamp = "2026-07-28T20:54:22+00:00"
        padding = "# Learnings\n\n\n" + block(
            "2026-07-29T09:00:00+00:00", "- **[trust: verified]** Filler."
        )
        files = {
            "2026-07-29.md": padding + block(stamp, "- **[trust: verified]** One."),
            "2026-07-30.md": padding + block(stamp, "- **[trust: verified]** Two."),
        }
        blocks = [b for name, text in files.items() for b in parse_blocks(name, text)]
        text = f"**Source:** 2026-07-28.md:{past_midnight_line}\n"

        out, findings = repair_citations(text, blocks, files)

        assert out == text
        assert not findings[0].repaired
        assert "ambiguous" in findings[0].reason

    def test_only_the_filename_changes(self, corpus, past_midnight_line):
        """Everything around a citation is a human's proposal text — do not touch it."""
        blocks, learnings = corpus
        line = past_midnight_line
        text = (
            "## Updates for `dev.md`\n\n### 1. A lesson\n"
            "**Change:** add\n> Body with 2026-07-28.md in it, and a :123 too.\n\n"
            f"**Source:** 2026-07-28.md:{line}\n\n---\n"
        )

        out, _ = repair_citations(text, blocks, learnings)

        assert "> Body with 2026-07-28.md in it, and a :123 too." in out
        assert out.replace(f"2026-07-29.md:{line}", f"2026-07-28.md:{line}") == text

    def test_a_non_dated_filename_is_never_repaired(self, corpus):
        """Only dated learnings files can be matched against a stamp."""
        blocks, learnings = corpus
        text = "**Source:** notes.md:500\n"

        out, findings = repair_citations(text, blocks, learnings)

        assert out == text
        assert not findings[0].repaired
        assert "not a dated learnings file" in findings[0].reason


class TestAnUnreadableFileIsNotAMissingOne:
    """Issue #113 (1) — the module's worst failure mode, confirmed in review.

    A file that fails `read_text()` used to be simply absent from the dict the
    module is handed, which is indistinguishable from a file that does not
    exist. Every citation to it then looked *provably* broken — the line "does
    not exist" in a file nothing ever opened — and the past-midnight record in
    the neighbouring file supplied a one-candidate match. The result was a
    valid citation rewritten to a wrong one and listed as a verified
    correction.
    """

    def test_a_valid_citation_is_not_rewritten_when_its_file_is_unreadable(
        self, corpus, past_midnight_line
    ):
        """The exact case measured in review, as a test.

        `2026-07-28.md:{line}` is *correct* here — but 07-28 could not be read,
        and 07-29 carries a record stamped 07-28 covering that line. Without
        knowing the file was unreadable the module repairs it, confidently and
        wrongly.
        """
        blocks, learnings = corpus
        line = past_midnight_line
        text = f"**Source:** 2026-07-28.md:{line}\n"

        out, findings = repair_citations(
            text, blocks, learnings, unreadable=["2026-07-28.md"]
        )

        assert out == text, "an unread file cannot prove a citation wrong"
        assert not any(f.repaired for f in findings)

    def test_the_skipped_file_is_reported_once_not_per_citation(self, corpus):
        """Silence would read as 'verified'. Repetition would bury the real ones."""
        blocks, learnings = corpus
        text = (
            "**Source:** 2026-07-28.md:1\n"
            "**Source:** 2026-07-28.md:2\n"
            "(Source: 2026-07-28.md:3)\n"
        )

        _, findings = repair_citations(
            text, blocks, learnings, unreadable=["2026-07-28.md"]
        )

        assert len(findings) == 1
        assert findings[0].cited_file == "2026-07-28.md"
        assert findings[0].line is None
        assert not findings[0].repaired
        assert "could not be read" in findings[0].reason

    def test_an_unreadable_file_nobody_cites_is_not_reported(self, corpus):
        """The reviewer is told what went unchecked, not what went unread."""
        blocks, learnings = corpus
        text = "**Source:** 2026-07-29.md:1\n"

        _, findings = repair_citations(
            text, blocks, learnings, unreadable=["2026-07-28.md"]
        )

        assert findings == []

    def test_other_files_are_still_repaired_normally(
        self, corpus, past_midnight_line
    ):
        """One unreadable file must not disarm the whole pass."""
        blocks, learnings = corpus
        line = past_midnight_line
        text = f"**Source:** 2026-07-28.md:{line}\n"

        out, findings = repair_citations(
            text, blocks, learnings, unreadable=["2026-07-30.md"]
        )

        assert f"**Source:** 2026-07-29.md:{line}" in out
        assert [f.repaired for f in findings] == [True]

    def test_the_report_names_the_file_without_a_line_number(self, corpus):
        blocks, learnings = corpus
        text = "**Source:** 2026-07-28.md:1\n"

        _, findings = repair_citations(
            text, blocks, learnings, unreadable=["2026-07-28.md"]
        )
        section = render_findings(findings)

        assert "- `2026-07-28.md` —" in section
        assert "2026-07-28.md:None" not in section


class TestBothEndsOfARangeAreChecked:
    """Issue #113 (2) — a range was only ever validated at its low end.

    `2026-07-28.md:1-9999` passed silently: `lo` was in range, the function
    returned early, and `hi` was never looked at. The proposal then reported
    every citation as verified while a reviewer following that range ran off
    the end of the file.
    """

    def test_a_range_whose_tail_does_not_exist_is_reported(self, corpus):
        blocks, learnings = corpus
        text = "**Source:** 2026-07-28.md:1-9999\n"

        out, findings = repair_citations(text, blocks, learnings)

        assert out == text, "half a verified range is not grounds to move it"
        assert len(findings) == 1
        assert not findings[0].repaired
        assert "9999" in findings[0].reason

    def test_a_range_wholly_in_range_is_still_silent(self, corpus):
        blocks, learnings = corpus
        text = "**Source:** 2026-07-29.md:1-2\n"

        out, findings = repair_citations(text, blocks, learnings)

        assert out == text
        assert findings == []

    def test_a_tail_that_resolves_elsewhere_is_reported_not_repaired(
        self, corpus, past_midnight_line
    ):
        """`lo` verifies in the cited file, `hi` points into another one.

        Two files cannot both be right, and which end is the mistake is not
        knowable from here — so it is a finding, never a rewrite.
        """
        blocks, learnings = corpus
        text = f"**Source:** 2026-07-28.md:1-{past_midnight_line}\n"

        out, findings = repair_citations(text, blocks, learnings)

        assert out == text
        assert len(findings) == 1
        assert not findings[0].repaired


class TestReporting:
    def test_nothing_to_report_renders_nothing(self):
        assert render_findings([]) == ""

    def test_repairs_and_refusals_are_both_listed(self, corpus, past_midnight_line):
        """A reviewer who can't tell them apart has to re-check every citation."""
        blocks, learnings = corpus
        text = (
            f"**Source:** 2026-07-28.md:{past_midnight_line}\n"
            "**Source:** 2020-01-01.md:9999\n"
        )

        _, findings = repair_citations(text, blocks, learnings)
        section = render_findings(findings)

        assert "## Citation Repairs" in section
        assert "→ `2026-07-29.md" in section
        assert "could not be verified" in section
        assert "2020-01-01.md:9999" in section


class TestFailOpen:
    def test_a_broken_corpus_never_loses_the_proposal(self, corpus):
        """The gate around this is fail-open; the module itself must not explode.

        Blocks that parse to nothing (an empty learnings dir, an unreadable file
        skipped upstream) mean every citation is unverifiable — which is a report,
        not a crash.
        """
        text = "**Source:** 2026-07-28.md:5\n"

        out, findings = repair_citations(text, [], {})

        assert out == text
        assert len(findings) == 1 and not findings[0].repaired


class TestAdvisoryVsBrokenFindings:
    """F6 (log-doctor, 2026-08-05): dream logged every unrepaired finding at
    WARNING, including citations naming a file that is not a dated learnings file
    at all. Those are model formatting slips — there is no file the reviewer could
    go and check — and this module's own docstring example (`(Source: f.md:12)`,
    quoted back at it through a learnings entry) produced two of them every run.
    Noise at WARNING is how a reader learns to skip the warnings that matter.
    """

    def test_a_non_dated_filename_is_advisory(self, corpus):
        blocks, learnings = corpus
        _, findings = repair_citations("**Source:** notes.md:500\n", blocks, learnings)
        assert findings[0].advisory

    def test_the_retired_docstring_placeholder_is_advisory(self, corpus):
        blocks, learnings = corpus
        _, findings = repair_citations("(Source: f.md:12)\n", blocks, learnings)
        assert [f.advisory for f in findings] == [True]

    def test_a_genuinely_broken_dated_citation_is_not_advisory(self, corpus):
        """The distinction has to be narrow, or it silences the real finding: a
        dated file whose cited line does not exist IS something to chase."""
        blocks, learnings = corpus
        _, findings = repair_citations(
            "**Source:** 2026-07-28.md:9999\n", blocks, learnings)
        assert findings and not any(f.advisory for f in findings)

    def test_a_repair_is_never_advisory(self, corpus, past_midnight_line):
        blocks, learnings = corpus
        _, findings = repair_citations(
            f"**Source:** 2026-07-28.md:{past_midnight_line}\n", blocks, learnings)
        assert findings[0].repaired and not findings[0].advisory

    def test_an_unreadable_file_finding_is_not_advisory(self, corpus):
        """A skipped check is exactly what the reviewer must be warned about."""
        blocks, learnings = corpus
        _, findings = repair_citations(
            "**Source:** 2026-07-28.md:1\n", blocks, learnings,
            unreadable=["2026-07-28.md"])
        assert findings and not any(f.advisory for f in findings)

    def test_advisory_findings_are_still_reported_to_the_reviewer(self, corpus):
        """Quieter in the log, not hidden from the proposal — the reviewer still
        needs to see that a citation did not verify."""
        blocks, learnings = corpus
        _, findings = repair_citations("(Source: f.md:12)\n", blocks, learnings)
        assert "f.md:12" in render_findings(findings)


class TestTheModuleDoesNotCiteItself:
    def test_no_example_in_the_source_matches_the_citation_pattern(self):
        """The placeholders documenting `_CITATION_RE` used to match it. A
        learnings entry quoting this file then carried a live-looking citation
        into the proposal, and the repairer dutifully flagged it — twice a run.

        Asserted as the property rather than as "the old literal is gone", so any
        newly-written example has to use the unmatchable `<...>` form too.
        """
        from pathlib import Path

        from lib import citation_repair

        source = Path(citation_repair.__file__).read_text(encoding="utf-8")
        found = [m.group(0) for m in citation_repair._CITATION_RE.finditer(source)]
        assert not found, (
            f"citation-shaped example(s) in the source: {found}. Written into a "
            "learnings entry these become findings against a file that does not "
            "exist — use the `<date>.md:<line>` form."
        )
