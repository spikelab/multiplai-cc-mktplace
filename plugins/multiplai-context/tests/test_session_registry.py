"""Tests for the hub session registry (lib/session_registry.py).

Covers the "hub input contract" (spikelab/multiplai-gui docs/api-contract.md):
- registry entry lifecycle: start → stop → notification → end
- contract fields (session_id, hostname, cwd, project, workspace,
  started_at, last_event)
- read-merge-write: hub-owned keys (e.g. adoption markers) survive updates
- atomic writes, never-raise guarantees, 7-day GC of ended entries
- hooks.json wiring of the Notification hook + registry calls in the
  lifecycle scripts
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PLUGIN_DIR / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import session_registry as sr


SID = "11111111-2222-3333-4444-555555555555"


def _hook_input(cwd="/tmp/proj", sid=SID):
    return {"session_id": sid, "cwd": cwd}


def _entry_path(data_dir: Path, sid=SID) -> Path:
    return data_dir / "sessions" / f"{sid}.json"


def _read_entry(data_dir: Path, sid=SID) -> dict:
    return json.loads(_entry_path(data_dir, sid).read_text())


# ===========================================================================
# record_event — creation and contract fields
# ===========================================================================

class TestRecordEventStart:

    def test_start_creates_entry_with_contract_fields(self, tmp_path):
        assert sr.record_event(tmp_path, _hook_input(), "start") is True
        entry = _read_entry(tmp_path)
        assert entry["session_id"] == SID
        assert entry["cwd"] == "/tmp/proj"
        assert entry["workspace"] == str(tmp_path.parent.parent)
        assert entry["hostname"], "hostname must be populated"
        # started_at and last_event.ts are tz-aware ISO-8601
        assert datetime.fromisoformat(entry["started_at"]).tzinfo is not None
        assert entry["last_event"]["kind"] == "start"
        assert datetime.fromisoformat(entry["last_event"]["ts"]).tzinfo is not None

    def test_hostname_prefers_env(self, tmp_path):
        with patch.dict(os.environ, {"HOSTNAME": "claude-test-01020304"}):
            sr.record_event(tmp_path, _hook_input(), "start")
        assert _read_entry(tmp_path)["hostname"] == "claude-test-01020304"

    def test_hostname_refreshed_on_every_event(self, tmp_path):
        """A session resumed in a new container must show the new hostname."""
        with patch.dict(os.environ, {"HOSTNAME": "claude-container-a"}):
            sr.record_event(tmp_path, _hook_input(), "start")
        with patch.dict(os.environ, {"HOSTNAME": "claude-container-b"}):
            sr.record_event(tmp_path, _hook_input(), "start")
        assert _read_entry(tmp_path)["hostname"] == "claude-container-b"

    def test_hostname_kept_when_current_unresolvable(self, tmp_path):
        """An empty current hostname must not clobber a known one."""
        with patch.dict(os.environ, {"HOSTNAME": "claude-container-a"}):
            sr.record_event(tmp_path, _hook_input(), "start")
        with patch.object(sr, "_hostname", return_value=""):
            sr.record_event(tmp_path, _hook_input(), "stop")
        assert _read_entry(tmp_path)["hostname"] == "claude-container-a"

    def test_project_resolved_when_possible(self, tmp_path):
        with patch.object(sr, "_resolve_project", return_value="myproj"):
            sr.record_event(tmp_path, _hook_input(), "start")
        assert _read_entry(tmp_path)["project"] == "myproj"

    def test_project_omitted_when_unresolvable(self, tmp_path):
        with patch.object(sr, "_resolve_project", return_value=None):
            sr.record_event(tmp_path, _hook_input(), "start")
        assert "project" not in _read_entry(tmp_path)

    def test_missing_session_id_is_noop(self, tmp_path):
        assert sr.record_event(tmp_path, {"cwd": "/x"}, "start") is False
        assert not (tmp_path / "sessions").exists() or not list(
            (tmp_path / "sessions").glob("*.json")
        )

    def test_path_traversal_session_id_rejected(self, tmp_path):
        assert sr.record_event(
            tmp_path, {"session_id": "../evil", "cwd": "/x"}, "start"
        ) is False

    def test_unknown_kind_rejected(self, tmp_path):
        assert sr.record_event(tmp_path, _hook_input(), "reboot") is False


class TestRecordEventUpdates:

    def test_stop_updates_last_event_and_preserves_identity(self, tmp_path):
        sr.record_event(tmp_path, _hook_input(), "start")
        started_at = _read_entry(tmp_path)["started_at"]
        assert sr.record_event(tmp_path, _hook_input(), "stop") is True
        entry = _read_entry(tmp_path)
        assert entry["last_event"]["kind"] == "stop"
        assert entry["started_at"] == started_at
        assert entry["session_id"] == SID

    def test_notification_and_end_kinds(self, tmp_path):
        sr.record_event(tmp_path, _hook_input(), "start")
        sr.record_event(tmp_path, _hook_input(), "notification")
        assert _read_entry(tmp_path)["last_event"]["kind"] == "notification"
        sr.record_event(tmp_path, _hook_input(), "end")
        assert _read_entry(tmp_path)["last_event"]["kind"] == "end"

    def test_unknown_keys_preserved(self, tmp_path):
        """Hub-written fields (adoption_marker etc.) survive hook updates."""
        sr.record_event(tmp_path, _hook_input(), "start")
        path = _entry_path(tmp_path)
        entry = json.loads(path.read_text())
        entry["adoption_marker"] = str(path.with_suffix(".adopt"))
        path.write_text(json.dumps(entry))
        sr.record_event(tmp_path, _hook_input(), "stop")
        assert _read_entry(tmp_path)["adoption_marker"] == str(
            path.with_suffix(".adopt")
        )

    def test_event_without_prior_entry_creates_one(self, tmp_path):
        """Hooks installed mid-session still register the session."""
        assert sr.record_event(tmp_path, _hook_input(), "stop") is True
        entry = _read_entry(tmp_path)
        assert entry["last_event"]["kind"] == "stop"
        assert entry["started_at"], "started_at falls back to the event time"

    def test_corrupt_entry_is_rewritten(self, tmp_path):
        path = _entry_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        assert sr.record_event(tmp_path, _hook_input(), "stop") is True
        assert _read_entry(tmp_path)["session_id"] == SID


class TestRecordEventRobustness:

    def test_atomic_no_tmp_left_behind(self, tmp_path):
        sr.record_event(tmp_path, _hook_input(), "start")
        files = sorted(p.name for p in (tmp_path / "sessions").iterdir())
        assert files == [f"{SID}.json"], "only the entry itself may remain"

    def test_failed_write_unlinks_tmp(self, tmp_path, monkeypatch):
        """A failed rename must not orphan a tmp file (GC globs *.json only)."""
        from lib import fsio

        def boom(*args, **kwargs):
            raise OSError("simulated rename failure")

        monkeypatch.setattr(fsio.os, "replace", boom)
        assert sr.record_event(tmp_path, _hook_input(), "start") is False
        assert list((tmp_path / "sessions").glob("*.json")) == []
        assert [
            p for p in (tmp_path / "sessions").iterdir() if "tmp" in p.name
        ] == []

    def test_never_raises_on_unwritable_dir(self, tmp_path):
        target = tmp_path / "blocked"
        target.write_text("a file where the data dir should be")
        # data_dir is a file → mkdir fails; must return False, not raise
        assert sr.record_event(target, _hook_input(), "start") is False

    def test_gitignore_dropped_at_data_root(self, tmp_path):
        sr.record_event(tmp_path, _hook_input(), "start")
        gi = tmp_path / ".gitignore"
        assert gi.exists() and gi.read_text().strip() == "*"


# ===========================================================================
# gc_stale
# ===========================================================================

def _write_entry(data_dir: Path, sid: str, kind: str, ts: datetime) -> Path:
    rdir = data_dir / "sessions"
    rdir.mkdir(parents=True, exist_ok=True)
    path = rdir / f"{sid}.json"
    path.write_text(json.dumps({
        "session_id": sid,
        "last_event": {"ts": ts.isoformat(), "kind": kind},
    }))
    return path


class TestGcStale:

    def test_old_ended_entry_removed(self, tmp_path):
        old = datetime.now(timezone.utc) - timedelta(days=8)
        _write_entry(tmp_path, "old-ended", "end", old)
        assert sr.gc_stale(tmp_path) == 1
        assert not (tmp_path / "sessions" / "old-ended.json").exists()

    def test_recent_ended_entry_kept(self, tmp_path):
        recent = datetime.now(timezone.utc) - timedelta(days=2)
        _write_entry(tmp_path, "new-ended", "end", recent)
        assert sr.gc_stale(tmp_path) == 0

    def test_idle_entry_within_live_window_kept(self, tmp_path):
        """A recently-idle session may resume — kept past the ended cutoff."""
        old = datetime.now(timezone.utc) - timedelta(days=20)
        _write_entry(tmp_path, "old-idle", "stop", old)
        assert sr.gc_stale(tmp_path) == 0

    def test_idle_entry_past_live_window_removed(self, tmp_path):
        """Containers killed without SessionEnd age out after live_days."""
        old = datetime.now(timezone.utc) - timedelta(days=31)
        _write_entry(tmp_path, "ghost-idle", "stop", old)
        assert sr.gc_stale(tmp_path) == 1
        assert not (tmp_path / "sessions" / "ghost-idle.json").exists()

    def test_orphan_adopt_marker_removed_with_entry(self, tmp_path):
        old = datetime.now(timezone.utc) - timedelta(days=8)
        path = _write_entry(tmp_path, "adopted", "end", old)
        path.with_suffix(".adopt").touch()
        sr.gc_stale(tmp_path)
        assert not path.with_suffix(".adopt").exists()

    def test_old_unparseable_entry_removed(self, tmp_path):
        rdir = tmp_path / "sessions"
        rdir.mkdir(parents=True)
        junk = rdir / "junk.json"
        junk.write_text("{not json")
        old = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
        os.utime(junk, (old, old))
        assert sr.gc_stale(tmp_path) == 1

    def test_fresh_unparseable_entry_kept(self, tmp_path):
        rdir = tmp_path / "sessions"
        rdir.mkdir(parents=True)
        (rdir / "junk.json").write_text("{not json")
        assert sr.gc_stale(tmp_path) == 0

    def test_missing_dir_is_noop(self, tmp_path):
        assert sr.gc_stale(tmp_path) == 0


# ===========================================================================
# Hook wiring
# ===========================================================================

class TestHookWiring:

    def _hooks(self):
        from conftest import parse_hooks
        return parse_hooks()

    def test_notification_hook_registered(self):
        hooks = [
            h for h in self._hooks()
            if h["event"] == "Notification"
            and "session_notification" in h["script"]
        ]
        assert len(hooks) == 1, (
            "hooks.json must register exactly one Notification hook for "
            "session_notification.py"
        )

    def test_notification_hook_has_uv_guard_and_timeout(self):
        hook = next(
            h for h in self._hooks() if h["event"] == "Notification"
        )
        assert "command -v uv" in hook["command"], (
            "Notification hook must carry the uv-guard wrapper like its siblings"
        )
        assert isinstance(hook["timeout"], int) and hook["timeout"] >= 5

    def test_notification_script_exists_with_pep723(self):
        script = SCRIPTS_DIR / "session_notification.py"
        assert script.exists()
        source = script.read_text()
        assert "# /// script" in source
        assert "multiplai-core" in source

    @pytest.mark.parametrize("script,kind", [
        ("session_start.py", '"start"'),
        ("session_stop.py", '"stop"'),
        ("session_end.py", '"end"'),
        ("session_notification.py", '"notification"'),
    ])
    def test_lifecycle_scripts_record_registry_events(self, script, kind):
        source = (SCRIPTS_DIR / script).read_text()
        assert "session_registry" in source, (
            f"{script} must call into lib.session_registry"
        )
        assert kind in source, f"{script} must record the {kind} event kind"

    def test_session_start_runs_gc(self):
        source = (SCRIPTS_DIR / "session_start.py").read_text()
        assert "gc_stale" in source, "SessionStart must GC stale registry entries"


# ===========================================================================
# End-to-end through the real hook scripts
# ===========================================================================

class TestLifecycleIntegration:

    def test_start_stop_end_lifecycle_via_scripts(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        hook_json = json.dumps({"session_id": SID, "cwd": str(tmp_path)})

        with patch.dict(os.environ, {
            "CLAUDE_PLUGIN_DATA": str(data_dir),
            "HOSTNAME": "claude-e2e-test",
        }, clear=False):
            from multiplai_core.paths import _reset_cache
            _reset_cache()
            try:
                import importlib
                import io

                import session_start
                import session_stop
                import session_end
                importlib.reload(session_start)
                importlib.reload(session_stop)
                importlib.reload(session_end)

                for mod, kind in (
                    (session_start, "start"),
                    (session_stop, "stop"),
                    (session_end, "end"),
                ):
                    monkeypatch.setattr(
                        "sys.stdin", io.StringIO(hook_json), raising=False
                    )
                    mod.main()
                    entry = _read_entry(data_dir)
                    assert entry["last_event"]["kind"] == kind
                    assert entry["hostname"] == "claude-e2e-test"
            finally:
                _reset_cache()
