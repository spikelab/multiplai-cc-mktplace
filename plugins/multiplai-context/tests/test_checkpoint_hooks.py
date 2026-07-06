"""Tests for the hook wiring: session_stop checkpoint pass, checkpoint_nudge,
and session_start rebuild injection.

Goal-safety is asserted throughout: the Stop hook must never emit a
``decision`` key (it would fight /goal loops), and child sessions
(subagents / nested hook sessions) must be ignored entirely.
"""

import io
import json
from datetime import datetime, timezone

import pytest

from conftest import import_script
from lib import checkpoint as cp

session_stop = import_script("session_stop_mod", "session_stop.py")
checkpoint_nudge = import_script("checkpoint_nudge_mod", "checkpoint_nudge.py")
session_start = import_script("session_start_mod", "session_start.py")

VALID_CHECKPOINT = "\n".join(
    f"## {s}\n- state for {s.lower()}" for s in cp.CHECKPOINT_SECTIONS
)


@pytest.fixture
def data_env(tmp_path, monkeypatch):
    from multiplai_core.paths import _reset_cache

    data_dir = tmp_path / "data"
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_data_dir", str(data_dir))
    _reset_cache()
    yield data_dir
    _reset_cache()


def _transcript(tmp_path, tokens, name="t.jsonl"):
    """Write a transcript whose last assistant record reports *tokens*."""
    rec = {
        "type": "assistant",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "working"}],
            "usage": {
                "input_tokens": 1_000,
                "cache_read_input_tokens": tokens - 1_000,
                "cache_creation_input_tokens": 0,
            },
        },
    }
    path = tmp_path / name
    path.write_text(json.dumps(rec) + "\n")
    return path


def _stop_payload(tmp_path, tokens, session_id="sess-1"):
    return {
        "session_id": session_id,
        "transcript_path": str(_transcript(tmp_path, tokens)),
        "cwd": str(tmp_path / "proj"),
    }


class _SpawnRecorder:
    def __init__(self):
        self.payloads = []

    def __call__(self, payload):
        self.payloads.append(payload)
        return True


def _run_stop(monkeypatch, capsys, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    session_stop.main()
    return capsys.readouterr().out


class TestSessionStopCheckpoint:
    def test_below_band_no_spawn(self, tmp_path, data_env, monkeypatch, capsys):
        rec = _SpawnRecorder()
        monkeypatch.setattr(session_stop, "_spawn_writer", rec)
        out = _run_stop(monkeypatch, capsys, _stop_payload(tmp_path, 60_000))
        assert rec.payloads == []
        assert out.strip() == ""

    def test_band_crossing_spawns_writer(self, tmp_path, data_env, monkeypatch, capsys):
        rec = _SpawnRecorder()
        monkeypatch.setattr(session_stop, "_spawn_writer", rec)
        _run_stop(monkeypatch, capsys, _stop_payload(tmp_path, 110_000))
        assert len(rec.payloads) == 1
        assert rec.payloads[0]["reason"] == "band"
        assert rec.payloads[0]["tokens"] == 110_000
        # Single-flight marker claimed for the spawned writer
        assert cp.writer_inflight(data_env, "sess-1") is True

    def test_inflight_writer_not_respawned(self, tmp_path, data_env, monkeypatch, capsys):
        rec = _SpawnRecorder()
        monkeypatch.setattr(session_stop, "_spawn_writer", rec)
        cp.claim_writer(data_env, "sess-1")
        _run_stop(monkeypatch, capsys, _stop_payload(tmp_path, 110_000))
        assert rec.payloads == []

    def test_handoff_emits_system_message_only(self, tmp_path, data_env, monkeypatch, capsys):
        rec = _SpawnRecorder()
        monkeypatch.setattr(session_stop, "_spawn_writer", rec)
        out = _run_stop(monkeypatch, capsys, _stop_payload(tmp_path, 210_000))
        frame = json.loads(out)
        assert "systemMessage" in frame
        assert "/clear" in frame["systemMessage"]
        # GOAL-SAFETY: never a decision key — must not block Stop
        assert "decision" not in frame

    def test_handoff_nudge_cooldown(self, tmp_path, data_env, monkeypatch, capsys):
        monkeypatch.setattr(session_stop, "_spawn_writer", _SpawnRecorder())
        out1 = _run_stop(monkeypatch, capsys, _stop_payload(tmp_path, 210_000))
        assert "systemMessage" in out1
        # Marginal growth below refresh step → silent
        out2 = _run_stop(monkeypatch, capsys, _stop_payload(tmp_path, 214_000))
        assert out2.strip() == ""
        # Growth past the refresh step → nudge again
        out3 = _run_stop(monkeypatch, capsys, _stop_payload(tmp_path, 240_000))
        assert "systemMessage" in out3

    def test_child_session_ignored(self, tmp_path, data_env, monkeypatch, capsys):
        rec = _SpawnRecorder()
        monkeypatch.setattr(session_stop, "_spawn_writer", rec)
        sub = tmp_path / "subagents"
        sub.mkdir()
        payload = {
            "session_id": "sub-1",
            "transcript_path": str(_transcript(sub, 210_000)),
            "cwd": str(tmp_path),
        }
        out = _run_stop(monkeypatch, capsys, payload)
        assert rec.payloads == []
        assert out.strip() == ""

    def test_disabled_config_noop(self, tmp_path, data_env, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_enabled", "false")
        rec = _SpawnRecorder()
        monkeypatch.setattr(session_stop, "_spawn_writer", rec)
        out = _run_stop(monkeypatch, capsys, _stop_payload(tmp_path, 210_000))
        assert rec.payloads == []
        assert out.strip() == ""

    def test_garbage_stdin_never_crashes(self, data_env, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
        session_stop.main()  # must not raise
        assert "decision" not in capsys.readouterr().out

    def test_spawn_failure_releases_marker(self, tmp_path, data_env, monkeypatch, capsys):
        monkeypatch.setattr(session_stop, "_spawn_writer", lambda p: False)
        _run_stop(monkeypatch, capsys, _stop_payload(tmp_path, 110_000))
        assert cp.writer_inflight(data_env, "sess-1") is False


class TestCheckpointNudge:
    def _run(self, monkeypatch, capsys, payload):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        checkpoint_nudge.main()
        return capsys.readouterr().out

    def test_below_threshold_silent(self, tmp_path, data_env, monkeypatch, capsys):
        out = self._run(monkeypatch, capsys, _stop_payload(tmp_path, 150_000))
        assert out.strip() == ""

    def test_above_threshold_emits_context(self, tmp_path, data_env, monkeypatch, capsys):
        out = self._run(monkeypatch, capsys, _stop_payload(tmp_path, 210_000))
        assert "CONTEXT BUDGET" in out
        assert "/clear" in out
        assert "Do not abandon or rush" in out

    def test_cooldown(self, tmp_path, data_env, monkeypatch, capsys):
        assert "CONTEXT BUDGET" in self._run(
            monkeypatch, capsys, _stop_payload(tmp_path, 210_000)
        )
        assert self._run(
            monkeypatch, capsys, _stop_payload(tmp_path, 212_000)
        ).strip() == ""

    def test_child_session_silent(self, tmp_path, data_env, monkeypatch, capsys):
        sub = tmp_path / "hook-sessions"
        sub.mkdir()
        payload = {
            "session_id": "child",
            "transcript_path": str(_transcript(sub, 300_000)),
            "cwd": str(tmp_path),
        }
        assert self._run(monkeypatch, capsys, payload).strip() == ""


class TestSessionStartRecovery:
    def test_injects_pending_checkpoint(self, tmp_path, data_env, capsys):
        cwd = str(tmp_path / "proj")
        cp.write_checkpoint_file(data_env, "old-sess", VALID_CHECKPOINT)
        cp.write_pending_marker(data_env, cwd, "old-sess", 214_000)

        ok = session_start._inject_checkpoint_recovery(data_env, cwd, "new-sess")
        out = capsys.readouterr().out
        assert ok is True
        assert "CONTEXT REBUILD" in out
        assert "## Current intent" in out
        # Marker consumed — a second start gets nothing
        assert session_start._inject_checkpoint_recovery(data_env, cwd, "newer") is False

    def test_no_marker_no_injection(self, tmp_path, data_env, capsys):
        ok = session_start._inject_checkpoint_recovery(
            data_env, str(tmp_path / "proj"), "new-sess"
        )
        assert ok is False
        assert capsys.readouterr().out.strip() == ""

    def test_invalid_checkpoint_not_injected(self, tmp_path, data_env, capsys):
        cwd = str(tmp_path / "proj")
        cp.write_checkpoint_file(data_env, "old-sess", "junk output")
        cp.write_pending_marker(data_env, cwd, "old-sess", 214_000)
        ok = session_start._inject_checkpoint_recovery(data_env, cwd, "new-sess")
        assert ok is False
        assert "CONTEXT REBUILD" not in capsys.readouterr().out

    def test_missing_checkpoint_file(self, tmp_path, data_env, capsys):
        cwd = str(tmp_path / "proj")
        cp.write_pending_marker(data_env, cwd, "old-sess", 214_000)  # no checkpoint.md
        assert session_start._inject_checkpoint_recovery(data_env, cwd, "new") is False

    def test_compact_source_injects_same_session(self, tmp_path, data_env, capsys):
        """Automatic rebuild: auto-compaction keeps the session id; the
        SessionStart(compact) hook must still consume the marker and inject."""
        cwd = str(tmp_path / "proj")
        cp.write_checkpoint_file(data_env, "sess-a", VALID_CHECKPOINT)
        cp.write_pending_marker(data_env, cwd, "sess-a", 214_000)
        cp.save_state(data_env, "sess-a", {
            "last_band_idx": 2, "last_checkpoint_tokens": 214_000,
            "last_checkpoint_ts": "2026-07-06T12:00:00+00:00",
        })

        ok = session_start._inject_checkpoint_recovery(
            data_env, cwd, "sess-a", source="compact"
        )
        assert ok is True
        assert "CONTEXT REBUILD" in capsys.readouterr().out
        # Counters reset so the post-compact window checkpoints again
        state = cp.load_state(data_env, "sess-a")
        assert state["last_band_idx"] == 0
        assert state["last_checkpoint_tokens"] == 0
        assert state["last_checkpoint_ts"] == "2026-07-06T12:00:00+00:00"

    def test_startup_source_still_blocks_same_session(self, tmp_path, data_env, capsys):
        """A plain resume of the SAME session must not self-inject."""
        cwd = str(tmp_path / "proj")
        cp.write_checkpoint_file(data_env, "sess-a", VALID_CHECKPOINT)
        cp.write_pending_marker(data_env, cwd, "sess-a", 214_000)
        ok = session_start._inject_checkpoint_recovery(
            data_env, cwd, "sess-a", source="resume"
        )
        assert ok is False

    def test_compact_without_marker_falls_back_to_own_checkpoint(
        self, tmp_path, data_env, capsys
    ):
        """Race guard: compaction fires while the writer is in flight (no
        pending marker yet) — the compact-path injection must fall back to
        the session's own checkpoint.md."""
        cwd = str(tmp_path / "proj")
        cp.write_checkpoint_file(data_env, "sess-a", VALID_CHECKPOINT)
        cp.save_state(data_env, "sess-a", {"last_checkpoint_tokens": 31_000})
        # No pending marker on purpose.
        ok = session_start._inject_checkpoint_recovery(
            data_env, cwd, "sess-a", source="compact"
        )
        out = capsys.readouterr().out
        assert ok is True
        assert "CONTEXT REBUILD" in out
        assert "31,000" in out

    def test_compact_without_marker_or_checkpoint_is_silent(
        self, tmp_path, data_env, capsys
    ):
        ok = session_start._inject_checkpoint_recovery(
            data_env, str(tmp_path / "proj"), "sess-a", source="compact"
        )
        assert ok is False
        assert capsys.readouterr().out.strip() == ""


class TestAutoModeNudgeSuppression:
    """With steered auto-compaction configured, the hooks stay quiet and let
    the automatic rebuild happen — no /clear nagging."""

    def test_stop_hook_silent_in_auto_mode(self, tmp_path, data_env, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "250000")
        monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "80")  # trigger 200K
        monkeypatch.setattr(session_stop, "_spawn_writer", _SpawnRecorder())
        out = _run_stop(monkeypatch, capsys, _stop_payload(tmp_path, 210_000))
        assert out.strip() == ""  # compaction imminent — no nag

    def test_stop_hook_warns_when_compaction_overdue(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "250000")
        monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "80")
        monkeypatch.setattr(session_stop, "_spawn_writer", _SpawnRecorder())
        out = _run_stop(monkeypatch, capsys, _stop_payload(tmp_path, 240_000))
        frame = json.loads(out)
        assert "auto-compaction" in frame["systemMessage"]
        assert "decision" not in frame

    def test_claude_nudge_silent_in_auto_mode(self, tmp_path, data_env, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "250000")
        monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "80")
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_stop_payload(tmp_path, 210_000))))
        checkpoint_nudge.main()
        assert capsys.readouterr().out.strip() == ""
