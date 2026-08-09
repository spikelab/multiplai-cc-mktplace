"""`/clear` saves before it discards, and a closing tab queues the save.

Nothing used to run on this edge at all: ``session_end.py`` recorded a
registry event and queued the diary extraction, and everything since the last
checkpoint went with the window.

The split is on ``reason`` and it is not cosmetic. On ``clear``/``resume`` the
container keeps running — verified in the field, the two halves of one
``/clear``-ed tab share hostname ``claude-work-04221854`` — so the detached
writer outlives this hook. On every other reason the container is exiting
under ``docker run --rm``, PID 1 goes, and a detached child goes with it: a
spawn there would look like it worked and produce nothing. Those queue a
marker for the host-side drain instead.

**What these tests can and cannot prove.** The spawn/queue *decision* is
asserted here against a fake spawn and a real queue directory. Whether a
detached child actually survives a real container teardown cannot be tested
from inside a session container — there is no Docker daemon here and no
`docker` on the SSH gateway allowlist — so that half is Spike's to check live.
"""

import io
import json
from datetime import datetime, timezone

import pytest

from conftest import import_script
from lib import checkpoint as cp
from lib import checkpoint_drain as cpd

session_end = import_script("session_end_checkpoint", "session_end.py")

VALID_CHECKPOINT = "\n".join(
    f"## {s}\n- state for {s.lower()}" for s in cp.CHECKPOINT_SECTIONS
)


@pytest.fixture
def data_env(tmp_path, monkeypatch):
    from multiplai_core.paths import _reset_cache

    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_DATA_DIR", str(tmp_path / "data"))
    _reset_cache()
    yield tmp_path / "data"
    _reset_cache()


class _SpawnRecorder:
    def __init__(self, ok=True):
        self.payloads: list[dict] = []
        self.ok = ok

    def __call__(self, payload):
        self.payloads.append(payload)
        return self.ok


def _transcript(tmp_path, tokens=120_000):
    rec = {
        "type": "assistant",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cwd": "/work/proj",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "working"}],
            "usage": {"input_tokens": 1_000,
                      "cache_read_input_tokens": tokens - 1_000,
                      "cache_creation_input_tokens": 0},
        },
    }
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(rec) + "\n")
    return path


def _run_end(monkeypatch, tmp_path, *, reason=None, session_id="s1"):
    payload = {
        "session_id": session_id,
        "transcript_path": str(_transcript(tmp_path)),
        "cwd": "/work/proj",
    }
    if reason is not None:
        payload["reason"] = reason
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    session_end.main()


class TestTheSplit:

    def test_clear_spawns_and_queues_nothing(self, tmp_path, data_env, monkeypatch):
        rec = _SpawnRecorder()
        monkeypatch.setattr(cp, "spawn_writer", rec)

        _run_end(monkeypatch, tmp_path, reason="clear")

        assert len(rec.payloads) == 1
        assert rec.payloads[0]["session_id"] == "s1"
        assert rec.payloads[0]["tokens"] == 120_000
        assert rec.payloads[0]["reason"] == "session-end:clear"
        assert cpd.pending_checkpoint_count(data_env) == 0
        # Single-flight marker claimed, exactly as the Stop hook does.
        assert cp.writer_inflight(data_env, "s1") is True

    def test_resume_spawns_too(self, tmp_path, data_env, monkeypatch):
        rec = _SpawnRecorder()
        monkeypatch.setattr(cp, "spawn_writer", rec)

        _run_end(monkeypatch, tmp_path, reason="resume")

        assert len(rec.payloads) == 1
        assert cpd.pending_checkpoint_count(data_env) == 0

    @pytest.mark.parametrize(
        "reason", ["prompt_input_exit", "logout", "other",
                   "bypass_permissions_disabled"]
    )
    def test_a_container_exiting_reason_queues_and_never_spawns(
        self, tmp_path, data_env, monkeypatch, reason
    ):
        rec = _SpawnRecorder()
        monkeypatch.setattr(cp, "spawn_writer", rec)

        _run_end(monkeypatch, tmp_path, reason=reason)

        assert rec.payloads == []
        assert cpd.pending_checkpoint_count(data_env) == 1
        marker = json.loads(
            (cpd.pending_dir(data_env) / "s1.json").read_text()
        )
        assert marker["session_id"] == "s1"
        assert marker["tokens"] == 120_000
        assert marker["reason"] == f"session-end:{reason}"
        assert marker["transcript_path"].endswith("t.jsonl")

    def test_a_missing_reason_behaves_as_other(self, tmp_path, data_env, monkeypatch):
        """The safe half of the split: a queued marker survives either way, a
        killed spawn does not."""
        rec = _SpawnRecorder()
        monkeypatch.setattr(cp, "spawn_writer", rec)

        _run_end(monkeypatch, tmp_path, reason=None)

        assert rec.payloads == []
        assert cpd.pending_checkpoint_count(data_env) == 1

    def test_an_unknown_future_reason_also_queues(
        self, tmp_path, data_env, monkeypatch
    ):
        rec = _SpawnRecorder()
        monkeypatch.setattr(cp, "spawn_writer", rec)

        _run_end(monkeypatch, tmp_path, reason="something_new")

        assert rec.payloads == []
        assert cpd.pending_checkpoint_count(data_env) == 1

    def test_the_extraction_marker_is_still_written(
        self, tmp_path, data_env, monkeypatch
    ):
        """The pre-existing job must not regress: SessionEnd still queues the
        diary extraction whatever it does about the checkpoint."""
        monkeypatch.setattr(cp, "spawn_writer", _SpawnRecorder())

        _run_end(monkeypatch, tmp_path, reason="clear")

        assert (data_env / "pending_extractions" / "s1.json").exists()

    def test_an_inflight_writer_is_not_respawned(
        self, tmp_path, data_env, monkeypatch
    ):
        rec = _SpawnRecorder()
        monkeypatch.setattr(cp, "spawn_writer", rec)
        cp.claim_writer(data_env, "s1")

        _run_end(monkeypatch, tmp_path, reason="clear")

        assert rec.payloads == []

    def test_a_failed_spawn_releases_the_marker(
        self, tmp_path, data_env, monkeypatch
    ):
        monkeypatch.setattr(cp, "spawn_writer", _SpawnRecorder(ok=False))

        _run_end(monkeypatch, tmp_path, reason="clear")

        assert cp.writer_inflight(data_env, "s1") is False

    def test_a_child_session_never_checkpoints(
        self, tmp_path, data_env, monkeypatch
    ):
        rec = _SpawnRecorder()
        monkeypatch.setattr(cp, "spawn_writer", rec)
        transcript = tmp_path / "subagents" / "t.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("")
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
            "session_id": "s1", "transcript_path": str(transcript),
            "cwd": "/work/proj", "reason": "clear",
        })))

        session_end.main()

        assert rec.payloads == []
        assert cpd.pending_checkpoint_count(data_env) == 0


class TestTheDrain:
    """The other end of the queue — what the host runs after the container is
    gone. The launcher change is deliberately none: ``claude.sh`` already runs
    ``drain_extractions.py``, which now drains both queues."""

    def test_a_queued_marker_becomes_a_writer_launch(
        self, tmp_path, data_env, monkeypatch
    ):
        launched: list[dict] = []

        class _Popen:
            def __init__(self, argv, **kwargs):
                self.argv = argv
                self.stdin = self
                self.returncode = 0

            def write(self, blob):
                launched.append(json.loads(blob.decode()))

            def close(self):
                pass

            def wait(self):
                return 0

        monkeypatch.setattr(cpd.subprocess, "Popen", _Popen)
        cpd.queue_pending_checkpoint(data_env, {
            "session_id": "s1", "transcript_path": "/t.jsonl",
            "cwd": "/work/proj", "tokens": 120_000,
            "reason": "session-end:prompt_input_exit",
        })
        writer = tmp_path / "checkpoint_writer.py"
        writer.write_text("# stub\n")

        result = cpd.process_pending_checkpoints(data_env, writer)

        assert result.launched == 1
        assert launched[0]["session_id"] == "s1"
        assert launched[0]["tokens"] == 120_000
        # The child is told where its marker is so it can drop it on the way
        # out — the same contract the extraction queue uses.
        assert launched[0]["marker_path"].endswith("s1.json")
        assert cpd.pending_checkpoint_count(data_env) == 0

    def test_the_writer_drops_its_own_marker(self, tmp_path, data_env, monkeypatch):
        """End to end for the part that IS testable here: a marker in, a
        checkpoint file out, the marker gone."""
        import asyncio
        from types import SimpleNamespace

        checkpoint_writer = import_script(
            "checkpoint_writer_drain", "checkpoint_writer.py"
        )

        async def fake(prompt, **kwargs):
            return SimpleNamespace(text=VALID_CHECKPOINT)

        monkeypatch.setattr(checkpoint_writer, "run_agent", fake)
        transcript = _transcript(tmp_path)
        marker = cpd.queue_pending_checkpoint(data_env, {
            "session_id": "s1", "transcript_path": str(transcript),
            "cwd": "/work/proj", "tokens": 120_000,
            "reason": "session-end:logout",
        })
        payload = json.loads(marker.read_text())
        payload["marker_path"] = str(marker)
        monkeypatch.setattr(
            "sys.stdin", type("S", (), {"read": staticmethod(lambda: json.dumps(payload))})
        )

        checkpoint_writer.main()

        assert cp.checkpoint_file(data_env, "s1").exists()
        assert cp.validate_checkpoint(cp.checkpoint_file(data_env, "s1").read_text())
        assert not marker.exists()

    def test_the_dequeue_is_the_shared_one(self):
        """Not a second implementation: the atomic rename, the mtime refresh
        and the stale-marker recovery are ``extraction_drain``'s, used by both
        queues."""
        from lib import extraction_drain

        source = (
            __import__("pathlib").Path(cpd.__file__).read_text(encoding="utf-8")
        )
        assert "claim_pending_markers" in source
        assert "os.rename(str(marker_file), str(dest))" not in source
        assert callable(extraction_drain.claim_pending_markers)

    def test_a_launch_failure_requeues_rather_than_losing_the_save(
        self, tmp_path, data_env, monkeypatch
    ):
        def _boom(*a, **k):
            raise OSError("no fork for you")

        monkeypatch.setattr(cpd.subprocess, "Popen", _boom)
        cpd.queue_pending_checkpoint(data_env, {
            "session_id": "s1", "transcript_path": "/t.jsonl", "cwd": "/w",
            "tokens": 1, "reason": "session-end:other",
        })
        writer = tmp_path / "checkpoint_writer.py"
        writer.write_text("# stub\n")

        result = cpd.process_pending_checkpoints(data_env, writer)

        assert result.launched == 0
        assert cpd.pending_checkpoint_count(data_env) == 1

    def test_the_host_drain_entry_point_drains_both_queues(
        self, tmp_path, data_env, monkeypatch, capsys
    ):
        import drain_extractions

        drain_extractions_calls: list[str] = []

        class _Popen:
            def __init__(self, argv, **kwargs):
                drain_extractions_calls.append(str(argv[-1]))
                self.stdin = self

            def write(self, blob):
                pass

            def close(self):
                pass

        monkeypatch.setattr(cpd.subprocess, "Popen", _Popen)
        cpd.queue_pending_checkpoint(data_env, {
            "session_id": "s1", "transcript_path": "/t.jsonl", "cwd": "/w",
            "tokens": 1, "reason": "session-end:other",
        })

        assert drain_extractions.main(["--data-dir", str(data_env)]) == 0
        assert any(c.endswith("checkpoint_writer.py") for c in drain_extractions_calls)
