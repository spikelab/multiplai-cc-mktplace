"""Tests for the 90-day config-audit gate at SessionStart.

``_config_audit_gate_open`` mirrors ``_dream_gate_open``: the gate is open
(nudge fires) when the state file is missing, stale (>90 days), or
unparseable; it is closed only when a parseable ``last_run`` timestamp is
within the 90-day window. State is stamped by the
``/multiplai-context:config-audit`` skill.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

    def test_missing_state_opens_gate(self, tmp_path):
        """No state file (first run) → gate open."""
        import session_start

        missing = tmp_path / "config_audit_state.yaml"
        assert session_start._config_audit_gate_open(missing) is True

    def test_empty_state_opens_gate(self, tmp_path):
        """State file with no last_run key → gate open."""
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

    def test_exactly_90_days_opens_gate(self, tmp_path):
        """The gate uses >=, matching the dream gate's semantics."""
        import session_start

        f = tmp_path / "config_audit_state.yaml"
        boundary = (
            datetime.now(timezone.utc) - timedelta(days=90, minutes=1)
        ).isoformat()
        _write_state(f, boundary)
        assert session_start._config_audit_gate_open(f) is True

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
