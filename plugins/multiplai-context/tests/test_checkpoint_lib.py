"""Unit tests for lib/checkpoint.py — config, token reading, triggers, markers."""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from lib import checkpoint as cp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assistant_record(
    input_tokens=1000, cache_read=0, cache_creation=0, sidechain=False, text="ok"
):
    rec = {
        "type": "assistant",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
                "output_tokens": 50,
            },
        },
    }
    if sidechain:
        rec["isSidechain"] = True
    return rec


def _write_transcript(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


VALID_CHECKPOINT = "\n".join(
    f"## {s}\n- something about {s.lower()}" for s in cp.CHECKPOINT_SECTIONS
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_defaults(self):
        cfg = cp.load_config()
        assert cfg.bands == (100_000, 200_000)
        assert cfg.handoff_tokens == 200_000
        assert cfg.enabled is True

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_tokens", "50000,90000,150000")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_handoff_tokens", "150000")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_refresh_tokens", "10000")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_model", "sonnet")
        cfg = cp.load_config()
        assert cfg.bands == (50_000, 90_000, 150_000)
        assert cfg.handoff_tokens == 150_000
        assert cfg.refresh_tokens == 10_000
        assert cfg.model == "sonnet"

    def test_malformed_bands_fall_back(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_tokens", "banana,42x")
        cfg = cp.load_config()
        assert cfg.bands == (100_000, 200_000)

    def test_malformed_int_falls_back(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_handoff_tokens", "many")
        cfg = cp.load_config()
        assert cfg.handoff_tokens == 200_000

    def test_handoff_clamped_to_last_band(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_handoff_tokens", "50000")
        cfg = cp.load_config()
        assert cfg.handoff_tokens == 200_000  # clamped up to bands[-1]

    def test_disabled(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_enabled", "false")
        assert cp.load_config().enabled is False


# ---------------------------------------------------------------------------
# Token reading
# ---------------------------------------------------------------------------

class TestReadContextTokens:
    def test_missing_file(self, tmp_path):
        assert cp.read_context_tokens(tmp_path / "nope.jsonl") == 0

    def test_sums_cache_fields(self, tmp_path):
        t = tmp_path / "t.jsonl"
        _write_transcript(
            t, [_assistant_record(input_tokens=1_200, cache_read=140_000, cache_creation=9_000)]
        )
        assert cp.read_context_tokens(t) == 150_200

    def test_uses_last_main_chain_record(self, tmp_path):
        t = tmp_path / "t.jsonl"
        _write_transcript(
            t,
            [
                _assistant_record(input_tokens=10_000),
                _assistant_record(input_tokens=90_000, cache_read=30_000),
                _assistant_record(input_tokens=500_000, sidechain=True),  # subagent
                {"type": "user", "message": {"role": "user", "content": "hi"}},
            ],
        )
        assert cp.read_context_tokens(t) == 120_000

    def test_garbage_lines_skipped(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text('not json\n{"type":"weird"}\n' + json.dumps(_assistant_record(5000)) + "\n")
        assert cp.read_context_tokens(t) == 5000

    def test_large_file_tail_scan(self, tmp_path):
        t = tmp_path / "t.jsonl"
        filler = [_assistant_record(input_tokens=1, text="x" * 5_000) for _ in range(200)]
        filler.append(_assistant_record(input_tokens=42_000, cache_read=8_000))
        _write_transcript(t, filler)
        assert t.stat().st_size > 512_000  # forces the tail-seek path
        assert cp.read_context_tokens(t) == 50_000

    def test_after_ts_ignores_stale_pre_compact_usage(self, tmp_path):
        """Post-rebuild, the tail still ends in pre-compact usage — must
        read as 0 (no fresh usage), not as the stale huge number."""
        t = tmp_path / "t.jsonl"
        _write_transcript(t, [_assistant_record(input_tokens=1_500, cache_read=42_500)])
        assert cp.read_context_tokens(t) == 44_000
        future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        assert cp.read_context_tokens(t, after_ts=future) == 0

    def test_after_ts_accepts_fresh_usage(self, tmp_path):
        t = tmp_path / "t.jsonl"
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _write_transcript(t, [_assistant_record(input_tokens=9_000)])
        assert cp.read_context_tokens(t, after_ts=past) == 9_000

    def test_after_ts_malformed_ignored(self, tmp_path):
        t = tmp_path / "t.jsonl"
        _write_transcript(t, [_assistant_record(input_tokens=9_000)])
        assert cp.read_context_tokens(t, after_ts="not-a-date") == 9_000


class TestChildSessionGuard:
    def test_env_guard(self, monkeypatch):
        monkeypatch.setenv("_HOOK_CHILD_SESSION", "1")
        assert cp.is_child_session("/x/y.jsonl") is True

    def test_path_guards(self):
        assert cp.is_child_session("/p/subagents/abc.jsonl") is True
        assert cp.is_child_session("/p/hook-sessions/abc.jsonl") is True
        assert cp.is_child_session("/p/projects/-x-y/abc.jsonl") is False


# ---------------------------------------------------------------------------
# Trigger decision
# ---------------------------------------------------------------------------

class TestTrigger:
    CFG = cp.CheckpointConfig()

    def test_band_index(self):
        assert cp.band_index(50_000, self.CFG.bands) == 0
        assert cp.band_index(100_000, self.CFG.bands) == 1
        assert cp.band_index(150_000, self.CFG.bands) == 1
        assert cp.band_index(250_000, self.CFG.bands) == 2

    def test_below_first_band_no_trigger(self):
        assert cp.checkpoint_trigger(60_000, {}, self.CFG) is None

    def test_first_band_crossing(self):
        assert cp.checkpoint_trigger(105_000, {}, self.CFG) == "band"

    def test_no_retrigger_same_band(self):
        state = {"last_band_idx": 1, "last_checkpoint_tokens": 105_000}
        assert cp.checkpoint_trigger(150_000, state, self.CFG) is None

    def test_second_band_crossing(self):
        state = {"last_band_idx": 1, "last_checkpoint_tokens": 105_000}
        assert cp.checkpoint_trigger(205_000, state, self.CFG) == "band"

    def test_refresh_above_handoff(self):
        state = {"last_band_idx": 2, "last_checkpoint_tokens": 205_000}
        assert cp.checkpoint_trigger(209_000, state, self.CFG) is None
        assert cp.checkpoint_trigger(231_000, state, self.CFG) == "refresh"

    def test_disabled_never_triggers(self):
        cfg = cp.CheckpointConfig(enabled=False)
        assert cp.checkpoint_trigger(500_000, {}, cfg) is None


class TestWriterMarker:
    def test_claim_and_release(self, tmp_path):
        assert cp.writer_inflight(tmp_path, "s1") is False
        cp.claim_writer(tmp_path, "s1")
        assert cp.writer_inflight(tmp_path, "s1") is True
        cp.release_writer(tmp_path, "s1")
        assert cp.writer_inflight(tmp_path, "s1") is False

    def test_stale_marker_ignored(self, tmp_path, monkeypatch):
        marker = cp.claim_writer(tmp_path, "s1")
        old = time.time() - 3600
        import os
        os.utime(marker, (old, old))
        assert cp.writer_inflight(tmp_path, "s1") is False


# ---------------------------------------------------------------------------
# Pending markers
# ---------------------------------------------------------------------------

class TestPendingMarker:
    CFG = cp.CheckpointConfig()

    def test_roundtrip(self, tmp_path):
        cwd = str(tmp_path / "myproj")
        cp.write_pending_marker(tmp_path, cwd, "old-sess", 210_000)
        payload = cp.consume_pending_marker(tmp_path, cwd, "new-sess", self.CFG)
        assert payload is not None
        assert payload["session_id"] == "old-sess"
        assert payload["tokens"] == 210_000
        # One-shot: second consume returns nothing
        assert cp.consume_pending_marker(tmp_path, cwd, "new-sess", self.CFG) is None

    def test_expired_marker_discarded(self, tmp_path):
        cwd = str(tmp_path / "myproj")
        marker = cp.write_pending_marker(tmp_path, cwd, "old-sess", 210_000)
        payload = json.loads(marker.read_text())
        payload["created_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=48)
        ).isoformat()
        marker.write_text(json.dumps(payload))
        assert cp.consume_pending_marker(tmp_path, cwd, "new-sess", self.CFG) is None
        assert not marker.exists()

    def test_same_session_resume_keeps_marker(self, tmp_path):
        cwd = str(tmp_path / "myproj")
        cp.write_pending_marker(tmp_path, cwd, "sess-a", 210_000)
        # The same session resuming must not consume its own marker…
        assert cp.consume_pending_marker(tmp_path, cwd, "sess-a", self.CFG) is None
        # …and a genuinely new session still finds it.
        assert cp.consume_pending_marker(tmp_path, cwd, "sess-b", self.CFG) is not None

    def test_same_session_allowed_after_compact(self, tmp_path):
        """source=compact rebuild: same session id, marker must be consumable."""
        cwd = str(tmp_path / "myproj")
        cp.write_pending_marker(tmp_path, cwd, "sess-a", 210_000)
        payload = cp.consume_pending_marker(
            tmp_path, cwd, "sess-a", self.CFG, allow_same_session=True
        )
        assert payload is not None and payload["session_id"] == "sess-a"

    def test_no_marker(self, tmp_path):
        assert cp.consume_pending_marker(tmp_path, "/x", "s", self.CFG) is None


class TestAutoCompactSteering:
    """Mirrors the native formula (binary v2.1.201):
    usable = clamp(window) − min(maxOutput, 20000);
    trigger = min(usable × pct/100, usable − 13000);
    windows configured below the 200K fire gate DISABLE auto-compact."""

    @pytest.fixture(autouse=True)
    def _no_ambient(self, monkeypatch):
        for var in (
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_unset_returns_none(self):
        assert cp.autocompact_trigger_tokens() is None

    def test_window_with_pct(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "250000")
        monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "90")
        # usable = 250000 − 20000 = 230000; min(207000, 217000) = 207000
        assert cp.autocompact_trigger_tokens() == 207_000

    def test_below_fire_gate_disables(self, monkeypatch):
        """Field-verified: a 100K env window doesn't lower the trigger —
        it hard-disables soft auto-compact (Ire gate)."""
        monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "100000")
        monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "30")
        assert cp.autocompact_trigger_tokens() is None

    def test_window_default_pct_uses_margin(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "200000")
        # usable = 180000; trigger = 180000 − 13000
        assert cp.autocompact_trigger_tokens() == 167_000

    def test_output_reserve_from_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "200000")
        monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "45")
        monkeypatch.setenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "16384")
        # usable = 200000 − 16384 = 183616; min(82627, 170616) = 82627
        assert cp.autocompact_trigger_tokens() == 82_627

    def test_malformed_returns_none(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "lots")
        assert cp.autocompact_trigger_tokens() is None


class TestResetSessionCounters:
    def test_resets_bands_keeps_ts(self, tmp_path):
        cp.save_state(tmp_path, "s1", {
            "last_band_idx": 2,
            "last_checkpoint_tokens": 210_000,
            "last_checkpoint_ts": "2026-07-06T12:00:00+00:00",
        })
        (cp.session_dir(tmp_path, "s1") / "nudge.json").write_text("{}")
        cp.reset_session_counters(tmp_path, "s1")
        state = cp.load_state(tmp_path, "s1")
        assert state["last_band_idx"] == 0
        assert state["last_checkpoint_tokens"] == 0
        assert state["last_checkpoint_ts"] == "2026-07-06T12:00:00+00:00"
        assert not (cp.session_dir(tmp_path, "s1") / "nudge.json").exists()


# ---------------------------------------------------------------------------
# Validation & rebuild seed
# ---------------------------------------------------------------------------

class TestValidationAndSeed:
    def test_valid_checkpoint(self):
        assert cp.validate_checkpoint(VALID_CHECKPOINT) is True

    def test_empty_invalid(self):
        assert cp.validate_checkpoint("") is False
        assert cp.validate_checkpoint("   \n ") is False

    def test_partial_sections_invalid(self):
        text = "## Current intent\n- x\n## Next action\n- y\n"
        assert cp.validate_checkpoint(text) is False

    def test_rebuild_seed_structure(self):
        seed = cp.build_rebuild_context(VALID_CHECKPOINT, 214_000)
        assert seed.startswith("--- CONTEXT REBUILD ---")
        assert "214,000 tokens" in seed
        assert "## Current intent" in seed
        assert seed.rstrip().endswith("--- END CONTEXT REBUILD ---")

    def test_state_roundtrip(self, tmp_path):
        cp.save_state(tmp_path, "s1", {"last_band_idx": 2})
        assert cp.load_state(tmp_path, "s1")["last_band_idx"] == 2
        assert cp.load_state(tmp_path, "missing") == {}
