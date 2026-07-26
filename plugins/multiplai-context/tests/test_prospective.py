"""Tests for the prospective memory channel.

The done-condition this covers: an INTENTION round-trips — extraction knows
the type, and a `prospective.md` with a past-due entry makes SessionStart emit
the nudge.

The other half of these tests is about what does *not* fire. A reminder system
that surfaces things at the wrong time is worse than none: the reader learns to
skip the nudge, and then it also fails on the day it was right.
"""

from datetime import date, timedelta
from pathlib import Path

import pytest

from scripts.lib.prospective import (CONDITION_SWEEP_DAYS, LEAD_DAYS,
                                     PROSPECTIVE_FILENAME, SWEEP_STATE_FILENAME,
                                     actionable, format_line, load,
                                     load_sweep_state, parse, render_nudge,
                                     save_sweep_state, sweep_key)

TODAY = date(2026, 7, 26)


def dated(days_from_today: int, text="Re-check the thing", captured=None) -> str:
    due = TODAY + timedelta(days=days_from_today)
    stamp = f" (captured {captured})" if captured else ""
    return f"- [due: {due.isoformat()}] {text}{stamp}"


class TestParsing:
    def test_parses_a_dated_intention(self):
        [i] = parse(dated(30, "Re-check the tax residency rule", captured="2026-07-26"))
        assert i.due == date(2026, 8, 25)
        assert i.text == "Re-check the tax residency rule"
        assert i.captured == date(2026, 7, 26)
        assert i.is_dated

    def test_parses_a_conditional_intention(self):
        [i] = parse("- [on: the runtime moves past v0.5] Re-run the config audit "
                    "(captured 2026-07-26)")
        assert i.due is None
        assert i.condition == "the runtime moves past v0.5"
        assert i.text == "Re-run the config audit"

    def test_capture_stamp_is_optional(self):
        [i] = parse("- [due: 2026-09-01] Do the thing")
        assert i.captured is None and i.text == "Do the thing"

    def test_prose_and_headings_are_ignored(self):
        text = ("# Prospective Memory\n\n"
                "Some explanatory prose about intentions.\n\n"
                "## Dated\n\n"
                + dated(3) + "\n")
        assert len(parse(text)) == 1

    def test_commented_out_template_examples_are_not_intentions(self):
        """The shipped template carries HTML-commented examples. If those
        parsed, every fresh install would nudge about a fake September task."""
        text = ("<!-- - [due: 2026-09-01] Re-check X (captured 2026-07-26) -->\n"
                "<!-- - [on: the image moves past v0.5] Re-run it -->\n")
        assert parse(text) == []

    def test_multiline_comment_blocks_are_skipped(self):
        """Caught by the shipped-template test: the template documents the
        line format *using the line format*, inside a `<!-- -->` block. Without
        comment-awareness a fresh install parses its own instructions."""
        text = ("<!--\n"
                "One intention per line, in one of two shapes:\n"
                "  - [due: YYYY-MM-DD] What to do (captured YYYY-MM-DD)\n"
                "  - [on: a condition in plain words] What to do\n"
                "-->\n"
                + dated(-1, "a real one") + "\n")
        assert [i.text for i in parse(text)] == ["a real one"]

    def test_commenting_one_out_silences_it(self):
        assert parse("<!-- " + dated(-5) + " -->") == []

    def test_a_malformed_date_is_dropped_not_guessed(self):
        assert parse("- [due: 2026-13-45] Impossible date") == []

    def test_line_numbers_are_recorded(self):
        [i] = parse("\n\n" + dated(1))
        assert i.lineno == 3


class TestStatus:
    @pytest.mark.parametrize("offset,expected", [
        (-30, "overdue"),
        (-1, "overdue"),
        (0, "due"),
        (LEAD_DAYS, "upcoming"),
        (LEAD_DAYS + 1, "future"),
    ])
    def test_status_boundaries(self, offset, expected):
        [i] = parse(dated(offset))
        assert i.status(TODAY) == expected

    def test_a_condition_is_never_dated_status(self):
        [i] = parse("- [on: something happens] Do it")
        assert i.status(TODAY) == "condition"


class TestActionable:
    def test_future_intentions_stay_silent(self):
        """The whole point of a due date is that it doesn't fire early."""
        assert actionable(parse(dated(60)), TODAY) == []

    def test_overdue_sorts_before_upcoming(self):
        text = dated(3, "soon") + "\n" + dated(-5, "late")
        assert [i.text for i in actionable(parse(text), TODAY)] == ["late", "soon"]

    def test_conditions_are_not_swept_every_session(self):
        """A condition can never be evaluated, so if it surfaced on every
        session it would be permanent noise nobody reads."""
        text = "- [on: X happens] Do it (captured 2026-07-25)"
        assert actionable(parse(text), TODAY) == []

    def test_conditions_resurface_on_the_sweep(self):
        captured = TODAY - timedelta(days=CONDITION_SWEEP_DAYS)
        text = f"- [on: X happens] Do it (captured {captured.isoformat()})"
        [i] = actionable(parse(text), TODAY)
        assert i.condition == "X happens"

    def test_an_undated_condition_surfaces_once_and_is_then_stamped(self):
        """Changed deliberately: an undated condition used to be ignored
        forever. A condition nobody can evaluate and nobody ever sees is
        indistinguishable from one that was never captured, so it surfaces now
        and the caller's stamp is what stops it repeating."""
        [i] = parse("- [on: X happens] Do it")
        assert actionable([i], TODAY) == [i]
        stamped = {sweep_key(i): TODAY}
        assert actionable([i], TODAY, last_surfaced=stamped) == []

    def test_a_missed_sweep_day_does_not_cost_another_full_cycle(self):
        """The bug this replaces: `% CONDITION_SWEEP_DAYS == 0` fired only on
        exact multiples, so one missed session — no session that day, a run
        either side of the UTC rollover, a closed laptop — silently waited
        another 30 days on the one channel where silence IS the failure."""
        captured = TODAY - timedelta(days=CONDITION_SWEEP_DAYS + 1)
        text = f"- [on: X happens] Do it (captured {captured.isoformat()})"
        assert len(actionable(parse(text), TODAY)) == 1

        way_overdue = TODAY - timedelta(days=CONDITION_SWEEP_DAYS * 3 + 7)
        text = f"- [on: X happens] Do it (captured {way_overdue.isoformat()})"
        assert len(actionable(parse(text), TODAY)) == 1

    def test_the_stamp_and_not_the_capture_date_drives_later_sweeps(self):
        captured = TODAY - timedelta(days=CONDITION_SWEEP_DAYS * 2)
        [i] = parse(f"- [on: X happens] Do it (captured {captured.isoformat()})")
        # Surfaced a week ago: not due again yet, even though capture is old.
        recent = {sweep_key(i): TODAY - timedelta(days=7)}
        assert actionable([i], TODAY, last_surfaced=recent) == []
        # Surfaced a full cycle ago: due again.
        old = {sweep_key(i): TODAY - timedelta(days=CONDITION_SWEEP_DAYS)}
        assert actionable([i], TODAY, last_surfaced=old) == [i]

    def test_a_stamp_for_a_different_intention_does_not_silence_this_one(self):
        [mine] = parse("- [on: X happens] Do it (captured 2026-01-01)")
        assert actionable([mine], TODAY,
                          last_surfaced={"unrelated :: thing": TODAY}) == [mine]

    def test_sweep_key_survives_reordering_and_reflowing(self):
        """Keyed on content, not line number: removing a line above, or
        re-wrapping the line, must not reset the clock."""
        [a] = parse("- [on: X happens] Do it (captured 2026-01-01)")
        [b] = parse("junk\n- [on:  X   happens] Do  it (captured 2026-01-01)".split("\n")[1])
        assert sweep_key(a) == sweep_key(b)

    def test_sweep_state_round_trips(self, tmp_path):
        path = tmp_path / "sweep.json"
        save_sweep_state(path, {"a :: b": TODAY})
        assert load_sweep_state(path) == {"a :: b": TODAY}

    def test_corrupt_sweep_state_degrades_to_never_surfaced(self, tmp_path):
        """Failing towards noise, never towards silence."""
        path = tmp_path / "sweep.json"
        path.write_text("{not json", encoding="utf-8")
        assert load_sweep_state(path) == {}
        path.write_text('{"a :: b": "not-a-date", "c :: d": "2026-07-01"}',
                        encoding="utf-8")
        assert load_sweep_state(path) == {"c :: d": date(2026, 7, 1)}

    def test_saving_sweep_state_never_raises(self, tmp_path):
        """Derived state must not be able to abort a session start."""
        blocked = tmp_path / "file-not-dir"
        blocked.write_text("x", encoding="utf-8")
        save_sweep_state(blocked / "sweep.json", {"a :: b": TODAY})


class TestNudge:
    def test_no_intentions_means_no_nudge(self):
        assert render_nudge([], TODAY) == ""

    def test_overdue_entry_produces_a_nudge(self):
        out = render_nudge(actionable(parse(dated(-3, "Re-check the rule")), TODAY), TODAY)
        assert "SYSTEM NUDGE" in out
        assert "Re-check the rule" in out
        assert "OVERDUE by 3 days" in out

    def test_singular_day(self):
        out = render_nudge(actionable(parse(dated(-1)), TODAY), TODAY)
        assert "OVERDUE by 1 day (was 2026-07-25)" in out

    def test_nudge_is_capped_and_counts_the_overflow(self):
        """Plan gate (b): a first rollout can find a pile of intentions at
        once. A nudge listing twenty is one the reader skips wholesale."""
        text = "\n".join(dated(-i, f"task {i}") for i in range(1, 13))
        out = render_nudge(actionable(parse(text), TODAY), TODAY, cap=5)
        assert out.count("\n- ") == 6  # 5 shown + the overflow line
        assert "and 7 more" in out

    def test_nudge_says_where_they_live_and_to_remove_them(self):
        out = render_nudge(actionable(parse(dated(-1)), TODAY), TODAY)
        assert PROSPECTIVE_FILENAME in out
        assert "removed" in out


class TestLoad:
    def test_missing_file_is_not_an_error(self, tmp_path):
        assert load(tmp_path) == []

    def test_reads_the_file(self, tmp_path):
        (tmp_path / PROSPECTIVE_FILENAME).write_text(dated(-2), encoding="utf-8")
        assert len(load(tmp_path)) == 1


class TestFormatLine:
    def test_round_trips_through_the_parser(self):
        line = format_line("Re-check the rule", due=date(2026, 9, 1),
                           captured=TODAY)
        [i] = parse(line)
        assert i.due == date(2026, 9, 1)
        assert i.text == "Re-check the rule"
        assert i.captured == TODAY

    def test_condition_round_trips(self):
        line = format_line("Re-run the audit", condition="the runtime updates",
                           captured=TODAY)
        [i] = parse(line)
        assert i.condition == "the runtime updates"

    def test_needs_exactly_one_trigger(self):
        with pytest.raises(ValueError):
            format_line("x")
        with pytest.raises(ValueError):
            format_line("x", due=TODAY, condition="y")


class TestShippedTemplate:
    TEMPLATE = (Path(__file__).resolve().parent.parent
                / "templates" / PROSPECTIVE_FILENAME)

    def test_template_exists(self):
        assert self.TEMPLATE.is_file()

    def test_a_fresh_install_nudges_about_nothing(self):
        """The template ships examples. If any of them parsed, day one of every
        install would open with a reminder about a task that doesn't exist."""
        assert parse(self.TEMPLATE.read_text(encoding="utf-8")) == []


class TestOnboardingSeedsTheFile:
    def test_setup_write_copies_the_template(self, tmp_path, monkeypatch):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import setup_write

        memory = tmp_path / "memory"
        templates = Path(__file__).resolve().parent.parent / "templates"

        class FakePaths:
            templates_dir = templates
            memory_dir = memory

        monkeypatch.setattr(setup_write, "get_paths", lambda: FakePaths())
        result = setup_write.write_memory_files()
        assert PROSPECTIVE_FILENAME in result["copied"]
        assert (memory / PROSPECTIVE_FILENAME).is_file()

    def test_setup_check_does_not_report_it_missing(self):
        """Deliberate asymmetry: `setup_check`'s `missing` list drives
        `all_present`, so adding this filename there would tell every existing
        install its memory is incomplete the next time /setup runs."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from multiplai_core.config import TEMPLATE_FILENAMES

        assert PROSPECTIVE_FILENAME not in TEMPLATE_FILENAMES


class TestExtractionKnowsTheType:
    def test_intention_is_a_declared_learning_type(self):
        from scripts.lib.extraction import EXTRACTION_PROMPT
        assert "INTENTION" in EXTRACTION_PROMPT

    def test_prompt_carries_todays_date(self):
        """A model asked to turn "in about six weeks" into a due date needs to
        know what day it is; without it, it invents one."""
        from scripts.lib.extraction import EXTRACTION_PROMPT
        assert "Today's date:" in EXTRACTION_PROMPT


class TestSessionStartIntegration:
    """The done-condition: a fixture file with a past-due entry makes the
    hook emit the nudge.

    Offsets are relative to the real `date.today()` here, not the frozen
    TODAY — the hook reads the clock itself, and a fixture pinned to a literal
    date would quietly stop testing anything the day after it was written.
    """

    def _run(self, tmp_path, capsys, contents: str | None) -> tuple[int, str]:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import session_start

        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        if contents is not None:
            (memory_dir / PROSPECTIVE_FILENAME).write_text(contents, encoding="utf-8")
        count = session_start._emit_prospective_nudge(memory_dir, data_dir)
        return count, capsys.readouterr().out

    @staticmethod
    def _line(days: int, text: str) -> str:
        due = date.today() + timedelta(days=days)
        return f"- [due: {due.isoformat()}] {text}"

    def test_past_due_entry_emits_the_nudge(self, tmp_path, capsys):
        count, out = self._run(
            tmp_path, capsys, self._line(-4, "Re-check the residency rule"))
        assert count == 1
        assert "SYSTEM NUDGE" in out
        assert "Re-check the residency rule" in out

    def test_future_entry_emits_nothing(self, tmp_path, capsys):
        assert self._run(tmp_path, capsys, self._line(90, "Later")) == (0, "")

    def test_absent_file_emits_nothing(self, tmp_path, capsys):
        assert self._run(tmp_path, capsys, None) == (0, "")

    def test_garbage_file_never_breaks_session_start(self, tmp_path, capsys):
        """SessionStart runs before the user types anything. A crash here costs
        the whole session, so this path fails open."""
        assert self._run(tmp_path, capsys, "not: valid [[[ ]]]") == (0, "")

    def _run_twice(self, tmp_path, capsys, contents: str):
        """Two hook invocations against ONE data dir, as consecutive sessions."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import session_start

        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (memory_dir / PROSPECTIVE_FILENAME).write_text(contents, encoding="utf-8")
        first = session_start._emit_prospective_nudge(memory_dir, data_dir)
        capsys.readouterr()
        second = session_start._emit_prospective_nudge(memory_dir, data_dir)
        capsys.readouterr()
        return first, second, data_dir

    def test_a_swept_condition_is_stamped_and_does_not_repeat_next_session(
            self, tmp_path, capsys):
        """The counterpart to dropping the modulo test: `>=` fires every session
        until something records that it fired."""
        captured = date.today() - timedelta(days=CONDITION_SWEEP_DAYS + 3)
        first, second, data_dir = self._run_twice(
            tmp_path, capsys,
            f"- [on: the runtime updates] Re-run the audit "
            f"(captured {captured.isoformat()})")
        assert (first, second) == (1, 0)

        state = load_sweep_state(data_dir / SWEEP_STATE_FILENAME)
        assert list(state.values()) == [date.today()]

    def test_a_dated_intention_is_never_stamped(self, tmp_path, capsys):
        """Dated intentions have a real gate — their due date. Stamping them
        would silence an overdue item after one sighting."""
        first, second, data_dir = self._run_twice(
            tmp_path, capsys, self._line(-4, "Re-check the residency rule"))
        assert (first, second) == (1, 1)
        assert not (data_dir / SWEEP_STATE_FILENAME).exists()

    def test_an_unwritable_data_dir_still_emits_the_nudge(self, tmp_path, capsys):
        """Losing the stamp costs a duplicate nudge. Losing the nudge is the
        failure this channel exists to prevent, so the print comes first."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import session_start

        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        captured = date.today() - timedelta(days=CONDITION_SWEEP_DAYS + 3)
        (memory_dir / PROSPECTIVE_FILENAME).write_text(
            f"- [on: X happens] Do it (captured {captured.isoformat()})",
            encoding="utf-8")
        not_a_dir = tmp_path / "blocked"
        not_a_dir.write_text("x", encoding="utf-8")

        count = session_start._emit_prospective_nudge(memory_dir, not_a_dir)
        assert count == 1
        assert "SYSTEM NUDGE" in capsys.readouterr().out
