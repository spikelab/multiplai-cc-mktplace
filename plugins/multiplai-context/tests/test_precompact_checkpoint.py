"""Tests for the PreCompact synchronous checkpoint pass."""

import json
import os
import time
from datetime import datetime, timezone

import pytest

from conftest import PLUGIN_ROOT, import_script
from lib import checkpoint as cp

pre_compact = import_script("pre_compact_mod", "pre_compact.py")


@pytest.fixture
def data_env(tmp_path, monkeypatch):
    from multiplai_core.paths import _reset_cache

    data_dir = tmp_path / "data"
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_data_dir", str(data_dir))
    # scripts_dir() must resolve to the real plugin so checkpoint_writer.py
    # exists (the autouse _isolate_env fixture scrubs CLAUDE_PLUGIN_ROOT).
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))
    _reset_cache()
    yield data_dir
    _reset_cache()


def _transcript(tmp_path, tokens, name="t.jsonl"):
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


def _hook_input(tmp_path, tokens, session_id="sess-pc"):
    return {
        "session_id": session_id,
        "transcript_path": str(_transcript(tmp_path, tokens)),
        "cwd": str(tmp_path / "proj"),
    }


class TestSyncCheckpoint:
    def test_runs_writer_synchronously(self, tmp_path, data_env, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(json.loads(kwargs["input"].decode("utf-8")))

            class R:
                returncode = 0
            return R()

        monkeypatch.setattr(pre_compact.subprocess, "run", fake_run)
        pre_compact._sync_checkpoint(_hook_input(tmp_path, 90_000), data_env)
        assert len(calls) == 1
        assert calls[0]["reason"] == "precompact"
        assert calls[0]["tokens"] == 90_000
        # Marker released after the synchronous run
        assert cp.writer_inflight(data_env, "sess-pc") is False

    def test_skips_child_sessions(self, tmp_path, data_env, monkeypatch):
        calls = []
        monkeypatch.setattr(
            pre_compact.subprocess, "run", lambda *a, **k: calls.append(1)
        )
        sub = tmp_path / "subagents"
        sub.mkdir()
        hook_input = {
            "session_id": "child",
            "transcript_path": str(_transcript(sub, 90_000)),
            "cwd": str(tmp_path),
        }
        pre_compact._sync_checkpoint(hook_input, data_env)
        assert calls == []

    def test_skips_when_disabled(self, tmp_path, data_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_enabled", "false")
        calls = []
        monkeypatch.setattr(
            pre_compact.subprocess, "run", lambda *a, **k: calls.append(1)
        )
        pre_compact._sync_checkpoint(_hook_input(tmp_path, 90_000), data_env)
        assert calls == []

    def test_skips_stale_post_rebuild_usage(self, tmp_path, data_env, monkeypatch):
        """Right after a rebuild the tail usage is stale — don't re-checkpoint."""
        calls = []
        monkeypatch.setattr(
            pre_compact.subprocess, "run", lambda *a, **k: calls.append(1)
        )
        hook_input = _hook_input(tmp_path, 90_000)
        cp.reset_session_counters(data_env, "sess-pc")  # stamps rebuild_ts=now
        # transcript record timestamp predates... write it in the past:
        rec = json.loads(
            (tmp_path / "t.jsonl").read_text().strip()
        )
        rec["timestamp"] = "2020-01-01T00:00:00+00:00"
        (tmp_path / "t.jsonl").write_text(json.dumps(rec) + "\n")
        pre_compact._sync_checkpoint(hook_input, data_env)
        assert calls == []

    def test_timeout_releases_marker(self, tmp_path, data_env, monkeypatch):
        import subprocess as sp

        def fake_run(cmd, **kwargs):
            raise sp.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(pre_compact.subprocess, "run", fake_run)
        pre_compact._sync_checkpoint(_hook_input(tmp_path, 90_000), data_env)
        assert cp.writer_inflight(data_env, "sess-pc") is False

    def test_waits_for_inflight_band_writer(self, tmp_path, data_env, monkeypatch):
        """An in-flight band writer is awaited, not raced with a second run."""
        calls = []
        monkeypatch.setattr(
            pre_compact.subprocess, "run", lambda *a, **k: calls.append(1)
        )
        monkeypatch.setattr(pre_compact, "_INFLIGHT_POLL_S", 0.01)
        marker = cp.claim_writer(data_env, "sess-pc")

        # Simulate the writer finishing shortly after PreCompact starts waiting.
        import threading

        def release():
            time.sleep(0.05)
            marker.unlink(missing_ok=True)

        t = threading.Thread(target=release)
        t.start()
        pre_compact._sync_checkpoint(_hook_input(tmp_path, 90_000), data_env)
        t.join()
        assert calls == []  # waited; never spawned a second writer

    def test_main_never_raises_on_garbage(self, data_env, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{broken"))
        pre_compact.main()  # must not raise


class TestSummaryDirective:
    """The stdout directive that steers the native summarizer to a stub."""

    def _write_valid_checkpoint(self, data_dir, session_id="sess-pc"):
        text = "\n".join(f"## {s}\ncontent" for s in cp.CHECKPOINT_SECTIONS)
        cp.session_dir(data_dir, session_id).mkdir(parents=True, exist_ok=True)
        cp.checkpoint_file(data_dir, session_id).write_text(text)

    def _pending_markers(self, data_dir):
        pdir = cp.checkpoints_root(data_dir) / "pending"
        return list(pdir.glob("*.json")) if pdir.exists() else []

    def test_emits_directive_and_pending_marker(self, tmp_path, data_env):
        self._write_valid_checkpoint(data_env)
        directive = pre_compact._summary_directive(
            _hook_input(tmp_path, 90_000), data_env
        )
        assert directive == pre_compact._SUMMARY_DIRECTIVE
        markers = self._pending_markers(data_env)
        assert len(markers) == 1
        payload = json.loads(markers[0].read_text())
        assert payload["session_id"] == "sess-pc"

    def test_silent_without_checkpoint(self, tmp_path, data_env):
        assert (
            pre_compact._summary_directive(_hook_input(tmp_path, 90_000), data_env)
            is None
        )
        assert self._pending_markers(data_env) == []

    def test_silent_on_invalid_checkpoint(self, tmp_path, data_env):
        cp.session_dir(data_env, "sess-pc").mkdir(parents=True, exist_ok=True)
        cp.checkpoint_file(data_env, "sess-pc").write_text("## Notes\nonly one")
        assert (
            pre_compact._summary_directive(_hook_input(tmp_path, 90_000), data_env)
            is None
        )

    def test_silent_when_disabled(self, tmp_path, data_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_enabled", "false")
        self._write_valid_checkpoint(data_env)
        assert (
            pre_compact._summary_directive(_hook_input(tmp_path, 90_000), data_env)
            is None
        )

    def test_silent_for_child_sessions(self, tmp_path, data_env):
        self._write_valid_checkpoint(data_env, session_id="child")
        sub = tmp_path / "subagents"
        sub.mkdir()
        hook_input = {
            "session_id": "child",
            "transcript_path": str(_transcript(sub, 90_000)),
            "cwd": str(tmp_path),
        }
        assert pre_compact._summary_directive(hook_input, data_env) is None

    def test_main_prints_directive_to_stdout(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        """E2E through main(): stdout carries exactly the directive."""
        self._write_valid_checkpoint(data_env)
        monkeypatch.setattr(
            pre_compact.subprocess, "run", lambda *a, **k: None
        )
        hook_input = _hook_input(tmp_path, 90_000)
        monkeypatch.setattr(
            "sys.stdin", __import__("io").StringIO(json.dumps(hook_input))
        )
        pre_compact.main()
        out = capsys.readouterr().out
        assert pre_compact._SUMMARY_DIRECTIVE in out

    def test_main_stdout_empty_without_checkpoint(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            pre_compact.subprocess, "run", lambda *a, **k: None
        )
        hook_input = _hook_input(tmp_path, 90_000)
        monkeypatch.setattr(
            "sys.stdin", __import__("io").StringIO(json.dumps(hook_input))
        )
        pre_compact.main()
        assert capsys.readouterr().out == ""


class _WriterOk:
    returncode = 0


class TestFreshnessGate:
    """The stub directive must never be emitted against a stale checkpoint.

    Setup pattern: a VALID checkpoint exists on disk but is stale (old
    mtime, distant/absent token watermark), then one of the four
    ``_sync_checkpoint`` silent-fail paths fires. In every case the
    directive must be suppressed (native summary keeps the state). Only a
    sync success this pass — or a demonstrably fresh checkpoint — emits.
    """

    def _write_stale_valid_checkpoint(self, data_dir, session_id="sess-pc"):
        """Valid 11-section checkpoint whose mtime is an hour in the past."""
        text = "\n".join(f"## {s}\ncontent" for s in cp.CHECKPOINT_SECTIONS)
        cp.session_dir(data_dir, session_id).mkdir(parents=True, exist_ok=True)
        path = cp.checkpoint_file(data_dir, session_id)
        path.write_text(text)
        old = time.time() - 3600
        os.utime(path, (old, old))
        return path

    def _run_main(self, hook_input, monkeypatch, capsys):
        monkeypatch.setattr(
            "sys.stdin", __import__("io").StringIO(json.dumps(hook_input))
        )
        pre_compact.main()
        return capsys.readouterr().out

    # --- the four _sync_checkpoint silent-fail paths ---

    def test_no_directive_when_tokens_zero(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        """Fail path 1: read_context_tokens() == 0 → sync skips, no stub."""
        self._write_stale_valid_checkpoint(data_env)
        calls = []
        monkeypatch.setattr(
            pre_compact.subprocess, "run",
            lambda *a, **k: calls.append(1) or _WriterOk(),
        )
        # Transcript with no usage records at all → tokens == 0.
        empty = tmp_path / "t.jsonl"
        empty.write_text(json.dumps({"type": "user"}) + "\n")
        hook_input = {
            "session_id": "sess-pc",
            "transcript_path": str(empty),
            "cwd": str(tmp_path / "proj"),
        }
        out = self._run_main(hook_input, monkeypatch, capsys)
        assert calls == []  # sync never spawned a writer
        assert pre_compact._SUMMARY_DIRECTIVE not in out

    def test_no_directive_when_inflight_writer_never_finishes(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        """Fail path 2: writer-lock (in-flight marker) wait times out."""
        self._write_stale_valid_checkpoint(data_env)
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_timeout_s", "0")
        monkeypatch.setattr(pre_compact, "_INFLIGHT_POLL_S", 0.01)
        cp.claim_writer(data_env, "sess-pc")  # marker that never releases
        out = self._run_main(
            _hook_input(tmp_path, 90_000), monkeypatch, capsys
        )
        assert pre_compact._SUMMARY_DIRECTIVE not in out
        cp.release_writer(data_env, "sess-pc")

    def test_no_directive_when_writer_script_missing(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        """Fail path 3: checkpoint_writer.py not found."""
        self._write_stale_valid_checkpoint(data_env)

        class _FakePaths:
            def scripts_dir(self):
                return tmp_path / "no-such-dir"

            def plugin_data(self):
                return data_env

        monkeypatch.setattr(pre_compact, "get_paths", lambda: _FakePaths())
        out = self._run_main(
            _hook_input(tmp_path, 90_000), monkeypatch, capsys
        )
        assert pre_compact._SUMMARY_DIRECTIVE not in out

    def test_no_directive_when_writer_times_out(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        """Fail path 4: the synchronous writer subprocess times out."""
        import subprocess as sp

        self._write_stale_valid_checkpoint(data_env)

        def fake_run(cmd, **kwargs):
            raise sp.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(pre_compact.subprocess, "run", fake_run)
        out = self._run_main(
            _hook_input(tmp_path, 90_000), monkeypatch, capsys
        )
        assert pre_compact._SUMMARY_DIRECTIVE not in out

    def test_no_directive_when_writer_fails_rc(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        """A writer exiting non-zero is a failure, not a fresh checkpoint."""
        self._write_stale_valid_checkpoint(data_env)

        class _WriterFail:
            returncode = 1

        monkeypatch.setattr(
            pre_compact.subprocess, "run", lambda *a, **k: _WriterFail()
        )
        out = self._run_main(
            _hook_input(tmp_path, 90_000), monkeypatch, capsys
        )
        assert pre_compact._SUMMARY_DIRECTIVE not in out

    # --- the success paths that MUST still emit ---

    def test_directive_after_successful_sync(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        """A stale checkpoint + successful sync this pass → stub emitted."""
        self._write_stale_valid_checkpoint(data_env)
        monkeypatch.setattr(
            pre_compact.subprocess, "run", lambda *a, **k: _WriterOk()
        )
        out = self._run_main(
            _hook_input(tmp_path, 90_000), monkeypatch, capsys
        )
        assert pre_compact._SUMMARY_DIRECTIVE in out

    def test_directive_with_fresh_watermark_despite_sync_failure(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        """Sync fails but the watermark is within one refresh band → emit."""
        import subprocess as sp

        self._write_stale_valid_checkpoint(data_env)
        cp.save_state(
            data_env, "sess-pc", {"last_checkpoint_tokens": 80_000}
        )  # 90K live − 80K covered = 10K < 25K refresh band

        def fake_run(cmd, **kwargs):
            raise sp.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(pre_compact.subprocess, "run", fake_run)
        out = self._run_main(
            _hook_input(tmp_path, 90_000), monkeypatch, capsys
        )
        assert pre_compact._SUMMARY_DIRECTIVE in out

    def test_directive_with_fresh_mtime_despite_sync_failure(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        """Sync fails but checkpoint.md was written seconds ago → emit."""
        import subprocess as sp

        path = self._write_stale_valid_checkpoint(data_env)
        now = time.time()
        os.utime(path, (now, now))  # a band writer just finished

        def fake_run(cmd, **kwargs):
            raise sp.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(pre_compact.subprocess, "run", fake_run)
        out = self._run_main(
            _hook_input(tmp_path, 90_000), monkeypatch, capsys
        )
        assert pre_compact._SUMMARY_DIRECTIVE in out

    def test_sync_checkpoint_returns_true_on_success(
        self, tmp_path, data_env, monkeypatch
    ):
        monkeypatch.setattr(
            pre_compact.subprocess, "run", lambda *a, **k: _WriterOk()
        )
        assert (
            pre_compact._sync_checkpoint(_hook_input(tmp_path, 90_000), data_env)
            is True
        )

    def test_sync_checkpoint_returns_true_after_inflight_writer_finishes(
        self, tmp_path, data_env, monkeypatch
    ):
        import threading

        monkeypatch.setattr(pre_compact, "_INFLIGHT_POLL_S", 0.01)
        marker = cp.claim_writer(data_env, "sess-pc")

        def release():
            time.sleep(0.05)
            marker.unlink(missing_ok=True)

        t = threading.Thread(target=release)
        t.start()
        result = pre_compact._sync_checkpoint(
            _hook_input(tmp_path, 90_000), data_env
        )
        t.join()
        assert result is True


class TestCliVersionCanary:
    """Item 2: warn when the CLI major outruns the verified steering channel."""

    def test_higher_major_logs_warning_but_still_emits(
        self, tmp_path, data_env, monkeypatch, caplog
    ):
        import logging

        monkeypatch.setenv("AI_AGENT", "claude-code_3-0-1_agent")
        text = "\n".join(f"## {s}\ncontent" for s in cp.CHECKPOINT_SECTIONS)
        cp.session_dir(data_env, "sess-pc").mkdir(parents=True, exist_ok=True)
        cp.checkpoint_file(data_env, "sess-pc").write_text(text)
        with caplog.at_level(logging.WARNING):
            directive = pre_compact._summary_directive(
                _hook_input(tmp_path, 90_000), data_env, sync_ok=True
            )
        assert directive == pre_compact._SUMMARY_DIRECTIVE  # never blocks
        assert any(
            "steering" in r.message for r in caplog.records
        ), "expected a canary warning on a newer CLI major"

    def test_verified_major_stays_silent(
        self, tmp_path, data_env, monkeypatch, caplog
    ):
        import logging

        monkeypatch.setenv("AI_AGENT", "claude-code_2-1-207_agent")
        with caplog.at_level(logging.WARNING):
            pre_compact._cli_version_canary()
        assert not any("steering" in r.message for r in caplog.records)

    def test_unknown_version_signal_stays_silent(self, monkeypatch, caplog):
        import logging

        monkeypatch.delenv("AI_AGENT", raising=False)
        with caplog.at_level(logging.WARNING):
            pre_compact._cli_version_canary()
        assert not any("steering" in r.message for r in caplog.records)
