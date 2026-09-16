"""Tests for skill-routing precision (lib/skill_precision.py + the CLI).

The property that must hold: a suggestion is a hit only when one of *its*
skills was invoked in *its* session, *after* it and *before* the session's
next prompt. Everything else is bookkeeping around that join.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from lib import skill_precision as sp  # noqa: E402
import skill_routing_precision as cli  # noqa: E402

SESS_A = "aaaaaaaa-1111-2222-3333-444444444444"
SESS_B = "bbbbbbbb-1111-2222-3333-444444444444"


def _log_line(ts: str, session: str, skills: list[str], memory: list[str] | None = None) -> str:
    return (
        f"[{ts}] [context_manager] [session:{session[:8]}] INFO: "
        f"ROUTING memory={json.dumps(memory or [])} skills={json.dumps(skills)} resources=[]\n"
    )


def _assistant_skill(ts: str, session: str, skill: str) -> str:
    return json.dumps({
        "type": "assistant", "timestamp": ts, "sessionId": session, "isSidechain": False,
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "on it"},
            {"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": skill}},
        ]},
    }) + "\n"


def _user(ts: str, session: str, text: str) -> str:
    return json.dumps({
        "type": "user", "timestamp": ts, "sessionId": session, "isSidechain": False,
        "message": {"role": "user", "content": text},
    }) + "\n"


def _tool_result(ts: str, session: str) -> str:
    return json.dumps({
        "type": "user", "timestamp": ts, "sessionId": session,
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "done"}]},
    }) + "\n"


@pytest.fixture
def world(tmp_path: Path) -> dict:
    """A logs dir with ROUTING lines and a config dir with two main transcripts
    plus one subagent transcript that must be ignored."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "context_manager-2026-09-14.log").write_text(
        # Session A: prompt 1 suggests costs+fleet-status; costs is invoked → hit.
        _log_line("2026-09-14T10:00:00", SESS_A, ["costs", "fleet-status"])
        # Session A: prompt 2 suggests gmail; nothing invoked → miss.
        + _log_line("2026-09-14T10:10:00", SESS_A, ["gmail"])
        # Session A: prompt 3 no suggestion → counts as a prompt only.
        + _log_line("2026-09-14T10:20:00", SESS_A, [])
        # Session B: suggests deep-research; invoked via slash command → hit.
        + _log_line("2026-09-14T11:00:00", SESS_B, ["deep-research"])
        # Unknown session: suggestion with no transcript.
        + _log_line("2026-09-14T12:00:00", "cccccccc-0000", ["transcribe"])
        # Unrelated line, must be ignored.
        + "[2026-09-14T12:00:01Z] [context_manager] [session:aaaaaaaa] INFO: No context to inject\n",
        encoding="utf-8",
    )

    projects = tmp_path / "config" / "projects" / "-proj"
    projects.mkdir(parents=True)
    (projects / f"{SESS_A}.jsonl").write_text(
        _user("2026-09-14T10:00:01.000Z", SESS_A, "what did this cost")
        + _assistant_skill("2026-09-14T10:00:05.000Z", SESS_A, "multiplai-context:costs")
        + _tool_result("2026-09-14T10:00:06.000Z", SESS_A)
        # gmail invoked only AFTER prompt 3 — outside prompt 2's window → no credit.
        + _user("2026-09-14T10:20:01.000Z", SESS_A, "check mail")
        + _assistant_skill("2026-09-14T10:20:05.000Z", SESS_A, "multiplai-messaging:gmail"),
        encoding="utf-8",
    )
    (projects / f"{SESS_B}.jsonl").write_text(
        _user("2026-09-14T11:00:02.000Z", SESS_B,
              "<command-message>deep-research</command-message>"
              "<command-name>/multiplai-research:deep-research</command-name>"),
        encoding="utf-8",
    )
    # Subagent of session A invoking gmail inside prompt 2's window: ignored.
    sub = projects / SESS_A / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-x.jsonl").write_text(
        _assistant_skill("2026-09-14T10:11:00.000Z", SESS_A, "multiplai-messaging:gmail"),
        encoding="utf-8",
    )
    return {"logs": logs, "config": tmp_path / "config"}


# --- parsing -----------------------------------------------------------------


def test_parse_routing_line_reads_ts_session_and_bare_skills():
    line = _log_line("2026-09-14T18:26:39", SESS_B, ["multiplai-context:costs", "fleet-status"])
    ts, prefix, skills = sp.parse_routing_line(line)
    assert ts == datetime(2026, 9, 14, 18, 26, 39, tzinfo=timezone.utc)
    assert prefix == "bbbbbbbb"
    assert skills == ["costs", "fleet-status"]


def test_parse_routing_line_ignores_other_lines():
    assert sp.parse_routing_line("[2026-09-14T18:26:39Z] [context_manager] [session:x] INFO: FALLBACK memory=[]") is None
    assert sp.parse_routing_line("garbage") is None


def test_bare_name_strips_plugin_and_slash():
    assert sp.bare_name("/multiplai-context:costs") == "costs"
    assert sp.bare_name("costs") == "costs"


def test_skill_invocations_reads_tool_and_command_and_skips_mechanics(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        _assistant_skill("2026-09-14T10:00:05.000Z", SESS_A, "multiplai-context:costs")
        + _user("2026-09-14T10:01:00.000Z", SESS_A, "<command-name>/clear</command-name>")
        + _user("2026-09-14T10:02:00.000Z", SESS_A, "<command-name>/deep-research</command-name>")
        + "not json\n",
        encoding="utf-8",
    )
    got = sp.skill_invocations(p)
    assert [(i.name, i.via) for i in got] == [("costs", "tool"), ("deep-research", "command")]


def test_main_transcripts_by_prefix_excludes_subagents(world):
    by = sp.main_transcripts_by_prefix(world["config"])
    assert set(by) == {"aaaaaaaa", "bbbbbbbb"}
    assert len(by["aaaaaaaa"]) == 1


# --- the join ------------------------------------------------------------------


def test_measure_credits_only_same_session_same_window(world):
    report = sp.run(world["logs"], world["config"], days=None)
    assert report.prompts_total == 5
    # Three suggestion events had a transcript (A×2, B×1); one had none.
    assert report.suggested_prompts == 3
    assert report.unmatched_sessions == 1
    assert report.hit_prompts == 2
    assert report.precision == pytest.approx(2 / 3)

    per = {r["skill"]: r for r in report.per_skill()}
    assert per["costs"] == {"skill": "costs", "suggested": 1, "invoked": 1, "ratio": 1.0}
    assert per["fleet-status"]["invoked"] == 0
    assert per["gmail"]["invoked"] == 0  # subagent call and later-window call both excluded
    assert per["deep-research"]["invoked"] == 1
    assert "transcribe" not in per  # unmatched session contributes no per-skill row


def test_window_filter_drops_old_lines(world):
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    report = sp.run(world["logs"], world["config"], days=30, now=now)
    assert report.prompts_total == 0
    assert report.precision is None


def test_ambiguous_prefix_is_skipped_not_guessed(world):
    # A second main transcript sharing session A's prefix.
    dup = world["config"] / "projects" / "-other" / f"{SESS_A[:8]}-9999-9999-9999-999999999999.jsonl"
    dup.parent.mkdir(parents=True)
    dup.write_text(_assistant_skill("2026-09-14T10:10:01.000Z", SESS_A, "gmail"), encoding="utf-8")
    report = sp.run(world["logs"], world["config"], days=None)
    assert report.ambiguous_sessions == 2
    assert report.suggested_prompts == 1  # only session B counts
    assert report.hit_prompts == 1


def test_never_invoked_needs_three_suggestions(world):
    extra = world["logs"] / "context_manager-2026-09-15.log"
    extra.write_text(
        _log_line("2026-09-15T10:00:00", SESS_B, ["fleet-status"])
        + _log_line("2026-09-15T10:01:00", SESS_B, ["fleet-status"]),
        encoding="utf-8",
    )
    report = sp.run(world["logs"], world["config"], days=None)
    assert [r["skill"] for r in report.never_invoked()] == ["fleet-status"]


# --- CLI -----------------------------------------------------------------------


def test_cli_markdown_and_json(world, capsys):
    rc = cli.main(["--days", "0", "--logs-dir", str(world["logs"]),
                   "--config-dir", str(world["config"])])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Prompt-level precision" in out and "66.7%" in out
    assert "2608.14036" in out

    rc = cli.main(["--days", "0", "--json", "--logs-dir", str(world["logs"]),
                   "--config-dir", str(world["config"])])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["hit_prompts"] == 2 and data["suggested_prompts"] == 3
    assert data["since"] is None


def test_cli_empty_dirs_report_na(tmp_path, capsys):
    rc = cli.main(["--logs-dir", str(tmp_path / "nologs"), "--config-dir", str(tmp_path / "nocfg")])
    assert rc == 0
    assert "n/a" in capsys.readouterr().out
