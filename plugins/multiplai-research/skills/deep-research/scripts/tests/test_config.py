"""Tests for per-node model/effort tier mappings and CLI overrides."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from research_pipeline import config as config_module
from research_pipeline.__main__ import build_parser
from research_pipeline.config import (
    DEFAULT_MODEL,
    _node_effort,
    _node_thinking,
    conf_effort,
    conf_thinking,
    PARSE_MODEL,
    PRESETS,
    ResearchConfig,
    THINKING_DISABLED,
)


@pytest.fixture(autouse=True)
def _forget_thinking_warnings():
    """conf_thinking dedupes its warnings in a module-level set, so a test
    asserting on that warning must not inherit another test's suppression."""
    config_module._warned_thinking_values.clear()
    yield
    config_module._warned_thinking_values.clear()


def _mk_config(tmp_path: Path) -> ResearchConfig:
    return ResearchConfig(
        query="q", output_dir=tmp_path, preset=PRESETS["quick"], date="2026-07-20"
    )


class TestModelTiers:
    def test_mechanical_nodes_on_parse_tier(self, tmp_path: Path) -> None:
        config = _mk_config(tmp_path)
        for node in ("search", "triage_relevance", "extract", "verify", "quality_check"):
            assert config.models[node] == PARSE_MODEL, node

    def test_reasoning_nodes_on_default_tier(self, tmp_path: Path) -> None:
        config = _mk_config(tmp_path)
        for node in ("plan", "diverge", "challenge", "reassess", "synthesize", "adversarial"):
            assert config.models[node] == DEFAULT_MODEL, node


class TestEffortTiers:
    def test_default_efforts(self, tmp_path: Path) -> None:
        config = _mk_config(tmp_path)
        assert config.efforts["extract"] == "low"
        assert config.efforts["search"] == "low"
        assert config.efforts["triage_relevance"] == "low"
        assert config.efforts["verify"] == "low"
        assert config.efforts["quality_check"] == "medium"
        assert config.efforts["synthesize"] is None
        assert config.efforts["plan"] is None
        assert config.efforts["adversarial"] is None

    def test_efforts_and_models_cover_same_nodes(self, tmp_path: Path) -> None:
        config = _mk_config(tmp_path)
        assert set(config.efforts) == set(config.models)

    def test_cli_effort_overrides_all_nodes(self, tmp_path: Path) -> None:
        args = build_parser().parse_args(
            ["--query", "q", "--output", str(tmp_path), "--effort", "high"]
        )
        config = ResearchConfig.from_cli_args(args)
        assert all(e == "high" for e in config.efforts.values())

    def test_no_cli_effort_keeps_per_node_defaults(self, tmp_path: Path) -> None:
        args = build_parser().parse_args(["--query", "q", "--output", str(tmp_path)])
        config = ResearchConfig.from_cli_args(args)
        assert config.efforts["extract"] == "low"
        assert config.efforts["synthesize"] is None


# --- Conf-driven reasoning effort ------------------------------------------


def _write_conf(tmp_path: Path, monkeypatch, body: str) -> None:
    monkeypatch.setenv("CLAUDE_MULTIPLAI_HOME", str(tmp_path))
    monkeypatch.delenv("MULTIPLAI_EFFORT", raising=False)
    (tmp_path / "multiplai.conf").write_text(body)


class TestConfEffort:
    """Model and effort are two axes of one tuning decision; only the model
    half used to be settable without a code edit."""

    def test_absent_conf_keeps_the_code_default(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch, "")
        assert conf_effort("deep-research.extract", "low") == "low"

    def test_section_effort_overrides_the_default(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch, "[deep-research.extract]\nEFFORT=medium\n")
        assert conf_effort("deep-research.extract", "low") == "medium"

    def test_effort_is_capped_by_the_ceiling(self, tmp_path, monkeypatch):
        """A budget run forces every node down — a conf override must not be
        the one thing that escapes the ceiling."""
        _write_conf(tmp_path, monkeypatch, "[deep-research.plan]\nEFFORT=high\n")
        monkeypatch.setenv("MULTIPLAI_EFFORT", "low")
        assert conf_effort("deep-research.plan", None) == "low"

    def test_blank_value_is_treated_as_unset(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch, "[deep-research.extract]\nEFFORT=\n")
        assert conf_effort("deep-research.extract", "low") == "low"


class TestNodeEffortPrecedence:
    def test_node_section_beats_skill_wide_section(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch,
                    "[deep-research]\nEFFORT=medium\n[deep-research.extract]\nEFFORT=low\n")
        assert _node_effort("extract", None) == "low"
        assert _node_effort("plan", None) == "medium"

    def test_skill_wide_section_beats_the_code_default(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch, "[deep-research]\nEFFORT=medium\n")
        assert _node_effort("extract", "low") == "medium"

    def test_nothing_configured_keeps_every_code_default(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch, "")
        assert _node_effort("extract", "low") == "low"
        assert _node_effort("plan", None) is None


def test_config_efforts_pick_up_the_conf(tmp_path, monkeypatch):
    """End-to-end: the conf value reaches the per-node map the nodes read."""
    _write_conf(tmp_path, monkeypatch, "[deep-research.extract]\nEFFORT=medium\n")
    config = ResearchConfig(query="q", output_dir=tmp_path,
                            preset=PRESETS["quick"], date="2026-07-20")
    assert config.efforts["extract"] == "medium"
    assert config.efforts["triage_relevance"] == "low"  # untouched default


def test_cli_effort_still_overrides_every_node(tmp_path, monkeypatch):
    """--effort is a whole-run override; the conf tunes per node."""
    _write_conf(tmp_path, monkeypatch, "[deep-research.extract]\nEFFORT=medium\n")
    parser = build_parser()
    args = parser.parse_args(["--query", "q", "--output", str(tmp_path), "--effort", "low"])
    config = ResearchConfig.from_cli_args(args)
    assert set(config.efforts.values()) == {"low"}


# --- Per-node extended thinking --------------------------------------------


class TestThinkingTiers:
    def test_default_thinking_map_exact(self, tmp_path, monkeypatch):
        """Mechanical nodes disable thinking; reasoning nodes keep the SDK
        default. The map is asserted exactly so a node added without a
        deliberate thinking decision fails here."""
        _write_conf(tmp_path, monkeypatch, "")  # isolate from any real conf
        config = _mk_config(tmp_path)
        assert config.thinkings == {
            "plan": None,
            "diverge": None,
            "challenge": None,
            "search": {"type": "disabled"},
            "triage_relevance": {"type": "disabled"},
            "extract": {"type": "disabled"},
            "verify": {"type": "disabled"},
            "reassess": None,
            "synthesize": None,
            "adversarial": None,
            "quality_check": None,
        }

    def test_thinkings_and_models_cover_same_nodes(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch, "")
        config = _mk_config(tmp_path)
        assert set(config.thinkings) == set(config.models)

    def test_disabled_nodes_get_independent_dicts(self, tmp_path, monkeypatch):
        """A caller mutating one node's dict must not affect another's."""
        _write_conf(tmp_path, monkeypatch, "")
        config = _mk_config(tmp_path)
        assert config.thinkings["search"] is not config.thinkings["extract"]
        assert config.thinkings["search"] is not THINKING_DISABLED


class TestConfThinking:
    """The efforts map got its conf half in conf_effort; conf_thinking is the
    same mechanic for the thinking axis."""

    def test_absent_conf_keeps_the_code_default(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch, "")
        assert conf_thinking("deep-research.search", THINKING_DISABLED) == THINKING_DISABLED
        assert conf_thinking("deep-research.plan", None) is None

    def test_truthy_value_restores_the_sdk_default(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch, "[deep-research.search]\nTHINKING=on\n")
        assert conf_thinking("deep-research.search", THINKING_DISABLED) is None

    def test_off_value_disables_thinking(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch, "[deep-research.plan]\nTHINKING=off\n")
        assert conf_thinking("deep-research.plan", None) == THINKING_DISABLED

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "disabled"])
    def test_every_off_spelling_disables_thinking(self, tmp_path, monkeypatch, value):
        _write_conf(tmp_path, monkeypatch, f"[deep-research.plan]\nTHINKING={value}\n")
        assert conf_thinking("deep-research.plan", None) == THINKING_DISABLED

    def test_blank_value_is_treated_as_unset(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch, "[deep-research.search]\nTHINKING=\n")
        assert conf_thinking("deep-research.search", THINKING_DISABLED) == THINKING_DISABLED

    def test_returned_default_is_a_copy(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch, "")
        got = conf_thinking("deep-research.search", THINKING_DISABLED)
        assert got == THINKING_DISABLED
        assert got is not THINKING_DISABLED

    def test_empty_dict_default_is_also_copied(self, tmp_path, monkeypatch):
        """`if default:` would have returned an empty default by reference —
        the one shape a truthiness check gets wrong."""
        _write_conf(tmp_path, monkeypatch, "")
        original: dict = {}
        got = conf_thinking("deep-research.search", original)
        assert got == {}
        assert got is not original

    def test_unrecognized_value_is_ignored_not_guessed(
        self, tmp_path, monkeypatch, caplog
    ):
        """A typo must keep the node's default, not silently disable thinking.
        `enable` is the dangerous one: the user asked for thinking ON."""
        _write_conf(tmp_path, monkeypatch, "[deep-research.plan]\nTHINKING=enable\n")
        with caplog.at_level(logging.WARNING, logger="research_pipeline.config"):
            assert conf_thinking("deep-research.plan", None) is None
            assert conf_thinking(
                "deep-research.plan", THINKING_DISABLED
            ) == THINKING_DISABLED
        assert any("not a recognized value" in r.getMessage() for r in caplog.records)

    def test_unrecognized_value_warns_once_per_task(
        self, tmp_path, monkeypatch, caplog
    ):
        """A bad skill-wide key is one log line, not one per node."""
        _write_conf(tmp_path, monkeypatch, "[deep-research]\nTHINKING=maybe\n")
        with caplog.at_level(logging.WARNING, logger="research_pipeline.config"):
            for node in ("plan", "search", "extract", "verify"):
                _node_thinking(node, None)
        warnings = [r for r in caplog.records if "not a recognized value" in r.getMessage()]
        assert len(warnings) == 1

    def test_unrecognized_skill_wide_value_leaves_reasoning_nodes_alone(
        self, tmp_path, monkeypatch
    ):
        """The regression this guards: a skill-wide typo used to disable
        thinking on every reasoning node."""
        _write_conf(tmp_path, monkeypatch, "[deep-research]\nTHINKING=enable\n")
        config = _mk_config(tmp_path)
        assert config.thinkings["plan"] is None
        assert config.thinkings["synthesize"] is None
        assert config.thinkings["adversarial"] is None
        assert config.thinkings["search"] == THINKING_DISABLED


class TestNodeThinkingPrecedence:
    def test_node_section_beats_skill_wide_section(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch,
                    "[deep-research]\nTHINKING=on\n[deep-research.search]\nTHINKING=off\n")
        assert _node_thinking("search", THINKING_DISABLED) == THINKING_DISABLED
        assert _node_thinking("extract", THINKING_DISABLED) is None

    def test_skill_wide_section_beats_the_code_default(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch, "[deep-research]\nTHINKING=on\n")
        assert _node_thinking("search", THINKING_DISABLED) is None

    def test_nothing_configured_keeps_every_code_default(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch, "")
        assert _node_thinking("search", THINKING_DISABLED) == THINKING_DISABLED
        assert _node_thinking("plan", None) is None


def test_config_thinkings_pick_up_the_conf(tmp_path, monkeypatch):
    """End-to-end: the conf override flips a node in the map the nodes read."""
    _write_conf(tmp_path, monkeypatch, "[deep-research.search]\nTHINKING=on\n")
    config = ResearchConfig(query="q", output_dir=tmp_path,
                            preset=PRESETS["quick"], date="2026-07-20")
    assert config.thinkings["search"] is None
    assert config.thinkings["extract"] == {"type": "disabled"}  # untouched default


class TestCliThinkingOverride:
    """`--thinking` is the per-run escape hatch, mirroring `--effort`. Without
    it the only way back from a disabled mechanical node was multiplai.conf,
    which is machine-level state and not a property of one run."""

    def _config(self, tmp_path, monkeypatch, *extra):
        _write_conf(tmp_path, monkeypatch, "")
        args = build_parser().parse_args(
            ["--query", "q", "--output", str(tmp_path), *extra]
        )
        return ResearchConfig.from_cli_args(args)

    def test_on_restores_the_sdk_default_everywhere(self, tmp_path, monkeypatch):
        config = self._config(tmp_path, monkeypatch, "--thinking", "on")
        assert set(config.thinkings) == set(config.models)
        assert all(v is None for v in config.thinkings.values())

    def test_off_disables_every_node(self, tmp_path, monkeypatch):
        config = self._config(tmp_path, monkeypatch, "--thinking", "off")
        assert all(v == THINKING_DISABLED for v in config.thinkings.values())

    def test_off_still_gives_each_node_its_own_dict(self, tmp_path, monkeypatch):
        config = self._config(tmp_path, monkeypatch, "--thinking", "off")
        assert config.thinkings["plan"] is not config.thinkings["search"]
        assert config.thinkings["plan"] is not THINKING_DISABLED

    def test_absent_flag_keeps_the_per_node_defaults(self, tmp_path, monkeypatch):
        config = self._config(tmp_path, monkeypatch)
        assert config.thinking is None
        assert config.thinkings["search"] == THINKING_DISABLED
        assert config.thinkings["plan"] is None

    def test_cli_beats_the_conf(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch, "[deep-research]\nTHINKING=on\n")
        args = build_parser().parse_args(
            ["--query", "q", "--output", str(tmp_path), "--thinking", "off"]
        )
        config = ResearchConfig.from_cli_args(args)
        assert all(v == THINKING_DISABLED for v in config.thinkings.values())

    def test_thinking_flag_leaves_the_effort_axis_alone(self, tmp_path, monkeypatch):
        """Each flag covers one axis — the doc claim SKILL.md now makes."""
        config = self._config(tmp_path, monkeypatch, "--thinking", "off")
        assert config.efforts["plan"] is None
        assert config.efforts["search"] == "low"
        assert config.efforts["quality_check"] == "medium"

    def test_rejects_anything_but_on_or_off(self, tmp_path, monkeypatch):
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                ["--query", "q", "--output", str(tmp_path), "--thinking", "maybe"]
            )
