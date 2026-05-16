"""Tests for lib/transcript_distiller.py.

Covers:
- iter_distilled_turns: keeps user/assistant, drops other types
- iter_distilled_turns: elides tool_use and tool_result blocks
- iter_distilled_turns: drops thinking blocks
- iter_distilled_turns: drops base64 content
- iter_distilled_turns: window filtering by since/until
- iter_distilled_turns: derives project from cwd
- distill: chunks output within token budget
- distill: returns [] for empty/missing file
- distill: truncates single oversized turns
- estimate_tokens: returns a positive integer for a non-empty file
"""

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _ts(offset_minutes: int = 0) -> str:
    dt = datetime(2026, 5, 16, 14, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=offset_minutes)
    return dt.isoformat()


def _user_record(text: str, *, cwd: str = "/work/proj", offset: int = 0) -> dict:
    return {"type": "user", "role": "user", "content": text, "cwd": cwd, "timestamp": _ts(offset)}


def _assistant_record(text: str, *, offset: int = 0) -> dict:
    return {"type": "assistant", "role": "assistant", "content": text, "timestamp": _ts(offset)}


# ---------------------------------------------------------------------------
# iter_distilled_turns
# ---------------------------------------------------------------------------

class TestIterDistilledTurns:
    def test_keeps_user_and_assistant(self, tmp_path):
        from lib.transcript_distiller import iter_distilled_turns
        f = tmp_path / "t.jsonl"
        _write_jsonl(f, [_user_record("hello"), _assistant_record("world")])
        turns = list(iter_distilled_turns(f))
        assert len(turns) == 2
        roles = {t["role"] for t in turns}
        assert roles == {"user", "assistant"}

    def test_drops_non_message_types(self, tmp_path):
        from lib.transcript_distiller import iter_distilled_turns
        f = tmp_path / "t.jsonl"
        _write_jsonl(f, [
            {"type": "summary", "content": "noise"},
            {"type": "attachment", "content": "noise"},
            _user_record("real"),
        ])
        turns = list(iter_distilled_turns(f))
        assert len(turns) == 1
        assert turns[0]["text"] == "real"

    def test_elides_tool_use_block(self, tmp_path):
        from lib.transcript_distiller import iter_distilled_turns
        content = [
            {"type": "text", "text": "before"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/foo.py"}},
        ]
        f = tmp_path / "t.jsonl"
        _write_jsonl(f, [{"type": "assistant", "role": "assistant", "content": content, "timestamp": _ts()}])
        turns = list(iter_distilled_turns(f))
        assert len(turns) == 1
        assert "before" in turns[0]["text"]
        assert "Read" in turns[0]["text"]
        assert "call Read" in turns[0]["text"]

    def test_elides_tool_result_block(self, tmp_path):
        from lib.transcript_distiller import iter_distilled_turns
        content = [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "x" * 500}
        ]
        f = tmp_path / "t.jsonl"
        _write_jsonl(f, [{"type": "user", "role": "user", "content": content, "timestamp": _ts()}])
        turns = list(iter_distilled_turns(f))
        assert len(turns) == 1
        assert "toolu_1" in turns[0]["text"]
        assert "x" * 500 not in turns[0]["text"]  # content was elided

    def test_drops_thinking_blocks(self, tmp_path):
        from lib.transcript_distiller import iter_distilled_turns
        content = [
            {"type": "thinking", "thinking": "deep thought"},
            {"type": "text", "text": "answer"},
        ]
        f = tmp_path / "t.jsonl"
        _write_jsonl(f, [{"type": "assistant", "role": "assistant", "content": content, "timestamp": _ts()}])
        turns = list(iter_distilled_turns(f))
        assert len(turns) == 1
        assert "deep thought" not in turns[0]["text"]
        assert "answer" in turns[0]["text"]

    def test_drops_base64_content(self, tmp_path):
        from lib.transcript_distiller import iter_distilled_turns
        b64 = "A" * 120 + "=="
        content = [{"type": "text", "text": b64}]
        f = tmp_path / "t.jsonl"
        _write_jsonl(f, [{"type": "user", "role": "user", "content": content, "timestamp": _ts()}])
        turns = list(iter_distilled_turns(f))
        assert len(turns) == 1
        assert "elided" in turns[0]["text"]
        assert b64 not in turns[0]["text"]

    def test_window_filter_since(self, tmp_path):
        from lib.transcript_distiller import iter_distilled_turns
        f = tmp_path / "t.jsonl"
        _write_jsonl(f, [
            _user_record("old", offset=-60),
            _user_record("new", offset=10),
        ])
        since = datetime(2026, 5, 16, 14, 5, tzinfo=timezone.utc)
        turns = list(iter_distilled_turns(f, since=since))
        assert len(turns) == 1
        assert turns[0]["text"] == "new"

    def test_window_filter_until(self, tmp_path):
        from lib.transcript_distiller import iter_distilled_turns
        f = tmp_path / "t.jsonl"
        _write_jsonl(f, [
            _user_record("early", offset=0),
            _user_record("late", offset=60),
        ])
        until = datetime(2026, 5, 16, 14, 30, tzinfo=timezone.utc)
        turns = list(iter_distilled_turns(f, until=until))
        assert len(turns) == 1
        assert turns[0]["text"] == "early"

    def test_derives_project_from_cwd(self, tmp_path):
        from lib.transcript_distiller import iter_distilled_turns
        f = tmp_path / "t.jsonl"
        _write_jsonl(f, [_user_record("x", cwd="/Users/spike/Documents/knowhere/PROJECTS/multiplai-plugin")])
        turns = list(iter_distilled_turns(f))
        assert turns[0]["project"] == "multiplai-plugin"

    def test_returns_empty_for_missing_file(self, tmp_path):
        from lib.transcript_distiller import iter_distilled_turns
        turns = list(iter_distilled_turns(tmp_path / "nonexistent.jsonl"))
        assert turns == []

    def test_skips_malformed_lines(self, tmp_path):
        from lib.transcript_distiller import iter_distilled_turns
        f = tmp_path / "t.jsonl"
        f.write_text("{bad json\n" + json.dumps(_user_record("good")) + "\n")
        turns = list(iter_distilled_turns(f))
        assert len(turns) == 1
        assert turns[0]["text"] == "good"


# ---------------------------------------------------------------------------
# distill
# ---------------------------------------------------------------------------

class TestDistill:
    def test_returns_list_of_strings(self, tmp_path):
        from lib.transcript_distiller import distill
        f = tmp_path / "t.jsonl"
        _write_jsonl(f, [_user_record("hello"), _assistant_record("world")])
        chunks = distill(f)
        assert isinstance(chunks, list)
        assert all(isinstance(c, str) for c in chunks)

    def test_content_included(self, tmp_path):
        from lib.transcript_distiller import distill
        f = tmp_path / "t.jsonl"
        _write_jsonl(f, [_user_record("hello world")])
        chunks = distill(f)
        combined = "\n".join(chunks)
        assert "hello world" in combined

    def test_chunks_within_budget(self, tmp_path):
        from lib.transcript_distiller import distill
        f = tmp_path / "t.jsonl"
        records = [_user_record("x " * 50, offset=i) for i in range(20)]
        _write_jsonl(f, records)
        token_budget = 200
        chunks = distill(f, token_budget=token_budget)
        char_budget = token_budget * 4
        for chunk in chunks:
            assert len(chunk) <= char_budget * 2  # allow single-turn headroom

    def test_returns_empty_for_missing_file(self, tmp_path):
        from lib.transcript_distiller import distill
        chunks = distill(tmp_path / "missing.jsonl")
        assert chunks == []

    def test_truncates_oversized_turn(self, tmp_path):
        from lib.transcript_distiller import distill
        f = tmp_path / "t.jsonl"
        _write_jsonl(f, [_user_record("a" * 50_000)])
        chunks = distill(f, token_budget=100)
        assert len(chunks) == 1
        # Should be truncated, not a 50KB chunk
        assert len(chunks[0]) < 10_000


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_returns_positive_for_nonempty_file(self, tmp_path):
        from lib.transcript_distiller import estimate_tokens
        f = tmp_path / "t.jsonl"
        _write_jsonl(f, [_user_record("hello")])
        est = estimate_tokens(f)
        assert est >= 1

    def test_returns_zero_for_missing_file(self, tmp_path):
        from lib.transcript_distiller import estimate_tokens
        est = estimate_tokens(tmp_path / "missing.jsonl")
        assert est == 0
