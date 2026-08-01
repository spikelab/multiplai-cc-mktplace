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
