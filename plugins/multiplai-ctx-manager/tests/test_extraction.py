"""Tests for lib/extraction.py — diary-first extraction shared library.

Covers:
- extract_units() delegates to LLM and parses response
- write_diary_entries() writes canonical diary/YYYY-MM-DD/<sid>.md
- write_diary_entries() header format (3 brackets on line 1)
- write_diary_entries() idempotency
- write_diary_entries() returns None when no diary content
- append_learnings() atomic write with flock + Session: dedup
- append_learnings() skips if session already present
- append_learnings() writes correction entries
- EXTRACTION_PROMPT is diary-first (not a one-liner constraint)
"""

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_client(content: str):
    from lib.model_client import ModelResponse
    client = AsyncMock()
    client.query = AsyncMock(return_value=ModelResponse(content=content))
    return client


def _sample_units():
    return [
        {
            "timestamp": "2026-05-16T14:00:00+00:00",
            "diary_entry": "Implemented extraction refactor. Moved LLM call into lib/extraction.py for reuse. Decision: diary-first because it is the source of truth; learnings are a projection of it.",
            "learnings": [
                {
                    "trust": "high",
                    "type": "PATTERN",
                    "description": "Diary-first extraction avoids discarding narrative context",
                    "target": "technical-pref.md",
                    "action": "Note that diary_entry is primary output",
                }
            ],
        },
        {
            "timestamp": "2026-05-16T15:00:00+00:00",
            "diary_entry": "Fixed synthesize_now to recurse day directories.",
            "learnings": [],
        },
    ]


# ---------------------------------------------------------------------------
# EXTRACTION_PROMPT structure
# ---------------------------------------------------------------------------

class TestExtractionPrompt:
    """EXTRACTION_PROMPT must be diary-first."""

    def test_prompt_imported(self):
        from lib.extraction import EXTRACTION_PROMPT
        assert EXTRACTION_PROMPT

    def test_prompt_asks_for_diary_entry_field(self):
        from lib.extraction import EXTRACTION_PROMPT
        assert "diary_entry" in EXTRACTION_PROMPT

    def test_prompt_no_max_chars_constraint_on_diary(self):
        """Old prompt had 'max 300 chars' — new one must not."""
        from lib.extraction import EXTRACTION_PROMPT
        assert "max 300 chars" not in EXTRACTION_PROMPT

    def test_prompt_has_valid_targets_placeholder(self):
        from lib.extraction import EXTRACTION_PROMPT
        assert "{valid_targets}" in EXTRACTION_PROMPT

    def test_prompt_has_transcript_placeholder(self):
        from lib.extraction import EXTRACTION_PROMPT
        assert "{transcript}" in EXTRACTION_PROMPT


# ---------------------------------------------------------------------------
# extract_units
# ---------------------------------------------------------------------------

class TestExtractUnits:
    def test_returns_units_on_valid_response(self):
        from lib.extraction import extract_units
        units_json = json.dumps({
            "units": [
                {"timestamp": "2026-05-16T14:00:00Z", "diary_entry": "Did X.", "learnings": []}
            ]
        })
        client = _make_mock_client(units_json)
        result = asyncio.run(extract_units("some transcript", valid_targets=["technical-pref.md"], client=client))
        assert len(result) == 1
        assert result[0]["diary_entry"] == "Did X."

    def test_calls_client_query(self):
        from lib.extraction import extract_units
        client = _make_mock_client(json.dumps({"units": []}))
        asyncio.run(extract_units("transcript", valid_targets=[], client=client))
        client.query.assert_awaited_once()

    def test_passes_system_prompt(self):
        from lib.extraction import extract_units
        client = _make_mock_client(json.dumps({"units": []}))
        asyncio.run(extract_units("t", valid_targets=[], client=client))
        call_kwargs = client.query.call_args
        assert call_kwargs.kwargs.get("system") or (call_kwargs.args and "system" in str(call_kwargs))

    def test_returns_empty_on_invalid_json(self):
        from lib.extraction import extract_units
        client = _make_mock_client("not json at all")
        result = asyncio.run(extract_units("t", valid_targets=[], client=client))
        assert result == []

    def test_tolerates_fenced_code_block(self):
        from lib.extraction import extract_units
        fenced = "```json\n" + json.dumps({"units": [{"timestamp": "", "diary_entry": "x", "learnings": []}]}) + "\n```"
        client = _make_mock_client(fenced)
        result = asyncio.run(extract_units("t", valid_targets=[], client=client))
        assert len(result) == 1


# ---------------------------------------------------------------------------
# write_diary_entries
# ---------------------------------------------------------------------------

class TestWriteDiaryEntries:
    def test_writes_to_day_dir(self, tmp_path):
        from lib.extraction import write_diary_entries
        units = _sample_units()
        ts = "2026-05-16T14:00:00+00:00"
        path = write_diary_entries(units, tmp_path, "sid-abc", "/some/cwd", ts)
        assert path is not None
        assert path == tmp_path / "2026-05-16" / "sid-abc.md"
        assert path.exists()

    def test_first_line_has_three_brackets(self, tmp_path):
        from lib.extraction import write_diary_entries
        units = _sample_units()
        ts = "2026-05-16T14:00:00+00:00"
        path = write_diary_entries(units, tmp_path, "sid-abc", "/some/cwd", ts)
        first_line = path.read_text().split("\n", 1)[0]
        brackets = re.findall(r"\[([^\]]+)\]", first_line)
        assert len(brackets) >= 3, f"Expected 3 brackets in header, got {brackets!r}"

    def test_first_line_contains_session_id(self, tmp_path):
        from lib.extraction import write_diary_entries
        ts = "2026-05-16T14:00:00+00:00"
        path = write_diary_entries(_sample_units(), tmp_path, "sid-xyz", "/cwd", ts)
        first_line = path.read_text().split("\n", 1)[0]
        assert "sid-xyz" in first_line

    def test_first_line_contains_cwd(self, tmp_path):
        from lib.extraction import write_diary_entries
        ts = "2026-05-16T14:00:00+00:00"
        path = write_diary_entries(_sample_units(), tmp_path, "s1", "/Users/spike/knowhere", ts)
        first_line = path.read_text().split("\n", 1)[0]
        assert "/Users/spike/knowhere" in first_line

    def test_body_contains_diary_entry(self, tmp_path):
        from lib.extraction import write_diary_entries
        ts = "2026-05-16T14:00:00+00:00"
        path = write_diary_entries(_sample_units(), tmp_path, "s1", "/cwd", ts)
        body = path.read_text()
        assert "Implemented extraction refactor" in body

    def test_idempotent_does_not_overwrite(self, tmp_path):
        from lib.extraction import write_diary_entries
        ts = "2026-05-16T14:00:00+00:00"
        path1 = write_diary_entries(_sample_units(), tmp_path, "sid-1", "/cwd", ts)
        original = path1.read_text()
        path2 = write_diary_entries(_sample_units(), tmp_path, "sid-1", "/other", ts)
        assert path1 == path2
        assert path1.read_text() == original, "Idempotent write must not overwrite existing file"

    def test_returns_none_when_no_diary_content(self, tmp_path):
        from lib.extraction import write_diary_entries
        units = [{"timestamp": "", "diary_entry": "", "learnings": []}]
        result = write_diary_entries(units, tmp_path, "s1", "/cwd", "2026-05-16T14:00:00+00:00")
        assert result is None

    def test_returns_none_for_empty_units(self, tmp_path):
        from lib.extraction import write_diary_entries
        result = write_diary_entries([], tmp_path, "s1", "/cwd", "2026-05-16T14:00:00+00:00")
        assert result is None

    def test_creates_day_dir(self, tmp_path):
        from lib.extraction import write_diary_entries
        ts = "2026-05-16T09:00:00+00:00"
        write_diary_entries(_sample_units(), tmp_path, "s1", "/cwd", ts)
        assert (tmp_path / "2026-05-16").is_dir()

    def test_date_from_unit_timestamp(self, tmp_path):
        """Date in path derived from unit timestamp, not provided timestamp."""
        from lib.extraction import write_diary_entries
        units = [{"timestamp": "2026-04-01T12:00:00+00:00", "diary_entry": "Work.", "learnings": []}]
        path = write_diary_entries(units, tmp_path, "s1", "/cwd", "2026-05-16T09:00:00+00:00")
        assert "2026-04-01" in str(path)


# ---------------------------------------------------------------------------
# append_learnings
# ---------------------------------------------------------------------------

class TestAppendLearnings:
    def test_writes_learning_entries(self, tmp_path):
        from lib.extraction import append_learnings
        lf = tmp_path / "2026-05-16.md"
        result = append_learnings(_sample_units(), lf, "sid-1", [], "2026-05-16T14:00:00+00:00")
        assert result is True
        content = lf.read_text()
        assert "PATTERN" in content
        assert "iary-first extraction" in content

    def test_dedup_skips_if_session_present(self, tmp_path):
        from lib.extraction import append_learnings
        lf = tmp_path / "2026-05-16.md"
        lf.write_text("---\n## Session Learnings\nSession: sid-1\n- existing\n")
        result = append_learnings(_sample_units(), lf, "sid-1", [], "2026-05-16T14:00:00+00:00")
        assert result is False
        assert lf.read_text().count("Session: sid-1") == 1

    def test_writes_correction_entries(self, tmp_path):
        from lib.extraction import append_learnings
        lf = tmp_path / "2026-05-16.md"
        corrections = [{"excerpt": "use X not Y", "category": "explicit_correction"}]
        result = append_learnings([], lf, "sid-2", corrections, "2026-05-16T14:00:00+00:00")
        assert result is True
        content = lf.read_text()
        assert "CORRECTION" in content
        assert "use X not Y" in content

    def test_creates_parent_dirs(self, tmp_path):
        from lib.extraction import append_learnings
        lf = tmp_path / "subdir" / "2026-05-16.md"
        append_learnings(_sample_units(), lf, "sid-3", [], "2026-05-16T14:00:00+00:00")
        assert lf.exists()

    def test_session_id_written_to_file(self, tmp_path):
        from lib.extraction import append_learnings
        lf = tmp_path / "2026-05-16.md"
        append_learnings(_sample_units(), lf, "my-session-id", [], "2026-05-16T14:00:00+00:00")
        assert "Session: my-session-id" in lf.read_text()

    def test_units_with_no_learnings_not_written(self, tmp_path):
        from lib.extraction import append_learnings
        units = [{"timestamp": "", "diary_entry": "x", "learnings": []}]
        lf = tmp_path / "2026-05-16.md"
        result = append_learnings(units, lf, "s1", [], "2026-05-16T14:00:00+00:00")
        assert result is False

    def test_learning_format_matches_kit_schema(self, tmp_path):
        """Learning entries must use the structured kit format."""
        from lib.extraction import append_learnings
        lf = tmp_path / "2026-05-16.md"
        append_learnings(_sample_units(), lf, "s1", [], "2026-05-16T14:00:00+00:00")
        content = lf.read_text()
        assert "**[trust:" in content
        assert "→ Target:" in content
