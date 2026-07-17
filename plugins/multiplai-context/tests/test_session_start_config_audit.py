"""Tests for the 90-day config-audit gate at SessionStart.

``_config_audit_gate_open`` semantics:

* **Missing state file** (fresh install): gate CLOSED — the file is seeded
  with ``last_run: now`` so the 90-day clock starts at install; nudging
  "the audit is due" with no record would be false. Seed failures are
  swallowed (still closed, retried next session).
* **Existing but unusable state** (corrupt YAML, missing/garbage
  ``last_run``): gate OPEN — fail-open recovery, mirroring the dream gate;
  a record existed and was lost, so the user re-stamps by running the skill.
* **Parseable ``last_run``**: open iff >=90 days old.

State is stamped deterministically by ``scripts/config_audit.py --stamp``
(invoked by the ``/multiplai-context:config-audit`` skill), which resolves
the data dir with the same ``get_paths()`` cascade the gate uses.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _write_state(path: Path, last_run: str) -> None:
    path.write_text(f'last_run: "{last_run}"\n', encoding="utf-8")


class TestConfigAuditGate:
    def test_gate_is_90_days(self):
        """The cadence constant is 90 days per the config-audit design."""
        import session_start

        assert session_start._CONFIG_AUDIT_GATE_DAYS == 90

    def test_missing_state_seeds_and_closes_gate(self, tmp_path):
        """No state file (fresh install) → gate closed, clock seeded to now.

        The day-one dampener: a fresh install has no record to be stale,
        so it must NOT be nudged; the seed starts the 90-day clock.
        """
        import session_start

        missing = tmp_path / "config_audit_state.yaml"
        assert session_start._config_audit_gate_open(missing) is False
        assert missing.is_file(), "gate must seed the state file on first run"

        import yaml

        state = yaml.safe_load(missing.read_text())
        seeded = datetime.fromisoformat(state["last_run"])
        assert datetime.now(timezone.utc) - seeded < timedelta(minutes=1)

    def test_seeded_state_keeps_gate_closed_on_next_check(self, tmp_path):
        """The seed written on first run parses and closes the gate after."""
        import session_start

        f = tmp_path / "config_audit_state.yaml"
        session_start._config_audit_gate_open(f)  # seeds
        assert session_start._config_audit_gate_open(f) is False

    def test_seed_write_failure_still_closes_gate(self, tmp_path):
        """Unwritable seed location → no raise, gate still closed.

        A filesystem hiccup must not turn into a false "audit due" nudge;
        seeding simply retries at the next session start.
        """
        import session_start

        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        f = blocker / "config_audit_state.yaml"  # parent is a file → write fails
        assert session_start._config_audit_gate_open(f) is False
        assert not f.exists()

    def test_empty_state_opens_gate(self, tmp_path):
        """State file exists but has no last_run key → gate open (recovery)."""
        import session_start

        f = tmp_path / "config_audit_state.yaml"
        f.write_text("proposal: something.md\n", encoding="utf-8")
        assert session_start._config_audit_gate_open(f) is True

    def test_stale_state_opens_gate(self, tmp_path):
        """last_run older than 90 days → gate open."""
        import session_start

        f = tmp_path / "config_audit_state.yaml"
        stale = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
        _write_state(f, stale)
        assert session_start._config_audit_gate_open(f) is True

    def test_exactly_90_days_opens_gate(self, tmp_path, monkeypatch):
        """The gate uses >= (not >): last_run EXACTLY 90 days old opens it.

        Time is frozen inside session_start so the boundary is pinned to
        the instant — a real-clock offset would silently degrade this to
        a >90d test and never distinguish >= from >.
        """
        import session_start

        frozen = datetime.now(timezone.utc)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen if tz else frozen.replace(tzinfo=None)

        monkeypatch.setattr(session_start, "datetime", _FrozenDatetime)
        f = tmp_path / "config_audit_state.yaml"
        _write_state(f, (frozen - timedelta(days=90)).isoformat())
        assert session_start._config_audit_gate_open(f) is True

    def test_just_under_90_days_closes_gate(self, tmp_path, monkeypatch):
        """One second inside the window (frozen clock) → gate closed."""
        import session_start

        frozen = datetime.now(timezone.utc)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen if tz else frozen.replace(tzinfo=None)

        monkeypatch.setattr(session_start, "datetime", _FrozenDatetime)
        f = tmp_path / "config_audit_state.yaml"
        _write_state(
            f, (frozen - timedelta(days=90) + timedelta(seconds=1)).isoformat()
        )
        assert session_start._config_audit_gate_open(f) is False

    def test_recent_state_closes_gate(self, tmp_path):
        """last_run within the 90-day window → gate closed."""
        import session_start

        f = tmp_path / "config_audit_state.yaml"
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        _write_state(f, recent)
        assert session_start._config_audit_gate_open(f) is False

    def test_just_inside_window_closes_gate(self, tmp_path):
        """89 days ago is still inside the window → gate closed."""
        import session_start

        f = tmp_path / "config_audit_state.yaml"
        inside = (datetime.now(timezone.utc) - timedelta(days=89)).isoformat()
        _write_state(f, inside)
        assert session_start._config_audit_gate_open(f) is False

    def test_naive_timestamp_assumed_utc(self, tmp_path):
        """A tz-naive recent timestamp is treated as UTC → gate closed."""
        import session_start

        f = tmp_path / "config_audit_state.yaml"
        naive = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).replace(tzinfo=None).isoformat()
        _write_state(f, naive)
        assert session_start._config_audit_gate_open(f) is False

    def test_unparseable_timestamp_opens_gate(self, tmp_path):
        """Garbage in last_run → gate open (recovery semantics)."""
        import session_start

        f = tmp_path / "config_audit_state.yaml"
        _write_state(f, "not-a-timestamp")
        assert session_start._config_audit_gate_open(f) is True

    def test_non_string_timestamp_opens_gate(self, tmp_path):
        """A non-string last_run (e.g. int) → gate open, no raise."""
        import session_start

        f = tmp_path / "config_audit_state.yaml"
        f.write_text("last_run: 42\n", encoding="utf-8")
        assert session_start._config_audit_gate_open(f) is True

    def test_unparseable_yaml_opens_gate(self, tmp_path):
        """A file that isn't valid YAML → gate open, no raise."""
        import session_start

        f = tmp_path / "config_audit_state.yaml"
        f.write_text("last_run: [unclosed\n\t{{garbage: ::\n", encoding="utf-8")
        assert session_start._config_audit_gate_open(f) is True


class TestConfigAuditNudge:
    def test_nudge_mentions_skill_command(self, capsys):
        """The nudge must point at /multiplai-context:config-audit."""
        import session_start

        session_start._emit_config_audit_nudge()
        out = capsys.readouterr().out
        assert "/multiplai-context:config-audit" in out
        assert "SYSTEM NUDGE" in out


class TestConfigAuditStamp:
    """scripts/config_audit.py --stamp writes the exact file the gate reads.

    The stamp resolves the data dir via ``get_paths()`` (same env cascade
    as the gate) instead of the model hand-locating a directory — the
    deterministic fix for installs where the data dir comes from
    CLAUDE_PLUGIN_DATA or CLAUDE_PLUGIN_OPTION_data_dir.
    """

    @pytest.fixture(autouse=True)
    def _anchored_data_dir(self, monkeypatch, tmp_path, reset_paths_cache):
        """Anchor paths.data_dir() at a sandbox dir for each test."""
        self.data_dir = (tmp_path / "plugin_data").resolve()
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(self.data_dir))

    def test_stamp_writes_to_data_dir(self):
        """The state file lands at paths.data_dir()/config_audit_state.yaml."""
        import config_audit

        path = config_audit.stamp()
        assert path == self.data_dir / "config_audit_state.yaml"
        assert path.is_file()

    def test_stamp_is_parseable_by_the_gate(self):
        """A fresh stamp closes the SessionStart gate."""
        import config_audit
        import session_start

        path = config_audit.stamp()
        assert session_start._config_audit_gate_open(path) is False

    def test_stamp_records_proposal_and_last_run(self):
        import yaml

        import config_audit

        proposal = ".multiplai/dreams/config-audit-2026-07-14.md"
        path = config_audit.stamp(proposal)
        state = yaml.safe_load(path.read_text())
        assert state["proposal"] == proposal
        last_dt = datetime.fromisoformat(state["last_run"])
        assert datetime.now(timezone.utc) - last_dt < timedelta(minutes=1)

    def test_stamp_overwrites_fresh_not_merge(self):
        """Only the latest run matters — a stale proposal key must not survive."""
        import yaml

        import config_audit

        config_audit.stamp("old-proposal.md")
        path = config_audit.stamp()  # clean audit, no proposal
        state = yaml.safe_load(path.read_text())
        assert "proposal" not in state
        assert "last_run" in state

    def test_stamp_is_atomic_no_tmp_leftover(self):
        """save_yaml's tmp+os.replace must leave no .tmp beside the state."""
        import config_audit

        path = config_audit.stamp()
        assert list(path.parent.glob("*.tmp")) == []

    def test_cli_requires_stamp_flag(self, monkeypatch, capsys):
        """Bare invocation is an error, not a silent no-op."""
        import config_audit

        monkeypatch.setattr(sys, "argv", ["config_audit.py"])
        with pytest.raises(SystemExit) as exc:
            config_audit.main()
        assert exc.value.code != 0

    def test_cli_stamp_writes_state(self, monkeypatch, capsys):
        import config_audit

        monkeypatch.setattr(
            sys,
            "argv",
            ["config_audit.py", "--stamp", "--proposal", "p.md"],
        )
        config_audit.main()
        out = capsys.readouterr().out
        assert "Stamped config_audit_state" in out
        assert (self.data_dir / "config_audit_state.yaml").is_file()
