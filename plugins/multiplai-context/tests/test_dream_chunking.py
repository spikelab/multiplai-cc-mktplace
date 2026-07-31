"""Tests for lib/dream_chunking.py — sizing and chunk planning for dream.

Two properties are asserted hardest because everything else is recoverable and
these are not:

1. **Line numbers survive chunking verbatim.** Every ``N: `` prefix a chunk
   renders must be the line's real 1-indexed position in its source file. Dream's
   ``**Source:** file:line`` provenance is read straight off these; a renumbered
   chunk produces citations that look valid and point at the wrong lines.
2. **Splits fall only between blocks.** A ``## Session Learnings`` record is
   indivisible — half a record is half a lesson and half a line range.
"""

import math

import pytest

from lib.dream_chunking import (
    BUDGET_FRACTION,
    Chunk,
    DEFAULT_THROUGHPUT_BYTES_PER_S,
    MAX_CHUNK_BYTES,
    MAX_ESCALATED_TIMEOUT_S,
    MIN_CHUNK_BYTES,
    chunk_budget_bytes,
    estimate_seconds,
    estimate_wall_clock,
    format_plan_line,
    plan_chunks,
    render_chunk,
    resolve_throughput,
)
from lib.learnings_ledger import Block, parse_blocks


def make_learnings(n_records: int, body_lines: int = 3, marker: str = "x") -> str:
    """A learnings file shaped like what lib/extraction.py appends."""
    out = []
    for i in range(n_records):
        out.append("---")
        out.append(f"## Session Learnings — 2026-07-{i + 1:02d}T00:00:00+00:00")
        out.append(f"Session: 0000000{i}-0000-0000-0000-000000000000")
        for j in range(body_lines):
            out.append(f"- **[trust: high]** {marker} lesson {i}.{j} " + "pad " * 8)
        out.append("")
    out.append("---")
    return "\n".join(out) + "\n"


def numbered_reference(text: str) -> dict[int, str]:
    """What ``dream.py::_read_all_learnings`` would emit for each line."""
    return {i: f"{i}: {line}" for i, line in enumerate(text.splitlines(), start=1)}


class TestResolveThroughput:
    def test_default_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("MULTIPLAI_DREAM_THROUGHPUT", raising=False)
        assert resolve_throughput() == DEFAULT_THROUGHPUT_BYTES_PER_S

    def test_observed_beats_default(self, monkeypatch):
        monkeypatch.delenv("MULTIPLAI_DREAM_THROUGHPUT", raising=False)
        assert resolve_throughput(120.0) == 120.0

    def test_env_beats_observed_and_is_read_at_call_time(self, monkeypatch):
        """The module is already imported when a run sets this — an import-time
        read would silently ignore every override."""
        monkeypatch.delenv("MULTIPLAI_DREAM_THROUGHPUT", raising=False)
        assert resolve_throughput(120.0) == 120.0
        monkeypatch.setenv("MULTIPLAI_DREAM_THROUGHPUT", "200")
        assert resolve_throughput(120.0) == 200.0

    @pytest.mark.parametrize("bad", ["", "fast", "0", "-5"])
    def test_unusable_env_falls_through_instead_of_raising(self, monkeypatch, bad):
        monkeypatch.setenv("MULTIPLAI_DREAM_THROUGHPUT", bad)
        assert resolve_throughput() == DEFAULT_THROUGHPUT_BYTES_PER_S


class TestEstimates:
    def test_seconds_scale_with_bytes(self, monkeypatch):
        monkeypatch.delenv("MULTIPLAI_DREAM_THROUGHPUT", raising=False)
        assert estimate_seconds(8500, 85.0) == pytest.approx(100.0)

    def test_the_plan_arithmetic_holds(self, monkeypatch):
        """254 KB at the measured default is the ~50 min the log table showed."""
        monkeypatch.delenv("MULTIPLAI_DREAM_THROUGHPUT", raising=False)
        assert 2500 < estimate_seconds(254_000) < 3200

    def test_budget_is_the_documented_fraction_of_the_timeout(self, monkeypatch):
        monkeypatch.delenv("MULTIPLAI_DREAM_THROUGHPUT", raising=False)
        assert chunk_budget_bytes(900, 85.0) == int(85.0 * 900 * BUDGET_FRACTION)

    def test_budget_clamps_low(self, monkeypatch):
        monkeypatch.delenv("MULTIPLAI_DREAM_THROUGHPUT", raising=False)
        assert chunk_budget_bytes(1, 85.0) == MIN_CHUNK_BYTES

    def test_budget_clamps_high(self, monkeypatch):
        monkeypatch.delenv("MULTIPLAI_DREAM_THROUGHPUT", raising=False)
        assert chunk_budget_bytes(100_000, 85.0) == MAX_CHUNK_BYTES

    def test_budget_of_a_zero_timeout_is_the_floor_not_a_crash(self, monkeypatch):
        monkeypatch.delenv("MULTIPLAI_DREAM_THROUGHPUT", raising=False)
        assert chunk_budget_bytes(0) == MIN_CHUNK_BYTES


@pytest.fixture(autouse=True)
def _no_throughput_env(monkeypatch):
    monkeypatch.delenv("MULTIPLAI_DREAM_THROUGHPUT", raising=False)


class TestPlanChunks:
    def test_count_is_derived_from_the_timeout(self):
        """The whole point: a shorter timeout means more, smaller chunks — not a
        gamble on whether one big call fits."""
        blocks = parse_blocks("f.md", make_learnings(40))
        small = plan_chunks(blocks, 300, throughput=85.0)
        large = plan_chunks(blocks, 900, throughput=85.0)
        assert len(small) > len(large) >= 1

    def test_every_block_appears_exactly_once(self):
        blocks = parse_blocks("f.md", make_learnings(30))
        packed = [b for c in plan_chunks(blocks, 300, throughput=85.0) for b in c.blocks]
        assert [b.key for b in packed] == [b.key for b in blocks]

    def test_indices_are_one_based_and_contiguous(self):
        blocks = parse_blocks("f.md", make_learnings(30))
        chunks = plan_chunks(blocks, 300, throughput=85.0)
        assert [c.index for c in chunks] == list(range(1, len(chunks) + 1))

    def test_chunks_stay_within_budget(self):
        blocks = parse_blocks("f.md", make_learnings(30))
        budget = chunk_budget_bytes(300, 85.0)
        for c in plan_chunks(blocks, 300, throughput=85.0):
            assert c.oversized or c.n_bytes <= budget

    def test_n_bytes_matches_the_rendered_payload(self):
        """The packer's running total and the renderer must not drift — a chunk
        that reports 20 KB and sends 40 KB is a timeout waiting to happen."""
        blocks = parse_blocks("f.md", make_learnings(30))
        for c in plan_chunks(blocks, 300, throughput=85.0):
            assert c.n_bytes == len(render_chunk(c).encode("utf-8"))

    def test_never_mixes_source_files_in_one_chunk(self):
        """Same-day learnings stay together so one call can dedup its repeats."""
        blocks = parse_blocks("a.md", make_learnings(4)) + parse_blocks(
            "b.md", make_learnings(4, marker="y")
        )
        for c in plan_chunks(blocks, 900, throughput=85.0):
            assert len({b.file for b in c.blocks}) == 1

    def test_a_file_change_starts_a_new_chunk_even_when_the_budget_allows_more(self):
        blocks = parse_blocks("a.md", make_learnings(1)) + parse_blocks(
            "b.md", make_learnings(1, marker="y")
        )
        assert len(plan_chunks(blocks, 900, throughput=85.0)) == 2

    def test_normal_chunks_carry_the_caller_timeout(self):
        blocks = parse_blocks("f.md", make_learnings(10))
        assert all(c.timeout_s == 900.0 for c in plan_chunks(blocks, 900, throughput=85.0))
        assert not any(c.oversized for c in plan_chunks(blocks, 900, throughput=85.0))

    def test_empty_input_plans_nothing(self):
        assert plan_chunks([], 900, throughput=85.0) == []


class TestOversizedBlock:
    def _oversized(self) -> list[Block]:
        huge = "## Session Learnings — big\n" + "\n".join(
            f"- **[trust: high]** {'z' * 200}" for _ in range(300)
        )
        return parse_blocks("big.md", huge)

    def test_gets_a_chunk_of_its_own(self):
        blocks = self._oversized() + parse_blocks("big.md", make_learnings(2))
        chunks = plan_chunks(blocks, 900, throughput=85.0)
        oversized = [c for c in chunks if c.oversized]
        assert len(oversized) == 1
        assert len(oversized[0].blocks) == 1
        assert oversized[0].n_bytes > chunk_budget_bytes(900, 85.0)

    def test_timeout_is_doubled_for_that_chunk_only(self):
        chunks = plan_chunks(self._oversized(), 300, throughput=85.0)
        assert chunks[0].timeout_s == 600.0

    def test_escalation_is_capped(self):
        chunks = plan_chunks(self._oversized(), 1500, throughput=85.0)
        assert chunks[0].timeout_s == MAX_ESCALATED_TIMEOUT_S

    def test_it_is_never_split_or_dropped(self):
        blocks = self._oversized()
        packed = [b for c in plan_chunks(blocks, 900, throughput=85.0) for b in c.blocks]
        assert [b.key for b in packed] == [b.key for b in blocks]

    def test_surrounding_blocks_keep_the_normal_timeout(self):
        blocks = parse_blocks("big.md", make_learnings(2)) + self._oversized()
        chunks = plan_chunks(blocks, 900, throughput=85.0)
        assert [c.timeout_s for c in chunks] == [900.0, 1800.0]


class TestRenderChunk:
    def test_line_numbers_are_the_originals(self):
        """The load-bearing assertion of this module."""
        text = make_learnings(12)
        reference = numbered_reference(text)
        blocks = parse_blocks("f.md", text)
        for chunk in plan_chunks(blocks, 300, throughput=85.0):
            for line in render_chunk(chunk).splitlines():
                if line.startswith("### File:") or not line.strip():
                    continue
                n = int(line.split(":", 1)[0])
                assert line == reference[n]

    def test_first_line_of_each_block_is_its_start_line(self):
        blocks = parse_blocks("f.md", make_learnings(6))
        for chunk in plan_chunks(blocks, 300, throughput=85.0):
            body = render_chunk(chunk).split("\n\n", 1)[1]
            assert body.startswith(f"{chunk.blocks[0].start_line}: ")

    def test_header_matches_read_all_learnings(self):
        blocks = parse_blocks("2026-07-30.md", make_learnings(2))
        chunk = plan_chunks(blocks, 900, throughput=85.0)[0]
        assert render_chunk(chunk).startswith("### File: 2026-07-30.md\n\n")

    def test_multiple_files_join_with_the_same_separator(self):
        a = parse_blocks("a.md", make_learnings(1))
        b = parse_blocks("b.md", make_learnings(1, marker="y"))
        mixed = Chunk(index=1, blocks=tuple(a + b), n_bytes=0, timeout_s=900.0)
        rendered = render_chunk(mixed)
        assert "\n\n---\n\n### File: b.md\n\n" in rendered
        assert rendered.count("### File:") == 2

    def test_no_content_is_invented_between_blocks(self):
        """Every rendered non-header line must be a real numbered source line."""
        text = make_learnings(4)
        reference = set(numbered_reference(text).values())
        chunk = plan_chunks(parse_blocks("f.md", text), 900, throughput=85.0)[0]
        for line in render_chunk(chunk).splitlines()[2:]:
            assert line in reference

    def test_empty_chunk_renders_empty(self):
        assert render_chunk(Chunk(1, (), 0, 900.0)) == ""


class TestPlanLine:
    def _chunks(self):
        return plan_chunks(parse_blocks("f.md", make_learnings(200)), 300, throughput=85.0)

    def test_wall_clock_uses_the_slowest_chunk_per_wave(self):
        chunks = self._chunks()
        waves = math.ceil(len(chunks) / 4)
        serial = sum(estimate_seconds(c.n_bytes, 85.0) for c in chunks)
        parallel = estimate_wall_clock(chunks, 4, 85.0)
        assert waves > 1
        assert parallel < serial

    def test_concurrency_of_one_is_the_serial_cost(self):
        chunks = self._chunks()
        serial = sum(estimate_seconds(c.n_bytes, 85.0) for c in chunks)
        assert estimate_wall_clock(chunks, 1, 85.0) == pytest.approx(serial)

    def test_nonsense_concurrency_does_not_divide_by_zero(self):
        assert estimate_wall_clock(self._chunks(), 0, 85.0) > 0

    def test_line_names_everything_a_reader_needs_before_spending(self):
        chunks = self._chunks()
        line = format_plan_line(
            new_bytes=12_345, total_bytes=254_000, chunks=chunks,
            concurrency=4, throughput=85.0,
        )
        assert "12,345" in line and "254,000" in line
        assert f"{len(chunks)} chunk(s)" in line
        assert "concurrency 4" in line
        assert f"{math.ceil(len(chunks) / 4)} wave(s)" in line
        assert "est." in line

    def test_oversized_chunks_are_called_out(self):
        big = parse_blocks(
            "big.md",
            "## Session Learnings — big\n"
            + "\n".join(f"- {'z' * 200}" for _ in range(300)),
        )
        line = format_plan_line(
            new_bytes=1, total_bytes=1, chunks=plan_chunks(big, 900, throughput=85.0),
            concurrency=4, throughput=85.0,
        )
        assert "oversized" in line

    def test_no_chunks_reports_zero_waves(self):
        line = format_plan_line(
            new_bytes=0, total_bytes=0, chunks=[], concurrency=4, throughput=85.0
        )
        assert "0 chunk(s)" in line and "0 wave(s)" in line
