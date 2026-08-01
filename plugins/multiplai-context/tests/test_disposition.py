"""Session disposition — `active | parked | done`, inferred from how you left.

Spike's design, and the reason it is worth building: there is **no new verb**.
A `/park` command is a discipline you have to remember at the exact moment you
are overwhelmed and walking away, which is the moment you remember nothing.
Instead you type "park it for now, I'll pick this up tomorrow" as you would
anyway, and the extraction pass that is already reading the whole transcript
for the diary picks it up on the same model call.

Three things are pinned here, matching the plan's criteria 8, 9 and 10:

* **8** — the classifier: "we're done" → `done`, "park it for now" → `parked`,
  no closing signal → `active`.
* **9** — it is written to a **new** registry key. `Session.status`
  (`working | waiting_input | idle | ended`) is frozen in the multiplai-gui API
  contract and describes *liveness*; disposition describes *intent*. A session
  can be `ended` and `parked` at once. Overloading the one field would have
  been the cheap change and the wrong one.
* **10** — a `parked` entry survives GC. The asymmetry it fixes is measured:
  transcripts live a year (`cleanupPeriodDays = 365`), so `claude --resume`
  works months later, but registry entries vanished in 7–30 days. A parked
  idea stayed *resumable* while going *invisible* — the original complaint.

Everything degrades to `active`. A disposition is a convenience bolted onto a
pipeline whose real job is the diary; it may never cost a session its entry.
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import extraction
from lib import fleet
from lib import session_registry as sr
from test_fleet import NOW, make_checkpoint, make_session


def _response(units_xml="<no-units/>", disposition=None):
    """A model response with an optional <disposition> block appended."""
    if disposition is None:
        return units_xml
    state, reason = disposition
    return (
        f"{units_xml}\n"
        "<disposition>\n"
        f"state: {state}\n"
        f"reason: {reason}\n"
        "</disposition>\n"
    )


def _client(content):
    from multiplai_core.model_client import ModelResponse
    c = AsyncMock()
    c.query = AsyncMock(return_value=ModelResponse(content=content))
    return c


# ---------------------------------------------------------------------------
# Criterion 8 — the classifier's output contract
# ---------------------------------------------------------------------------

class TestPromptContract:
    """The model is asked for this and nothing looser."""

    def test_the_prompt_asks_for_a_disposition_block(self):
        assert "<disposition>" in extraction.EXTRACTION_PROMPT
        assert "state: active | parked | done" in extraction.EXTRACTION_PROMPT

    def test_the_prompt_names_the_three_states_and_no_others(self):
        section = extraction.EXTRACTION_PROMPT.split("## Session disposition", 1)[1]
        section = section.split("## Rules", 1)[0]
        for state in extraction.DISPOSITIONS:
            assert f"`{state}`" in section

    def test_the_prompt_tells_it_to_default_to_active_when_unsure(self):
        """The asymmetry matters: labelling live work `done` hides it from the
        fleet view and drops the registry's protection; the reverse costs a
        line."""
        assert "When unsure, emit `active`" in extraction.EXTRACTION_PROMPT

    def test_the_prompt_distinguishes_parked_from_merely_unfinished(self):
        assert "parked means they SAID" in extraction.EXTRACTION_PROMPT


class TestParseDisposition:

    @pytest.mark.parametrize("state", ["active", "parked", "done"])
    def test_each_state_round_trips(self, state):
        raw = _response(disposition=(state, "because"))

        assert extraction.parse_disposition(raw) == {
            "state": state, "reason": "because",
        }

    def test_an_absent_block_is_active(self):
        assert extraction.parse_disposition("<no-units/>") == {
            "state": "active", "reason": "",
        }

    def test_an_unknown_state_is_active(self):
        raw = _response(disposition=("abandoned", "made this up"))

        assert extraction.parse_disposition(raw)["state"] == "active"

    def test_a_malformed_block_is_active(self):
        raw = "<disposition>\ngarbage with no keys\n</disposition>"

        assert extraction.parse_disposition(raw)["state"] == "active"

    def test_a_missing_reason_is_empty_not_a_failure(self):
        raw = "<disposition>\nstate: parked\n</disposition>"

        assert extraction.parse_disposition(raw) == {"state": "parked", "reason": ""}

    def test_the_state_is_case_insensitive(self):
        raw = "<disposition>\nState: DONE\nreason: shipped\n</disposition>"

        assert extraction.parse_disposition(raw)["state"] == "done"

    def test_the_last_block_wins(self):
        raw = (
            "<disposition>\nstate: active\n</disposition>\n"
            "<disposition>\nstate: done\nreason: actually finished\n</disposition>"
        )

        assert extraction.parse_disposition(raw)["state"] == "done"

    def test_empty_input_does_not_raise(self):
        for raw in ("", None):
            assert extraction.parse_disposition(raw)["state"] == "active"


class TestExtractionPath:
    """Criterion 8, end to end through the LLM call."""

    def _extract(self, content):
        return asyncio.run(extraction.extract_units_and_disposition(
            "t", valid_targets=[], client=_client(content),
        ))

    def test_a_session_ending_in_were_done_is_done(self):
        _, d = self._extract(_response(disposition=("done", "user said we're done")))

        assert d["state"] == "done"
        assert d["reason"] == "user said we're done"

    def test_a_session_ending_in_park_it_for_now_is_parked(self):
        _, d = self._extract(_response(disposition=("parked", "park it for now")))

        assert d["state"] == "parked"

    def test_a_session_with_no_closing_signal_is_active(self):
        _, d = self._extract(_response())

        assert d["state"] == "active"

    def test_units_are_returned_unchanged_alongside(self):
        units_xml = (
            "<unit>\n<timestamp>2026-08-01T10:00:00Z</timestamp>\n"
            "<diary>\nDid a thing.\n</diary>\n</unit>"
        )
        units, d = self._extract(_response(units_xml, ("parked", "later")))

        assert len(units) == 1
        assert units[0]["diary_entry"] == "Did a thing."
        assert d["state"] == "parked"

    def test_a_garbled_disposition_does_not_cost_the_units(self):
        """The whole point of parsing it after the units: this field is a
        convenience and must never fail an extraction."""
        units_xml = "<unit>\n<diary>\nWork.\n</diary>\n</unit>"
        units, d = self._extract(units_xml + "\n<disposition>???</disposition>")

        assert len(units) == 1
        assert d["state"] == "active"

    def test_extract_units_still_returns_a_bare_list(self):
        """The old entry point has many callers; its signature is unchanged."""
        result = asyncio.run(extraction.extract_units(
            "t", valid_targets=[], client=_client(_response(disposition=("done", "x"))),
        ))

        assert isinstance(result, list)

    def test_it_costs_no_extra_model_call(self):
        client = _client(_response(disposition=("done", "x")))
        asyncio.run(extraction.extract_units_and_disposition(
            "t", valid_targets=[], client=client,
        ))

        client.query.assert_awaited_once()


# ---------------------------------------------------------------------------
# Criterion 9 — a NEW registry key, not an overload of `status`
# ---------------------------------------------------------------------------

def _entry(data_dir, sid, kind="end", ago=timedelta(days=1), **extra):
    rdir = data_dir / "sessions"
    rdir.mkdir(parents=True, exist_ok=True)
    ts = (datetime.now(timezone.utc) - ago).isoformat()
    path = rdir / f"{sid}.json"
    path.write_text(json.dumps({
        "session_id": sid,
        "cwd": "/work",
        "last_event": {"ts": ts, "kind": kind},
        **extra,
    }))
    return path


class TestRecordDisposition:

    def test_it_writes_under_its_own_key(self, tmp_path):
        path = _entry(tmp_path, "s1")

        assert sr.record_disposition(tmp_path, "s1", "parked", "back tomorrow")

        entry = json.loads(path.read_text())
        assert entry["disposition"]["state"] == "parked"
        assert entry["disposition"]["reason"] == "back tomorrow"
        assert entry["disposition"]["ts"]

    def test_session_status_is_untouched(self, tmp_path):
        """`working | waiting_input | idle | ended` is frozen in the
        multiplai-gui API contract. Disposition is a different axis, and a
        session may be `ended` AND `parked` at the same time."""
        path = _entry(tmp_path, "s1", kind="end")
        before = json.loads(path.read_text())["last_event"]

        sr.record_disposition(tmp_path, "s1", "parked")

        entry = json.loads(path.read_text())
        assert entry["last_event"] == before
        assert entry["last_event"]["kind"] == "end"
        assert entry["disposition"]["state"] == "parked"

    def test_the_code_path_does_not_read_status(self):
        """Asserted against the parsed function body, not the text — the
        docstring names both fields precisely in order to say it leaves them
        alone."""
        import ast

        source = (SCRIPTS_DIR / "lib" / "session_registry.py").read_text()
        fn = next(
            n for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.FunctionDef) and n.name == "record_disposition"
        )
        body = fn.body[1:] if ast.get_docstring(fn) else fn.body
        code = "\n".join(ast.unparse(stmt) for stmt in body)

        assert "last_event" not in code
        assert "status" not in code

    def test_other_keys_are_preserved(self, tmp_path):
        """The hub writes keys this module does not own, and extraction runs
        minutes after the session ended — long enough for it to be resumed."""
        path = _entry(tmp_path, "s1", hub_owned="do not clobber", project="alpha")

        sr.record_disposition(tmp_path, "s1", "done")

        entry = json.loads(path.read_text())
        assert entry["hub_owned"] == "do not clobber"
        assert entry["project"] == "alpha"

    def test_an_unknown_state_is_refused(self, tmp_path):
        path = _entry(tmp_path, "s1")

        assert sr.record_disposition(tmp_path, "s1", "abandoned") is False
        assert "disposition" not in json.loads(path.read_text())

    def test_it_will_not_create_a_missing_entry(self, tmp_path):
        """A disposition with no session is not a parked session — it is a
        stray file GC would have to learn about."""
        (tmp_path / "sessions").mkdir()

        assert sr.record_disposition(tmp_path, "ghost", "parked") is False
        assert not (tmp_path / "sessions" / "ghost.json").exists()

    def test_a_path_traversing_session_id_is_refused(self, tmp_path):
        assert sr.record_disposition(tmp_path, "../escape", "parked") is False
        assert sr.record_disposition(tmp_path, "..", "parked") is False

    def test_an_unparseable_entry_is_not_clobbered(self, tmp_path):
        (tmp_path / "sessions").mkdir()
        path = tmp_path / "sessions" / "s1.json"
        path.write_text("{not json")

        assert sr.record_disposition(tmp_path, "s1", "parked") is False
        assert path.read_text() == "{not json"

    def test_it_never_raises(self, tmp_path):
        assert sr.record_disposition(tmp_path / "nonexistent", "s1", "parked") is False


class TestEntryDisposition:

    def test_absent_is_active(self):
        assert sr.entry_disposition({}) == "active"

    def test_malformed_is_active(self):
        assert sr.entry_disposition({"disposition": "parked"}) == "active"
        assert sr.entry_disposition({"disposition": {"state": "nonsense"}}) == "active"

    def test_a_real_value_is_read(self):
        assert sr.entry_disposition({"disposition": {"state": "done"}}) == "done"


# ---------------------------------------------------------------------------
# Criterion 10 — parked survives GC
# ---------------------------------------------------------------------------

class TestGcExemption:

    def test_a_parked_entry_survives_past_the_ended_window(self, tmp_path):
        _entry(tmp_path, "parked", kind="end", ago=timedelta(days=90),
               disposition={"state": "parked", "reason": "next quarter"})

        assert sr.gc_stale(tmp_path) == 0
        assert (tmp_path / "sessions" / "parked.json").exists()

    def test_a_parked_entry_survives_past_the_live_window_too(self, tmp_path):
        """`GC_LIVE_AFTER_DAYS` is 30 — a parked tab that was never formally
        ended is exactly the case this exists for."""
        _entry(tmp_path, "parked", kind="stop", ago=timedelta(days=400),
               disposition={"state": "parked"})

        assert sr.gc_stale(tmp_path) == 0

    def test_a_done_entry_still_ages_out(self, tmp_path):
        _entry(tmp_path, "done", kind="end", ago=timedelta(days=8),
               disposition={"state": "done", "reason": "shipped"})

        assert sr.gc_stale(tmp_path) == 1
        assert not (tmp_path / "sessions" / "done.json").exists()

    def test_an_unlabelled_entry_still_ages_out(self, tmp_path):
        _entry(tmp_path, "plain", kind="end", ago=timedelta(days=8))

        assert sr.gc_stale(tmp_path) == 1

    def test_a_fresh_parked_entry_also_survives(self, tmp_path):
        _entry(tmp_path, "parked", kind="end", ago=timedelta(hours=1),
               disposition={"state": "parked"})

        assert sr.gc_stale(tmp_path) == 0

    def test_the_exemption_does_not_rescue_unparseable_entries(self, tmp_path):
        """No disposition can be read from broken JSON, so the existing
        mtime-based removal is unchanged."""
        (tmp_path / "sessions").mkdir()
        path = tmp_path / "sessions" / "junk.json"
        path.write_text("{not json")
        old = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
        import os
        os.utime(path, (old, old))

        assert sr.gc_stale(tmp_path) == 1


# ---------------------------------------------------------------------------
# The wiring — extract_learnings.py, where the three pieces meet
# ---------------------------------------------------------------------------

class TestExtractLearningsWiring:

    def _run(self, tmp_path, chunk_results, *, session_id="s1"):
        """Drive ``extract()`` with a canned per-chunk extraction result.

        Each item is either a ``(units, disposition)`` pair or an exception to
        raise for that chunk.
        """
        import extract_learnings as el

        class _Paths:
            def memory_dir(self): return tmp_path / "memory"
            def learnings_file(self): return tmp_path / "memory" / "learnings.md"
            def diary_dir(self): return tmp_path / "diary"
            def catalogs_dir(self): return tmp_path / "catalogs"
            def data_dir(self): return tmp_path

        async def _fake_extract(chunk, **kwargs):
            result = chunk_results[int(chunk)]
            if isinstance(result, Exception):
                raise result
            return result

        stdin = json.dumps({"session_id": session_id, "cwd": "/work"})
        with patch.object(el, "get_paths", _Paths), \
             patch.object(el, "load_target_charters", lambda *a, **k: []), \
             patch.object(el, "create_client", AsyncMock(return_value=object())), \
             patch.object(el, "_distill_transcript",
                          lambda *a: [str(i) for i in range(len(chunk_results))]), \
             patch.object(el, "extract_units_and_disposition", _fake_extract), \
             patch.object(el.sys, "stdin", type("S", (), {"read": staticmethod(lambda: stdin)})):
            return asyncio.run(el.extract())

    def _state(self, tmp_path, sid="s1"):
        entry = json.loads((tmp_path / "sessions" / f"{sid}.json").read_text())
        return entry.get("disposition", {}).get("state")

    def test_the_disposition_reaches_the_registry(self, tmp_path):
        _entry(tmp_path, "s1")

        self._run(tmp_path, [([], {"state": "parked", "reason": "tomorrow"})])

        assert self._state(tmp_path) == "parked"

    def test_only_the_final_chunk_counts(self, tmp_path):
        """Extraction is per chunk but disposition is per session, and the
        closing exchange is the entire signal — earlier chunks end mid-work
        and would always say `active`."""
        _entry(tmp_path, "s1")

        self._run(tmp_path, [
            ([], {"state": "active", "reason": ""}),
            ([], {"state": "active", "reason": ""}),
            ([], {"state": "done", "reason": "shipped"}),
        ])

        assert self._state(tmp_path) == "done"

    def test_a_session_with_nothing_to_diarise_is_still_labelled(self, tmp_path):
        """A session that is just "park it, I'm out" produces no units at all
        — and is exactly the session that most needs the label."""
        _entry(tmp_path, "s1")

        assert self._run(tmp_path, [([], {"state": "parked", "reason": "out"})]) is True
        assert self._state(tmp_path) == "parked"

    def test_a_failed_llm_records_nothing(self, tmp_path):
        """The marker is retained for retry, so a fabricated `active` would be
        a guess written as a fact — and would strip a parked session's GC
        protection on the way."""
        _entry(tmp_path, "s1")

        assert self._run(tmp_path, [RuntimeError("boom")]) is False

        assert self._state(tmp_path) is None

    def test_a_failure_in_an_earlier_chunk_still_blocks_the_write(self, tmp_path):
        _entry(tmp_path, "s1")

        self._run(tmp_path, [RuntimeError("boom"), ([], {"state": "done", "reason": "x"})])

        assert self._state(tmp_path) is None

    def test_no_registry_entry_is_not_an_error(self, tmp_path):
        (tmp_path / "sessions").mkdir()

        assert self._run(tmp_path, [([], {"state": "parked", "reason": "x"})]) is True


# ---------------------------------------------------------------------------
# 4d — how it reads in AGENTS.md
# ---------------------------------------------------------------------------

class TestFleetView:

    def test_a_parked_session_reads_as_parked_not_idle(self, tmp_path):
        make_session(tmp_path, "p", kind="stop", ago=timedelta(days=3))
        _add_disposition(tmp_path, "p", "parked", "picking this up after the release")

        md = fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)

        assert "## Parked (1)" in md
        assert "## Idle" not in md
        assert "picking this up after the release" in md

    def test_a_parked_session_survives_its_container_ending(self, tmp_path):
        """The whole point: you closed the tab, and it is still on the list."""
        make_session(tmp_path, "p", kind="end")
        _add_disposition(tmp_path, "p", "parked", "tomorrow")

        f = fleet.collect(tmp_path, NOW)

        assert f.agents[0].live is True
        assert "## Parked (1)" in fleet.render_agents_md(f, NOW)

    def test_a_done_session_drops_off_even_while_running(self, tmp_path):
        make_session(tmp_path, "d", kind="stop", ago=timedelta(minutes=2))
        _add_disposition(tmp_path, "d", "done", "shipped it")

        f = fleet.collect(tmp_path, NOW)
        md = fleet.render_agents_md(f, NOW)

        assert f.agents[0].live is False
        assert "## Working" not in md
        assert "1 finished, not listed" in md

    def test_an_active_session_is_unaffected(self, tmp_path):
        make_session(tmp_path, "a", kind="stop", ago=timedelta(minutes=2))
        _add_disposition(tmp_path, "a", "active", "still going")

        assert "## Working (1)" in fleet.render_agents_md(fleet.collect(tmp_path, NOW), NOW)

    def test_a_parked_session_still_collides(self, tmp_path):
        """Parked work holds its files just as much as running work — arguably
        more, since nobody is watching it."""
        make_session(tmp_path, "p", kind="end", project="alpha")
        make_session(tmp_path, "b", project="beta")
        make_checkpoint(tmp_path, "p", files=("src/shared.py",))
        make_checkpoint(tmp_path, "b", files=("src/shared.py",))
        _add_disposition(tmp_path, "p", "parked")

        assert len(fleet.collect(tmp_path, NOW).collisions) == 1

    def test_a_parked_session_counts_as_a_front(self, tmp_path):
        make_session(tmp_path, "p", kind="end")
        _add_disposition(tmp_path, "p", "parked")

        assert "1 front" in fleet.render_fleet_line(fleet.collect(tmp_path, NOW), NOW)

    def test_the_header_and_the_body_agree(self, tmp_path):
        """Deriving both from `Agent.group` is what stops the count drifting
        from what is actually listed."""
        make_session(tmp_path, "w", ago=timedelta(minutes=2))
        make_session(tmp_path, "n", kind="notification", ago=timedelta(minutes=2))
        make_session(tmp_path, "i", ago=timedelta(days=2))
        make_session(tmp_path, "p", kind="end")
        make_session(tmp_path, "e", kind="end")
        _add_disposition(tmp_path, "p", "parked")

        f = fleet.collect(tmp_path, NOW)
        md = fleet.render_agents_md(f, NOW)
        listed = sum(len(f.in_group(g)) for g in ("Needs you", "Working", "Parked", "Idle"))

        assert listed == len(f.live) == 4
        assert "4 live" in md
        assert "1 finished, not listed" in md

    def test_a_malformed_disposition_reads_as_active(self, tmp_path):
        make_session(tmp_path, "a", ago=timedelta(minutes=2))
        path = tmp_path / "sessions" / "a.json"
        entry = json.loads(path.read_text())
        entry["disposition"] = "parked"  # a string, not the block
        path.write_text(json.dumps(entry))

        assert fleet.collect(tmp_path, NOW).agents[0].disposition == "active"


def _add_disposition(data_dir, sid, state, reason=""):
    path = data_dir / "sessions" / f"{sid}.json"
    entry = json.loads(path.read_text())
    entry["disposition"] = {"state": state, "reason": reason, "ts": NOW.isoformat()}
    path.write_text(json.dumps(entry))
