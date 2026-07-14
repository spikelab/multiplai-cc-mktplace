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
