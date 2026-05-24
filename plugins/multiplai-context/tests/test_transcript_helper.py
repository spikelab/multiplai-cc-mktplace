"""Tests for scripts/lib/transcript_helper.py — last-assistant-response extraction."""

import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# read_last_assistant_response
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


class TestReadLastAssistantResponse:
    def test_none_path_returns_none(self):
        from lib.transcript_helper import read_last_assistant_response
        assert read_last_assistant_response(None) is None

    def test_empty_string_path_returns_none(self):
        from lib.transcript_helper import read_last_assistant_response
        assert read_last_assistant_response("") is None

    def test_missing_file_returns_none(self, tmp_path):
        from lib.transcript_helper import read_last_assistant_response
        assert read_last_assistant_response(tmp_path / "ghost.jsonl") is None

    def test_directory_path_returns_none(self, tmp_path):
        from lib.transcript_helper import read_last_assistant_response
        assert read_last_assistant_response(tmp_path) is None

    def test_empty_file_returns_none(self, tmp_path):
        from lib.transcript_helper import read_last_assistant_response
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        assert read_last_assistant_response(path) is None

    def test_extracts_string_content_from_raw_shape(self, tmp_path):
        """Raw {role, content} shape with string content."""
        from lib.transcript_helper import read_last_assistant_response
        path = tmp_path / "t.jsonl"
        _write_jsonl(path, [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "Hello world"},
        ])
        assert read_last_assistant_response(path) == "Hello world"

    def test_extracts_blocks_from_raw_shape(self, tmp_path):
        """Raw shape with block-list content."""
        from lib.transcript_helper import read_last_assistant_response
        path = tmp_path / "t.jsonl"
        _write_jsonl(path, [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "First chunk."},
                    {"type": "tool_use", "id": "abc"},
                    {"type": "text", "text": "Second chunk."},
                ],
            },
        ])
        result = read_last_assistant_response(path)
        assert result is not None
        assert "First chunk." in result
        assert "Second chunk." in result

    def test_extracts_from_sdk_shape(self, tmp_path):
        """SDK shape: {type: 'assistant', message: {content: [...]}}."""
        from lib.transcript_helper import read_last_assistant_response
        path = tmp_path / "t.jsonl"
        _write_jsonl(path, [
            {"type": "user", "message": {"content": "hi"}},
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Reply via SDK"}],
                },
            },
        ])
        assert read_last_assistant_response(path) == "Reply via SDK"

    def test_returns_most_recent_when_multiple_assistant_turns(self, tmp_path):
        from lib.transcript_helper import read_last_assistant_response
        path = tmp_path / "t.jsonl"
        _write_jsonl(path, [
            {"role": "assistant", "content": "first turn"},
            {"role": "user", "content": "follow-up"},
            {"role": "assistant", "content": "second turn"},
        ])
        assert read_last_assistant_response(path) == "second turn"

    def test_no_assistant_turn_returns_none(self, tmp_path):
        from lib.transcript_helper import read_last_assistant_response
        path = tmp_path / "t.jsonl"
        _write_jsonl(path, [
            {"role": "user", "content": "alone"},
        ])
        assert read_last_assistant_response(path) is None

    def test_malformed_jsonl_lines_skipped(self, tmp_path):
        from lib.transcript_helper import read_last_assistant_response
        path = tmp_path / "t.jsonl"
        path.write_text(
            "{not json at all\n"
            + json.dumps({"role": "assistant", "content": "Recovered"}) + "\n",
            encoding="utf-8",
        )
        assert read_last_assistant_response(path) == "Recovered"

    def test_handles_large_file_via_tail_read(self, tmp_path):
        """File larger than tail buffer still finds the most recent turn."""
        from lib.transcript_helper import read_last_assistant_response
        path = tmp_path / "big.jsonl"
        # Pad with many user turns + a single trailing assistant turn.
        records = [{"role": "user", "content": "x" * 1000} for _ in range(200)]
        records.append({"role": "assistant", "content": "Trailing reply"})
        _write_jsonl(path, records)
        assert path.stat().st_size > 100_000
        assert read_last_assistant_response(path) == "Trailing reply"

    def test_empty_assistant_content_returns_none(self, tmp_path):
        from lib.transcript_helper import read_last_assistant_response
        path = tmp_path / "t.jsonl"
        _write_jsonl(path, [{"role": "assistant", "content": "   "}])
        assert read_last_assistant_response(path) is None

    def test_assistant_blocks_with_no_text_returns_none(self, tmp_path):
        from lib.transcript_helper import read_last_assistant_response
        path = tmp_path / "t.jsonl"
        _write_jsonl(path, [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "x"}],
            },
        ])
        assert read_last_assistant_response(path) is None
