"""Tests for skill-routing precision (lib/skill_precision.py + the CLI).

The property that must hold: a suggestion is a hit only when one of *its*
skills was invoked in *its* session, in transcript order, at or after the
prompt that carried it and before the session's next real prompt. The
suggestion itself is read from the prompt's ``UserPromptSubmit`` hook
attachment, never from a log.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from lib import skill_precision as sp  # noqa: E402
import skill_routing_precision as cli  # noqa: E402

SESS_A = "aaaaaaaa-1111-2222-3333-444444444444"
SESS_B = "bbbbbbbb-1111-2222-3333-444444444444"

_N = [0]


def _uuid() -> str:
    _N[0] += 1
    return f"{_N[0]:08x}-0000-0000-0000-000000000000"


def _entry(kind: str, ts: str, session: str, *, uuid: str | None = None,
           parent: str | None = None, **extra) -> dict:
    e = {"type": kind, "timestamp": ts, "sessionId": session, "isSidechain": False,
         "uuid": uuid or _uuid(), "parentUuid": parent}
    e.update(extra)
    return e


def _user(ts, session, text, **kw) -> dict:
    return _entry("user", ts, session, message={"role": "user", "content": text}, **kw)


def _meta_user(ts, session, **kw) -> dict:
    return _entry("user", ts, session, isMeta=True, message={"role": "user", "content": [
        {"type": "text", "text": "Base directory for this skill: /x"}]}, **kw)


def _tool_result(ts, session, **kw) -> dict:
    return _entry("user", ts, session, message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "done"}]}, **kw)


def _assistant_skill(ts, session, skill, **kw) -> dict:
    return _entry("assistant", ts, session, message={"role": "assistant", "content": [
        {"type": "text", "text": "on it"},
        {"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": skill}}]}, **kw)


def _attachment(ts, session, content, **kw) -> dict:
    return _entry("attachment", ts, session, attachment={
        "type": "hook_additional_context", "hookName": "UserPromptSubmit", "content": content}, **kw)


def _hook_text(skills: list[str], memory: tuple[str, ...] = ("preferences.md",)) -> str:
    parts = ["=== MEMORY ===", ""]
    for m in memory:
        parts.append(f"## {m}\nsome memory")
    if skills:
        parts.append("")
        parts.append("=== SKILLS ===")
        for s in skills:
            parts.append(f"\n## {s}\nSummary.\nInvoke with /plugin:{s} when relevant.")
    return "\n".join(parts)


def _write(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


@pytest.fixture
def world(tmp_path: Path) -> dict:
    """A config dir with two main transcripts plus one subagent transcript."""
    projects = tmp_path / "config" / "projects" / "-proj"
    spill_dir = projects / SESS_A / "tool-results"
    spill_dir.mkdir(parents=True)

    # --- Session A --------------------------------------------------------
    # Prompt 1 suggests costs + fleet-status (spilled to a persisted file, and
    # the attachment hangs two hops below the prompt); costs invoked → hit.
    p1 = _user("2026-09-14T10:00:00.000Z", SESS_A, "what did this cost")
    spill = spill_dir / "hook-1-additionalContext.txt"
    spill.write_text(_hook_text(["costs", "fleet-status"]), encoding="utf-8")
    a1_mid = _entry("attachment", "2026-09-14T10:00:03.000Z", SESS_A, parent=p1["uuid"],
                    attachment={"type": "queued_command", "content": "x"})
    a1 = _attachment("2026-09-14T10:00:04.000Z", SESS_A, [
        f"<persisted-output>\nOutput too large. Full output saved to: {spill}\n\nPreview\n"],
        parent=a1_mid["uuid"])
    s1 = _assistant_skill("2026-09-14T10:00:05.000Z", SESS_A, "multiplai-context:costs", parent=a1["uuid"])
    r1 = _tool_result("2026-09-14T10:00:06.000Z", SESS_A, parent=s1["uuid"])
    # Prompt 2 suggests gmail (inline attachment); gmail invoked only after
    # prompt 3 → miss for prompt 2.
    p2 = _user("2026-09-14T10:10:00.000Z", SESS_A, "anything else?", parent=r1["uuid"])
    a2 = _attachment("2026-09-14T10:10:03.000Z", SESS_A, _hook_text(["gmail"]), parent=p2["uuid"])
    # Prompt 3: no skills block → a prompt, not a suggestion event.
    p3 = _user("2026-09-14T10:20:00.000Z", SESS_A, "check mail", parent=a2["uuid"])
    a3 = _attachment("2026-09-14T10:20:03.000Z", SESS_A, _hook_text([]), parent=p3["uuid"])
    s3 = _assistant_skill("2026-09-14T10:20:05.000Z", SESS_A, "multiplai-messaging:gmail", parent=a3["uuid"])
    _write(projects / f"{SESS_A}.jsonl", [p1, a1_mid, a1, s1, r1, p2, a2, p3, a3, s3])

    # --- Session B: slash command --------------------------------------
    # The command prompt is followed by the isMeta skill-body entry; the hook
    # attachment hangs off the meta entry, and its timestamp is *after* the
    # prompt's (production shape). Must still credit the command.
    pb = _user("2026-09-14T11:00:00.000Z", SESS_B,
               "<command-message>deep-research</command-message>"
               "<command-name>/multiplai-research:deep-research</command-name>")
    mb = _meta_user("2026-09-14T11:00:00.000Z", SESS_B, parent=pb["uuid"])
    ab = _attachment("2026-09-14T11:00:04.000Z", SESS_B, _hook_text(["deep-research", "think"]),
                     parent=mb["uuid"])
    # A /clear-style mechanics command must not count as an invocation.
    pb2 = _user("2026-09-14T11:30:00.000Z", SESS_B,
                "<command-name>/model</command-name>", parent=ab["uuid"])
    ab2 = _attachment("2026-09-14T11:30:02.000Z", SESS_B, _hook_text(["think"]), parent=pb2["uuid"])
    _write(projects / f"{SESS_B}.jsonl", [pb, mb, ab, pb2, ab2])

    # --- Subagent of session A invoking gmail inside prompt 2's window ---
    _write(projects / SESS_A / "subagents" / "agent-x.jsonl",
           [_assistant_skill("2026-09-14T10:11:00.000Z", SESS_A, "multiplai-messaging:gmail")])
    return {"config": tmp_path / "config", "projects": projects, "spill": spill}


# --- names and attachment parsing -------------------------------------------


def test_bare_name_strips_plugin_and_slash():
    assert sp.bare_name("multiplai-context:costs") == "costs"
    assert sp.bare_name("/costs") == "costs"
    assert sp.bare_name("  plane ") == "plane"


def test_suggested_skills_reads_only_the_skills_block_and_dedups():
    text = _hook_text(["costs", "plugin:fleet-status", "costs"]) + "\n\n=== PROJECT STATE ===\n## not-a-skill\n"
    assert sp.suggested_skills(text) == ["costs", "fleet-status"]
    assert sp.suggested_skills(_hook_text([])) == []
    assert sp.suggested_skills("") == []


def test_attachment_text_follows_persisted_pointer(tmp_path):
    spill = tmp_path / "hook-x.txt"
    spill.write_text("=== SKILLS ===\n## costs\n", encoding="utf-8")
    e = _attachment("t", SESS_A, [f"<persisted-output>\nFull output saved to: {spill}\n"])
    assert sp.suggested_skills(sp.attachment_text(e)) == ["costs"]
    gone = _attachment("t", SESS_A, ["<persisted-output>\nFull output saved to: /nope/missing.txt\n"])
    assert sp.attachment_text(gone) == ""
    assert sp.attachment_text(_attachment("t", SESS_A, "inline === SKILLS ===\n## slack")) .endswith("## slack")


# --- transcript discovery ----------------------------------------------------


def test_iter_main_transcripts_excludes_subagents_and_honours_mtime(world):
    found = sorted(p.name for p in sp.iter_main_transcripts(world["config"]))
    assert found == [f"{SESS_A}.jsonl", f"{SESS_B}.jsonl"]
    old = world["projects"] / f"{SESS_B}.jsonl"
    stale = time.time() - 40 * 86400
    os.utime(old, (stale, stale))
    recent = sorted(p.name for p in sp.iter_main_transcripts(
        world["config"], modified_since=datetime.now(timezone.utc) - timedelta(days=30)))
    assert recent == [f"{SESS_A}.jsonl"]


def test_iter_main_transcripts_missing_dir_is_empty(tmp_path):
    assert list(sp.iter_main_transcripts(tmp_path)) == []


# --- the join ----------------------------------------------------------------


def test_measure_transcript_windows_by_order_not_timestamp(world):
    prompts, suggestions, earliest = sp.measure_transcript(world["projects"] / f"{SESS_A}.jsonl")
    assert prompts == 3  # tool_result and meta entries are not prompts
    assert earliest == datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
    by_skills = {tuple(s.skills): s.invoked for s in suggestions}
    assert by_skills == {("costs", "fleet-status"): ["costs"], ("gmail",): []}


def test_measure_transcript_credits_slash_command_via_meta_chain(world):
    prompts, suggestions, _ = sp.measure_transcript(world["projects"] / f"{SESS_B}.jsonl")
    assert prompts == 2
    assert [(s.skills, s.invoked) for s in suggestions] == [
        (["deep-research", "think"], ["deep-research"]),
        (["think"], []),  # /model is session mechanics, not a skill use
    ]


def test_run_ignores_subagent_transcripts_and_reports_totals(world):
    report = sp.run(world["config"], days=None)
    assert report.transcripts_read == 2
    assert report.prompts_total == 5
    assert report.suggested_prompts == 4
    assert report.hit_prompts == 2
    assert report.precision == pytest.approx(0.5)
    rows = {r["skill"]: (r["suggested"], r["invoked"]) for r in report.per_skill()}
    assert rows["gmail"] == (1, 0)  # subagent's gmail call never credited
    assert rows["think"] == (2, 0)
    assert rows["costs"] == (1, 1)


def test_run_window_drops_old_prompts(world):
    now = datetime(2026, 9, 14, 10, 15, tzinfo=timezone.utc)
    report = sp.run(world["config"], days=1, now=now)
    # since = 09-13T10:15 → everything is inside; shrink to 5 minutes via now.
    assert report.prompts_total == 5
    later = sp.run(world["config"], days=None, now=now)
    assert later.prompts_total == 5
    cut = sp.measure_transcript(world["projects"] / f"{SESS_A}.jsonl",
                                since=datetime(2026, 9, 14, 10, 5, tzinfo=timezone.utc))
    assert cut[0] == 2 and [s.skills for s in cut[1]] == [["gmail"]]
    assert cut[2] == datetime(2026, 9, 14, 10, 10, tzinfo=timezone.utc)


def test_run_days_zero_means_all_and_negative_rejected(world):
    assert sp.run(world["config"], days=0).since is None
    with pytest.raises(ValueError):
        sp.run(world["config"], days=-1)


def test_never_invoked_needs_three_suggestions(world):
    report = sp.run(world["config"], days=None)
    assert report.never_invoked() == []
    assert [r["skill"] for r in report.never_invoked(min_suggested=2)] == ["think"]


# --- CLI ---------------------------------------------------------------------


def test_cli_markdown_json_and_json_out(world, tmp_path, capsys):
    out = tmp_path / "report.json"
    assert cli.main(["--config-dir", str(world["config"]), "--days", "0", "--json-out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "**Prompt-level precision** | **50.0%**" in text
    assert "all transcripts" in text
    saved = json.loads(out.read_text())
    assert saved["precision"] == pytest.approx(0.5)
    assert saved["earliest_prompt"] == "2026-09-14T10:00:00+00:00"
    assert cli.main(["--config-dir", str(world["config"]), "--days", "0", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["hit_prompts"] == 2


def test_cli_rejects_negative_days(world):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config-dir", str(world["config"]), "--days", "-7"])
    assert exc.value.code == 2


def test_cli_empty_dir_reports_na(tmp_path, capsys):
    assert cli.main(["--config-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "**n/a**" in text
    assert "earliest prompt read n/a" in text
