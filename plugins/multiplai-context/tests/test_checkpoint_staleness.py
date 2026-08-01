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

On cost: the plan's stop-and-ask gate says to stop if the defaults would
plausibly exceed a few writes per hour per session, because extraction shares
one rate limit with interactive work. At 3 hours stale / 30 minutes minimum
age, a 4-hour session writes twice. `TestWriteVolume` asserts that arithmetic
rather than leaving it in a comment.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import checkpoint as cp

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
            tmp_path, "s1", state_with_checkpoint(timedelta(hours=1)), CFG
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

    def test_the_defaults_are_conservative(self):
        cfg = cp.CheckpointConfig()

        assert cfg.stale_hours == 3.0
        assert cfg.min_session_minutes == 30

    def test_both_fields_have_their_own_env_knob(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_stale_hours", "1.5")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_min_session_minutes", "10")

        cfg = cp.load_config()

        assert cfg.stale_hours == 1.5
        assert cfg.min_session_minutes == 10

    def test_a_malformed_value_falls_back_rather_than_crashing(self, monkeypatch):
        """A config problem must never crash a hook — the existing
        `load_config` contract."""
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_stale_hours", "soon")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_min_session_minutes", "a lot")

        cfg = cp.load_config()

        assert cfg.stale_hours == 3.0
        assert cfg.min_session_minutes == 30

    def test_a_negative_value_is_clamped_not_inverted(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_stale_hours", "-5")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_min_session_minutes", "-1")

        cfg = cp.load_config()

        assert cfg.stale_hours == 0.0        # i.e. disabled, never "always"
        assert cfg.min_session_minutes == 0


class TestTtlHoursUntouched:
    """`ttl_hours` means pending-marker expiry. Reusing it for staleness would
    have been the smaller diff and would have broken rebuild expiry silently."""

    def test_the_stale_knob_does_not_move_ttl_hours(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_stale_hours", "1")

        assert cp.load_config().ttl_hours == 6.0

    def test_the_ttl_knob_does_not_move_stale_hours(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_checkpoint_ttl_hours", "1")

        cfg = cp.load_config()
        assert cfg.ttl_hours == 1.0
        assert cfg.stale_hours == 3.0

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


# ---------------------------------------------------------------------------
# The cost the plan asked to be stated rather than assumed
# ---------------------------------------------------------------------------

class TestWriteVolume:

    def _writes_over(self, tmp_path, hours, cfg=CFG, step_minutes=10):
        """Replay a session as a series of Stop hooks, counting writes.

        Each step is one completed turn. A write updates `last_checkpoint_ts`,
        exactly as `checkpoint_writer.py` does.
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

    def test_a_four_hour_session_writes_twice(self, tmp_path):
        """The plan's stop-and-ask gate: more than a few writes per hour per
        session and this would need discussing before merge. Two writes over
        four hours is 0.5/hour."""
        assert self._writes_over(tmp_path, hours=4) == 2

    def test_a_one_hour_session_writes_once(self, tmp_path):
        assert self._writes_over(tmp_path, hours=1) == 1

    def test_a_twenty_minute_session_writes_nothing(self, tmp_path):
        assert self._writes_over(tmp_path, hours=1 / 3) == 0

    def test_an_aggressive_config_is_possible_but_not_the_default(self, tmp_path):
        cfg = cp.CheckpointConfig(stale_hours=0.5, min_session_minutes=10)

        assert self._writes_over(tmp_path, hours=4, cfg=cfg) > 2
