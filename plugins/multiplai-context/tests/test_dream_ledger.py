"""Tests for lib/dream_ledger.py — the record of what dream already consolidated.

The ledger is what lets dream process only what is new without moving learnings
files out of the way. Two properties carry the whole design and are asserted
hardest here: keys survive a whitespace-only reformat (or a stray editor save
re-processes a whole file), and block line numbers are the ORIGINAL 1-indexed
ones (dream's `**Source:**` provenance cites them).
"""

import json

from lib.dream_ledger import (
    Block,
    block_key,
    load,
    parse_blocks,
    prune,
    record,
    save,
    unprocessed,
)

LEARNINGS = """\
---
## Session Learnings — 2026-07-30T19:41:34.088000+00:00
Session: 2553c72b-9614-4359-acc0-0ea1a0477b69
- **[trust: verified]** PATTERN First lesson. → Target: dev.md — do the thing.

---
## Session Learnings — 2026-07-30T20:08:58.668000+00:00
Session: 2553c72b-9614-4359-acc0-0ea1a0477b69
- **[trust: high]** OBSERVATION Second lesson. → Target: life.md — note it.
- **[trust: high]** OBSERVATION Third lesson, same record.

---
"""


class TestParseBlocks:
    def test_finds_each_record(self):
        blocks = parse_blocks("2026-07-30.md", LEARNINGS)
        assert len(blocks) == 2

    def test_line_numbers_are_original_and_inclusive(self):
        blocks = parse_blocks("2026-07-30.md", LEARNINGS)
        lines = LEARNINGS.splitlines()
        # The heading line reported for block 1 really is a heading.
        assert lines[blocks[0].start_line - 1].startswith("## Session Learnings")
        assert lines[blocks[1].start_line - 1].startswith("## Session Learnings")
        # end_line is inclusive and lands on content, not on the separator or a blank.
        assert lines[blocks[0].end_line - 1].startswith("- **[trust: verified]**")
        assert lines[blocks[1].end_line - 1].startswith("- **[trust: high]** OBSERVATION Third")

    def test_blocks_do_not_overlap_or_include_separator(self):
        blocks = parse_blocks("2026-07-30.md", LEARNINGS)
        assert blocks[0].end_line < blocks[1].start_line
        assert "---" not in blocks[0].text

    def test_preamble_before_first_heading_is_dropped(self):
        blocks = parse_blocks("f.md", LEARNINGS)
        assert blocks[0].start_line > 1
        assert not blocks[0].text.startswith("---")

    def test_no_headings_yields_nothing(self):
        assert parse_blocks("f.md", "just some prose\nand more\n") == []

    def test_file_name_is_carried(self):
        assert all(b.file == "2026-07-30.md" for b in parse_blocks("2026-07-30.md", LEARNINGS))


class TestBlockKey:
    def test_stable_across_trailing_whitespace_reformat(self):
        original = parse_blocks("f.md", LEARNINGS)
        reformatted = parse_blocks(
            "f.md", "\n".join(l + "   " for l in LEARNINGS.splitlines())
        )
        assert [b.key for b in original] == [b.key for b in reformatted]

    def test_content_change_changes_the_key(self):
        a = block_key("## Session Learnings — x\n- one")
        b = block_key("## Session Learnings — x\n- two")
        assert a != b

    def test_independent_of_source_file(self):
        """Same record text in two files is the same learning — dedup, don't duplicate."""
        text = "## Session Learnings — x\n- one"
        assert parse_blocks("a.md", text)[0].key == parse_blocks("b.md", text)[0].key


class TestUnprocessed:
    def test_excludes_recorded_keys(self):
        blocks = parse_blocks("f.md", LEARNINGS)
        ledger = {"version": 1, "processed": {blocks[0].key: {"file": "f.md"}}}
        assert [b.key for b in unprocessed(blocks, ledger)] == [blocks[1].key]

    def test_empty_ledger_passes_everything_through(self):
        blocks = parse_blocks("f.md", LEARNINGS)
        assert len(unprocessed(blocks, {"processed": {}})) == len(blocks)

    def test_deduplicates_within_the_batch(self):
        blocks = parse_blocks("a.md", LEARNINGS) + parse_blocks("b.md", LEARNINGS)
        assert len(blocks) == 4
        assert len(unprocessed(blocks, {"processed": {}})) == 2

    def test_preserves_input_order(self):
        blocks = parse_blocks("f.md", LEARNINGS)
        assert [b.key for b in unprocessed(blocks, {"processed": {}})] == [
            b.key for b in blocks
        ]


class TestLoadSaveRecord:
    def test_missing_file_loads_empty(self, tmp_path):
        assert load(tmp_path / "nope.json")["processed"] == {}

    def test_corrupt_file_degrades_to_empty(self, tmp_path):
        p = tmp_path / "ledger.json"
        p.write_text("{not json")
        assert load(p)["processed"] == {}

    def test_wrong_shape_degrades_to_empty(self, tmp_path):
        p = tmp_path / "ledger.json"
        p.write_text('{"processed": ["a", "b"]}')
        assert load(p)["processed"] == {}

    def test_record_then_load_round_trip(self, tmp_path):
        p = tmp_path / "ledger.json"
        blocks = parse_blocks("f.md", LEARNINGS)
        assert record(p, blocks, "processed-learnings-2026-07-31.md") == 2
        ledger = load(p)
        assert set(ledger["processed"]) == {b.key for b in blocks}
        entry = ledger["processed"][blocks[0].key]
        assert entry["file"] == "f.md"
        assert entry["proposal"] == "processed-learnings-2026-07-31.md"
        assert entry["at"].endswith("Z")

    def test_record_is_idempotent(self, tmp_path):
        p = tmp_path / "ledger.json"
        blocks = parse_blocks("f.md", LEARNINGS)
        assert record(p, blocks, "prop.md") == 2
        assert record(p, blocks, "prop.md") == 0
        assert len(load(p)["processed"]) == 2

    def test_record_accumulates_across_chunks(self, tmp_path):
        """Per-chunk recording is what makes a killed run resumable."""
        p = tmp_path / "ledger.json"
        blocks = parse_blocks("f.md", LEARNINGS)
        record(p, [blocks[0]], "prop.md")
        record(p, [blocks[1]], "prop.md")
        assert len(load(p)["processed"]) == 2

    def test_record_of_nothing_writes_nothing(self, tmp_path):
        p = tmp_path / "ledger.json"
        assert record(p, [], "prop.md") == 0
        assert not p.exists()

    def test_save_leaves_no_temp_files(self, tmp_path):
        p = tmp_path / "sub" / "ledger.json"
        save(p, {"version": 1, "processed": {}})
        assert p.is_file()
        assert [f.name for f in p.parent.iterdir()] == ["ledger.json"]

    def test_save_output_is_valid_json(self, tmp_path):
        p = tmp_path / "ledger.json"
        save(p, {"version": 1, "processed": {"abc": {"file": "f.md"}}})
        assert json.loads(p.read_text())["processed"]["abc"]["file"] == "f.md"


class TestPrune:
    def test_drops_keys_for_deleted_files(self, tmp_path):
        p = tmp_path / "ledger.json"
        record(p, parse_blocks("gone.md", LEARNINGS), "prop.md")
        record(p, [Block("here.md", 1, 2, "x", "deadbeefdeadbeef")], "prop.md")
        assert prune(p, {"here.md"}) == 2
        assert set(load(p)["processed"]) == {"deadbeefdeadbeef"}

    def test_keeps_everything_when_all_files_present(self, tmp_path):
        p = tmp_path / "ledger.json"
        record(p, parse_blocks("f.md", LEARNINGS), "prop.md")
        assert prune(p, {"f.md"}) == 0
        assert len(load(p)["processed"]) == 2

    def test_prune_of_empty_ledger_is_a_noop(self, tmp_path):
        assert prune(tmp_path / "ledger.json", {"f.md"}) == 0
