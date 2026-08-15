"""lib/thinking.py — shared thinking-config resolution for mechanical calls.

The contract every mechanical call site depends on: extended thinking is
disabled by default; a truthy option value restores the SDK default; and a
dependency that cannot carry the keyword also resolves to "send nothing" — with
exactly one warning per target naming the fix.

The load-bearing assertion in this file is :class:`TestThinkingKwargs`. The
"send nothing" cases must produce a call with **no** ``thinking`` keyword at
all, not ``thinking=None``: an old core rejects the keyword *name*, whatever its
value. That decision lives in exactly one function so it can be tested in
exactly one place; the per-site tests only check that each subsystem routes
through it.
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
OPTION = th.DUPLICATION_THINKING_OPTION
ENV_VAR = option_var(OPTION)


@pytest.fixture(autouse=True)
def _isolate_probe_cache(monkeypatch):
    """`_SUPPORT_CACHE` is module-global and answers for the whole process.

    Reset it around every test here so one test's stubbed probe can never be
    inherited by another — which would make the outcome depend on collection
    order, and silently make the warn-once assertions vacuous.
    """
    monkeypatch.setattr(th, "_SUPPORT_CACHE", {})


@pytest.fixture
def supported(monkeypatch):
    """Pretend both dependency boundaries can carry ``thinking=``."""
    monkeypatch.setattr(th, "core_supports_thinking", lambda target=th.QUERY: True)


@pytest.fixture
def unsupported(monkeypatch):
    monkeypatch.setattr(th, "core_supports_thinking", lambda target=th.QUERY: False)


class TestDefaults:
    def test_default_is_disabled(self, monkeypatch, supported):
        monkeypatch.delenv(ENV_VAR, raising=False)
        assert th.resolve_thinking(OPTION) == {"type": "disabled"}

    def test_unrecognised_value_stays_disabled(self, monkeypatch, supported, caplog):
        """A typo must not read as "on". core's option_bool warns and falls
        back to the default, which is what this plugin wants: thinking off."""
        monkeypatch.setenv(ENV_VAR, "maybe")
        with caplog.at_level(logging.WARNING):
            assert th.resolve_thinking(OPTION) == th.THINKING_DISABLED
        assert any("Malformed plugin option" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("value", ["0", "false", "no", "off"])
    def test_explicit_false_stays_disabled(self, monkeypatch, supported, value):
        monkeypatch.setenv(ENV_VAR, value)
        assert th.resolve_thinking(OPTION) == th.THINKING_DISABLED


class TestOptBack:
    @pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
    def test_truthy_value_restores_sdk_default(self, monkeypatch, supported, value):
        monkeypatch.setenv(ENV_VAR, value)
        assert th.resolve_thinking(OPTION) is None

    @pytest.mark.parametrize("value", ["TRUE", " on ", "Yes"])
    def test_case_and_whitespace_insensitive(self, monkeypatch, supported, value):
        monkeypatch.setenv(ENV_VAR, value)
        assert th.resolve_thinking(OPTION) is None


class TestThinkingKwargs:
    """The one place the omit-or-send decision is made, so the one place it is
    worth asserting. Mirrors test_memory_router.py's pair of assertions."""

    def test_sends_the_keyword_when_enabled_and_supported(
        self, monkeypatch, supported
    ):
        monkeypatch.delenv(ENV_VAR, raising=False)
        assert th.thinking_kwargs(OPTION) == {"thinking": {"type": "disabled"}}

    def test_omits_the_keyword_entirely_on_opt_back(self, monkeypatch, supported):
        """`thinking=None` would not do — the keyword must not be sent."""
        monkeypatch.setenv(ENV_VAR, "true")
        assert th.thinking_kwargs(OPTION) == {}
        assert "thinking" not in th.thinking_kwargs(OPTION)

    def test_omits_the_keyword_entirely_when_unsupported(
        self, monkeypatch, unsupported
    ):
        """An old core rejects the *name*, whatever its value, so a dependency
        that cannot carry it must be handed nothing at all."""
        monkeypatch.delenv(ENV_VAR, raising=False)
        assert th.thinking_kwargs(OPTION) == {}
        assert "thinking" not in th.thinking_kwargs(OPTION)

    def test_each_call_gets_its_own_dict(self, monkeypatch, supported):
        """No two model calls may share one mutable config on its way to the
        SDK — and none of them may alias the module constant."""
        monkeypatch.delenv(ENV_VAR, raising=False)
        first = th.thinking_kwargs(OPTION)["thinking"]
        second = th.thinking_kwargs(OPTION)["thinking"]
        assert first == second == {"type": "disabled"}
        assert first is not second
        assert first is not th.THINKING_DISABLED

    def test_splats_into_a_call(self, monkeypatch, supported):
        """The shape call sites actually use."""
        monkeypatch.delenv(ENV_VAR, raising=False)
        captured = {}

        def fake_query(*, system, **kwargs):
            captured.update(kwargs)

        fake_query(system="s", **th.thinking_kwargs(OPTION))
        assert captured == {"thinking": {"type": "disabled"}}


class TestUnsupportedWarning:
    def test_warns_once_per_target_naming_the_boundary(self, monkeypatch, caplog):
        monkeypatch.setattr(th, "_probe", lambda target: (False, "core is old."))
        with caplog.at_level(logging.WARNING, logger="lib.thinking"):
            assert th.core_supports_thinking() is False
            assert th.core_supports_thinking() is False
        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "lib.thinking"
        ]
        assert len(warnings) == 1, "warn once per process, not per call"

    def test_the_fix_is_one_an_installed_user_can_perform(self, monkeypatch, caplog):
        """docs/degradation-contract.md rule 1: the *vanilla* fix. An installed
        plugin has no repo root and no lockfile to re-resolve."""
        monkeypatch.setattr(th, "_probe", lambda target: (False, "core is old."))
        with caplog.at_level(logging.WARNING, logger="lib.thinking"):
            th.core_supports_thinking()
        message = caplog.records[0].getMessage()
        assert "reinstall it from the marketplace" in message
        assert "uv lock" not in message
        assert "repo root" not in message

    def test_no_version_number_is_claimed_anywhere(self):
        """0.14.0 was never cut: the newest published core tag is v0.13.0 and
        the rev this repo pins already carries the kwarg. Name the capability,
        not a version that does not exist."""
        assert "0.14" not in th._UNSUPPORTED_FIX
        source = (SCRIPTS_DIR / "lib" / "thinking.py").read_text(encoding="utf-8")
        assert "0.14.0" not in source

    def test_supported_path_stays_quiet(self, monkeypatch, caplog):
        monkeypatch.setattr(th, "_probe", lambda target: (True, ""))
        with caplog.at_level(logging.WARNING, logger="lib.thinking"):
            assert th.core_supports_thinking() is True
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]


class TestCoreProbe:
    def test_probe_tracks_the_resolved_signatures(self):
        """Both targets, against whatever core the lockfile resolves.

        This cannot assert the core is new enough (both sides read the same
        signature) — it catches the probe drifting from reality (wrong module,
        wrong attribute, exception swallowed into a wrong default).
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
        monkeypatch.setattr(
            th, "_probe", lambda target: (calls.append(target) or (True, "")),
        )
        assert th.core_supports_thinking() is True
        assert th.core_supports_thinking() is True
        assert th.core_supports_thinking(th.RUN_AGENT) is True
        assert calls == [th.QUERY, th.RUN_AGENT]

    def test_an_unknown_target_raises(self):
        """A typo must not fall through to the ModelClient default: that would
        probe one call path and hand the keyword to another."""
        with pytest.raises(ValueError, match="unknown thinking probe target"):
            th.probe_core_thinking("run-agent")
        with pytest.raises(ValueError):
            th.core_supports_thinking("agent_runner")

    def test_a_kwargs_callable_counts_as_support(self, monkeypatch):
        """Test fakes take **kwargs, and there is no declared forwarding path
        left to probe past one."""
        def fake(prompt, **kwargs):
            return None

        monkeypatch.setitem(th._TARGETS, th.RUN_AGENT, ("run_agent", lambda: fake))
        assert th.probe_core_thinking(th.RUN_AGENT) is True

    def test_an_old_core_is_unsupported(self, monkeypatch):
        def old_run_agent(prompt, *, model=None, effort=None):
            return None

        monkeypatch.setitem(
            th._TARGETS, th.RUN_AGENT, ("run_agent", lambda: old_run_agent)
        )
        assert th.probe_core_thinking(th.RUN_AGENT) is False


class TestTheSdkBoundaryIsNotProbedHere:
    """The second boundary — `claude_agent_sdk.ClaudeAgentOptions` needing a
    `thinking` field, because core forwards the value there unguarded — is real
    and deliberately unchecked in this plugin.

    No script under `scripts/` may import the SDK (test_integration_wiring.py →
    TestNoDirectSDKImportsAnywhere: all model access goes through core), and
    core exports no capability accessor for it. Probing it here would mean
    routing around a gate rather than fixing the thing the gate is pointing at.
    These tests pin that decision so it is a choice on the record, not a
    regression someone silently "fixes" by adding the import back.
    """

    def test_this_module_does_not_import_the_sdk(self):
        source = (SCRIPTS_DIR / "lib" / "thinking.py").read_text(encoding="utf-8")
        assert "import claude_agent_sdk" not in source
        assert "from claude_agent_sdk" not in source

    def test_core_still_converts_the_mismatch_to_a_typed_failure(self):
        """What makes the unchecked boundary survivable: every call site here
        sees an ordinary model-call failure it already handles, not a raw
        TypeError escaping from ClaudeAgentOptions."""
        from multiplai_core.agent_runner import AgentRunError
        from multiplai_core.model_client import SDKQueryError

        assert issubclass(AgentRunError, Exception)
        assert issubclass(SDKQueryError, Exception)


class TestOptionRegistry:
    def test_every_option_key_is_declared_with_a_false_default(self):
        declared = json.loads(
            (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text()
        )["userConfig"]
        keys = [
            value for name, value in vars(th).items()
            if name.endswith("_THINKING_OPTION")
        ]
        assert len(keys) == 6, f"expected six subsystem options, found {keys}"
        for key in keys:
            assert key in declared, f"{key} missing from plugin.json userConfig"
            assert declared[key]["default"] is False
            assert declared[key]["type"] == "boolean"

    def test_the_retired_doctor_option_is_gone(self):
        """`doctor_thinking` moved only one of the doctor's two passes, which
        is a trap. It was replaced by `duplication_thinking` before release, so
        nothing should still name it."""
        declared = json.loads(
            (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text()
        )["userConfig"]
        assert "doctor_thinking" not in declared
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        assert "doctor_thinking" not in readme
