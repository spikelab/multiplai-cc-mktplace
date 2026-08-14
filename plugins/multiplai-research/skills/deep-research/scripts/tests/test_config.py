"""Tests for per-node model/effort tier mappings and CLI overrides."""

from __future__ import annotations

from pathlib import Path

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

    def test_non_truthy_value_disables_thinking(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch, "[deep-research.plan]\nTHINKING=off\n")
        assert conf_thinking("deep-research.plan", None) == THINKING_DISABLED

    def test_blank_value_is_treated_as_unset(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch, "[deep-research.search]\nTHINKING=\n")
        assert conf_thinking("deep-research.search", THINKING_DISABLED) == THINKING_DISABLED

    def test_returned_default_is_a_copy(self, tmp_path, monkeypatch):
        _write_conf(tmp_path, monkeypatch, "")
        got = conf_thinking("deep-research.search", THINKING_DISABLED)
        assert got == THINKING_DISABLED
        assert got is not THINKING_DISABLED


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
