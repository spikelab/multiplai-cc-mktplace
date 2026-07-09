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

from log_doctor import (  # noqa: E402
    SCENARIOS,
    cluster,
    discover,
    injection_stats,
    load_routing_decisions,
    normalize,
    parse_expect_spec,
    parse_file,
    probe_check,
    probe_new_entries,
    probe_snapshot,
    render_injections_markdown,
    scan,
)

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


NEW_SESSION_LINES = (
    "[2026-07-06T12:00:00Z] [session_start] [session:abcd1234] INFO: Model client selected: AgentSDKClient\n"
    "[2026-07-06T12:00:01Z] [session_start] [session:abcd1234] INFO: Session started: 756ff009-4dfa-4f31-b2af-6be4e6ca413f\n"
)


class TestProbe:
    def test_snapshot_records_log_files_only(self, logs_dir):
        snap = probe_snapshot(logs_dir)
        assert "backfill-2026-07-05.log" in snap["files"]
        assert "notalog.txt" not in snap["files"]

    def test_new_entries_sees_only_appended_content(self, logs_dir):
        snap = probe_snapshot(logs_dir)
        f = logs_dir / "session_start.log"
        f.write_text(NEW_SESSION_LINES)  # new file
        with (logs_dir / "backfill-2026-07-05.log").open("a") as fh:
            fh.write(
                "[2026-07-06T12:00:02Z] [backfill] [session:abcd1234] "
                "INFO: Backfill using AgentSDKClient\n"
            )
        entries = probe_new_entries(logs_dir, snap)
        msgs = [e.msg for e in entries]
        assert len(entries) == 3
        assert any("Session started" in m for m in msgs)
        assert any("Backfill using AgentSDKClient" in m for m in msgs)
        # pre-existing content is not re-read
        assert not any("AsyncMock" in m for m in msgs)

    def test_new_entries_rereads_rotated_file(self, logs_dir):
        snap = probe_snapshot(logs_dir)
        # simulate rotation: file truncated and rewritten smaller
        (logs_dir / "backfill-2026-07-05.log").write_text(
            "[2026-07-06T12:00:00Z] [backfill] [session:--------] INFO: fresh after rotation\n"
        )
        entries = probe_new_entries(logs_dir, snap)
        assert [e.msg for e in entries] == ["fresh after rotation"]

    def test_check_passes_on_expected_lines(self, logs_dir):
        snap = probe_snapshot(logs_dir)
        (logs_dir / "session_start.log").write_text(NEW_SESSION_LINES)
        entries = probe_new_entries(logs_dir, snap)
        verdict = probe_check(
            entries, SCENARIOS["session-start"]["expect"],
            SCENARIOS["session-start"]["subsystems"],
        )
        assert verdict["passed"]
        assert all(r["ok"] for r in verdict["expectations"])

    def test_check_fails_when_expected_line_missing(self, logs_dir):
        snap = probe_snapshot(logs_dir)
        entries = probe_new_entries(logs_dir, snap)  # nothing happened
        verdict = probe_check(
            entries, SCENARIOS["session-start"]["expect"],
            SCENARIOS["session-start"]["subsystems"],
        )
        assert not verdict["passed"]

    def test_check_fails_on_unexpected_error(self, logs_dir):
        snap = probe_snapshot(logs_dir)
        (logs_dir / "session_start.log").write_text(
            NEW_SESSION_LINES
            + "[2026-07-06T12:00:02Z] [session_start] [session:abcd1234] ERROR: boom\n"
        )
        entries = probe_new_entries(logs_dir, snap)
        expect = SCENARIOS["session-start"]["expect"]
        forbid = SCENARIOS["session-start"]["subsystems"]
        assert not probe_check(entries, expect, forbid)["passed"]
        assert probe_check(entries, expect, forbid, allow_errors=True)["passed"]

    def test_check_matches_component_in_aggregate_log(self, logs_dir):
        snap = probe_snapshot(logs_dir)
        with (logs_dir / "hook-errors.log").open("a") as fh:
            fh.write(
                "\n[2026-07-06T12:00:00Z] [extract_learnings] [session:--------] "
                "ERROR: Could not create model client for extraction\n"
            )
        entries = probe_new_entries(logs_dir, snap)
        # forbid list names the component; the file-level subsystem is hook-errors
        verdict = probe_check(
            entries, [("extract_learnings", "ERROR", "model client")],
            ["extract_learnings"], allow_errors=True,
        )
        assert verdict["passed"]

    def test_parse_expect_spec(self):
        assert parse_expect_spec("dream:INFO:Dream using") == (
            "dream", "INFO", "Dream using"
        )
        assert parse_expect_spec("x:*:a:b")[2] == "a:b"  # regex may contain colons
        with pytest.raises(ValueError):
            parse_expect_spec("no-level-no-pattern")

    def test_scenario_registry_is_well_formed(self):
        import re as _re
        for name, sc in SCENARIOS.items():
            assert sc["trigger"] and sc["subsystems"], name
            for sub, lvl, pat in sc["expect"]:
                assert sub and lvl, name
                _re.compile(pat)


CM_LOG = (
    "[2026-07-06T07:46:18Z] [context_manager] [session:--------] INFO: "
    'ROUTING_SCORES memory={"picked": [["prompt-eng-guide.md", 10.806], '
    '["CLAUDE.md", 9.931], ["life.md", 3.335]], "cap": 10, '
    '"n_candidates": 12, "n_picked": 3, "capped": false, "floor_excluded": 1.0}\n'
    "[2026-07-06T07:46:18Z] [context_manager] [session:--------] INFO: "
    'COOLDOWN turn=4 window=4 suppressed={"memory": ["prompt-eng-guide.md", "CLAUDE.md"]}\n'
    "[2026-07-06T07:50:00Z] [context_manager] [session:--------] INFO: "
    'ROUTING_SCORES memory={"picked": [["dolcebot.md", 5.0]], "cap": 10, '
    '"n_candidates": 3, "n_picked": 1, "capped": true, "floor_excluded": 2.0}\n'
)

ACTIVITY_INJECT_JSONL = (
    '{"ts": "2026-07-06T07:46:18Z", "component": "context", "event": "inject",'
    ' "level": "INFO", "session": "351388d2", "msg": "injected 1 memory",'
    ' "files": ["life.md"], "bytes": 20648}\n'
    '{"ts": "2026-07-06T07:51:00Z", "component": "context", "event": "skip",'
    ' "level": "INFO", "session": "351388d2",'
    ' "msg": "router abstained \\u2014 no memory matched, nothing injected"}\n'
)


class TestInjections:
    @pytest.fixture
    def routing_logs(self, tmp_path):
        (tmp_path / "context_manager.log").write_text(CM_LOG)
        (tmp_path / "activity.jsonl").write_text(ACTIVITY_INJECT_JSONL)
        return tmp_path

    def test_joins_scores_cooldown_and_inject_event(self, routing_logs):
        decisions = load_routing_decisions(routing_logs)
        d = next(x for x in decisions if x.event == "inject")
        assert d.session == "351388d2"
        assert d.injected == ["life.md"]
        assert d.bytes == 20648
        picked = dict(d.scores["memory"]["picked"])
        assert picked["life.md"] == 3.335
        assert "prompt-eng-guide.md" in d.suppressed["memory"]

    def test_counts_abstains_and_cap_hits(self, routing_logs):
        stats = injection_stats(load_routing_decisions(routing_logs))
        assert stats["injects"] == 1
        assert stats["abstains"] == 1
        assert stats["cap_hits"] == 1

    def test_per_file_stats(self, routing_logs):
        stats = injection_stats(load_routing_decisions(routing_logs))
        rows = {r["file"]: r for r in stats["files"]}
        assert rows["life.md"]["injected"] == 1
        assert rows["life.md"]["picked"] == 1
        assert rows["prompt-eng-guide.md"]["suppressed"] == 1
        assert rows["prompt-eng-guide.md"]["injected"] == 0

    def test_file_filter(self, routing_logs):
        stats = injection_stats(
            load_routing_decisions(routing_logs), file_filter="life.md"
        )
        assert [r["file"] for r in stats["files"]] == ["life.md"]

    def test_trace_shows_embedded_prompt_and_drops_stale_note(self, tmp_path):
        """0.5.3+ ROUTING_SCORES payloads embed the prompt: the trace
        must surface it, and the 'prompts are not logged' note must
        not appear (it would contradict the traces)."""
        (tmp_path / "context_manager.log").write_text(
            "[2026-07-07T10:00:00Z] [context_manager] [session:--------] INFO: "
            'ROUTING_SCORES memory={"picked": [["life.md", 3.335]], "cap": 10, '
            '"n_candidates": 3, "n_picked": 1, "capped": false, '
            '"floor_excluded": null, "prompt": "why does it inject stuff"}\n'
        )
        (tmp_path / "activity.jsonl").write_text(ACTIVITY_INJECT_JSONL)
        decisions = load_routing_decisions(tmp_path)
        md = render_injections_markdown(
            injection_stats(decisions), decisions, None, trace=5
        )
        assert '- prompt: "why does it inject stuff"' in md
        assert "predate plugin 0.5.3" not in md
        assert "needs the session transcript" not in md

    def test_legacy_logs_keep_transcript_note(self, routing_logs):
        """Pre-0.5.3 lines carry no prompt — the transcript-digging
        guidance stays, reworded to say why."""
        decisions = load_routing_decisions(routing_logs)
        md = render_injections_markdown(
            injection_stats(decisions), decisions, None, trace=5
        )
        assert "- prompt:" not in md
        assert "predate plugin 0.5.3" in md
        assert "needs the session transcript" in md
