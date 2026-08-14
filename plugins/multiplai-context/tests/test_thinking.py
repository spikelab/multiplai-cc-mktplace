"""lib/thinking.py — shared thinking-config resolution for mechanical calls.

The contract every mechanical call site depends on: extended thinking is
disabled by default; a truthy option value restores the SDK default (as
``None`` — "send no config"); and an unsupported core also resolves to
``None`` — the "omit the keyword entirely" signal — with exactly one warning
naming the fix. The per-site wiring (that each subsystem's model call actually
receives the argument) is asserted in that subsystem's own test file.
"""

import json
import logging
import sys
from pathlib import Path

import pytest

from multiplai_core.plugin_options import option_var

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import lib.thinking as th

# Any of the six option keys exercises the shared code path; the per-key
# wiring lives at the call sites, not here.
OPTION = th.DOCTOR_THINKING_OPTION
ENV_VAR = option_var(OPTION)


@pytest.fixture
def supported(monkeypatch):
    """Pretend the resolved core accepts ``thinking=`` on both paths."""
    monkeypatch.setattr(th, "core_supports_thinking", lambda target=th.QUERY: True)


class TestDefaults:
    def test_default_is_disabled(self, monkeypatch, supported):
        monkeypatch.delenv(ENV_VAR, raising=False)
        assert th.resolve_thinking(OPTION) == {"type": "disabled"}

    def test_default_is_the_shared_constant(self, monkeypatch, supported):
        monkeypatch.delenv(ENV_VAR, raising=False)
        assert th.resolve_thinking(OPTION) is th.THINKING_DISABLED

    def test_unrecognised_value_stays_disabled(self, monkeypatch, supported):
        monkeypatch.setenv(ENV_VAR, "maybe")
        assert th.resolve_thinking(OPTION) == th.THINKING_DISABLED


class TestOptBack:
    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "enabled"])
    def test_truthy_value_restores_sdk_default(self, monkeypatch, supported, value):
        monkeypatch.setenv(ENV_VAR, value)
        assert th.resolve_thinking(OPTION) is None

    @pytest.mark.parametrize("value", ["TRUE", " on ", "Enabled"])
    def test_case_and_whitespace_insensitive(self, monkeypatch, supported, value):
        monkeypatch.setenv(ENV_VAR, value)
        assert th.resolve_thinking(OPTION) is None


class TestUnsupportedCore:
    @pytest.fixture
    def unsupported(self, monkeypatch):
        monkeypatch.setattr(
            th, "core_supports_thinking", lambda target=th.QUERY: False
        )
        monkeypatch.setattr(th, "_WARNED_TARGETS", set())

    def test_yields_none_and_exactly_one_warning(
        self, monkeypatch, unsupported, caplog
    ):
        monkeypatch.delenv(ENV_VAR, raising=False)
        with caplog.at_level(logging.WARNING, logger="lib.thinking"):
            assert th.resolve_thinking(OPTION) is None
            assert th.resolve_thinking(OPTION) is None
        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "lib.thinking"
        ]
        assert len(warnings) == 1, "warn once per process, not per call"
        assert "uv lock --upgrade-package multiplai-core" in warnings[0].getMessage()

    def test_each_target_warns_once_naming_its_own_path(
        self, monkeypatch, unsupported, caplog
    ):
        monkeypatch.delenv(ENV_VAR, raising=False)
        with caplog.at_level(logging.WARNING, logger="lib.thinking"):
            th.resolve_thinking(OPTION)
            th.resolve_thinking(OPTION, target=th.RUN_AGENT)
            th.resolve_thinking(OPTION, target=th.RUN_AGENT)
        messages = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(messages) == 2
        assert any("ModelClient.query" in m for m in messages)
        assert any("run_agent" in m for m in messages)

    def test_opt_back_on_an_old_core_stays_quiet(
        self, monkeypatch, unsupported, caplog
    ):
        """A user who asked for thinking on an old core gets it (the SDK
        default) — there is nothing to warn about."""
        monkeypatch.setenv(ENV_VAR, "true")
        with caplog.at_level(logging.WARNING, logger="lib.thinking"):
            assert th.resolve_thinking(OPTION) is None
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]


class TestCoreProbe:
    def test_probe_tracks_the_resolved_signatures(self):
        """Both targets, against whatever core the lockfile resolves.

        Same contract as the router's probe test: this cannot assert the core
        is new enough (both sides read the same signature) — it catches the
        probe drifting from reality (wrong module, wrong attribute, exception
        swallowed into a wrong default).
        """
        import inspect
        from multiplai_core.agent_runner import run_agent
        from multiplai_core.model_client import ModelClient

        assert th.probe_core_thinking(th.QUERY) is (
            "thinking" in inspect.signature(ModelClient.query).parameters
        )
        assert th.probe_core_thinking(th.RUN_AGENT) is (
            "thinking" in inspect.signature(run_agent).parameters
        )

    def test_cache_probes_each_target_once(self, monkeypatch):
        calls = []
        monkeypatch.setattr(th, "_SUPPORT_CACHE", {})
        monkeypatch.setattr(
            th, "probe_core_thinking",
            lambda target=th.QUERY: calls.append(target) or True,
        )
        assert th.core_supports_thinking() is True
        assert th.core_supports_thinking() is True
        assert th.core_supports_thinking(th.RUN_AGENT) is True
        assert calls == [th.QUERY, th.RUN_AGENT]


class TestOptionRegistry:
    def test_all_six_options_are_declared_with_a_false_default(self):
        declared = json.loads(
            (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text()
        )["userConfig"]
        for key in (
            th.UTILISATION_THINKING_OPTION,
            th.NOW_THINKING_OPTION,
            th.DOCTOR_THINKING_OPTION,
            th.EXTRACTION_THINKING_OPTION,
            th.CHECKPOINT_THINKING_OPTION,
            th.CATALOG_THINKING_OPTION,
        ):
            assert key in declared, f"{key} missing from plugin.json userConfig"
            assert declared[key]["default"] is False
