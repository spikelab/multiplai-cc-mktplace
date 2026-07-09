"""Tests for the detached cost-collection launcher at SessionStart.

``_launch_cost_collection`` fires the collector only when ``enable_costs`` is
set. It must be a strict no-op otherwise (cost accounting is opt-in), and it
must never raise — a failed launch can't be allowed to break session start.
"""

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


class _PopenSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return object()  # stand-in for a Popen handle; never awaited


class TestLaunchCostCollection:
    def test_no_launch_when_disabled(self, monkeypatch):
        """enable_costs unset → no subprocess, returns False."""
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_enable_costs", raising=False)
        import session_start

        spy = _PopenSpy()
        monkeypatch.setattr(session_start.subprocess, "Popen", spy)
        assert session_start._launch_cost_collection(SCRIPTS_DIR) is False
        assert spy.calls == []

    def test_launches_when_enabled(self, monkeypatch):
        """enable_costs=true → detached collector launched, returns True."""
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_enable_costs", "true")
        import session_start

        spy = _PopenSpy()
        monkeypatch.setattr(session_start.subprocess, "Popen", spy)
        assert session_start._launch_cost_collection(SCRIPTS_DIR) is True
        assert len(spy.calls) == 1
        args, kwargs = spy.calls[0]
        assert str(SCRIPTS_DIR / "collect_costs.py") in args
        # Must be detached so a minutes-long first backfill can't block the hook.
        assert kwargs.get("start_new_session") is True

    def test_missing_script_no_launch(self, monkeypatch, tmp_path):
        """enable_costs=true but no collect_costs.py present → no launch, no raise."""
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_enable_costs", "true")
        import session_start

        spy = _PopenSpy()
        monkeypatch.setattr(session_start.subprocess, "Popen", spy)
        assert session_start._launch_cost_collection(tmp_path) is False
        assert spy.calls == []

    def test_launch_failure_is_swallowed(self, monkeypatch):
        """A Popen that raises must not propagate — returns False."""
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_enable_costs", "true")
        import session_start

        def _boom(*a, **k):
            raise OSError("no exec for you")

        monkeypatch.setattr(session_start.subprocess, "Popen", _boom)
        assert session_start._launch_cost_collection(SCRIPTS_DIR) is False
