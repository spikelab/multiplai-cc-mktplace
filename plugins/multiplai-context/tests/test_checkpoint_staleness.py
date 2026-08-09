"""Age-based checkpointing — the dormant tab finally gets a checkpoint.

Every existing trigger is token-based, so a tab that sat at 40K tokens for
three days has no checkpoint at all. That is the inversion this fixes: the
session whose state you have most thoroughly lost track of was the one the
fleet view had least to say about, because `AGENTS.md` renders intent, next
action and files-in-hand from the checkpoint.

Criterion 12 has three parts, and the third is the one with teeth:

* `staleness_trigger()` exists,
* it is called from `session_stop.py`,
* it is driven by a **new** config field. `CheckpointConfig.ttl_hours` already
  means *pending-marker expiry* and is consumed by `consume_pending_marker`;
  reusing it would have been a one-word change that silently broke rebuild
  expiry. `TestTtlHoursUntouched` pins that separation.

On cost: the default moved from 3 hours to 0.5 on 2026-08-09, which inverts
what this file used to assert. Three hours kept write volume low and let a
session's unwritten segment grow until the writer could not finish it —
prevention beats the retry, so the cadence is now deliberately about two
writes per hour per session. At 0.5 hours stale / 30 minutes minimum age a
4-hour session writes eight small delta-merges. `TestWriteVolume` asserts
that arithmetic rather than leaving it in a comment.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from conftest import import_script
from lib import checkpoint as cp
from lib import session_registry as sr

session_stop = import_script("session_stop_staleness_mod", "session_stop.py")

CFG = cp.CheckpointConfig()


def make_registry_entry(data_dir, sid="s1", *, age=timedelta(hours=2)):
    """A session registry entry, aged. This is where session start time lives —
    `state.json` is created by the first checkpoint *write*, so for the
    sessions this trigger targets it does not exist."""
    rdir = data_dir / "sessions"
    rdir.mkdir(parents=True, exist_ok=True)
    started = (datetime.now(timezone.utc) - age).isoformat()
    (rdir / f"{sid}.json").write_text(json.dumps({
        "session_id": sid,
        "cwd": "/work",
        "started_at": started,
        "last_event": {"ts": started, "kind": "stop"},
    }))


def state_with_checkpoint(age=timedelta(hours=1)):
    return {"last_checkpoint_ts": (datetime.now(timezone.utc) - age).isoformat()}


def _write_marker(data_dir, *, cwd="/work", sid="s1", age=timedelta(hours=1)):
    """A pending handoff marker, aged. Keyed the way the writer keys it —
    hand-naming the file makes the read miss and the test pass vacuously."""
    pdir = data_dir / "checkpoints" / "pending"
    pdir.mkdir(parents=True, exist_ok=True)
    marker = pdir / f"{cp._project_key(cwd)}.json"
    marker.write_text(json.dumps({
        "session_id": sid,
        "cwd": cwd,
        "tokens": 210_000,
        "checkpoint_path": str(cp.checkpoint_file(data_dir, sid)),
        "created_at": (datetime.now(timezone.utc) - age).isoformat(),
    }))
    return marker


# ---------------------------------------------------------------------------
# The trigger
# ---------------------------------------------------------------------------

class TestStalenessTrigger:

    def test_an_old_session_with_no_checkpoint_fires(self, tmp_path):
        """The case the trigger exists for: never crossed a band, still open."""
        make_registry_entry(tmp_path, age=timedelta(hours=2))

        assert cp.staleness_trigger(tmp_path, "s1", {}, CFG) == "stale"

    def test_a_young_session_does_not_fire(self, tmp_path):
        """Without the age gate every session that completes a turn would
        write one."""
        make_registry_entry(tmp_path, age=timedelta(minutes=5))

        assert cp.staleness_trigger(tmp_path, "s1", {}, CFG) is None

    def test_the_age_gate_is_the_configured_minimum(self, tmp_path):
        make_registry_entry(tmp_path, age=timedelta(minutes=29))
        assert cp.staleness_trigger(tmp_path, "s1", {}, CFG) is None

        make_registry_entry(tmp_path, age=timedelta(minutes=31))
        assert cp.staleness_trigger(tmp_path, "s1", {}, CFG) == "stale"

    def test_a_fresh_checkpoint_does_not_fire(self, tmp_path):
        make_registry_entry(tmp_path, age=timedelta(hours=4))

        assert cp.staleness_trigger(
            tmp_path, "s1", state_with_checkpoint(timedelta(minutes=12)), CFG
        ) is None

    def test_a_checkpoint_past_stale_hours_fires(self, tmp_path):
        make_registry_entry(tmp_path, age=timedelta(hours=6))

        assert cp.staleness_trigger(
            tmp_path, "s1", state_with_checkpoint(timedelta(hours=4)), CFG
        ) == "stale"

    def test_a_naive_checkpoint_timestamp_is_read_as_utc(self, tmp_path):
        """Timestamps in this store are written with tzinfo, but a
        hand-edited or older state file may not be — and comparing an aware
        `now` to a naive `last` raises."""
        make_registry_entry(tmp_path, age=timedelta(hours=6))
        naive = (datetime.now(timezone.utc) - timedelta(hours=4)).replace(tzinfo=None)

        assert cp.staleness_trigger(
            tmp_path, "s1", {"last_checkpoint_ts": naive.isoformat()}, CFG
        ) == "stale"

    def test_a_corrupt_checkpoint_timestamp_fires(self, tmp_path):
        """Unreadable age means we cannot tell — and the alternative to firing
        is never refreshing this session again."""
        make_registry_entry(tmp_path, age=timedelta(hours=6))

        assert cp.staleness_trigger(
            tmp_path, "s1", {"last_checkpoint_ts": "not a date"}, CFG
        ) == "stale"


class TestUnknownSessionAge:
    """A session of unknown age is not evidence of a stale one, so the trigger
    declines and today's band-only behaviour stands."""

    def test_no_registry_entry_does_not_fire(self, tmp_path):
        assert cp.staleness_trigger(tmp_path, "s1", {}, CFG) is None

    def test_an_unparseable_entry_does_not_fire(self, tmp_path):
        (tmp_path / "sessions").mkdir()
        (tmp_path / "sessions" / "s1.json").write_text("{not json")

        assert cp.staleness_trigger(tmp_path, "s1", {}, CFG) is None

    def test_a_missing_started_at_does_not_fire(self, tmp_path):
        (tmp_path / "sessions").mkdir()
        (tmp_path / "sessions" / "s1.json").write_text(json.dumps({"cwd": "/work"}))

        assert cp.staleness_trigger(tmp_path, "s1", {}, CFG) is None

    def test_a_naive_started_at_is_read_as_utc(self, tmp_path):
        (tmp_path / "sessions").mkdir()
        naive = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(tzinfo=None)
        (tmp_path / "sessions" / "s1.json").write_text(
            json.dumps({"started_at": naive.isoformat()})
        )

        assert cp.staleness_trigger(tmp_path, "s1", {}, CFG) == "stale"

    def test_it_never_raises(self, tmp_path):
        assert cp.staleness_trigger(tmp_path / "gone", "s1", {}, CFG) is None


class TestDisabling:

    def test_stale_hours_zero_disables_it(self, tmp_path):
        make_registry_entry(tmp_path, age=timedelta(days=3))
        cfg = cp.CheckpointConfig(stale_hours=0)

        assert cp.staleness_trigger(tmp_path, "s1", {}, cfg) is None

    def test_checkpointing_disabled_disables_it(self, tmp_path):
        make_registry_entry(tmp_path, age=timedelta(days=3))
        cfg = cp.CheckpointConfig(enabled=False)

        assert cp.staleness_trigger(tmp_path, "s1", {}, cfg) is None


# ---------------------------------------------------------------------------
# Criterion 12 — a NEW config field, and ttl_hours untouched
# ---------------------------------------------------------------------------

class TestConfig:

    def test_the_defaults_favour_recoverability_over_write_volume(self):
        """0.5, not 3.0 — deliberately about two writes an hour rather than
        well under one. At 3.0 a session's unwritten segment reached 174,154
        characters against a healthy 23,287, which is what made the model
        call unfinishable and the failure self-sustaining."""
        cfg = cp.CheckpointConfig()

        assert cfg.stale_hours == 0.5
        assert cfg.min_session_minutes == 30

    def test_both_fields_have_their_own_env_knob(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_CHECKPOINT_STALE_HOURS", "1.5")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_CHECKPOINT_MIN_SESSION_MINUTES", "10")

        cfg = cp.load_config()

        assert cfg.stale_hours == 1.5
        assert cfg.min_session_minutes == 10

    def test_a_malformed_value_falls_back_rather_than_crashing(self, monkeypatch):
        """A config problem must never crash a hook — the existing
        `load_config` contract."""
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_CHECKPOINT_STALE_HOURS", "soon")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_CHECKPOINT_MIN_SESSION_MINUTES", "a lot")

        cfg = cp.load_config()

        assert cfg.stale_hours == 0.5
        assert cfg.min_session_minutes == 30

    def test_a_negative_value_is_clamped_not_inverted(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_CHECKPOINT_STALE_HOURS", "-5")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_CHECKPOINT_MIN_SESSION_MINUTES", "-1")

        cfg = cp.load_config()

        assert cfg.stale_hours == 0.0        # i.e. disabled, never "always"
        assert cfg.min_session_minutes == 0


class TestTtlHoursUntouched:
    """`ttl_hours` means pending-marker expiry. Reusing it for staleness would
    have been the smaller diff and would have broken rebuild expiry silently."""

    def test_the_stale_knob_does_not_move_ttl_hours(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_CHECKPOINT_STALE_HOURS", "1")

        assert cp.load_config().ttl_hours == 6.0

    def test_the_ttl_knob_does_not_move_stale_hours(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_CHECKPOINT_TTL_HOURS", "1")

        cfg = cp.load_config()
        assert cfg.ttl_hours == 1.0
        assert cfg.stale_hours == 0.5

    def test_marker_expiry_still_uses_ttl_hours(self, tmp_path):
        """The consumer that would have broken, exercised directly. 7h old
        against ttl_hours=6 is expired; against stale_hours=3 it would be
        doubly so — which is exactly why the two must not be one field."""
        _write_marker(tmp_path, age=timedelta(hours=7))

        assert cp.consume_pending_marker(tmp_path, "/work", "s2", CFG) is None

    def test_a_fresh_marker_still_survives(self, tmp_path):
        """The half that a shared field would have silently broken: 4h old is
        past stale_hours (3) but well inside ttl_hours (6)."""
        _write_marker(tmp_path, age=timedelta(hours=4))

        payload = cp.consume_pending_marker(tmp_path, "/work", "s2", CFG)

        assert payload is not None and payload["session_id"] == "s1"


# ---------------------------------------------------------------------------
# Criterion 12 — the call site
# ---------------------------------------------------------------------------

class TestSessionStopWiring:

    def test_session_stop_calls_it(self):
        source = (SCRIPTS_DIR / "session_stop.py").read_text()

        assert "cp.staleness_trigger(" in source

    def test_it_is_a_fallback_not_a_replacement(self):
        """The band triggers still win — they carry the token count that
        decides handoff, and `checkpoint_trigger` must be consulted first."""
        source = (SCRIPTS_DIR / "session_stop.py").read_text()

        assert "reason = reason or cp.staleness_trigger(" in source
        assert source.index("cp.checkpoint_trigger(") < source.index("cp.staleness_trigger(")

    def test_the_single_flight_guard_still_applies(self):
        """A stale trigger must not be able to spawn a second writer."""
        source = (SCRIPTS_DIR / "session_stop.py").read_text()
        spawn = source.split("cp.staleness_trigger(", 1)[1]

        assert "if reason and not cp.writer_inflight(" in spawn


class TestCheckpointPassExecuted:
    """The wiring, executed. The textual pins above catch reorderings; these
    run `_checkpoint_pass` for real (writer stubbed) and catch behavioural
    breaks the string match cannot — a stale fire that never reaches the
    writer, or a gate that silently swallows the stale path."""

    def _transcript(self, tmp_path, tokens=40_000):
        """A main-chain assistant record putting the session at *tokens* —
        below every band, so only the stale trigger can fire."""
        t = tmp_path / "transcript.jsonl"
        t.write_text(json.dumps({
            "type": "assistant",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": 1_000,
                    "cache_read_input_tokens": tokens - 1_000,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 50,
                },
            },
        }) + "\n")
        return t

    def _run(self, tmp_path, monkeypatch, transcript):
        """Execute _checkpoint_pass with the writer stubbed; return payloads."""
        monkeypatch.delenv("_HOOK_CHILD_SESSION", raising=False)
        payloads = []
        monkeypatch.setattr(
            session_stop, "_spawn_writer", lambda p: payloads.append(p) or True
        )
        session_stop._checkpoint_pass(
            {
                "session_id": "s1",
                "transcript_path": str(transcript),
                "cwd": str(tmp_path),
            },
            tmp_path,
        )
        return payloads

    def test_a_stale_session_fires_and_reason_reaches_the_writer(
        self, tmp_path, monkeypatch
    ):
        make_registry_entry(tmp_path, age=timedelta(hours=2))

        payloads = self._run(tmp_path, monkeypatch, self._transcript(tmp_path))

        assert len(payloads) == 1
        assert payloads[0]["reason"] == "stale"
        assert payloads[0]["session_id"] == "s1"
        assert payloads[0]["tokens"] == 40_000

    def test_a_young_session_below_every_band_spawns_nothing(
        self, tmp_path, monkeypatch
    ):
        make_registry_entry(tmp_path, age=timedelta(minutes=5))

        payloads = self._run(tmp_path, monkeypatch, self._transcript(tmp_path))

        assert payloads == []

    def test_an_unknown_token_count_blocks_the_stale_path_too(
        self, tmp_path, monkeypatch
    ):
        """The `tokens <= 0` gate sits before the staleness check, so a
        session whose transcript carries no readable usage records gets no
        age checkpoint either — unknown size declines to fire, same
        philosophy as unknown age. Behavioural pin of the documented gate."""
        make_registry_entry(tmp_path, age=timedelta(hours=2))
        empty = tmp_path / "transcript.jsonl"
        empty.write_text("")  # exists, but no usage records → tokens == 0

        payloads = self._run(tmp_path, monkeypatch, empty)

        assert payloads == []

    def test_an_inflight_writer_blocks_a_stale_spawn(self, tmp_path, monkeypatch):
        """The single-flight guard, executed rather than string-matched."""
        make_registry_entry(tmp_path, age=timedelta(hours=2))
        cp.claim_writer(tmp_path, "s1")

        payloads = self._run(tmp_path, monkeypatch, self._transcript(tmp_path))

        assert payloads == []


class TestRegistryGcAgeReset:
    """Registry GC resets the age anchor — pinned, documented behaviour.

    `gc_stale` collects a non-parked entry after `GC_LIVE_AFTER_DAYS` (30) of
    silence; on resume `record_event` (which runs before the checkpoint pass)
    recreates it with `started_at = now`, so the longest-dormant tabs read as
    brand-new and skip their age checkpoint until `min_session_minutes` into
    the resumed work. Accepted degradation — see `_session_started_at`. If a
    GC exemption ever changes this, these pins are the ones to flip."""

    def test_a_gcd_entry_recreated_on_resume_reads_as_brand_new(self, tmp_path):
        make_registry_entry(tmp_path, age=timedelta(days=40))
        assert sr.gc_stale(tmp_path) == 1  # dormant > GC_LIVE_AFTER_DAYS

        # The user returns: Stop's record_event runs first and recreates
        # the entry with started_at = now...
        assert sr.record_event(tmp_path, {"session_id": "s1", "cwd": "/work"}, "stop")

        # ...so the 40-day-dormant session declines to fire.
        assert cp.staleness_trigger(tmp_path, "s1", {}, CFG) is None

    def test_a_parked_entry_survives_gc_and_keeps_its_age(self, tmp_path):
        """The common way a tab goes dormant that long is parked — and parked
        entries are GC-exempt, so their age anchor survives and the trigger
        fires on resume."""
        make_registry_entry(tmp_path, age=timedelta(days=40))
        assert sr.record_disposition(tmp_path, "s1", "parked", "long idea")

        assert sr.gc_stale(tmp_path) == 0
        assert cp.staleness_trigger(tmp_path, "s1", {}, CFG) == "stale"


# ---------------------------------------------------------------------------
# The cost the plan asked to be stated rather than assumed
# ---------------------------------------------------------------------------

class TestWriteVolume:

    def _writes_over(self, tmp_path, hours, cfg=CFG, step_minutes=10):
        """Replay a session as a series of Stop hooks, counting writes.

        Each step is one completed turn. A write updates `last_checkpoint_ts`,
        exactly as `checkpoint_writer.py` does.

        Duplication risk, accepted: this simulation re-implements the writer's
        state bookkeeping (the `last_checkpoint_ts` update on success) and
        re-anchors the registry file per step instead of injecting a clock —
        `staleness_trigger` reads `datetime.now` directly, and threading a
        clock parameter through production code for one test class isn't
        worth it. If `checkpoint_writer.py`'s state contract changes (e.g.
        it stops overwriting `last_checkpoint_ts`, or writes it at a
        different point), this cadence arithmetic diverges from reality and
        must be updated with it.
        """
        make_registry_entry(tmp_path, age=timedelta(hours=hours))
        started = datetime.now(timezone.utc) - timedelta(hours=hours)
        state: dict = {}
        writes = 0
        for minute in range(0, int(hours * 60) + 1, step_minutes):
            at = started + timedelta(minutes=minute)
            # Re-anchor the registry so "now" is the simulated moment.
            (tmp_path / "sessions" / "s1.json").write_text(json.dumps({
                "started_at": (
                    datetime.now(timezone.utc) - (at - started)
                ).isoformat(),
            }))
            shifted = {}
            if state.get("last_checkpoint_ts"):
                elapsed = at - datetime.fromisoformat(state["last_checkpoint_ts"])
                shifted["last_checkpoint_ts"] = (
                    datetime.now(timezone.utc) - elapsed
                ).isoformat()
            if cp.staleness_trigger(tmp_path, "s1", shifted, cfg) == "stale":
                writes += 1
                state["last_checkpoint_ts"] = at.isoformat()
        return writes

    def test_a_four_hour_session_writes_eight_small_deltas(self, tmp_path):
        """The point of the whole change, as arithmetic: eight 30-minute
        slices instead of two multi-hour ones. Each write distills only the
        segment since the last one, so eight writes is eight SMALL writes."""
        assert self._writes_over(tmp_path, hours=4) == 8

    def test_a_one_hour_session_writes_twice(self, tmp_path):
        assert self._writes_over(tmp_path, hours=1) == 2

    def test_a_twenty_minute_session_writes_nothing(self, tmp_path):
        assert self._writes_over(tmp_path, hours=1 / 3) == 0

    def test_the_old_conservative_cadence_is_still_configurable(self, tmp_path):
        """`checkpoint_stale_hours` still buys back the pre-0.33.0 volume for
        anyone who wants it — only the default moved."""
        cfg = cp.CheckpointConfig(stale_hours=3.0, min_session_minutes=30)

        assert self._writes_over(tmp_path, hours=4, cfg=cfg) == 2
