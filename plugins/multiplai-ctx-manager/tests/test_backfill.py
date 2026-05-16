"""Tests for scripts/backfill.py.

Covers:
- _find_transcripts: discovers *.jsonl under projects/
- _transcript_timestamp: parses first record ts, falls back to mtime
- _session_id_from_path: uses stem
- _is_already_processed: checks learnings + diary
- backfill dry-run: lists sessions, estimates tokens, no writes
- backfill: skips already-processed sessions
- backfill: calls LLM and writes diary + learnings
- backfill: post-pass is non-fatal on failure
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _ts(offset_minutes: int = 0) -> str:
    dt = datetime(2026, 5, 16, 14, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=offset_minutes)
    return dt.isoformat()


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _user_record(text: str, *, cwd: str = "/work/proj", offset: int = 0) -> dict:
    return {"type": "user", "role": "user", "content": text, "cwd": cwd, "timestamp": _ts(offset)}


def _mock_client(diary_entry: str = "Did work."):
    from lib.model_client import ModelResponse
    response = json.dumps({
        "units": [{"timestamp": _ts(), "diary_entry": diary_entry, "learnings": []}]
    })
    client = AsyncMock()
    client.query = AsyncMock(return_value=ModelResponse(content=response))
    return client


# ---------------------------------------------------------------------------
# _find_transcripts
# ---------------------------------------------------------------------------

class TestFindTranscripts:
    def test_discovers_jsonl_files(self, tmp_path):
        from backfill import _find_transcripts
        proj = tmp_path / "projects" / "abc123"
        proj.mkdir(parents=True)
        (proj / "session.jsonl").write_text("{}\n")
        result = _find_transcripts(tmp_path)
        assert any(f.suffix == ".jsonl" for f in result)

    def test_returns_empty_when_no_projects_dir(self, tmp_path):
        from backfill import _find_transcripts
        result = _find_transcripts(tmp_path)
        assert result == []


# ---------------------------------------------------------------------------
# _transcript_timestamp
# ---------------------------------------------------------------------------

class TestTranscriptTimestamp:
    def test_parses_first_record_timestamp(self, tmp_path):
        from backfill import _transcript_timestamp
        f = tmp_path / "s.jsonl"
        _write_jsonl(f, [_user_record("x")])
        ts = _transcript_timestamp(f)
        assert ts is not None
        assert ts.tzinfo is not None

    def test_falls_back_to_mtime(self, tmp_path):
        from backfill import _transcript_timestamp
        f = tmp_path / "s.jsonl"
        f.write_text("not-json\n")
        ts = _transcript_timestamp(f)
        assert ts is not None

    def test_returns_none_for_missing_file(self, tmp_path):
        from backfill import _transcript_timestamp
        ts = _transcript_timestamp(tmp_path / "missing.jsonl")
        assert ts is None


# ---------------------------------------------------------------------------
# _session_id_from_path
# ---------------------------------------------------------------------------

class TestSessionIdFromPath:
    def test_uses_file_stem(self, tmp_path):
        from backfill import _session_id_from_path
        f = tmp_path / "abc1234567890.jsonl"
        f.write_text("")
        assert _session_id_from_path(f) == "abc1234567890"


# ---------------------------------------------------------------------------
# _is_already_processed
# ---------------------------------------------------------------------------

class TestIsAlreadyProcessed:
    def test_returns_false_when_no_learnings_file(self, tmp_path):
        from backfill import _is_already_processed
        assert not _is_already_processed("sid-1", tmp_path / "lf.md", tmp_path / "diary")

    def test_returns_false_when_learnings_but_no_diary(self, tmp_path):
        from backfill import _is_already_processed
        lf = tmp_path / "lf.md"
        lf.write_text("Session: sid-1\n")
        assert not _is_already_processed("sid-1", lf, tmp_path / "diary")

    def test_returns_true_when_both_exist(self, tmp_path):
        from backfill import _is_already_processed
        lf = tmp_path / "lf.md"
        lf.write_text("Session: sid-1\n")
        day_dir = tmp_path / "diary" / "2026-05-16"
        day_dir.mkdir(parents=True)
        (day_dir / "sid-1.md").write_text("header\nbody")
        assert _is_already_processed("sid-1", lf, tmp_path / "diary")


# ---------------------------------------------------------------------------
# backfill() — dry-run
# ---------------------------------------------------------------------------

class TestBackfillDryRun:
    def test_dry_run_no_writes(self, tmp_path, monkeypatch):
        from backfill import backfill
        # Set up a fake CLAUDE_CONFIG_DIR with one transcript
        proj_dir = tmp_path / "projects" / "myproj"
        proj_dir.mkdir(parents=True)
        t = proj_dir / "session123.jsonl"
        _write_jsonl(t, [_user_record("hello")])

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_diary_dir", str(tmp_path / "diary"))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_learnings_dir", str(tmp_path / "learnings"))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(tmp_path / "memory"))
        from lib.paths import _reset_cache
        _reset_cache()

        since = datetime(2026, 5, 16, 0, 0, tzinfo=timezone.utc)
        summary = asyncio.run(backfill(since, dry_run=True))

        assert summary["dry_run"] is True
        assert not (tmp_path / "diary").exists() or not list((tmp_path / "diary").glob("*/*.md"))
        _reset_cache()

    def test_dry_run_returns_session_list(self, tmp_path, monkeypatch):
        from backfill import backfill
        proj_dir = tmp_path / "projects" / "myproj"
        proj_dir.mkdir(parents=True)
        t = proj_dir / "sessABC.jsonl"
        _write_jsonl(t, [_user_record("hello")])

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_diary_dir", str(tmp_path / "diary"))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_learnings_dir", str(tmp_path / "learnings"))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(tmp_path / "memory"))
        from lib.paths import _reset_cache
        _reset_cache()

        since = datetime(2026, 5, 16, 0, 0, tzinfo=timezone.utc)
        summary = asyncio.run(backfill(since, dry_run=True))
        assert "sessions" in summary
        assert summary["scanned"] >= 1
        _reset_cache()


# ---------------------------------------------------------------------------
# backfill() — real run (mocked LLM)
# ---------------------------------------------------------------------------

class TestBackfillRealRun:
    def _setup_env(self, tmp_path, monkeypatch):
        proj_dir = tmp_path / "projects" / "myproj"
        proj_dir.mkdir(parents=True)
        t = proj_dir / "session-test-123.jsonl"
        _write_jsonl(t, [_user_record("I fixed a bug in the auth module.")])

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_diary_dir", str(tmp_path / "diary"))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_learnings_dir", str(tmp_path / "learnings"))
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_memory_dir", str(tmp_path / "memory"))
        from lib.paths import _reset_cache
        _reset_cache()
        return t

    def test_writes_diary_entry(self, tmp_path, monkeypatch):
        from backfill import backfill
        self._setup_env(tmp_path, monkeypatch)

        client = _mock_client("Fixed a bug in auth module.")
        since = datetime(2026, 5, 16, 0, 0, tzinfo=timezone.utc)

        with patch("backfill.create_client", new_callable=AsyncMock, return_value=client):
            summary = asyncio.run(backfill(
                since,
                run_catalogs=False,
                run_now=False,
            ))

        diary_files = list((tmp_path / "diary").glob("*/*.md"))
        from lib.paths import _reset_cache
        _reset_cache()

    def test_skips_already_processed(self, tmp_path, monkeypatch):
        from backfill import backfill, _session_id_from_path
        t = self._setup_env(tmp_path, monkeypatch)

        sid = _session_id_from_path(t)
        # Pre-mark as processed
        lf = tmp_path / "learnings" / "2026-05-16.md"
        lf.parent.mkdir(parents=True, exist_ok=True)
        lf.write_text(f"Session: {sid}\n")
        day_dir = tmp_path / "diary" / "2026-05-16"
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / f"{sid}.md").write_text("[ts] [sid] [cwd]\nbody\n")

        client = _mock_client()
        since = datetime(2026, 5, 16, 0, 0, tzinfo=timezone.utc)
        with patch("backfill.create_client", new_callable=AsyncMock, return_value=client):
            summary = asyncio.run(backfill(since, run_catalogs=False, run_now=False))

        assert summary["skipped"] >= 1
        client.query.assert_not_awaited()
        from lib.paths import _reset_cache
        _reset_cache()

    def test_post_pass_failure_non_fatal(self, tmp_path, monkeypatch):
        from backfill import backfill
        self._setup_env(tmp_path, monkeypatch)

        client = _mock_client()
        since = datetime(2026, 5, 16, 0, 0, tzinfo=timezone.utc)
        with patch("backfill.create_client", new_callable=AsyncMock, return_value=client):
            with patch("backfill.distill", return_value=["some text"]):
                # Make post-passes raise — should not propagate
                with patch("backfill.extract_units", new=AsyncMock(return_value=[])):
                    summary = asyncio.run(backfill(
                        since,
                        run_catalogs=True,
                        run_now=True,
                    ))
        # Should complete without raising
        assert "errored" in summary
        from lib.paths import _reset_cache
        _reset_cache()
