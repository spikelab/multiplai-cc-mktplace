"""Tests for per-node model/effort tier mappings and CLI overrides."""

from __future__ import annotations

from pathlib import Path

from research_pipeline.__main__ import build_parser
from research_pipeline.config import (
    DEFAULT_MODEL,
    PARSE_MODEL,
    PRESETS,
    ResearchConfig,
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
