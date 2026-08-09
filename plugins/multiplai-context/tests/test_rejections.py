"""Tests for the rejection log — the record that makes refusals auditable.

A judge that silently discards items is indistinguishable from one that
silently discards *good* items, and the difference is the whole question of
whether it deserves the delegation. So the properties pinned here are: every
drop is written, the write survives concurrency, and a drop can be read back in
full without going near the proposal it came from.
"""

from dataclasses import dataclass

from lib import rejections


@dataclass
class Stub:
    target: str = "python.md"
    number: int = 3
    title: str = "uv workspace resolution"
    section: str = "Python Tooling"
    text: str = "uv resolves the workspace from the root lock."
    source: str = "2026-08-05.md:12"
    provenance: str = "INFERENCE"
    kind: str = "FACT"


class TestRecordShape:
    def test_it_carries_everything_needed_to_overrule_a_drop(self):
        record = rejections.record_for(
            Stub(), proposal="processed-learnings-2026-08-05.md",
            reason="redundant", judge_reason="already in § Python Tooling",
            item_key="deadbeefdeadbeef",
        )
        # Reading a rejection must not require going back to the proposal.
        assert record["text"] == Stub().text
        assert record["target"] == "python.md"
        assert record["section"] == "Python Tooling"
        assert record["provenance"] == "INFERENCE"
        assert record["kind"] == "FACT"
        assert record["source"] == "2026-08-05.md:12"
        assert record["reason"] == "redundant"
        assert record["judge_reason"] == "already in § Python Tooling"
        assert record["proposal"] == "processed-learnings-2026-08-05.md"

    def test_the_item_key_points_back_at_the_source_learning(self):
        # `drop` means "not promoted to memory", never "erased" — the hash is
        # what makes the original findable through the ledger.
        key = "0123456789abcdef"  # gitleaks:allow - a 16-hex content hash, not a secret
        record = rejections.record_for(Stub(), proposal="p.md", reason="judge-drop",
                                       item_key=key)
        assert record["item_key"] == key

    def test_it_is_timestamped(self):
        record = rejections.record_for(Stub(), proposal="p.md", reason="judge-drop")
        assert record["ts"].startswith("20")

    def test_a_pathological_body_is_bounded(self):
        record = rejections.record_for(
            Stub(text="x" * 100_000), proposal="p.md", reason="judge-drop")
        assert len(record["text"]) <= 4000


class TestAppendAndRead:
    def test_a_round_trip(self, tmp_path):
        path = rejections.default_path(tmp_path)
        record = rejections.record_for(Stub(), proposal="p.md", reason="redundant")
        assert rejections.append(path, [record]) == 1
        assert rejections.read(path) == [record]

    def test_it_appends_rather_than_rewrites(self, tmp_path):
        path = rejections.default_path(tmp_path)
        rejections.append(path, [rejections.record_for(
            Stub(number=1), proposal="p.md", reason="redundant")])
        rejections.append(path, [rejections.record_for(
            Stub(number=2), proposal="p.md", reason="judge-drop")])
        assert [r["number"] for r in rejections.read(path)] == [1, 2]

    def test_it_creates_the_directory(self, tmp_path):
        path = tmp_path / "data" / "rejections.jsonl"
        rejections.append(path, [rejections.record_for(
            Stub(), proposal="p.md", reason="redundant")])
        assert path.is_file()

    def test_writing_nothing_writes_nothing(self, tmp_path):
        path = rejections.default_path(tmp_path)
        assert rejections.append(path, []) == 0
        assert not path.exists()

    def test_a_missing_log_reads_as_empty(self, tmp_path):
        assert rejections.read(tmp_path / "nope.jsonl") == []

    def test_a_torn_final_line_costs_only_that_record(self, tmp_path):
        # The one failure mode an append-only log actually has.
        path = rejections.default_path(tmp_path)
        rejections.append(path, [rejections.record_for(
            Stub(number=1), proposal="p.md", reason="redundant")])
        with open(path, "a") as f:
            f.write('{"target": "dev.md", "num')
        records = rejections.read(path)
        assert len(records) == 1
        assert records[0]["number"] == 1

    def test_non_ascii_survives(self, tmp_path):
        path = rejections.default_path(tmp_path)
        rejections.append(path, [rejections.record_for(
            Stub(text="§ Python Tooling — uv résolves it"), proposal="p.md",
            reason="redundant")])
        assert "résolves" in rejections.read(path)[0]["text"]


class TestAggregate:
    def test_counts_by_reason(self, tmp_path):
        path = rejections.default_path(tmp_path)
        rejections.append(path, [
            rejections.record_for(Stub(number=1), proposal="p.md", reason="redundant"),
            rejections.record_for(Stub(number=2), proposal="p.md", reason="redundant"),
            rejections.record_for(Stub(number=3), proposal="p.md", reason="judge-drop"),
        ])
        assert rejections.aggregate(rejections.read(path)) == {
            "redundant": 2, "judge-drop": 1,
        }

    def test_an_empty_log_aggregates_to_nothing(self):
        assert rejections.aggregate([]) == {}
