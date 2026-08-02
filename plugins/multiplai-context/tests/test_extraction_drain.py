"""Tests for the shared deferred-extraction drain (plan criterion 1).

The point of ``lib/extraction_drain.py`` is that the in-container
``session_start.py`` and the host-side ``drain_extractions.py`` dequeue
markers through *one* implementation. These tests pin both halves of that:
the behaviour of the shared drain, and the fact that ``session_start`` really
does route through it rather than keeping a private copy.
"""

import json
import os
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
    """Stand-in for subprocess.Popen that records argv and swallows stdin.

    ``returncode`` is what every child's ``wait()`` reports; ``broken_pipe``
    makes stdin writes raise BrokenPipeError, i.e. a child that spawned but
    died before reading its payload.
    """

    class _Handle:
        def __init__(self, returncode, broken_pipe):
            self.stdin = _PopenSpy._Stdin(broken_pipe)
            self.waited = False
            self._returncode = returncode

        def wait(self):
            self.waited = True
            return self._returncode

    class _Stdin:
        def __init__(self, broken_pipe):
            self.written = b""
            self.closed = False
            self._broken_pipe = broken_pipe

        def write(self, data):
            if self._broken_pipe:
                raise BrokenPipeError("child died before reading stdin")
            self.written += data

        def close(self):
            self.closed = True

    def __init__(self, returncode=0, broken_pipe=False):
        self.calls = []
        self.handles = []
        self._returncode = returncode
        self._broken_pipe = broken_pipe

    def __call__(self, args, **kwargs):
        handle = self._Handle(self._returncode, self._broken_pipe)
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

        result = extraction_drain.process_deferred_extractions(data_dir, extract_script)
        assert result.launched == 1
        assert result.failed == 0

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

    def test_marker_trigger_is_forwarded_to_the_child(self, drain_dirs, monkeypatch):
        """A PreCompact marker's `trigger` must reach `extract()` — it is how
        the child knows the session is still live and its checkpoint must not
        be retired."""
        data_dir, extract_script = drain_dirs
        _write_marker(data_dir, "sess-pc", trigger="pre_compact")
        spy = _PopenSpy()
        monkeypatch.setattr(extraction_drain.subprocess, "Popen", spy)

        extraction_drain.process_deferred_extractions(data_dir, extract_script)

        payload = json.loads(spy.handles[0].stdin.written.decode())
        assert payload["trigger"] == "pre_compact"

    def test_a_triggerless_marker_forwards_an_empty_trigger(self, drain_dirs, monkeypatch):
        """SessionEnd markers carry no `trigger`; the payload still carries
        the key (empty) so the child never KeyErrors on it."""
        data_dir, extract_script = drain_dirs
        _write_marker(data_dir, "sess-a")
        spy = _PopenSpy()
        monkeypatch.setattr(extraction_drain.subprocess, "Popen", spy)

        extraction_drain.process_deferred_extractions(data_dir, extract_script)

        payload = json.loads(spy.handles[0].stdin.written.decode())
        assert payload["trigger"] == ""

    def test_missing_extract_script_is_a_noop(self, drain_dirs, monkeypatch):
        data_dir, _ = drain_dirs
        _write_marker(data_dir, "sess-a")
        spy = _PopenSpy()
        monkeypatch.setattr(extraction_drain.subprocess, "Popen", spy)

        assert extraction_drain.process_deferred_extractions(
            data_dir, data_dir / "nope.py"
        ).launched == 0
        # Marker stays queued for a caller that does have the script.
        assert len(list((data_dir / "pending_extractions").glob("*.json"))) == 1
        assert spy.calls == []

    def test_unparseable_marker_is_discarded(self, drain_dirs, monkeypatch):
        data_dir, extract_script = drain_dirs
        (data_dir / "pending_extractions" / "junk.json").write_text("{not json", encoding="utf-8")
        spy = _PopenSpy()
        monkeypatch.setattr(extraction_drain.subprocess, "Popen", spy)

        assert extraction_drain.process_deferred_extractions(
            data_dir, extract_script
        ).launched == 0
        assert list((data_dir / "processing_extractions").glob("*.json")) == []
        assert spy.calls == []

    def test_launch_failure_requeues_the_marker(self, drain_dirs, monkeypatch):
        data_dir, extract_script = drain_dirs
        _write_marker(data_dir, "sess-a")

        def _boom(*a, **k):
            raise OSError("no uv")

        monkeypatch.setattr(extraction_drain.subprocess, "Popen", _boom)

        assert extraction_drain.process_deferred_extractions(
            data_dir, extract_script
        ).launched == 0
        # Back in the queue — a later drain retries rather than losing it.
        assert [p.name for p in (data_dir / "pending_extractions").glob("*.json")] == [
            "sess-a.json"
        ]

    def test_dequeued_old_marker_is_not_immediately_stale(self, drain_dirs, monkeypatch):
        """Regression: staleness must be measured from launch, not from when
        SessionEnd wrote the marker.

        ``os.rename`` preserves mtime, so a marker written Friday and drained
        Monday used to look hours stale the instant it entered
        ``processing_extractions/`` — a concurrent recovery pass would requeue
        it and launch a duplicate extraction while the first child was still
        running.
        """
        data_dir, extract_script = drain_dirs
        marker = _write_marker(data_dir, "sess-a")
        old = time.time() - extraction_drain.STALE_SECONDS - 3600
        os.utime(marker, (old, old))
        spy = _PopenSpy()
        monkeypatch.setattr(extraction_drain.subprocess, "Popen", spy)

        extraction_drain.process_deferred_extractions(data_dir, extract_script)

        dest = data_dir / "processing_extractions" / "sess-a.json"
        assert time.time() - dest.stat().st_mtime < extraction_drain.STALE_SECONDS
        # The concrete symptom: an immediately-following recovery pass must
        # NOT requeue it (that requeue is what launched the duplicate).
        extraction_drain.recover_stale_processing(
            data_dir / "processing_extractions", data_dir / "pending_extractions"
        )
        assert dest.exists()
        assert list((data_dir / "pending_extractions").glob("*.json")) == []

    def test_broken_pipe_after_spawn_does_not_requeue(self, drain_dirs, monkeypatch):
        """A child that died before reading stdin still EXISTS — its marker
        belongs to stale recovery, not to the requeue-on-launch-failure path
        (requeueing would let a second drain launch a duplicate).
        """
        data_dir, extract_script = drain_dirs
        _write_marker(data_dir, "sess-a")
        spy = _PopenSpy(broken_pipe=True)
        monkeypatch.setattr(extraction_drain.subprocess, "Popen", spy)

        result = extraction_drain.process_deferred_extractions(data_dir, extract_script)

        assert result.launched == 1
        assert list((data_dir / "pending_extractions").glob("*.json")) == []
        assert [p.name for p in (data_dir / "processing_extractions").glob("*.json")] == [
            "sess-a.json"
        ]

    def test_wait_counts_children_that_exited_nonzero(self, drain_dirs, monkeypatch):
        data_dir, extract_script = drain_dirs
        _write_marker(data_dir, "sess-a")
        spy = _PopenSpy(returncode=3)
        monkeypatch.setattr(extraction_drain.subprocess, "Popen", spy)

        result = extraction_drain.process_deferred_extractions(
            data_dir, extract_script, wait=True
        )

        assert result == extraction_drain.DrainResult(launched=1, failed=1)

    def test_without_wait_children_are_never_counted_failed(self, drain_dirs, monkeypatch):
        data_dir, extract_script = drain_dirs
        _write_marker(data_dir, "sess-a")
        spy = _PopenSpy(returncode=3)
        monkeypatch.setattr(extraction_drain.subprocess, "Popen", spy)

        result = extraction_drain.process_deferred_extractions(data_dir, extract_script)

        assert result == extraction_drain.DrainResult(launched=1, failed=0)

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
        # No residue: neither the marker nor a claim temp file remains.
        assert list(proc_dir.iterdir()) == []

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

    def test_losing_the_claim_race_does_not_resurrect_the_marker(
        self, drain_dirs, monkeypatch
    ):
        """Regression: two drains recovering the same stale marker.

        Pre-fix, both read the marker and both rewrote it in place — the
        loser's ``write_text`` RECREATED the file in
        ``processing_extractions/`` after the winner had already requeued it,
        and its re-replace double-incremented ``attempts`` toward the
        quarantine cap. The fix claims the marker by atomic rename first; the
        loser gets FileNotFoundError and must walk away leaving nothing
        behind.
        """
        data_dir, _ = drain_dirs
        proc_dir = data_dir / "processing_extractions"
        pending_dir = data_dir / "pending_extractions"
        m = proc_dir / "sess-a.json"
        m.write_text(json.dumps({"session_id": "a"}), encoding="utf-8")
        old = time.time() - extraction_drain.STALE_SECONDS - 60
        os.utime(m, (old, old))

        def winner_got_there_first(src, dst):
            # The concurrent drain completed its whole recovery between our
            # stat and our claim: the source name is gone.
            Path(src).unlink()
            raise FileNotFoundError(src)

        monkeypatch.setattr(extraction_drain.os, "rename", winner_got_there_first)

        extraction_drain.recover_stale_processing(proc_dir, pending_dir)

        # The loser contributed nothing: no recreated marker, no claim temp
        # file, no second requeue bumping attempts.
        assert list(proc_dir.iterdir()) == []
        assert list(pending_dir.iterdir()) == []


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


class TestDrainEntryPointBehavior:
    """Behavioral tests of ``drain_extractions.main()`` — arg parsing, exit
    codes, error surfacing. These exercise the real entry point end to end
    (with only ``subprocess.Popen`` stubbed), where the classes above pin the
    shared library; the source-substring tripwires in
    ``TestDrainReportsBothQueues`` stay as cheap regression guards.
    """

    def test_drains_the_given_data_dir_and_exits_zero(
        self, drain_dirs, monkeypatch, capsys
    ):
        import drain_extractions

        data_dir, _ = drain_dirs
        _write_marker(data_dir, "sess-a")
        spy = _PopenSpy()
        monkeypatch.setattr(extraction_drain.subprocess, "Popen", spy)

        assert drain_extractions.main(["--data-dir", str(data_dir)]) == 0

        # It targets the real extract_learnings.py beside the entry point.
        argv, _ = spy.calls[0]
        assert argv[-1].endswith("extract_learnings.py")
        assert list((data_dir / "pending_extractions").glob("*.json")) == []
        # Default invocation (the launcher path) is silent on stdout.
        assert capsys.readouterr().out == ""

    def test_verbose_prints_the_summary(self, drain_dirs, monkeypatch, capsys):
        import drain_extractions

        data_dir, _ = drain_dirs
        _write_marker(data_dir, "sess-a")
        spy = _PopenSpy()
        monkeypatch.setattr(extraction_drain.subprocess, "Popen", spy)

        assert drain_extractions.main(["--data-dir", str(data_dir), "--verbose"]) == 0
        assert "1 extraction(s) launched" in capsys.readouterr().out

    def test_missing_extract_script_errors_on_stderr_without_verbose(
        self, tmp_path, monkeypatch, capsys
    ):
        """Regression: the launcher runs the drain non-verbose; a missing
        script must still say so on stderr, not exit 1 silently.
        """
        import drain_extractions

        monkeypatch.setattr(
            drain_extractions, "__file__", str(tmp_path / "drain_extractions.py")
        )

        assert drain_extractions.main(["--data-dir", str(tmp_path / "data")]) == 1
        assert "extract_learnings.py missing" in capsys.readouterr().err

    def test_wait_propagates_child_failure_to_the_exit_code(
        self, drain_dirs, monkeypatch, capsys
    ):
        """Regression: `drain … --wait && echo ok` must not print ok when
        every child failed.
        """
        import drain_extractions

        data_dir, _ = drain_dirs
        _write_marker(data_dir, "sess-a")
        spy = _PopenSpy(returncode=1)
        monkeypatch.setattr(extraction_drain.subprocess, "Popen", spy)

        assert drain_extractions.main(["--data-dir", str(data_dir), "--wait"]) == 1
        assert "1 of 1 extraction child(ren) failed" in capsys.readouterr().err


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
