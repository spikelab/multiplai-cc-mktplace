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

import random

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
        """254 KB of learnings costs the ~94 min of serial work the fixture measured.

        Checked against a real 283 KB / 231-block run rather than the plan's
        original log table: its 11 successful chunks summed to 5,645 s for
        257,137 bytes, i.e. 45.5 B/s end to end. The default must predict that
        within a sane margin — too high and the first run sizes chunks it cannot
        finish inside the 900 s deadline, which is exactly how that run lost a
        chunk to a double timeout.
        """
        monkeypatch.delenv("MULTIPLAI_DREAM_THROUGHPUT", raising=False)
        measured_serial_s = 5_645 * (254_000 / 257_137)
        assert estimate_seconds(254_000) == pytest.approx(measured_serial_s, rel=0.15)

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

    def test_concurrency_beats_serial(self):
        chunks = self._chunks()
        assert len(chunks) > 4, "needs more chunks than workers to mean anything"
        serial = sum(estimate_seconds(c.n_bytes, 85.0) for c in chunks)
        assert estimate_wall_clock(chunks, 4, 85.0) < serial

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
        assert "est." in line
        # No wave count: work is scheduled by a semaphore, not in waves, and
        # printing "5 wave(s)" beside a 24-minute estimate invites reading it
        # as 5 x 6 min = 30.
        assert "wave" not in line

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

    def test_no_chunks_is_reported_without_crashing(self):
        line = format_plan_line(
            new_bytes=0, total_bytes=0, chunks=[], concurrency=4, throughput=85.0
        )
        assert "0 chunk(s)" in line


class TestWallClockMatchesRealRuns:
    """The estimator is checked against a measured run, not against itself.

    `_draft_chunks` schedules behind an `asyncio.Semaphore`, so a finished chunk
    frees its slot at once. An earlier version summed per-wave maxima, which
    assumes a barrier that does not exist.

    The numbers below are the **clean** 283 KB fixture run — 19 chunks, all
    completed, no retries. An earlier version of this test used a run with a
    failed chunk in it, which is not a calibration point: a failure burns two
    full timeout periods on a worker, and no sizing model predicts that.
    """

    # 19 chunks from the clean run: (bytes, measured seconds). 287,479 bytes
    # over 5,875 s of serial work => 48.9 B/s, which is why the 50.0 cold-start
    # default is close enough. Chunk phase 21:13:30 -> 21:41:15 = 1,665 s at
    # concurrency 4.
    MEASURED = [
        (16832, 296), (17291, 309), (17975, 325), (17611, 361), (6006, 136),
        (13382, 348), (17968, 318), (17955, 416), (17542, 274), (15219, 241),
        (17802, 247), (17454, 329), (1722, 52), (17358, 373), (17950, 320),
        (17393, 403), (4887, 176), (17404, 395), (17728, 556),
    ]
    ACTUAL_WALL_CLOCK_S = 1665

    def _chunks(self):
        return [
            Chunk(index=i, blocks=(), n_bytes=b, timeout_s=900.0, oversized=False)
            for i, (b, _) in enumerate(self.MEASURED, 1)
        ]

    def _throughput(self):
        return sum(b for b, _ in self.MEASURED) / sum(s for _, s in self.MEASURED)

    def test_the_prediction_is_a_floor_the_real_run_cannot_beat(self):
        """`max(work / m, slowest)` is a lower bound on makespan, not a fit.

        Asserting it as a bound rather than as a tolerance is deliberate: it is
        a theorem about scheduling, so it holds for runs nobody has measured
        yet. One measured run is not enough to tune a two-sided fit against.
        """
        got = estimate_wall_clock(self._chunks(), 4, self._throughput())
        assert got <= self.ACTUAL_WALL_CLOCK_S

    def test_the_floor_is_close_enough_to_be_worth_printing(self):
        """A floor is only useful if it is a tight one.

        The clean run came in 13% above the prediction — list scheduling leaves
        a tail no lower bound sees. 25% is the point at which `--check` would
        start misleading someone deciding whether to start a run now.
        """
        got = estimate_wall_clock(self._chunks(), 4, self._throughput())
        assert got >= self.ACTUAL_WALL_CLOCK_S * 0.75

    def test_the_estimate_does_not_depend_on_chunk_order(self):
        """The property that rules out the per-wave-maxima model.

        Not accuracy — on this run the wave model was +9.8% against this one's
        -11.8%, so accuracy does not separate them. Order does. The scheduler
        takes work off the list as slots free and is indifferent to the order it
        arrived in; summing per-wave maxima is not. An estimate that moves when
        nothing about the run moved is not measuring the run.
        """
        chunks, tp, n = self._chunks(), self._throughput(), 4
        baseline = estimate_wall_clock(chunks, n, tp)

        rng = random.Random(0)
        for _ in range(200):
            shuffled = chunks[:]
            rng.shuffle(shuffled)
            assert estimate_wall_clock(shuffled, n, tp) == pytest.approx(baseline)

    def test_the_wave_models_answer_moves_by_16_percent_on_order_alone(self):
        """Quantifies the above exactly, so the guard can't pass for free.

        The extremes are computed, not sampled: the sum of per-group maxima is
        largest when the biggest chunks each head their own group, and smallest
        when they cluster into one. For this run that is 1,564 s vs 1,832 s —
        268 s apart, 16% of a 1,665 s run, with the scheduler doing exactly the
        same work either way.
        """
        tp, n = self._throughput(), 4
        desc = sorted(
            (estimate_seconds(c.n_bytes, tp) for c in self._chunks()), reverse=True
        )
        n_groups = -(-len(desc) // n)
        spread_max = sum(desc[:n_groups])
        spread_min = sum(desc[i] for i in range(0, len(desc), n))

        assert spread_max - spread_min > 0.15 * self.ACTUAL_WALL_CLOCK_S
        # Ours sits below even the wave model's best case, as a bound should.
        assert estimate_wall_clock(self._chunks(), n, tp) < spread_min

    def test_never_predicts_less_than_the_slowest_chunk(self):
        """No concurrency shortens one indivisible call."""
        chunks, tp = self._chunks(), self._throughput()
        slowest = max(estimate_seconds(c.n_bytes, tp) for c in chunks)
        assert estimate_wall_clock(chunks, 999, tp) >= slowest
