"""Tests for memory utilisation telemetry.

The one thing that must never break here: **a missing judgement is not a
verdict of "unused".** Every other property in this file is in service of that
one, because the failure it guards against is silent and catastrophic — an
outage in the judge pass would otherwise mark the entire corpus dead weight,
and the phase after this one prunes on exactly this signal.

No test calls a live model. The two estimators are exercised through their
parsers and their recorded shapes.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from lib import utilisation as util  # noqa: E402
from lib import utilisation_judge as judge  # noqa: E402

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _ts(days_ago: int = 0) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _inject(file: str, section: str | None, size: int, turn: int = 1) -> dict:
    return {"file": file, "section": section, "bytes": size,
            "turn": turn, "co_picks": 1}


def _session(
    name: str,
    injected: list[dict],
    *,
    self_report=None,
    judge_entries=None,
    days_ago: int = 0,
    transcript: str = "",
) -> dict:
    record = util.new_session_record(name, ts=_ts(days_ago))
    record["injected"] = injected
    record["self_report"] = self_report
    record["judge"] = judge_entries
    if transcript:
        record["transcript"] = transcript
    return record


def _write(path: Path, records: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records),
        encoding="utf-8",
    )
    return path


# --- 1. round-trip and durability -------------------------------------------

class TestAppendReadRoundTrip:
    def test_round_trip(self, tmp_path):
        path = util.utilisation_path(tmp_path)
        util.record_injections(path, "aaaa1111", [_inject("dev.md", "Testing", 900)])
        records = util.read_records(path)
        assert len(records) == 1
        assert records[0]["session"] == "aaaa1111"
        assert records[0]["injected"][0]["section"] == "Testing"
        assert records[0]["self_report"] is None
        assert records[0]["judge"] is None

    def test_second_write_merges_into_the_same_record(self, tmp_path):
        path = util.utilisation_path(tmp_path)
        util.record_injections(path, "aaaa1111", [_inject("dev.md", "Testing", 900)])
        util.record_self_report(path, "aaaa1111", [
            {"file": "dev.md", "section": "Testing",
             "evidence": "ran the suite", "supported": True},
        ])
        records = util.read_records(path)
        assert len(records) == 1
        assert records[0]["self_report"][0]["evidence"] == "ran the suite"

    def test_re_recording_injections_replaces_rather_than_appends(self, tmp_path):
        """A PreCompact-deferred extraction runs twice for one session."""
        path = util.utilisation_path(tmp_path)
        entries = [_inject("dev.md", "Testing", 900)]
        util.record_injections(path, "aaaa1111", entries)
        util.record_injections(path, "aaaa1111", entries)
        agg = util.aggregate(util.read_records(path))
        assert agg.sections["dev.md#Testing"].retrieved == 1

    def test_truncated_line_costs_only_itself(self, tmp_path):
        """The shape a kill mid-write leaves on a naive appender."""
        path = util.utilisation_path(tmp_path)
        util.record_injections(path, "aaaa1111", [_inject("dev.md", "Testing", 900)])
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"session": "bbbb2222", "injec')
        records = util.read_records(path)
        assert [r["session"] for r in records] == ["aaaa1111"]

    def test_file_stays_valid_jsonl_after_a_failed_write(self, tmp_path, monkeypatch):
        """A kill during ``_write_all`` leaves the previous version intact."""
        path = util.utilisation_path(tmp_path)
        util.record_injections(path, "aaaa1111", [_inject("dev.md", "Testing", 900)])
        before = path.read_text(encoding="utf-8")

        def boom(*_a, **_kw):
            raise KeyboardInterrupt("killed mid-write")

        monkeypatch.setattr(util.os, "replace", boom)
        with pytest.raises(KeyboardInterrupt):
            util.record_injections(path, "bbbb2222", [_inject("me.md", None, 100)])

        assert path.read_text(encoding="utf-8") == before
        for line in path.read_text(encoding="utf-8").splitlines():
            json.loads(line)
        assert not list(tmp_path.glob(".tmp-utilisation-*"))


# --- 2. a record with `injected` only ---------------------------------------

class TestInjectedOnly:
    def test_aggregates_with_null_estimators(self):
        agg = util.aggregate([_session("s1", [
            _inject("dev.md", "Testing", 900),
            _inject("dev.md", "Testing", 900, turn=4),
            _inject("me.md", None, 400),
        ])])
        assert agg.sessions == 1
        stat = agg.sections["dev.md#Testing"]
        assert (stat.retrieved, stat.sessions, stat.bytes) == (2, 1, 1800)
        assert stat.bytes_per_injection == 900
        # No estimator ran: rates are None — *not* zero.
        assert stat.rate("self_report") is None
        assert stat.rate("judge") is None
        assert stat.estimated_uses("judge") is None
        assert agg.sections["me.md"].section is None

    def test_row_is_not_ranked_without_estimator_data(self):
        agg = util.aggregate([_session("s1", [_inject("dev.md", "Testing", 900)])])
        ranked, insufficient = util.rank(agg)
        assert ranked == []
        assert [r["key"] for r in insufficient] == ["dev.md#Testing"]
        assert insufficient[0]["sufficient"] is False
        assert insufficient[0]["cost_per_use"] is None
        assert insufficient[0]["rank_basis"] is None


# --- 3. the failure-mode assertion (contract C4) ----------------------------

class TestFailedJudgeIsNotAVerdict:
    def _records(self, judged: bool) -> list[dict]:
        entries = [_inject("dev.md", "Testing", 900)]
        return [
            _session(f"s{i}", entries,
                     judge_entries=([] if judged else None),
                     transcript=f"/transcripts/s{i}.jsonl")
            for i in range(6)
        ]

    def test_null_judge_contributes_no_observation(self):
        agg = util.aggregate(self._records(judged=False))
        stat = agg.sections["dev.md#Testing"]
        assert stat.judge_observed == 0
        assert stat.judge_used == 0
        assert stat.rate("judge") is None, "a missing judgement must not read as 0% used"
        assert stat.zero_estimated_use("judge") is False

    def test_a_failed_batch_never_marks_a_section_unused(self, tmp_path, monkeypatch):
        """End to end: the judge call fails, nothing is written, nothing is unused.

        The transcript is stubbed present on purpose. Without it this test never
        reached the model at all — it asserted an outage while exercising a
        missing transcript, which is precisely the conflation the split of
        ``kept_default`` exists to expose.
        """
        path = _write(util.utilisation_path(tmp_path), self._records(judged=False))
        monkeypatch.setattr(judge, "distilled_transcript", lambda *a, **k: "transcript")

        client = MagicMock()
        called = []

        async def blow_up(**_kw):
            called.append(1)
            raise RuntimeError("rate limited")

        client.query = blow_up
        report = asyncio.run(judge.judge_sessions(
            path, client=client, model="m", sample_size=6))

        assert called, "the model was never called — this is not the outage path"
        assert report["judged"] == 0
        assert report["kept_default"] == 6
        assert report["unavailable"] == 6, "a model failure IS the outage signal"
        assert report["not_judgeable"] == 0
        after = util.read_records(path)
        assert all(r["judge"] is None for r in after)
        stat = util.aggregate(after).sections["dev.md#Testing"]
        assert stat.judge_observed == 0
        assert stat.rate("judge") is None

    def test_a_missing_transcript_is_not_an_outage(self, tmp_path):
        """Transcripts age out faster than the 90-day retention window.

        Folded into one counter, "no transcript" becomes a permanent, growing
        baseline that buries the signal it shares a number with.
        """
        records = [
            _session(f"s{i}", [_inject("dev.md", "Testing", 900)], judge_entries=None)
            for i in range(3)
        ]
        path = _write(util.utilisation_path(tmp_path), records)

        client = MagicMock()

        async def never_called(**_kw):
            raise AssertionError("should not reach the model")

        client.query = never_called
        report = asyncio.run(judge.judge_sessions(
            path, client=client, model="m", sample_size=3))

        assert report["kept_default"] == 3
        assert report["not_judgeable"] == 3
        assert report["unavailable"] == 0

    def test_an_empty_verdict_block_is_not_judge_coverage(self, tmp_path, monkeypatch):
        """Recording it would increment ``sessions_judged`` with no observations."""
        path = _write(util.utilisation_path(tmp_path), self._records(judged=False))
        monkeypatch.setattr(judge, "distilled_transcript", lambda *a, **k: "transcript")

        client = MagicMock()

        async def empty(**_kw):
            return SimpleNamespace(content="<verdicts>\n</verdicts>")

        client.query = empty
        report = asyncio.run(judge.judge_sessions(
            path, client=client, model="m", sample_size=6))

        assert report["judged"] == 0
        assert report["empty_verdicts"] == 6
        after = util.read_records(path)
        assert all(r["judge"] is None for r in after)
        assert util.aggregate(after).sessions_judged == 0

    def test_an_explicit_unused_verdict_does_count(self):
        records = [
            _session(f"s{i}", [_inject("dev.md", "Testing", 900)],
                     judge_entries=[{"file": "dev.md", "section": "Testing",
                                     "used": False, "evidence": ""}])
            for i in range(6)
        ]
        stat = util.aggregate(records).sections["dev.md#Testing"]
        assert (stat.judge_observed, stat.judge_used) == (6, 0)
        assert stat.rate("judge") == 0.0
        assert stat.zero_estimated_use("judge") is True
        assert stat.bytes_per_estimated_use("judge") is None

    def test_a_judge_verdict_for_an_uninjected_section_is_discarded(self):
        records = [_session("s1", [_inject("dev.md", "Testing", 900)],
                            judge_entries=[{"file": "other.md", "section": "X",
                                            "used": True, "evidence": "e"}])]
        agg = util.aggregate(records)
        assert "other.md#X" not in agg.sections
        assert agg.sections["dev.md#Testing"].judge_observed == 0


# --- 4. unsupported self-report claims --------------------------------------

class TestSelfReportEvidence:
    def test_claim_without_evidence_is_unsupported_and_uncounted(self):
        records = [
            _session(f"s{i}", [_inject("dev.md", "Testing", 900)], self_report=[
                {"file": "dev.md", "section": "Testing",
                 "evidence": "", "supported": False},
            ])
            for i in range(6)
        ]
        stat = util.aggregate(records).sections["dev.md#Testing"]
        assert stat.self_report_observed == 6
        assert stat.self_report_used == 0

    def test_claim_with_evidence_counts(self):
        records = [
            _session(f"s{i}", [_inject("dev.md", "Testing", 900)], self_report=[
                {"file": "dev.md", "section": "Testing",
                 "evidence": "quoted the release steps", "supported": True},
            ])
            for i in range(6)
        ]
        stat = util.aggregate(records).sections["dev.md#Testing"]
        assert (stat.self_report_observed, stat.self_report_used) == (6, 6)

    def test_empty_self_report_is_a_real_answer(self):
        """'None of them' observes every injected section as unused."""
        records = [_session(f"s{i}", [_inject("dev.md", "Testing", 900)],
                            self_report=[]) for i in range(6)]
        stat = util.aggregate(records).sections["dev.md#Testing"]
        assert (stat.self_report_observed, stat.self_report_used) == (6, 0)
        assert stat.rate("self_report") == 0.0

    def test_parser_marks_evidence_free_claims_unsupported(self):
        from lib.extraction import parse_utilisation

        entries = parse_utilisation(
            "<utilisation>"
            '<used file="dev.md" section="Testing">ran the suite</used>'
            '<used file="me.md"></used>'
            "</utilisation>"
        )
        assert entries == [
            {"file": "dev.md", "section": "Testing",
             "evidence": "ran the suite", "supported": True},
            {"file": "me.md", "section": None, "evidence": "", "supported": False},
        ]

    def test_an_absent_block_is_null_and_an_empty_one_is_zero(self):
        """The invariant this feature is *named* for, on the estimator that
        runs every session.

        `[]` for both was the defect: the caller cannot tell "asked and got
        nothing" from "never answered" by whether it passed an injected list,
        because it gates on whether the model call *succeeded* — and a truncated
        reply is a success that parses. `<utilisation>` is emitted last, after
        every diary unit, with a 4096-token default and no override, so
        truncation is the ordinary failure. Recorded as `[]` it means "observed
        and used nothing", which puts a live section top of the pruning table.
        """
        from lib.extraction import parse_utilisation

        # No block at all -> we do not know.
        assert parse_utilisation("<unit></unit>") is None
        assert parse_utilisation("") is None
        assert parse_utilisation(None) is None
        # Truncated mid-response, the real shape of the failure.
        assert parse_utilisation(
            "<unit><diary_entry>a</diary_entry></unit>\n<unit>\n<diary_ent"
        ) is None
        # An explicit "none of them" IS an answer.
        assert parse_utilisation("<utilisation></utilisation>") == []
        assert parse_utilisation("<utilisation>\n\n</utilisation>") == []

    def test_a_null_self_report_is_not_recorded_as_a_zero(self, tmp_path):
        """End to end: the aggregate must show `None`, not `0.0`."""
        path = util.utilisation_path(tmp_path)
        entries = [_inject("multiplai.md", "Release Flow", 6000)]
        util.record_injections(path, "s1", entries, transcript="")

        # Nothing recorded, because parse_utilisation said None.
        table = util.build_table(
            util.read_records(path), known_keys=["multiplai.md#Release Flow"]
        )
        row = (table["sections"] + table["insufficient_data"])[0]
        assert row["self_report"]["rate"] is None
        assert row["zero_estimated_use"]["self_report"] is False

        # Whereas an explicit empty answer is a real zero.
        util.record_self_report(path, "s1", [])
        table = util.build_table(
            util.read_records(path), known_keys=["multiplai.md#Release Flow"]
        )
        row = (table["sections"] + table["insufficient_data"])[0]
        assert row["self_report"]["rate"] == 0.0


# --- 5. ranking and the low-n flag ------------------------------------------

class TestRanking:
    def _corpus(self) -> list[dict]:
        records = []
        # cheap-and-used: 1 KB, judged used 6/6
        for i in range(6):
            records.append(_session(
                f"c{i}", [_inject("small.md", "Used", 1000)],
                judge_entries=[{"file": "small.md", "section": "Used",
                                "used": True, "evidence": "e"}]))
        # expensive-and-rarely-used: 14 KB, judged used 1/6
        for i in range(6):
            records.append(_session(
                f"e{i}", [_inject("big.md", "Bloat", 14000)],
                judge_entries=[{"file": "big.md", "section": "Bloat",
                                "used": i == 0, "evidence": "e" if i == 0 else ""}]))
        return records

    def test_orders_by_bytes_per_estimated_use(self):
        ranked, insufficient = util.rank(util.aggregate(self._corpus()))
        assert [r["key"] for r in ranked] == ["big.md#Bloat", "small.md#Used"]
        assert insufficient == []
        assert ranked[0]["cost_per_use"] == pytest.approx(14000 * 6 / 1)
        assert ranked[1]["cost_per_use"] == pytest.approx(1000)
        assert all(r["rank_basis"] == "judge" for r in ranked)

    def test_zero_estimated_use_outranks_every_finite_cost(self):
        records = self._corpus()
        for i in range(6):
            records.append(_session(
                f"d{i}", [_inject("dead.md", "Never", 500)],
                judge_entries=[{"file": "dead.md", "section": "Never",
                                "used": False, "evidence": ""}]))
        ranked, _ = util.rank(util.aggregate(records))
        assert ranked[0]["key"] == "dead.md#Never"
        assert ranked[0]["cost_per_use"] is None
        assert ranked[0]["zero_estimated_use"]["judge"] is True

    def test_low_n_row_is_flagged_and_not_ranked(self):
        records = self._corpus()
        records.append(_session(
            "thin1", [_inject("thin.md", "Rare", 50000)],
            judge_entries=[{"file": "thin.md", "section": "Rare",
                            "used": False, "evidence": ""}]))
        ranked, insufficient = util.rank(util.aggregate(records))
        assert "thin.md#Rare" not in [r["key"] for r in ranked]
        thin = next(r for r in insufficient if r["key"] == "thin.md#Rare")
        assert thin["sufficient"] is False
        assert thin["judge"]["observed"] == 1

    def test_self_report_is_the_basis_when_the_judge_is_thin(self):
        records = []
        for i in range(6):
            records.append(_session(
                f"s{i}", [_inject("dev.md", "Testing", 1000)],
                self_report=[{"file": "dev.md", "section": "Testing",
                              "evidence": "e", "supported": True}],
                judge_entries=([{"file": "dev.md", "section": "Testing",
                                 "used": True, "evidence": "e"}] if i == 0 else None)))
        ranked, _ = util.rank(util.aggregate(records))
        assert ranked[0]["rank_basis"] == "self_report"
        assert ranked[0]["judge"]["observed"] == 1

    def test_every_row_is_labelled_an_estimate(self):
        table = util.build_table(self._corpus())
        assert all(row["estimate"] is True for row in table["sections"])
        assert "ESTIMATED" in util.render_table(table)
        assert "estimate" in table["disclaimer"].lower()


# --- 6. estimator disagreement ----------------------------------------------

class TestDisagreement:
    def _records(self, judge_used: bool) -> list[dict]:
        return [
            _session(f"s{i}", [_inject("dev.md", "Testing", 1000)],
                     self_report=[{"file": "dev.md", "section": "Testing",
                                   "evidence": "e", "supported": True}],
                     judge_entries=[{"file": "dev.md", "section": "Testing",
                                     "used": judge_used, "evidence": "e"}])
            for i in range(6)
        ]

    def test_wide_gap_marks_the_row(self):
        # self-report 100% used, judge 0% used — the classic over-report.
        agg = util.aggregate(self._records(judge_used=False))
        stat = agg.sections["dev.md#Testing"]
        assert stat.rate("self_report") == 1.0
        assert stat.rate("judge") == 0.0
        assert stat.disagreement() is True
        ranked, _ = util.rank(agg)
        assert ranked[0]["disagreement"] is True

    def test_agreement_is_not_marked(self):
        agg = util.aggregate(self._records(judge_used=True))
        assert agg.sections["dev.md#Testing"].disagreement() is False

    def test_the_two_estimates_are_never_blended(self):
        row = util.rank(util.aggregate(self._records(judge_used=False)))[0][0]
        assert row["self_report"]["rate"] == 1.0
        assert row["judge"]["rate"] == 0.0
        # No averaged field anywhere in the row.
        assert "score" not in row
        assert set(row["estimated_uses"]) == {"self_report", "judge"}

    def test_one_sided_data_never_counts_as_disagreement(self):
        records = [_session(f"s{i}", [_inject("dev.md", "Testing", 1000)],
                            self_report=[]) for i in range(6)]
        assert util.aggregate(records).sections["dev.md#Testing"].disagreement() is False

    def test_rendered_table_marks_the_row(self):
        table = util.build_table(self._records(judge_used=False))
        assert " !" in util.render_table(table)


# --- 7. compaction ----------------------------------------------------------

class TestCompaction:
    def _corpus(self) -> list[dict]:
        old = [
            _session(f"o{i}", [_inject("dev.md", "Testing", 1000),
                               _inject("me.md", None, 300)],
                     self_report=[{"file": "dev.md", "section": "Testing",
                                   "evidence": "e", "supported": True}],
                     judge_entries=[{"file": "dev.md", "section": "Testing",
                                     "used": i % 2 == 0, "evidence": "e"}],
                     days_ago=120)
            for i in range(5)
        ]
        recent = [
            _session(f"r{i}", [_inject("dev.md", "Testing", 1000)],
                     self_report=[], days_ago=1)
            for i in range(3)
        ]
        return old + recent

    def test_totals_survive_and_detail_is_dropped(self, tmp_path):
        path = _write(util.utilisation_path(tmp_path), self._corpus())
        before = util.aggregate(util.read_records(path))

        result = util.compact(path, now=NOW)
        assert result["collapsed"] == 5
        assert result["kept"] == 3

        after_records = util.read_records(path)
        assert len(after_records) == 4  # one totals + three recent sessions
        assert after_records[0]["kind"] == "totals"

        after = util.aggregate(after_records)
        for key, stat in before.sections.items():
            other = after.sections[key]
            for name in util._TOTALS_FIELDS:
                assert getattr(other, name) == getattr(stat, name), f"{key}.{name}"
        assert after.sessions == before.sessions

    def test_the_table_is_unchanged_by_compaction(self, tmp_path):
        path = _write(util.utilisation_path(tmp_path), self._corpus())
        before = util.build_table(util.read_records(path))
        util.compact(path, now=NOW)
        after = util.build_table(util.read_records(path))
        assert before["sections"] == after["sections"]
        assert before["insufficient_data"] == after["insufficient_data"]

    def test_compacting_twice_is_idempotent(self, tmp_path):
        path = _write(util.utilisation_path(tmp_path), self._corpus())
        util.compact(path, now=NOW)
        first = util.build_table(util.read_records(path))
        assert util.compact(path, now=NOW)["collapsed"] == 0
        assert util.build_table(util.read_records(path))["sections"] == first["sections"]

    def test_a_record_with_no_timestamp_is_kept(self, tmp_path):
        record = _session("x", [_inject("dev.md", "Testing", 100)], days_ago=400)
        record["ts"] = "not-a-date"
        path = _write(util.utilisation_path(tmp_path), [record])
        assert util.compact(path, now=NOW)["collapsed"] == 0

    def test_empty_file_is_a_no_op(self, tmp_path):
        path = util.utilisation_path(tmp_path)
        assert util.compact(path, now=NOW) == {"collapsed": 0, "kept": 0, "sections": 0}


# --- 8. the extraction pass's primary product is unchanged ------------------

class TestDiaryOutputIsNotDegraded:
    """Item 2's guard, in the only form an offline test can take.

    The real question — does asking for a utilisation block make the *model*
    write a shorter diary? — needs a live A/B and is therefore Spike's to run;
    it is recorded as such in the PR. What IS testable, and is the mechanical
    way this change could silently shorten the diary, is that the prompt and
    parser still extract every byte of diary text from a fixed response. This
    fixture is the pre-change response shape.
    """

    RESPONSE = (
        "<unit>\n<timestamp>2026-08-08T10:00:00Z</timestamp>\n"
        "<diary>\nFirst paragraph of the narrative, with detail.\n\n"
        "Second paragraph, longer, describing the decisions and why they were "
        "taken the way they were.\n</diary>\n"
        "<learning>\ntrust: verified\ntype: OBSERVATION\ntarget: dev.md\n"
        "description: a thing\naction: record it\n</learning>\n</unit>\n"
        "<unit>\n<timestamp>2026-08-08T11:00:00Z</timestamp>\n"
        "<diary>\nA second unit of work, also with substance.\n</diary>\n</unit>\n"
        "<disposition>\nstate: done\nreason: shipped\n</disposition>\n"
    )

    #: Diary characters the PRE-CHANGE pipeline extracted from RESPONSE.
    #: Measured by running ``_parse_units`` from the base branch
    #: (``feat/memory-section-anchors``) against this exact fixture, not by
    #: reading it off the new code — otherwise the guard would ratchet to
    #: whatever the change happened to produce.
    EXPECTED_DIARY_CHARS = 184

    def _run(self, response: str, injected):
        from lib.extraction import extract_session_signals

        client = MagicMock()

        async def query(**_kw):
            return MagicMock(content=response)

        client.query = query
        return asyncio.run(extract_session_signals(
            "transcript", valid_targets=["dev.md"], client=client,
            injected_sections=injected,
        ))

    def test_diary_text_is_byte_identical_with_and_without_the_new_block(self):
        base_units, _, _ = self._run(self.RESPONSE, None)
        with_block, _, claims = self._run(
            self.RESPONSE + "<utilisation>"
            '<used file="dev.md" section="Testing">used it</used>'
            "</utilisation>",
            ["dev.md#Testing"],
        )
        assert [u["diary_entry"] for u in base_units] == \
               [u["diary_entry"] for u in with_block]
        assert claims and claims[0]["supported"] is True

    def test_diary_length_matches_the_pre_change_fixture(self):
        units, disposition, claims = self._run(self.RESPONSE, ["dev.md#Testing"])
        total = sum(len(u["diary_entry"]) for u in units)
        assert total == self.EXPECTED_DIARY_CHARS
        assert len(units) == 2
        assert disposition["state"] == "done"
        # This fixture's response carries no <utilisation> block, so the honest
        # answer is "not estimated" rather than "estimated at zero".
        assert claims is None

    def test_the_units_only_entry_point_rides_the_same_call(self):
        """extract_units delegates to extract_session_signals — one model
        response serves both shapes (the two-value middle layer is gone)."""
        from lib.extraction import extract_units

        client = MagicMock()

        async def query(**_kw):
            return MagicMock(content=self.RESPONSE)

        client.query = query
        units = asyncio.run(extract_units(
            "transcript", valid_targets=["dev.md"], client=client))
        assert len(units) == 2


# --- prompt shape -----------------------------------------------------------

class TestPromptShape:
    def test_the_injected_list_lives_in_the_per_call_half(self):
        """It changes every call; in the system half it would kill the cache."""
        from lib.extraction import EXTRACTION_SYSTEM, EXTRACTION_USER

        assert "{injected_sections}" not in EXTRACTION_SYSTEM
        assert "{injected_sections}" in EXTRACTION_USER

    def test_the_empty_answer_is_offered_explicitly(self):
        from lib.extraction import EXTRACTION_SYSTEM

        assert "An empty answer is valid" in EXTRACTION_SYSTEM
        assert "evidence" in EXTRACTION_SYSTEM
        # The prompt must NOT predict the answer it is asking for.
        assert "Most injected" not in EXTRACTION_SYSTEM

    def test_the_injected_list_is_fenced_as_untrusted(self):
        from lib.extraction import render_injected_sections

        rendered = render_injected_sections(["dev.md#Testing"])
        assert "<untrusted-content" in rendered
        assert "dev.md#Testing" in rendered

    def test_no_injected_sections_says_so(self):
        from lib.extraction import NO_INJECTED_SECTIONS, render_injected_sections

        assert render_injected_sections([]) == NO_INJECTED_SECTIONS


# --- inject-event ingestion (P1's contract) ---------------------------------

class TestInjectedFromInjectEvents:
    def test_empty_section_list_means_the_whole_file(self):
        entries = util.injected_from_inject_events([{
            "component": "context", "event": "inject", "turn": 2,
            "sections_by_file": {"me.md": []}, "bytes_by_file": {"me.md": 4321},
        }])
        assert entries == [{"file": "me.md", "section": None, "bytes": 4321,
                            "turn": 2, "co_picks": 1}]

    def test_a_file_with_no_key_contributes_nothing(self):
        entries = util.injected_from_inject_events([{
            "component": "context", "event": "inject",
            "sections_by_file": {"dev.md": ["Testing"]},
            "bytes_by_file": {"dev.md": 100},
        }])
        assert [e["file"] for e in entries] == ["dev.md"]

    def test_co_picked_sections_split_the_file_bytes_and_say_so(self):
        entries = util.injected_from_inject_events([{
            "component": "context", "event": "inject",
            "sections_by_file": {"dev.md": ["A", "B"]},
            "bytes_by_file": {"dev.md": 1000},
        }])
        assert [(e["section"], e["bytes"], e["co_picks"]) for e in entries] == [
            ("A", 500, 2), ("B", 500, 2)]

    def test_other_events_are_ignored(self):
        assert util.injected_from_inject_events([
            {"component": "context", "event": "skip", "sections_by_file": {"a.md": []}},
            {"component": "diary", "event": "inject", "sections_by_file": {"b.md": []}},
        ]) == []

    def test_reads_the_activity_log_by_session_prefix(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        lines = [
            {"component": "context", "event": "inject", "session": "aaaa1111",
             "sections_by_file": {"dev.md": ["Testing"]}, "bytes_by_file": {"dev.md": 10}},
            {"component": "context", "event": "inject", "session": "bbbb2222",
             "sections_by_file": {"me.md": []}, "bytes_by_file": {"me.md": 20}},
        ]
        (logs / "activity.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in lines) + "not json\n",
            encoding="utf-8")
        events = util.inject_events_for_session(logs, "aaaa1111-full-uuid-here")
        assert len(events) == 1
        assert events[0]["sections_by_file"] == {"dev.md": ["Testing"]}

    def test_missing_logs_dir_is_empty(self, tmp_path):
        assert util.inject_events_for_session(tmp_path / "nope", "aaaa1111") == []


# --- never-retrieved --------------------------------------------------------

class TestNeverRetrieved:
    def test_splits_never_picked_from_whole_file_only(self):
        agg = util.aggregate([_session("s1", [
            _inject("dev.md", "Testing", 100),
            _inject("me.md", None, 100),
        ])])
        never, whole_only = util.never_retrieved(
            agg, ["dev.md", "dev.md#Testing", "dev.md#Other",
                  "me.md", "me.md#Bio", "cold.md", "cold.md#Section"])
        # dev.md was only ever picked as a section, so neither the bare file
        # nor its OTHER sections ever reached a prompt.
        assert never == ["cold.md", "cold.md#Section", "dev.md", "dev.md#Other"]
        # me.md was injected whole, so its sections DID reach prompts — a
        # routing observation, not dead weight.
        assert whole_only == ["me.md#Bio"]

    def test_catalog_keys_uses_p1_section_anchors(self):
        keys = util.catalog_keys({"entries": [
            {"source": "big.md", "section_anchors": [
                {"name": "One", "gloss": "g"}, {"name": "Two", "gloss": "g"}]},
            {"source": "small.md"},
            {"source": "", "section_anchors": []},
        ]})
        assert keys == ["big.md", "big.md#One", "big.md#Two", "small.md"]

    def test_the_table_keeps_the_two_lists_apart(self):
        table = util.build_table(
            [_session("s1", [_inject("me.md", None, 100)])],
            known_keys=["me.md", "me.md#Bio", "cold.md"])
        assert table["never_retrieved"] == ["cold.md"]
        assert table["only_as_whole_file"] == ["me.md#Bio"]
        rendered = util.render_table(table)
        assert "Never retrieved (1)" in rendered
        assert "only inside a whole-file injection" in rendered


# --- the judge --------------------------------------------------------------

class TestJudgeParsing:
    ALLOWED = ["dev.md#Testing", "me.md"]

    def test_parses_yes_and_no(self):
        entries = judge.parse_verdicts(
            "<verdicts>"
            '<verdict file="dev.md" section="Testing" used="yes">ran it</verdict>'
            '<verdict file="me.md" used="no"></verdict>'
            "</verdicts>", self.ALLOWED)
        assert entries == [
            {"file": "dev.md", "section": "Testing", "used": True, "evidence": "ran it"},
            {"file": "me.md", "section": None, "used": False, "evidence": ""},
        ]

    def test_yes_without_evidence_is_downgraded(self):
        entries = judge.parse_verdicts(
            '<verdicts><verdict file="me.md" used="yes"></verdict></verdicts>',
            self.ALLOWED)
        assert entries[0]["used"] is False

    def test_a_section_that_was_not_injected_is_discarded(self):
        entries = judge.parse_verdicts(
            '<verdicts><verdict file="hallucinated.md" used="yes">x</verdict></verdicts>',
            self.ALLOWED)
        assert entries == []

    def test_duplicates_keep_the_first_verdict(self):
        entries = judge.parse_verdicts(
            "<verdicts>"
            '<verdict file="me.md" used="yes">first</verdict>'
            '<verdict file="me.md" used="no"></verdict>'
            "</verdicts>", self.ALLOWED)
        assert len(entries) == 1 and entries[0]["used"] is True

    def test_a_missing_block_raises_so_nothing_is_written(self):
        with pytest.raises(judge.JudgeParseError):
            judge.parse_verdicts("I could not do that", self.ALLOWED)

    def test_an_empty_block_is_a_real_answer(self):
        assert judge.parse_verdicts("<verdicts></verdicts>", self.ALLOWED) == []


class TestJudgePrompt:
    def test_both_untrusted_inputs_are_fenced(self):
        prompt = judge.build_prompt(["dev.md#Testing"], "user: do the thing")
        assert prompt.count("<untrusted-content") == 2
        assert "dev.md#Testing" in prompt
        assert "do the thing" in prompt

    def test_the_system_prompt_states_the_fence_rule_and_offers_no(self):
        assert "untrusted" in judge.JUDGE_SYSTEM.lower()
        assert "An empty answer is valid" in judge.JUDGE_SYSTEM
        # The prompt must NOT predict the answer it is asking for.
        assert "Most injected" not in judge.JUDGE_SYSTEM

    def test_transcript_is_bounded(self):
        prompt = judge.build_prompt(["a.md"], "x" * (judge.TRANSCRIPT_CHAR_BUDGET * 2))
        assert len(prompt) < judge.TRANSCRIPT_CHAR_BUDGET * 2


class TestJudgeSampling:
    def _path(self, tmp_path, n: int) -> Path:
        records = [
            _session(f"s{i}", [_inject("dev.md", "Testing", 100)], days_ago=i)
            for i in range(n)
        ]
        records.append(_session("judged", [_inject("dev.md", "Testing", 100)],
                                judge_entries=[]))
        return _write(util.utilisation_path(tmp_path), records)

    def _client(self, response: str):
        client = MagicMock()

        async def query(**_kw):
            return MagicMock(content=response)

        client.query = query
        return client

    def test_samples_newest_first_and_reports_coverage(self, tmp_path, monkeypatch):
        path = self._path(tmp_path, 8)
        monkeypatch.setattr(judge, "distilled_transcript", lambda *_a, **_k: "text")
        # Every eligible record needs a transcript path to be judgeable.
        records = util.read_records(path)
        for record in records:
            record["transcript"] = str(tmp_path / "t.jsonl")
        _write(path, records)

        client = self._client(
            '<verdicts><verdict file="dev.md" section="Testing" used="yes">'
            "e</verdict></verdicts>")
        report = asyncio.run(judge.judge_sessions(
            path, client=client, model="m", sample_size=3))
        assert report["eligible"] == 8
        assert report["sampled"] == 3
        assert report["judged"] == 3
        assert report["kept_default"] == 0
        # kept_default is the sum of the three reasons below it.
        assert report["unavailable"] == 0
        assert report["not_judgeable"] == 0
        assert report["empty_verdicts"] == 0
        judged = {r["session"] for r in util.read_records(path)
                  if r.get("judge") is not None}
        assert judged == {"judged", "s0", "s1", "s2"}

    def test_a_session_without_a_transcript_keeps_its_default(self, tmp_path):
        path = self._path(tmp_path, 2)
        client = self._client("<verdicts></verdicts>")
        report = asyncio.run(judge.judge_sessions(
            path, client=client, model="m", sample_size=5))
        assert report["judged"] == 0
        assert report["kept_default"] == 2
        assert all(r["judge"] is None for r in util.read_records(path)
                   if r["session"].startswith("s"))

    def test_already_judged_sessions_are_not_eligible(self, tmp_path):
        path = self._path(tmp_path, 0)
        assert util.sessions_awaiting_judge(util.read_records(path)) == []


class TestConcurrentWritersDoNotLoseRecords:
    """Requirement: two sessions writing at once both survive.

    Nothing in this suite wrote to ``utilisation.jsonl`` from two writers, which
    is exactly why the module could ship a docstring asserting contention was
    harmless. It was not: the atomic rewrite guarantees the file is never
    *corrupt* and says nothing about a lost update, and a lost update here drops
    the appended record **entirely** — both estimator halves and the injected
    list with it. Measured before the lock: 40 concurrent writers left 2 records.

    This workspace runs parallel containers and extraction drains at the *next*
    ``SessionStart``, so coinciding drains are ordinary.
    """

    WRITERS = 12

    def test_every_concurrent_session_record_survives(self, tmp_path, monkeypatch):
        import threading

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        path = util.utilisation_path(tmp_path)
        start = threading.Barrier(self.WRITERS)
        errors: list[BaseException] = []

        def write(i: int) -> None:
            try:
                start.wait(timeout=10)
                util.record_injections(
                    path, f"s{i:04d}",
                    [_inject("dev.md", "Testing", 100)],
                    transcript="",
                )
            except BaseException as exc:  # pragma: no cover - diagnostic
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(i,))
                   for i in range(self.WRITERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, errors
        sessions = {
            r["session"] for r in util.read_records(path) if util.is_session_record(r)
        }
        assert sessions == {f"s{i:04d}" for i in range(self.WRITERS)}

    def test_the_file_stays_valid_jsonl_under_contention(self, tmp_path, monkeypatch):
        import threading

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        path = util.utilisation_path(tmp_path)

        def write(i: int) -> None:
            util.record_injections(
                path, f"s{i:04d}", [_inject("dev.md", None, 10)], transcript="")

        threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                json.loads(line)

    def test_a_half_written_record_is_never_left_behind(self, tmp_path, monkeypatch):
        """The pre-existing guarantee must survive the added lock."""
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        path = util.utilisation_path(tmp_path)
        util.record_injections(path, "s1", [_inject("dev.md", None, 10)], transcript="")
        before = path.read_text(encoding="utf-8")

        original = util._write_all

        def boom(p, records):  # noqa: ANN001
            raise OSError("disk full")

        monkeypatch.setattr(util, "_write_all", boom)
        with pytest.raises(OSError):
            util.record_injections(path, "s2", [_inject("dev.md", None, 10)], transcript="")
        monkeypatch.setattr(util, "_write_all", original)

        assert path.read_text(encoding="utf-8") == before
        # And the lock is released, so the next write still works.
        util.record_injections(path, "s3", [_inject("dev.md", None, 10)], transcript="")
        sessions = {
            r["session"] for r in util.read_records(path) if util.is_session_record(r)
        }
        assert sessions == {"s1", "s3"}


class TestCompactionIsLockedToo:
    def test_compaction_does_not_drop_a_concurrent_session(self, tmp_path, monkeypatch):
        """``compact`` rewrites the file from a snapshot, so an interleaved
        session record would be collapsed away entirely."""
        import threading

        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        path = util.utilisation_path(tmp_path)
        _write(path, [
            _session(f"old{i}", [_inject("dev.md", "Testing", 100)],
                     self_report=[], days_ago=200)
            for i in range(4)
        ])

        def compacting() -> None:
            util.compact(path, now=NOW)

        def writing() -> None:
            util.record_injections(
                path, "fresh", [_inject("dev.md", "Testing", 100)], transcript="")

        threads = [threading.Thread(target=compacting),
                   threading.Thread(target=writing)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        records = util.read_records(path)
        sessions = {r["session"] for r in records if util.is_session_record(r)}
        assert "fresh" in sessions, "the concurrent session record was collapsed away"


# --- the machine contract ---------------------------------------------------

class TestTableContract:
    def test_shape_is_stable_and_json_serialisable(self):
        table = util.build_table(
            [_session("s1", [_inject("dev.md", "Testing", 100)])],
            known_keys=["dev.md", "dev.md#Testing", "cold.md"])
        json.dumps(table)
        assert set(table) == {
            "schema_version", "generated_at", "disclaimer", "estimator_notes",
            "thresholds", "coverage", "sections", "insufficient_data",
            "never_retrieved", "never_retrieved_sufficient",
            "never_retrieved_reason", "only_as_whole_file",
        }
        assert table["schema_version"] == util.SCHEMA_VERSION
        assert set(table["estimator_notes"]) == {"self_report", "judge"}

    def test_never_retrieved_carries_a_coverage_floor(self):
        """The human renderings caveat this; the machine contract must too.

        ``never_retrieved`` is the surface P5's dead-weight pass consumes
        unattended, and on a fresh install — or after a telemetry gap — it is
        the entire corpus.
        """
        thin = util.build_table(
            [_session("s1", [_inject("dev.md", "Testing", 100)])],
            known_keys=["dev.md", "dev.md#Testing", "cold.md"])
        # `dev.md` bare is in the list too: only its #Testing section was ever
        # injected, so the whole file genuinely never reached a prompt.
        assert "cold.md" in thin["never_retrieved"]
        assert thin["never_retrieved_sufficient"] is False
        assert "1 session" in thin["never_retrieved_reason"]

        thick = util.build_table(
            [_session(f"s{i}", [_inject("dev.md", "Testing", 100)])
             for i in range(util.MIN_COVERAGE_SESSIONS)],
            known_keys=["dev.md", "dev.md#Testing", "cold.md"])
        assert thick["never_retrieved_sufficient"] is True
        assert thick["never_retrieved_reason"] is None

    def test_the_thin_log_caveat_is_rendered_for_a_human_too(self):
        thin = util.build_table(
            [_session("s1", [_inject("dev.md", "Testing", 100)])],
            known_keys=["dev.md", "cold.md"])
        assert "Not enough history to read this list" in util.render_table(thin)

    def test_row_shape_is_stable(self):
        table = util.build_table([
            _session(f"s{i}", [_inject("dev.md", "Testing", 100)], self_report=[])
            for i in range(6)])
        row = table["sections"][0]
        assert set(row) == {
            "key", "file", "section", "retrieved", "sessions", "bytes",
            "bytes_per_injection", "self_report", "judge", "estimated_uses",
            "bytes_per_estimated_use", "zero_estimated_use", "rank_basis",
            "cost_per_use", "sufficient", "disagreement", "estimate",
        }

    def test_load_table_reads_the_on_disk_log(self, tmp_path):
        _write(util.utilisation_path(tmp_path),
               [_session("s1", [_inject("dev.md", "Testing", 100)])])
        table = util.load_table(tmp_path)
        assert table["coverage"]["sessions"] == 1

    def test_empty_corpus_renders_without_inventing_anything(self, tmp_path):
        table = util.load_table(tmp_path)
        assert table["sections"] == []
        rendered = util.render_table(table)
        assert "No section has enough estimator observations" in rendered
        assert "ESTIMATED" in rendered


class TestExtractLearningsWiring:
    """The whole pipeline, offline: activity log in, utilisation.jsonl out.

    This is the "Done means" criterion that nothing else covers — that a real
    session end actually produces a non-empty record, from real ``inject``
    events, without a live model.
    """

    def _run(self, tmp_path, response: str, *, session_id="aaaa1111-full-id",
             activity=True):
        from unittest.mock import AsyncMock, patch

        import extract_learnings as el

        logs = tmp_path / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        if activity:
            (logs / "activity.jsonl").write_text("".join(
                json.dumps(r) + "\n" for r in [
                    {"component": "context", "event": "inject",
                     "session": "aaaa1111", "turn": 1,
                     "sections_by_file": {"multiplai.md": ["Release Flow"]},
                     "bytes_by_file": {"multiplai.md": 6123}},
                    {"component": "context", "event": "inject",
                     "session": "aaaa1111", "turn": 3,
                     "sections_by_file": {"multiplai.md": ["Release Flow"],
                                          "me.md": []},
                     "bytes_by_file": {"multiplai.md": 6123, "me.md": 900}},
                    {"component": "context", "event": "inject",
                     "session": "zzzz9999", "turn": 1,
                     "sections_by_file": {"other.md": []},
                     "bytes_by_file": {"other.md": 10}},
                ]), encoding="utf-8")

        class _Paths:
            def memory_dir(self): return tmp_path / "memory"
            def learnings_file(self): return tmp_path / "learnings.md"
            def diary_dir(self): return tmp_path / "diary"
            def catalogs_dir(self): return tmp_path / "catalogs"
            def data_dir(self): return tmp_path
            def logs_dir(self): return logs

        client = MagicMock()

        async def query(**_kw):
            return MagicMock(content=response)

        client.query = query
        stdin = json.dumps({
            "session_id": session_id, "cwd": "/work",
            "transcript_path": str(tmp_path / "t.jsonl"),
        })
        with patch.object(el, "get_paths", _Paths), \
             patch.object(el, "load_target_charters", lambda *a, **k: []), \
             patch.object(el, "create_client", AsyncMock(return_value=client)), \
             patch.object(el, "_distill_transcript", lambda *a: (["chunk one"], None)), \
             patch.object(el, "write_diary_entries", lambda *a, **k: None), \
             patch.object(el, "append_learnings", lambda *a, **k: True), \
             patch.object(el, "record_disposition", lambda *a, **k: True), \
             patch.object(el.sys, "stdin",
                          type("S", (), {"read": staticmethod(lambda: stdin)})):
            asyncio.run(el.extract())
        return util.read_records(util.utilisation_path(tmp_path))

    RESPONSE = (
        "<unit><timestamp>2026-08-08T10:00:00Z</timestamp>"
        "<diary>\nDid the release.\n</diary></unit>"
        "<disposition>state: done\nreason: shipped</disposition>"
        "<utilisation>"
        '<used file="multiplai.md" section="Release Flow">followed release.sh</used>'
        '<used file="hallucinated.md" section="Nope">invented</used>'
        "</utilisation>"
    )

    def test_end_to_end_writes_both_halves(self, tmp_path):
        records = self._run(tmp_path, self.RESPONSE)
        assert len(records) == 1
        record = records[0]
        assert record["session"] == "aaaa1111"
        assert record["session_id"] == "aaaa1111-full-id"
        assert record["transcript"].endswith("t.jsonl")

        # Only this session's events, both turns, whole-file me.md included.
        assert sorted(
            (e["file"], e["section"], e["bytes"], e["turn"])
            for e in record["injected"]
        ) == [
            ("me.md", None, 900, 3),
            ("multiplai.md", "Release Flow", 6123, 1),
            ("multiplai.md", "Release Flow", 6123, 3),
        ]

        # The invented section is dropped; the real one survives with evidence.
        assert record["self_report"] == [
            {"file": "multiplai.md", "section": "Release Flow",
             "evidence": "followed release.sh", "supported": True},
        ]
        assert record["judge"] is None

        agg = util.aggregate(records)
        stat = agg.sections["multiplai.md#Release Flow"]
        assert (stat.retrieved, stat.self_report_observed, stat.self_report_used) == (2, 1, 1)
        assert agg.sections["me.md"].self_report_used == 0

    def test_the_prompt_carried_the_injected_list(self, tmp_path):
        """Estimator A is worthless if the model is not told what it had."""
        from unittest.mock import AsyncMock, patch

        import extract_learnings as el

        seen = {}

        async def fake(chunk, **kwargs):
            seen["injected"] = kwargs.get("injected_sections")
            return [], {"state": "active", "reason": ""}, []

        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "activity.jsonl").write_text(json.dumps({
            "component": "context", "event": "inject", "session": "aaaa1111",
            "sections_by_file": {"dev.md": ["Testing"]},
            "bytes_by_file": {"dev.md": 10}}) + "\n", encoding="utf-8")

        class _Paths:
            def memory_dir(self): return tmp_path / "memory"
            def learnings_file(self): return tmp_path / "learnings.md"
            def diary_dir(self): return tmp_path / "diary"
            def catalogs_dir(self): return tmp_path / "catalogs"
            def data_dir(self): return tmp_path
            def logs_dir(self): return logs

        stdin = json.dumps({"session_id": "aaaa1111", "cwd": "/work"})
        with patch.object(el, "get_paths", _Paths), \
             patch.object(el, "load_target_charters", lambda *a, **k: []), \
             patch.object(el, "create_client", AsyncMock(return_value=object())), \
             patch.object(el, "_distill_transcript", lambda *a: (["c"], None)), \
             patch.object(el, "extract_session_signals", fake), \
             patch.object(el.sys, "stdin",
                          type("S", (), {"read": staticmethod(lambda: stdin)})):
            asyncio.run(el.extract())
        assert seen["injected"] == ["dev.md#Testing"]

    def test_no_inject_events_writes_nothing(self, tmp_path):
        assert self._run(tmp_path, self.RESPONSE, activity=False) == []

    def test_a_dead_model_still_records_the_retrieval_half(self, tmp_path):
        """Contract C3: no estimate without a client, but the facts survive."""
        from unittest.mock import AsyncMock, patch

        import extract_learnings as el

        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "activity.jsonl").write_text(json.dumps({
            "component": "context", "event": "inject", "session": "aaaa1111",
            "sections_by_file": {"dev.md": ["Testing"]},
            "bytes_by_file": {"dev.md": 10}}) + "\n", encoding="utf-8")

        class _Paths:
            def memory_dir(self): return tmp_path / "memory"
            def learnings_file(self): return tmp_path / "learnings.md"
            def diary_dir(self): return tmp_path / "diary"
            def catalogs_dir(self): return tmp_path / "catalogs"
            def data_dir(self): return tmp_path
            def logs_dir(self): return logs

        stdin = json.dumps({"session_id": "aaaa1111", "cwd": "/work"})
        with patch.object(el, "get_paths", _Paths), \
             patch.object(el, "load_target_charters", lambda *a, **k: []), \
             patch.object(el, "create_client",
                          AsyncMock(side_effect=RuntimeError("no client"))), \
             patch.object(el, "_distill_transcript", lambda *a: (["c"], None)), \
             patch.object(el.sys, "stdin",
                          type("S", (), {"read": staticmethod(lambda: stdin)})):
            asyncio.run(el.extract())

        records = util.read_records(util.utilisation_path(tmp_path))
        assert len(records) == 1
        assert records[0]["injected"]
        assert records[0]["self_report"] is None, \
            "no model means no estimate — never a fabricated empty one"


class TestMaintainerPasses:
    def test_compaction_pass_is_a_no_op_without_a_log(self, tmp_path):
        import memory_maintainer as mm

        result = mm.run_utilisation_compact(tmp_path)
        assert result.ran is False and "no utilisation log" in result.detail

    def test_compaction_pass_collapses_old_records(self, tmp_path):
        import memory_maintainer as mm

        _write(util.utilisation_path(tmp_path), [
            _session("old", [_inject("dev.md", "Testing", 100)], days_ago=200),
        ])
        result = mm.run_utilisation_compact(tmp_path)
        assert result.ran is True and "collapsed 1" in result.detail

    def test_judge_pass_is_skipped_when_nothing_is_eligible(self, tmp_path):
        import memory_maintainer as mm

        result = mm.run_utilisation_judge(tmp_path)
        assert result.ran is False
        assert "awaiting" in result.detail

    def test_judge_pass_degrades_when_no_client_can_be_made(self, tmp_path, monkeypatch):
        """Contract C3 — records nothing, never guesses, never crashes."""
        import multiplai_core.model_client as mc

        import memory_maintainer as mm

        _write(util.utilisation_path(tmp_path),
               [_session("s1", [_inject("dev.md", "Testing", 100)])])

        async def boom(**_kw):
            raise RuntimeError("neither Agent SDK nor API key is available")

        monkeypatch.setattr(mc, "create_client", boom)
        result = mm.run_utilisation_judge(tmp_path)
        assert result.ran is False
        assert "error" in result.detail
        assert all(r["judge"] is None
                   for r in util.read_records(util.utilisation_path(tmp_path)))

    def test_judge_pass_can_be_turned_off(self, tmp_path, monkeypatch):
        from multiplai_core.plugin_options import option_var

        import memory_maintainer as mm

        monkeypatch.setenv(option_var("utilisation_judge_sample"), "0")
        result = mm.run_utilisation_judge(tmp_path)
        assert result.ran is False and "disabled" in result.detail

    def test_dry_run_writes_nothing(self, tmp_path):
        import memory_maintainer as mm

        path = _write(util.utilisation_path(tmp_path), [
            _session("old", [_inject("dev.md", "Testing", 100)], days_ago=200),
        ])
        before = path.read_text(encoding="utf-8")
        assert mm.run_utilisation_compact(tmp_path, dry_run=True).ran is True
        assert mm.run_utilisation_judge(tmp_path, dry_run=True).ran is True
        assert path.read_text(encoding="utf-8") == before

    def test_both_passes_are_registered(self):
        import inspect

        import memory_maintainer as mm

        source = inspect.getsource(mm.run_maintenance)
        assert "run_utilisation_compact" in source
        assert "run_utilisation_judge" in source


class TestSectionKeys:
    @pytest.mark.parametrize("file,section,key", [
        ("dev.md", "Testing", "dev.md#Testing"),
        ("dev.md", None, "dev.md"),
        ("dev.md", "", "dev.md"),
    ])
    def test_round_trip(self, file, section, key):
        assert util.section_key(file, section) == key
        assert util.split_key(key) == (file, section or None)


class TestJudgeThinking:
    """The judge call is mechanical verdict extraction: it carries the
    thinking config resolved from ``utilisation_thinking`` (default:
    disabled — see lib/thinking.py)."""

    def _run(self, monkeypatch, *, supported):
        import lib.thinking as th
        from multiplai_core.plugin_options import option_var

        monkeypatch.setattr(
            th, "core_supports_thinking", lambda target=None: supported
        )
        monkeypatch.delenv(option_var(th.UTILISATION_THINKING_OPTION), raising=False)
        monkeypatch.setattr(judge, "distilled_transcript", lambda *a, **k: "text")

        captured = {}

        async def query(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content="<verdicts></verdicts>")

        client = MagicMock()
        client.query = query
        record = _session("s1", [_inject("dev.md", "Testing", 100)],
                          transcript="/transcripts/s1.jsonl")
        asyncio.run(judge.judge_one_detailed(client, record, model="m"))
        assert captured, "the model path must have been taken"
        return captured

    def test_judge_call_receives_thinking_disabled_by_default(self, monkeypatch):
        assert self._run(monkeypatch, supported=True)["thinking"] == {
            "type": "disabled"
        }

    def test_keyword_omitted_entirely_when_unsupported(self, monkeypatch):
        """Routed through thinking_kwargs, so an unsupported dependency is
        handed no keyword rather than `thinking=None`."""
        assert "thinking" not in self._run(monkeypatch, supported=False)
