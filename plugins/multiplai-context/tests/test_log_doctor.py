"""Tests for scripts/log_doctor.py — pure parsing, no LLM/API calls.

Covers:
- discover: filename → subsystem grouping, focus filter
- parse_file: standard format + traceback attachment, activity short
  format, JSONL (bad lines counted as unparsed)
- normalize: variable parts collapse to a stable signature
- cluster: dedup, severity-then-frequency ranking, traceback-tail samples
- health_checks: oversized hook-errors.log
- scan: subsystem focus, errors-only, since-date file exclusion
"""

import sys
import textwrap
from datetime import date
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from log_doctor import cluster, discover, normalize, parse_file, scan  # noqa: E402

STANDARD_LOG = textwrap.dedent("""\
    [2026-07-05T10:00:00Z] [backfill] [session:--------] INFO: Backfill using AsyncMock
    [2026-07-05T10:00:01Z] [backfill] [session:--------] WARNING: generate_catalog failed (non-fatal): main() takes 0 positional arguments but 1 was given
    [2026-07-05T10:00:02Z] [backfill] [session:abc12345] ERROR: LLM call failed during extraction
    Traceback (most recent call last):
      File "/some/path/model_client.py", line 501, in create_client
        raise RuntimeError(
    RuntimeError: Neither the Agent SDK nor an API key is available.
    [2026-07-05T10:00:03Z] [backfill] [session:abc12345] ERROR: LLM call failed during extraction
    """)

ACTIVITY_LOG = textwrap.dedent("""\
    07:36:08Z [5159085d] [context] injected 3 memory · 0 skills · 0 resources
    07:43:31Z [--------] [context] router matched nothing — fell back to recency
    """)

ACTIVITY_JSONL = (
    '{"ts": "2026-07-05T07:36:08Z", "component": "context", "event": "inject",'
    ' "level": "INFO", "session": "5159085d", "msg": "injected 3 memory"}\n'
    "not json at all\n"
)


@pytest.fixture
def logs_dir(tmp_path: Path) -> Path:
    (tmp_path / "backfill-2026-07-05.log").write_text(STANDARD_LOG)
    (tmp_path / "activity-2026-07-05.log").write_text(ACTIVITY_LOG)
    (tmp_path / "activity-2026-07-05.jsonl").write_text(ACTIVITY_JSONL)
    (tmp_path / "hook-errors.log").write_text("x" * 200_000)
    (tmp_path / "notalog.txt").write_text("ignored")
    return tmp_path


class TestDiscover:
    def test_groups_by_subsystem(self, logs_dir):
        found = discover(logs_dir)
        assert set(found) == {"backfill", "activity", "hook-errors"}
        assert len(found["activity"]) == 2

    def test_subsystem_filter(self, logs_dir):
        found = discover(logs_dir, subsystems=["backfill"])
        assert set(found) == {"backfill"}


class TestParseFile:
    def test_standard_with_traceback(self, logs_dir):
        entries, stat = parse_file(
            logs_dir / "backfill-2026-07-05.log", "backfill", date(2026, 7, 5)
        )
        assert stat.entries == 4
        assert stat.levels == {"INFO": 1, "WARNING": 1, "ERROR": 2}
        err = entries[2]
        assert err.session == "abc12345"
        assert err.detail_lines == 4
        assert "Neither the Agent SDK" in err.detail_tail

    def test_activity_short_format(self, logs_dir):
        entries, stat = parse_file(
            logs_dir / "activity-2026-07-05.log", "activity", date(2026, 7, 5)
        )
        assert stat.entries == 2
        assert entries[0].ts is not None and entries[0].ts.hour == 7
        assert entries[0].session == "5159085d"

    def test_jsonl_counts_bad_lines_as_unparsed(self, logs_dir):
        entries, stat = parse_file(
            logs_dir / "activity-2026-07-05.jsonl", "activity", date(2026, 7, 5)
        )
        assert stat.entries == 1
        assert stat.unparsed == 1
        assert entries[0].msg == "injected 3 memory"


class TestNormalize:
    def test_collapses_variable_parts(self):
        a = normalize("Wrote diary entry to /Users/spike/x/2026-07-05.md")
        b = normalize("Wrote diary entry to /Users/spike/y/2026-07-06.md")
        assert a == b
        assert normalize("retry 3 of 5") == normalize("retry 4 of 5")


class TestCluster:
    def test_dedups_and_ranks_errors_first(self, logs_dir):
        entries, _ = parse_file(
            logs_dir / "backfill-2026-07-05.log", "backfill", date(2026, 7, 5)
        )
        clusters = cluster(entries)
        assert clusters[0].level == "ERROR"
        assert clusters[0].count == 2
        # sample with a traceback tail is preferred
        assert "Neither the Agent SDK" in clusters[0].sample.detail_tail


class TestScan:
    def test_health_check_flags_oversized_hook_errors(self, logs_dir):
        _, _, notes, _ = scan(logs_dir)
        assert any("hook-errors.log" in n and "truncation" in n for n in notes)

    def test_subsystem_focus(self, logs_dir):
        clusters, _, _, found = scan(logs_dir, subsystems=["backfill"])
        assert set(found) == {"backfill"}
        assert all(c.subsystem == "backfill" for c in clusters)

    def test_errors_only(self, logs_dir):
        clusters, _, _, _ = scan(
            logs_dir, subsystems=["backfill"], errors_only=True
        )
        assert all(c.level in ("ERROR", "WARNING") for c in clusters)

    def test_since_excludes_old_files(self, logs_dir):
        _, stats, _, _ = scan(
            logs_dir, subsystems=["backfill"], since=date(2026, 7, 6)
        )
        assert sum(s.entries for s in stats) == 0
