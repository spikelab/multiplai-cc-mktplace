"""Tests for checkpoint_writer.py — distillation, model call, atomic writes."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from conftest import import_script
from lib import checkpoint as cp

checkpoint_writer = import_script("checkpoint_writer", "checkpoint_writer.py")

VALID_CHECKPOINT = "\n".join(
    f"## {s}\n- state for {s.lower()}" for s in cp.CHECKPOINT_SECTIONS
)


@pytest.fixture
def data_env(tmp_path, monkeypatch):
    """Point plugin data_dir at a tmp dir (paths cache reset around the test)."""
    from multiplai_core.paths import _reset_cache

    data_dir = tmp_path / "data"
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_data_dir", str(data_dir))
    _reset_cache()
    yield data_dir
    _reset_cache()


def _turn(role, text, ts, tool_blocks=None):
    content = [{"type": "text", "text": text}] + (tool_blocks or [])
    rec = {
        "type": role,
        "timestamp": ts.isoformat(),
        "cwd": "/work/proj",
        "message": {"role": role, "content": content},
    }
    if role == "assistant":
        rec["message"]["usage"] = {"input_tokens": 1000, "output_tokens": 10}
    return rec


def _write_transcript(path, turns):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(t) for t in turns) + "\n")


def _fake_run_agent(captured, text=VALID_CHECKPOINT):
    async def fake(prompt, **kwargs):
        captured.append({"prompt": prompt, "kwargs": kwargs})
        return SimpleNamespace(text=text)

    return fake


def _payload(session_id, transcript, tokens=110_000, reason="band"):
    return {
        "session_id": session_id,
        "transcript_path": str(transcript),
        "cwd": "/work/proj",
        "tokens": tokens,
        "reason": reason,
    }


class TestWriteCheckpoint:
    def test_writes_valid_checkpoint(self, tmp_path, data_env, monkeypatch):
        now = datetime.now(timezone.utc)
        transcript = tmp_path / "t.jsonl"
        _write_transcript(
            transcript,
            [
                _turn("user", "please build the widget", now - timedelta(minutes=10)),
                _turn(
                    "assistant", "building it", now,
                    tool_blocks=[{"type": "tool_use", "name": "Write",
                                  "input": {"file_path": "/work/proj/w.py"}, "id": "t1"}],
                ),
            ],
        )
        captured = []
        monkeypatch.setattr(checkpoint_writer, "run_agent", _fake_run_agent(captured))

        ok = asyncio.run(checkpoint_writer.write_checkpoint(_payload("s1", transcript)))

        assert ok is True
        cp_file = cp.checkpoint_file(data_env, "s1")
        assert cp_file.exists()
        assert cp.validate_checkpoint(cp_file.read_text())
        # Transcript content reached the writer prompt
        assert "please build the widget" in captured[0]["prompt"]
        # State updated
        state = cp.load_state(data_env, "s1")
        assert state["last_band_idx"] == 1
        assert state["last_checkpoint_tokens"] == 110_000

    def test_incremental_second_write(self, tmp_path, data_env, monkeypatch):
        now = datetime.now(timezone.utc)
        transcript = tmp_path / "t.jsonl"
        _write_transcript(
            transcript, [_turn("user", "old work item alpha", now - timedelta(hours=2))]
        )
        captured = []
        monkeypatch.setattr(checkpoint_writer, "run_agent", _fake_run_agent(captured))
        assert asyncio.run(checkpoint_writer.write_checkpoint(_payload("s1", transcript)))

        # Append newer turns, then write again
        with transcript.open("a") as f:
            f.write(json.dumps(_turn("user", "new work item beta", now + timedelta(minutes=5))) + "\n")
        assert asyncio.run(
            checkpoint_writer.write_checkpoint(_payload("s1", transcript, tokens=205_000))
        )

        second_prompt = captured[1]["prompt"]
        assert "PREVIOUS CHECKPOINT" in second_prompt
        assert "new work item beta" in second_prompt
        # Turns older than the first checkpoint are not re-distilled
        assert "old work item alpha" not in second_prompt

    def test_handoff_writes_pending_marker(self, tmp_path, data_env, monkeypatch):
        now = datetime.now(timezone.utc)
        transcript = tmp_path / "t.jsonl"
        _write_transcript(transcript, [_turn("user", "work", now)])
        monkeypatch.setattr(checkpoint_writer, "run_agent", _fake_run_agent([]))

        assert asyncio.run(
            checkpoint_writer.write_checkpoint(_payload("s1", transcript, tokens=210_000))
        )
        payload = cp.consume_pending_marker(
            data_env, "/work/proj", "other-session", cp.load_config()
        )
        assert payload is not None
        assert payload["session_id"] == "s1"

    def test_below_handoff_no_marker(self, tmp_path, data_env, monkeypatch):
        now = datetime.now(timezone.utc)
        transcript = tmp_path / "t.jsonl"
        _write_transcript(transcript, [_turn("user", "work", now)])
        monkeypatch.setattr(checkpoint_writer, "run_agent", _fake_run_agent([]))

        assert asyncio.run(
            checkpoint_writer.write_checkpoint(_payload("s1", transcript, tokens=110_000))
        )
        assert cp.consume_pending_marker(
            data_env, "/work/proj", "other", cp.load_config()
        ) is None

    def test_model_failure_keeps_previous(self, tmp_path, data_env, monkeypatch):
        cp.write_checkpoint_file(data_env, "s1", VALID_CHECKPOINT)
        now = datetime.now(timezone.utc)
        transcript = tmp_path / "t.jsonl"
        _write_transcript(transcript, [_turn("user", "work", now)])

        async def boom(prompt, **kwargs):
            raise RuntimeError("model exploded")

        monkeypatch.setattr(checkpoint_writer, "run_agent", boom)
        ok = asyncio.run(checkpoint_writer.write_checkpoint(_payload("s1", transcript)))
        assert ok is False
        assert cp.checkpoint_file(data_env, "s1").read_text() == VALID_CHECKPOINT

    def test_invalid_output_rejected(self, tmp_path, data_env, monkeypatch):
        now = datetime.now(timezone.utc)
        transcript = tmp_path / "t.jsonl"
        _write_transcript(transcript, [_turn("user", "work", now)])
        monkeypatch.setattr(
            checkpoint_writer, "run_agent", _fake_run_agent([], text="I could not comply.")
        )
        ok = asyncio.run(checkpoint_writer.write_checkpoint(_payload("s1", transcript)))
        assert ok is False
        assert not cp.checkpoint_file(data_env, "s1").exists()

    def test_child_session_skipped(self, tmp_path, data_env, monkeypatch):
        transcript = tmp_path / "subagents" / "t.jsonl"
        _write_transcript(
            transcript, [_turn("user", "work", datetime.now(timezone.utc))]
        )
        called = []
        monkeypatch.setattr(checkpoint_writer, "run_agent", _fake_run_agent(called))
        ok = asyncio.run(checkpoint_writer.write_checkpoint(_payload("s1", transcript)))
        assert ok is False
        assert called == []

    def test_empty_transcript_skips(self, tmp_path, data_env, monkeypatch):
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("")
        called = []
        monkeypatch.setattr(checkpoint_writer, "run_agent", _fake_run_agent(called))
        ok = asyncio.run(checkpoint_writer.write_checkpoint(_payload("s1", transcript)))
        assert ok is False
        assert called == []


class TestMainReleasesMarker:
    def test_marker_released_even_on_failure(self, tmp_path, data_env, monkeypatch):
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("")
        cp.claim_writer(data_env, "s1")

        payload = json.dumps(_payload("s1", transcript))
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO(payload))
        checkpoint_writer.main()
        assert cp.writer_inflight(data_env, "s1") is False


class TestPromptShape:
    def test_fresh_prompt_has_all_sections(self):
        prompt = checkpoint_writer.build_writer_prompt("", "USER: hi")
        for section in cp.CHECKPOINT_SECTIONS:
            assert f"## {section}" in prompt
        assert "PREVIOUS CHECKPOINT" not in prompt

    def test_segment_cap(self):
        huge = "x" * 1_000_000
        capped = checkpoint_writer._cap_segment(huge)
        assert len(capped) < 300_000
        assert "elided for length" in capped
