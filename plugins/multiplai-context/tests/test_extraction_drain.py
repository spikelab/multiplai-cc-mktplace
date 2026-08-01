"""Tests for the shared deferred-extraction drain (plan criterion 1).

The point of ``lib/extraction_drain.py`` is that the in-container
``session_start.py`` and the host-side ``drain_extractions.py`` dequeue
markers through *one* implementation. These tests pin both halves of that:
the behaviour of the shared drain, and the fact that ``session_start`` really
does route through it rather than keeping a private copy.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import extraction_drain  # noqa: E402


class _PopenSpy:
    """Stand-in for subprocess.Popen that records argv and swallows stdin."""

    class _Handle:
        def __init__(self):
            self.stdin = _PopenSpy._Stdin()
            self.waited = False

        def wait(self):
            self.waited = True
            return 0

    class _Stdin:
        def __init__(self):
            self.written = b""
            self.closed = False

        def write(self, data):
            self.written += data

        def close(self):
            self.closed = True

    def __init__(self):
        self.calls = []
        self.handles = []

    def __call__(self, args, **kwargs):
        handle = self._Handle()
        self.calls.append((args, kwargs))
        self.handles.append(handle)
        return handle


@pytest.fixture
def drain_dirs(tmp_path):
    """A data dir with the queue layout plus a stub extract script."""
    data_dir = tmp_path / "data"
    (data_dir / "pending_extractions").mkdir(parents=True)
    (data_dir / "processing_extractions").mkdir(parents=True)
    extract_script = tmp_path / "extract_learnings.py"
    extract_script.write_text("# stub\n", encoding="utf-8")
    return data_dir, extract_script


def _write_marker(data_dir: Path, sid: str, **extra) -> Path:
    marker = data_dir / "pending_extractions" / f"{sid}.json"
    payload = {
        "session_id": sid,
        "cwd": "/work",
        "transcript_path": f"/transcripts/{sid}.jsonl",
    }
    payload.update(extra)
    marker.write_text(json.dumps(payload), encoding="utf-8")
    return marker


class TestProcessDeferredExtractions:
    def test_marker_is_dequeued_and_child_launched(self, drain_dirs, monkeypatch):
        data_dir, extract_script = drain_dirs
        _write_marker(data_dir, "sess-a")
        spy = _PopenSpy()
        monkeypatch.setattr(extraction_drain.subprocess, "Popen", spy)

        assert extraction_drain.process_deferred_extractions(data_dir, extract_script) == 1

        # Dequeued: gone from pending, present in processing.
        assert list((data_dir / "pending_extractions").glob("*.json")) == []
        assert [p.name for p in (data_dir / "processing_extractions").glob("*.json")] == [
            "sess-a.json"
        ]

        argv, kwargs = spy.calls[0]
        assert argv == ["uv", "run", "--no-project", str(extract_script)]
        assert kwargs["start_new_session"] is True
        payload = json.loads(spy.handles[0].stdin.written.decode())
        assert payload["session_id"] == "sess-a"
        # The transcript PATH is passed, never its contents.
        assert payload["transcript_path"] == "/transcripts/sess-a.jsonl"
        assert payload["marker_path"].endswith("processing_extractions/sess-a.json")

    def test_missing_extract_script_is_a_noop(self, drain_dirs, monkeypatch):
        data_dir, _ = drain_dirs
        _write_marker(data_dir, "sess-a")
        spy = _PopenSpy()
        monkeypatch.setattr(extraction_drain.subprocess, "Popen", spy)

        assert extraction_drain.process_deferred_extractions(
            data_dir, data_dir / "nope.py"
        ) == 0
        # Marker stays queued for a caller that does have the script.
        assert len(list((data_dir / "pending_extractions").glob("*.json"))) == 1
        assert spy.calls == []

    def test_unparseable_marker_is_discarded(self, drain_dirs, monkeypatch):
        data_dir, extract_script = drain_dirs
        (data_dir / "pending_extractions" / "junk.json").write_text("{not json", encoding="utf-8")
        spy = _PopenSpy()
        monkeypatch.setattr(extraction_drain.subprocess, "Popen", spy)

        assert extraction_drain.process_deferred_extractions(data_dir, extract_script) == 0
        assert list((data_dir / "processing_extractions").glob("*.json")) == []
        assert spy.calls == []

    def test_launch_failure_requeues_the_marker(self, drain_dirs, monkeypatch):
        data_dir, extract_script = drain_dirs
        _write_marker(data_dir, "sess-a")

        def _boom(*a, **k):
            raise OSError("no uv")

        monkeypatch.setattr(extraction_drain.subprocess, "Popen", _boom)

        assert extraction_drain.process_deferred_extractions(data_dir, extract_script) == 0
        # Back in the queue — a later drain retries rather than losing it.
        assert [p.name for p in (data_dir / "pending_extractions").glob("*.json")] == [
            "sess-a.json"
        ]

    def test_wait_joins_children_and_shows_their_stderr(self, drain_dirs, monkeypatch):
        """--wait is what makes the by-hand auth proof observable."""
        data_dir, extract_script = drain_dirs
        _write_marker(data_dir, "sess-a")
        spy = _PopenSpy()
        monkeypatch.setattr(extraction_drain.subprocess, "Popen", spy)

        extraction_drain.process_deferred_extractions(data_dir, extract_script, wait=True)

        assert spy.handles[0].waited is True
        assert spy.calls[0][1]["stderr"] is None  # inherited, not swallowed

    def test_default_swallows_child_stderr(self, drain_dirs, monkeypatch):
        data_dir, extract_script = drain_dirs
        _write_marker(data_dir, "sess-a")
        spy = _PopenSpy()
        monkeypatch.setattr(extraction_drain.subprocess, "Popen", spy)

        extraction_drain.process_deferred_extractions(data_dir, extract_script)

        assert spy.handles[0].waited is False
        assert spy.calls[0][1]["stderr"] is subprocess.DEVNULL


class TestRecoverStaleProcessing:
    def test_fresh_marker_is_left_alone(self, drain_dirs):
        data_dir, _ = drain_dirs
        proc_dir = data_dir / "processing_extractions"
        (proc_dir / "sess-a.json").write_text(json.dumps({"session_id": "a"}), encoding="utf-8")

        extraction_drain.recover_stale_processing(proc_dir, data_dir / "pending_extractions")

        assert (proc_dir / "sess-a.json").exists()

    def test_stale_marker_is_requeued_with_an_attempt_count(self, drain_dirs):
        data_dir, _ = drain_dirs
        proc_dir = data_dir / "processing_extractions"
        pending_dir = data_dir / "pending_extractions"
        m = proc_dir / "sess-a.json"
        m.write_text(json.dumps({"session_id": "a"}), encoding="utf-8")
        old = time.time() - extraction_drain.STALE_SECONDS - 60
        import os

        os.utime(m, (old, old))

        extraction_drain.recover_stale_processing(proc_dir, pending_dir)

        requeued = pending_dir / "sess-a.json"
        assert requeued.exists()
        assert json.loads(requeued.read_text())["attempts"] == 1

    def test_marker_past_max_attempts_is_quarantined(self, drain_dirs):
        data_dir, _ = drain_dirs
        proc_dir = data_dir / "processing_extractions"
        pending_dir = data_dir / "pending_extractions"
        m = proc_dir / "sess-a.json"
        m.write_text(
            json.dumps({"session_id": "a", "attempts": extraction_drain.MAX_ATTEMPTS}),
            encoding="utf-8",
        )
        old = time.time() - extraction_drain.STALE_SECONDS - 60
        import os

        os.utime(m, (old, old))

        extraction_drain.recover_stale_processing(proc_dir, pending_dir)

        assert not (pending_dir / "sess-a.json").exists()
        assert (data_dir / "failed_extractions" / "sess-a.json").exists()


class TestPendingCount:
    def test_counts_only_queued_markers(self, drain_dirs):
        data_dir, _ = drain_dirs
        assert extraction_drain.pending_count(data_dir) == 0
        _write_marker(data_dir, "sess-a")
        _write_marker(data_dir, "sess-b")
        assert extraction_drain.pending_count(data_dir) == 2

    def test_missing_directory_counts_zero(self, tmp_path):
        assert extraction_drain.pending_count(tmp_path / "nothing") == 0

    def test_an_orphan_in_flight_is_not_counted_as_pending(self, drain_dirs):
        """The distinction the two counters exist to make.

        A marker orphaned by a container that died mid-extraction is real work
        — ``recover_stale_processing`` will requeue it — but it is invisible to
        ``pending_count``. A caller that reports only the pending count
        announces an empty queue and then drains something.
        """
        data_dir, _ = drain_dirs
        orphan = data_dir / "processing_extractions" / "sess-orphan.json"
        orphan.write_text(json.dumps({"session_id": "sess-orphan"}), encoding="utf-8")

        assert extraction_drain.pending_count(data_dir) == 0
        assert extraction_drain.processing_count(data_dir) == 1


class TestProcessingCount:
    def test_counts_only_in_flight_markers(self, drain_dirs):
        data_dir, _ = drain_dirs
        assert extraction_drain.processing_count(data_dir) == 0
        _write_marker(data_dir, "sess-a")
        assert extraction_drain.processing_count(data_dir) == 0

        for sid in ("sess-b", "sess-c"):
            (data_dir / "processing_extractions" / f"{sid}.json").write_text(
                json.dumps({"session_id": sid}), encoding="utf-8"
            )
        assert extraction_drain.processing_count(data_dir) == 2

    def test_missing_directory_counts_zero(self, tmp_path):
        assert extraction_drain.processing_count(tmp_path / "nothing") == 0


class TestDrainReportsBothQueues:
    """Regression: the entry point must not report an empty queue and then work.

    ``pending_count`` is sampled *before* ``process_deferred_extractions`` runs
    its recovery step, so an orphan rescue logged only the pending count and
    read as `0 marker(s) pending` → `Drained 1`. Verified against the source:
    the log call has to name both queues.
    """

    def test_the_log_line_names_both_queues(self):
        source = (SCRIPTS_DIR / "drain_extractions.py").read_text(encoding="utf-8")
        assert "processing_count(data_dir)" in source
        assert "%d pending, %d in flight" in source
        assert "%d marker(s) pending" not in source

    def test_the_entry_point_imports_both_counters(self):
        import drain_extractions

        assert drain_extractions.pending_count is extraction_drain.pending_count
        assert drain_extractions.processing_count is extraction_drain.processing_count


class TestSingleImplementation:
    """Criterion 1: the marker-move loop must appear exactly once."""

    def test_session_start_delegates_to_the_shared_drain(self):
        import session_start

        assert (
            session_start.process_deferred_extractions
            is extraction_drain.process_deferred_extractions
        )

    def test_marker_move_loop_is_not_duplicated(self):
        """No second copy of the dequeue anywhere under scripts/.

        Keyed on the atomic rename that *is* the dequeue — health_check.py
        legitimately reads ``processing_extractions/`` to report on it, which
        is not a second implementation.
        """
        needle = "os.rename(str(marker_file), str(dest))"
        hits = [
            p.relative_to(SCRIPTS_DIR)
            for p in SCRIPTS_DIR.rglob("*.py")
            if "__pycache__" not in p.parts
            and needle in p.read_text(encoding="utf-8", errors="replace")
        ]
        assert hits == [Path("lib/extraction_drain.py")]
