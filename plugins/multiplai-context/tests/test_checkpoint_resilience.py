"""A failed save leaves something behind, says so, and can be found again.

Three defects this pins, all from the 2026-08-08 incident:

* **A failed write left nothing and the failure fed itself.** The bookmark only
  advanced on success, so every retry re-distilled a bigger backlog than the
  last — 174,154 characters against a healthy 23,287 — until the model call
  could not finish at all. The fix is a degraded write: previous checkpoint
  verbatim plus the raw unsummarised window, and only then the bookmark moves.
  Moving the bookmark WITHOUT keeping the content would discard the window
  silently, which is why the two go together.
* **Eight consecutive failures over 18 hours reached nobody.** For a component
  whose entire job is not losing work, silence is the wrong default.
* **A checkpoint nothing could find.** The rebuild pointer was written only
  after a successful write AND only above 200K tokens, and was keyed by
  project alone — so a clean 143K session left nothing restorable, and two
  windows on one project overwrote each other's pointer.
"""

import asyncio
import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from conftest import import_script
from lib import checkpoint as cp

checkpoint_writer = import_script("checkpoint_writer_resilience", "checkpoint_writer.py")
session_stop = import_script("session_stop_resilience", "session_stop.py")

VALID_CHECKPOINT = "\n".join(
    f"## {s}\n- state for {s.lower()}" for s in cp.CHECKPOINT_SECTIONS
)


@pytest.fixture
def data_env(tmp_path, monkeypatch):
    from multiplai_core.paths import _reset_cache

    data_dir = tmp_path / "data"
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_DATA_DIR", str(data_dir))
    _reset_cache()
    yield data_dir
    _reset_cache()


def _write_transcript(path, *, tokens=110_000, text="working on the widget"):
    """A transcript with one user turn, one tool call and a usage record."""
    now = datetime.now(timezone.utc).isoformat()
    records = [
        {"type": "user", "timestamp": now, "cwd": "/work/proj",
         "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
        {"type": "assistant", "timestamp": now, "cwd": "/work/proj",
         "message": {
             "role": "assistant",
             "content": [
                 {"type": "text", "text": "on it"},
                 {"type": "tool_use", "name": "Edit", "id": "t1",
                  "input": {"file_path": "/work/proj/widget.py"}},
             ],
             "usage": {"input_tokens": 1_000,
                       "cache_read_input_tokens": tokens - 1_000,
                       "cache_creation_input_tokens": 0},
         }},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _payload(session_id, transcript, tokens=110_000, reason="band"):
    return {
        "session_id": session_id,
        "transcript_path": str(transcript),
        "cwd": "/work/proj",
        "tokens": tokens,
        "reason": reason,
    }


def _timeout(*a, **k):
    async def boom(prompt, **kwargs):
        raise TimeoutError("run_agent [checkpoint:s1] timed out after 600s")

    return boom


def _good(text=VALID_CHECKPOINT):
    async def fake(prompt, **kwargs):
        return SimpleNamespace(text=text)

    return fake


# ---------------------------------------------------------------------------
# Criterion 5 — a timed-out save still leaves a file, and moves the bookmark
# ---------------------------------------------------------------------------

class TestDegradedWrite:

    def test_a_timed_out_call_still_leaves_a_checkpoint(
        self, tmp_path, data_env, monkeypatch
    ):
        cp.write_checkpoint_file(data_env, "s1", VALID_CHECKPOINT)
        transcript = _write_transcript(tmp_path / "t.jsonl")
        monkeypatch.setattr(checkpoint_writer, "run_agent", _timeout())

        ok = asyncio.run(
            checkpoint_writer.write_checkpoint(_payload("s1", transcript))
        )

        assert ok is False  # honest: this was not a clean write
        written = cp.checkpoint_file(data_env, "s1")
        assert written.exists()
        text = written.read_text()
        assert text.startswith(VALID_CHECKPOINT)
        assert cp.validate_checkpoint(text)

    def test_a_timed_out_call_advances_the_bookmark(
        self, tmp_path, data_env, monkeypatch
    ):
        """The absorbing-failure fix. The bookmark advances because the window
        it covers is inside the file — advancing it alone would discard the
        window, keeping the content is what makes advancing honest."""
        cp.write_checkpoint_file(data_env, "s1", VALID_CHECKPOINT)
        transcript = _write_transcript(tmp_path / "t.jsonl")
        monkeypatch.setattr(checkpoint_writer, "run_agent", _timeout())

        asyncio.run(checkpoint_writer.write_checkpoint(_payload("s1", transcript)))

        state = cp.load_state(data_env, "s1")
        assert state.get("last_checkpoint_ts")
        assert state.get("last_reason") == "band-degraded"

    def test_the_raw_window_is_kept_not_invented(
        self, tmp_path, data_env, monkeypatch
    ):
        cp.write_checkpoint_file(data_env, "s1", VALID_CHECKPOINT)
        transcript = _write_transcript(
            tmp_path / "t.jsonl", text="please rename the widget module"
        )
        monkeypatch.setattr(checkpoint_writer, "run_agent", _timeout())

        asyncio.run(checkpoint_writer.write_checkpoint(_payload("s1", transcript)))

        text = cp.checkpoint_file(data_env, "s1").read_text()
        assert "## Unsummarised since" in text
        assert "please rename the widget module" in text
        assert "tools: Edit" in text

    def test_with_no_previous_checkpoint_nothing_is_fabricated(
        self, tmp_path, data_env, monkeypatch
    ):
        """The stop-and-ask gate, answered by declining rather than by
        relaxing the validator: with nothing to carry forward, the six
        sections it requires could only be filled by inventing them. So this
        case writes nothing and leaves the bookmark alone — the window is
        retried, never skipped."""
        transcript = _write_transcript(tmp_path / "t.jsonl")
        monkeypatch.setattr(checkpoint_writer, "run_agent", _timeout())

        ok = asyncio.run(checkpoint_writer.write_checkpoint(_payload("s1", transcript)))

        assert ok is False
        assert not cp.checkpoint_file(data_env, "s1").exists()
        assert cp.load_state(data_env, "s1").get("last_checkpoint_ts") is None
        assert cp.consecutive_failures(cp.load_state(data_env, "s1")) == 1

    def test_a_run_of_degraded_writes_stays_bounded(
        self, tmp_path, data_env, monkeypatch
    ):
        """Each failure appends one section, so without a cap a bad afternoon
        would grow checkpoint.md without limit."""
        cp.write_checkpoint_file(data_env, "s1", VALID_CHECKPOINT)
        monkeypatch.setattr(checkpoint_writer, "run_agent", _timeout())

        for i in range(6):
            transcript = _write_transcript(tmp_path / f"t{i}.jsonl", text=f"turn {i}")
            asyncio.run(
                checkpoint_writer.write_checkpoint(_payload("s1", transcript))
            )

        text = cp.checkpoint_file(data_env, "s1").read_text()
        assert text.count("## Unsummarised since") == checkpoint_writer._MAX_DEGRADED_SECTIONS
        assert cp.validate_checkpoint(text)

    def test_invalid_output_degrades_the_same_way(
        self, tmp_path, data_env, monkeypatch
    ):
        cp.write_checkpoint_file(data_env, "s1", VALID_CHECKPOINT)
        transcript = _write_transcript(tmp_path / "t.jsonl")
        monkeypatch.setattr(
            checkpoint_writer, "run_agent", _good("I could not comply.")
        )

        ok = asyncio.run(checkpoint_writer.write_checkpoint(_payload("s1", transcript)))

        assert ok is False
        assert "## Unsummarised since" in cp.checkpoint_file(data_env, "s1").read_text()

    def test_nothing_new_to_write_is_not_a_failure(
        self, tmp_path, data_env, monkeypatch
    ):
        """An idle session must never read as degraded."""
        transcript = tmp_path / "empty.jsonl"
        transcript.write_text("")
        monkeypatch.setattr(checkpoint_writer, "run_agent", _timeout())

        asyncio.run(checkpoint_writer.write_checkpoint(_payload("s1", transcript)))

        assert cp.consecutive_failures(cp.load_state(data_env, "s1")) == 0


class TestDegradedSectionBuilder:

    def test_it_uses_no_model_and_invents_nothing(self):
        segment = (
            "[2026-08-09T12:00:00+00:00] [proj] user: rename the widget\n\n"
            "[2026-08-09T12:01:00+00:00] [proj] assistant: sure "
            "[call Edit({\"file_path\": \"/w/x.py\"})] [call Bash({\"cmd\": \"ls\"})]"
        )

        out = checkpoint_writer.build_degraded_section(segment, "2026-08-09T11:00:00+00:00")

        assert "rename the widget" in out
        assert "tools: Edit, Bash" in out
        assert out.startswith("## Unsummarised since 2026-08-09T11:00:00+00:00")

    def test_an_empty_window_says_so_rather_than_guessing(self):
        out = checkpoint_writer.build_degraded_section("", None)
        assert "no user turns or tool calls" in out

    def test_it_refuses_to_build_on_an_invalid_previous_checkpoint(self):
        assert checkpoint_writer.build_degraded_checkpoint("", "seg", None) == ""
        assert checkpoint_writer.build_degraded_checkpoint(
            "## Notes\n- only one section", "seg", None
        ) == ""


# ---------------------------------------------------------------------------
# Criterion 6 — two failures in a row reach the user; a success resets it
# ---------------------------------------------------------------------------

class TestDegradedIsSurfaced:

    def _stop(self, monkeypatch, capsys, tmp_path, tokens=110_000, session_id="s1"):
        payload = {
            "session_id": session_id,
            "transcript_path": str(
                _write_transcript(tmp_path / f"stop-{session_id}.jsonl", tokens=tokens)
            ),
            "cwd": str(tmp_path / "proj"),
        }
        monkeypatch.setattr(session_stop.cp, "spawn_writer", lambda p: True)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        session_stop.main()
        return capsys.readouterr().out

    def _fail_writes(self, tmp_path, data_env, monkeypatch, n):
        cp.write_checkpoint_file(data_env, "s1", VALID_CHECKPOINT)
        monkeypatch.setattr(checkpoint_writer, "run_agent", _timeout())
        for i in range(n):
            transcript = _write_transcript(tmp_path / f"w{i}.jsonl", text=f"turn {i}")
            asyncio.run(checkpoint_writer.write_checkpoint(_payload("s1", transcript)))

    def test_one_failure_stays_quiet(self, tmp_path, data_env, monkeypatch, capsys):
        """One failure is a retryable blip — the next Stop refires."""
        self._fail_writes(tmp_path, data_env, monkeypatch, 1)

        out = self._stop(monkeypatch, capsys, tmp_path)

        assert out.strip() == ""

    def test_two_failures_produce_a_system_message(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        self._fail_writes(tmp_path, data_env, monkeypatch, 2)

        out = self._stop(monkeypatch, capsys, tmp_path)

        frame = json.loads(out)
        assert frame["systemMessage"]
        assert "checkpoint" in frame["systemMessage"].lower()
        # Never a decision: this hook must not fight a /goal loop.
        assert "decision" not in frame

    def test_it_is_said_once_per_new_failure_not_once_per_stop(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        self._fail_writes(tmp_path, data_env, monkeypatch, 2)

        assert self._stop(monkeypatch, capsys, tmp_path).strip() != ""
        assert self._stop(monkeypatch, capsys, tmp_path).strip() == ""

    def test_a_later_success_resets_the_counter(
        self, tmp_path, data_env, monkeypatch
    ):
        self._fail_writes(tmp_path, data_env, monkeypatch, 2)
        assert cp.consecutive_failures(cp.load_state(data_env, "s1")) == 2

        monkeypatch.setattr(checkpoint_writer, "run_agent", _good())
        transcript = _write_transcript(tmp_path / "good.jsonl", text="recovered")
        ok = asyncio.run(checkpoint_writer.write_checkpoint(_payload("s1", transcript)))

        assert ok is True
        state = cp.load_state(data_env, "s1")
        assert cp.consecutive_failures(state) == 0
        assert state.get("last_reason") == "band"
        assert "last_failure" not in state


# ---------------------------------------------------------------------------
# Criteria 7 & 8 — the pointer exists, and windows stop clobbering each other
# ---------------------------------------------------------------------------

class TestPointer:

    def test_a_session_below_the_handoff_threshold_gets_a_pointer(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        """The 143K case: a clean session, a perfectly good checkpoint, and
        nothing on disk that could ever find it."""
        cp.write_checkpoint_file(data_env, "s1", VALID_CHECKPOINT)
        payload = {
            "session_id": "s1",
            "transcript_path": str(
                _write_transcript(tmp_path / "t.jsonl", tokens=120_000)
            ),
            "cwd": str(tmp_path / "proj"),
        }
        monkeypatch.setattr(session_stop.cp, "spawn_writer", lambda p: True)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        session_stop.main()
        capsys.readouterr()

        claimed = cp.consume_pending_marker(
            data_env, str(tmp_path / "proj"), "s2", cp.load_config()
        )
        assert claimed is not None
        assert claimed["session_id"] == "s1"
        assert claimed["tokens"] == 120_000

    def test_a_session_whose_latest_write_failed_still_gets_one(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        """Root cause A caused root cause B: every write failed, so no marker
        was ever written, so there was nothing for the next SessionStart to
        claim. A stale checkpoint that restores beats a fresh one that does
        not exist."""
        cp.write_checkpoint_file(data_env, "s1", VALID_CHECKPOINT)
        monkeypatch.setattr(checkpoint_writer, "run_agent", _timeout())
        asyncio.run(
            checkpoint_writer.write_checkpoint(
                _payload("s1", _write_transcript(tmp_path / "w.jsonl"))
            )
        )
        # Now a Stop with the writer failing again — the marker must still be
        # there, refreshed independently of this run's outcome.
        payload = {
            "session_id": "s1",
            "transcript_path": str(
                _write_transcript(tmp_path / "t.jsonl", tokens=90_000)
            ),
            "cwd": str(tmp_path / "proj"),
        }
        monkeypatch.setattr(session_stop.cp, "spawn_writer", lambda p: False)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        session_stop.main()
        capsys.readouterr()

        claimed = cp.consume_pending_marker(
            data_env, str(tmp_path / "proj"), "s2", cp.load_config()
        )
        assert claimed is not None and claimed["session_id"] == "s1"

    def test_two_windows_on_one_project_do_not_overwrite_each_other(
        self, data_env, monkeypatch
    ):
        """The clobber, directly: both sessions are legitimately "DolceBot",
        and last writer wins. On 2026-08-08 that handed a cleared pi-eval
        window a docker window's checkpoint."""
        monkeypatch.setenv("HOSTNAME", "claude-work-04221854")
        first = cp.write_pending_marker(data_env, "/work/DolceBot", "pieval", 263_000)

        monkeypatch.setenv("HOSTNAME", "claude-work-08122648")
        second = cp.write_pending_marker(data_env, "/work/DolceBot", "docker", 143_000)

        assert first != second
        assert first.exists() and second.exists()
        assert json.loads(first.read_text())["session_id"] == "pieval"
        assert json.loads(second.read_text())["session_id"] == "docker"

    def test_the_window_that_was_cleared_gets_its_own_checkpoint_back(
        self, data_env, monkeypatch
    ):
        """`/clear` keeps the same container — verified in the field, sessions
        24c0a766 and 2e29e3cb one second apart on claude-work-04221854 — so
        hostname is what tells the two windows apart."""
        monkeypatch.setenv("HOSTNAME", "claude-work-04221854")
        cp.write_pending_marker(data_env, "/work/DolceBot", "pieval", 263_000)
        monkeypatch.setenv("HOSTNAME", "claude-work-08122648")
        cp.write_pending_marker(data_env, "/work/DolceBot", "docker", 143_000)

        monkeypatch.setenv("HOSTNAME", "claude-work-04221854")
        claimed = cp.consume_pending_marker(
            data_env, "/work/DolceBot", "post-clear", cp.load_config()
        )

        assert claimed is not None
        assert claimed["session_id"] == "pieval"

    def test_a_legacy_project_only_marker_is_still_claimable(
        self, data_env, monkeypatch
    ):
        """Markers written before this change are keyed by project alone.
        Dropping them on upgrade would lose a live handoff."""
        legacy = (
            cp.checkpoints_root(data_env) / "pending"
            / f"{cp._project_key('/work/DolceBot')}.json"
        )
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({
            "session_id": "old",
            "cwd": "/work/DolceBot",
            "tokens": 210_000,
            "checkpoint_path": str(cp.checkpoint_file(data_env, "old")),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }))
        monkeypatch.setenv("HOSTNAME", "claude-work-04221854")

        claimed = cp.consume_pending_marker(
            data_env, "/work/DolceBot", "new", cp.load_config()
        )

        assert claimed is not None and claimed["session_id"] == "old"

    def test_ttl_and_claim_semantics_are_unchanged(self, data_env, monkeypatch):
        """Explicitly out of scope for this change: expiry is still
        ttl_hours, and a claim still removes the marker."""
        monkeypatch.setenv("HOSTNAME", "host-a")
        cfg = cp.load_config()
        assert cfg.ttl_hours == 6.0

        cp.write_pending_marker(data_env, "/work/proj", "s1", 10)
        assert cp.consume_pending_marker(data_env, "/work/proj", "s2", cfg) is not None
        assert cp.consume_pending_marker(data_env, "/work/proj", "s3", cfg) is None

    def test_an_expired_marker_is_still_discarded(self, data_env, monkeypatch):
        monkeypatch.setenv("HOSTNAME", "host-a")
        marker = cp.write_pending_marker(data_env, "/work/proj", "s1", 10)
        payload = json.loads(marker.read_text())
        payload["created_at"] = "2020-01-01T00:00:00+00:00"
        marker.write_text(json.dumps(payload))

        assert cp.consume_pending_marker(
            data_env, "/work/proj", "s2", cp.load_config()
        ) is None

    def test_the_retirement_guard_still_finds_a_host_keyed_marker(
        self, data_env, monkeypatch
    ):
        """`pending_marker_owner` is what saved the incident file from
        deletion. It scans, so the new filename must not hide markers from
        it."""
        monkeypatch.setenv("HOSTNAME", "host-a")
        cp.write_pending_marker(data_env, "/work/proj", "s1", 10)
        cp.session_dir(data_env, "s1").mkdir(parents=True, exist_ok=True)
        cp.write_checkpoint_file(data_env, "s1", VALID_CHECKPOINT)

        assert cp.pending_marker_owner(data_env, "s1") is not None
        removed, kept = cp.retire_checkpoint(data_env, "s1")
        assert removed is False and "pending rebuild marker" in kept


class TestTheAlertKeepsWorkingAfterARecovery:
    """The high-water mark must reset, or the alert quietly dies.

    ``_degraded_message`` suppresses when the mark it stored is >= the current
    failure count. Nothing reset that mark on a successful write, so the
    SECOND run of failures in a session's life (2 failures against a stored 2)
    was silent, and every later incident needed a strictly longer run than the
    last to be heard. The guarantee this PR advertises — two in a row tells you
    — decayed to nothing over a long session.
    """

    def _fail_writes(self, tmp_path, data_env, monkeypatch, n, start=0):
        cp.write_checkpoint_file(data_env, "s1", VALID_CHECKPOINT)
        monkeypatch.setattr(checkpoint_writer, "run_agent", _timeout())
        for i in range(n):
            transcript = _write_transcript(
                tmp_path / f"f{start + i}.jsonl", text=f"turn {start + i}"
            )
            asyncio.run(checkpoint_writer.write_checkpoint(_payload("s1", transcript)))

    def _stop(self, monkeypatch, capsys, tmp_path, n=0):
        payload = {
            "session_id": "s1",
            "transcript_path": str(_write_transcript(tmp_path / f"stop{n}.jsonl")),
            "cwd": str(tmp_path / "proj"),
        }
        monkeypatch.setattr(session_stop.cp, "spawn_writer", lambda p: True)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        session_stop.main()
        return capsys.readouterr().out

    def test_a_second_run_of_failures_is_still_reported(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        self._fail_writes(tmp_path, data_env, monkeypatch, 2)
        assert self._stop(monkeypatch, capsys, tmp_path, 1).strip() != ""

        monkeypatch.setattr(checkpoint_writer, "run_agent", _good())
        asyncio.run(checkpoint_writer.write_checkpoint(
            _payload("s1", _write_transcript(tmp_path / "ok.jsonl", text="recovered"))
        ))
        # The success itself says nothing, and clears the mark on its way past.
        assert self._stop(monkeypatch, capsys, tmp_path, 2).strip() == ""

        self._fail_writes(tmp_path, data_env, monkeypatch, 2, start=10)

        out = self._stop(monkeypatch, capsys, tmp_path, 3)
        assert json.loads(out)["systemMessage"]

    def test_the_mark_is_dropped_the_moment_a_write_succeeds(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        self._fail_writes(tmp_path, data_env, monkeypatch, 2)
        self._stop(monkeypatch, capsys, tmp_path, 1)
        assert session_stop._degraded_file(data_env, "s1").exists()

        monkeypatch.setattr(checkpoint_writer, "run_agent", _good())
        asyncio.run(checkpoint_writer.write_checkpoint(
            _payload("s1", _write_transcript(tmp_path / "ok.jsonl", text="recovered"))
        ))
        self._stop(monkeypatch, capsys, tmp_path, 2)

        assert not session_stop._degraded_file(data_env, "s1").exists()

    def test_one_failure_does_not_clear_the_mark(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        """Only a success resets. A single failure is mid-run, not recovery."""
        self._fail_writes(tmp_path, data_env, monkeypatch, 2)
        self._stop(monkeypatch, capsys, tmp_path, 1)
        self._fail_writes(tmp_path, data_env, monkeypatch, 1, start=20)

        self._stop(monkeypatch, capsys, tmp_path, 2)

        assert session_stop._degraded_file(data_env, "s1").exists()


# ---------------------------------------------------------------------------
# A queued end-of-session failure has exactly one place left to be reported
# ---------------------------------------------------------------------------

class TestTheExitCodeCarriesTheOutcome:
    """The session that queued the checkpoint has ended.

    So ``session_stop``'s degraded message will never fire for it again, and
    the waiting host drain is the only thing left that can notice. A writer
    that always exited 0 made ``process_pending_checkpoints(wait=True).failed``
    permanently zero — the report existed but was unreachable.
    """

    def _run_main(self, monkeypatch, payload):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        return checkpoint_writer.main()

    def test_a_degraded_write_exits_nonzero(self, tmp_path, data_env, monkeypatch):
        cp.write_checkpoint_file(data_env, "s1", VALID_CHECKPOINT)
        monkeypatch.setattr(checkpoint_writer, "run_agent", _timeout())

        code = self._run_main(
            monkeypatch,
            _payload("s1", _write_transcript(tmp_path / "t.jsonl")),
        )

        assert code == 1

    def test_a_good_write_exits_zero(self, tmp_path, data_env, monkeypatch):
        monkeypatch.setattr(checkpoint_writer, "run_agent", _good())

        code = self._run_main(
            monkeypatch,
            _payload("s1", _write_transcript(tmp_path / "t.jsonl")),
        )

        assert code == 0

    def test_nothing_new_to_write_is_not_a_failure(
        self, tmp_path, data_env, monkeypatch
    ):
        """``write_checkpoint`` returns False for an idle session too. The exit
        code follows the failure COUNTER, not that bool, so an idle drain does
        not cry wolf."""
        monkeypatch.setattr(checkpoint_writer, "run_agent", _good())
        empty = tmp_path / "empty.jsonl"
        empty.write_text("")

        code = self._run_main(monkeypatch, _payload("s1", empty))

        assert code == 0

    def test_the_marker_is_still_dropped_on_a_failing_run(
        self, tmp_path, data_env, monkeypatch
    ):
        """A non-zero exit must not resurrect the queue marker — the drain
        reports the failure, it does not retry it."""
        cp.write_checkpoint_file(data_env, "s1", VALID_CHECKPOINT)
        monkeypatch.setattr(checkpoint_writer, "run_agent", _timeout())
        marker = tmp_path / "marker.json"
        marker.write_text("{}")
        payload = _payload("s1", _write_transcript(tmp_path / "t.jsonl"))
        payload["marker_path"] = str(marker)

        assert self._run_main(monkeypatch, payload) == 1
        assert not marker.exists()


class TestDegradedSectionsDoNotAccumulateOnSuccess:

    def test_a_section_the_model_copied_through_is_bounded(
        self, tmp_path, data_env, monkeypatch
    ):
        """The fold-and-drop instruction is a prompt rule now, but a model that
        ignores it must not grow checkpoint.md without limit. Bounded, not
        stripped: stripping would destroy the raw window in exactly the case
        where the model did NOT fold it in."""
        sections = "\n\n".join(
            checkpoint_writer.build_degraded_section("", f"2026-08-0{i}T00:00:00Z")
            for i in range(1, 7)
        )
        monkeypatch.setattr(
            checkpoint_writer, "run_agent", _good(VALID_CHECKPOINT + "\n\n" + sections)
        )

        ok = asyncio.run(checkpoint_writer.write_checkpoint(
            _payload("s1", _write_transcript(tmp_path / "t.jsonl"))
        ))

        assert ok is True
        written = cp.checkpoint_file(data_env, "s1").read_text()
        assert written.count(checkpoint_writer._DEGRADED_HEADING) == (
            checkpoint_writer._MAX_DEGRADED_SECTIONS
        )

    def test_the_prompt_tells_the_model_to_drop_them(self):
        """The instruction has to reach the model as a RULE. Living only inside
        the checkpoint text made it data the model was free to copy."""
        prompt = checkpoint_writer.build_writer_prompt(VALID_CHECKPOINT, "seg")

        assert checkpoint_writer._DEGRADED_HEADING in prompt
        assert "no such section" in prompt


class TestTimeout:

    def test_the_writer_timeout_is_ten_minutes(self):
        """240s was measured sitting on the boundary: attempt 1 timed out at
        240s and attempt 2 finished at 480s, which made a slow write a coin
        flip that lost eight times running."""
        assert cp.CheckpointConfig().timeout_s == 600

    def test_it_is_still_overridable(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_CHECKPOINT_TIMEOUT_S", "120")
        assert cp.load_config().timeout_s == 120
