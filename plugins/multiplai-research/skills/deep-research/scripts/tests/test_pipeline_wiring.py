"""Integration-style test: verify the pipeline module imports and wiring work.

Does NOT make any real LLM or search API calls. Mocks the LLM and search layer
with deterministic stubs to exercise the orchestrator's state transitions,
gate wiring, and resume logic.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from research_pipeline.config import PRESETS, ResearchConfig
from research_pipeline.models import (
    PlanResult,
    SearchResult,
    Source,
    ReputationTier,
    Finding,
)
from research_pipeline.state import ResearchState, Stage


@pytest.fixture
def fake_config(tmp_path: Path) -> ResearchConfig:
    args = argparse.Namespace(
        query="test query",
        output=tmp_path,
        preset="quick",
        auto=True,
        parallel=False,
        agents=None,
        deep=False,
        challenge=False,
        no_challenge=False,
        no_memory=False,
        date="2026-04-05",
        research_type="general",
        personal_context="",
        prior_knowledge="",
        plan_only=False,
        approved_plan=None,
    )
    return ResearchConfig.from_cli_args(args)


class TestPipelineWiring:
    def test_pipeline_module_imports(self) -> None:
        from research_pipeline.pipeline import run_pipeline, validate_api_keys

        assert callable(run_pipeline)
        assert callable(validate_api_keys)

    def test_config_preset_selected(self, fake_config: ResearchConfig) -> None:
        assert fake_config.preset.name == "quick"
        assert fake_config.preset.sources == 10
        assert fake_config.preset.summary_level == "gist"
        assert fake_config.preset.min_sources == 5

    def test_config_paths_derived_from_query(self, fake_config: ResearchConfig) -> None:
        out = fake_config.output_file_path()
        assert out.name == "test-query-2026-04-05.md"
        state_file = fake_config.state_file_path()
        assert state_file.name == "test-query-2026-04-05-state.json"

    def test_challenge_mode_auto_on_thorough(self, tmp_path: Path) -> None:
        args = argparse.Namespace(
            query="x",
            output=tmp_path,
            preset="thorough",
            auto=True,
            parallel=False,
            agents=None,
            deep=False,
            challenge=False,
            no_challenge=False,
            no_memory=False,
            date="2026-04-05",
            research_type="general",
            personal_context="",
            prior_knowledge="",
            plan_only=False,
            approved_plan=None,
        )
        config = ResearchConfig.from_cli_args(args)
        assert config.challenge_enabled is True

    def test_validate_api_keys_reports_missing_when_no_claude_tools(self) -> None:
        from research_pipeline.pipeline import validate_api_keys

        with patch.dict(os.environ, {}, clear=True):
            errors = validate_api_keys(prefer_claude_tools=False)
        assert len(errors) >= 1
        assert any("TAVILY_API_KEY" in e for e in errors)

    def test_validate_api_keys_passes_with_claude_tools_default(self) -> None:
        """When prefer_claude_tools=True (default), no external keys needed."""
        from research_pipeline.pipeline import validate_api_keys

        with patch.dict(os.environ, {}, clear=True):
            errors = validate_api_keys(prefer_claude_tools=True)
        assert errors == []

    def test_validate_api_keys_passes_with_external_keys(self) -> None:
        from research_pipeline.pipeline import validate_api_keys

        with patch.dict(
            os.environ,
            {"TAVILY_API_KEY": "stub", "EXA_API_KEY": "stub"},
            clear=True,
        ):
            errors = validate_api_keys(prefer_claude_tools=False)
        assert errors == []


class TestGatesInPipeline:
    def test_gates_wired_to_pipeline(self) -> None:
        """Spot-check that the gates module is imported by the pipeline."""
        from research_pipeline import pipeline as p

        assert hasattr(p, "query_diversity_gate")
        assert hasattr(p, "min_sources_gate")
        assert hasattr(p, "coverage_gate")
        assert hasattr(p, "reassess_gate")
        assert hasattr(p, "critical_source_gate")

    def test_quality_check_node_wired(self) -> None:
        """Quality check node is imported by the pipeline."""
        from research_pipeline import pipeline as p

        assert hasattr(p, "quality_check_node")


class TestStateResume:
    def test_resume_skips_completed_stages(
        self, fake_config: ResearchConfig, tmp_path: Path
    ) -> None:
        """State loaded with stage=SEARCH_COMPLETE should be treated as past SEARCH."""
        state = ResearchState.new(
            query=fake_config.query,
            output_file=fake_config.output_file_path(),
            state_file=fake_config.state_file_path(),
        )
        state.plan = PlanResult(
            sub_questions=["q1", "q2"],
            primary_queries=["p1"],
        )
        state.search_results = [
            SearchResult(
                url="https://a", title="A", snippet="", source_api="tavily"
            )
        ]
        state.advance_to(Stage.SEARCH_COMPLETE)

        loaded = ResearchState.load(fake_config.state_file_path())
        assert loaded.is_complete(Stage.PLAN_COMPLETE)
        assert loaded.is_complete(Stage.SEARCH_COMPLETE)
        assert not loaded.is_complete(Stage.TRIAGE_COMPLETE)
        assert len(loaded.search_results) == 1

    def test_resume_preserves_findings(
        self, fake_config: ResearchConfig
    ) -> None:
        state = ResearchState.new(
            query=fake_config.query,
            output_file=fake_config.output_file_path(),
            state_file=fake_config.state_file_path(),
        )
        state.sources = [
            Source(url="https://a", title="A", snippet="a"),
            Source(url="https://b", title="B", snippet="b"),
        ]
        state.mark_source_extracted(
            "https://a",
            content="content",
            findings=[
                Finding(
                    fact="fact1",
                    source_url="https://a",
                    source_title="A",
                    reputation=ReputationTier.AUTHORITATIVE,
                )
            ],
        )

        loaded = ResearchState.load(fake_config.state_file_path())
        assert len(loaded.findings) == 1
        assert len(loaded.pending_sources()) == 1


class TestMicroPreset:
    def test_micro_preset_exists(self) -> None:
        """PRESETS['micro'] has expected field values."""
        micro = PRESETS["micro"]
        assert micro.name == "micro"
        assert micro.sources == 3
        assert micro.max_total_fetches == 3
        assert micro.link_depth == 0
        assert micro.max_sub_pages == 0
        assert micro.follow_links is False
        assert micro.summary_level == "gist"
        assert micro.min_sources == 1
        assert micro.max_sub_questions == 2
        assert micro.max_reassess_findings == 20


class TestEffortAndModelConfig:
    def test_effort_on_research_config(self) -> None:
        """ResearchConfig stores the effort field correctly."""
        config = ResearchConfig(
            query="test",
            output_dir=Path("/tmp"),
            preset=PRESETS["quick"],
            effort="high",
        )
        assert config.effort == "high"

    def test_effort_defaults_to_none(self) -> None:
        """ResearchConfig.effort defaults to None when not specified."""
        config = ResearchConfig(
            query="test",
            output_dir=Path("/tmp"),
            preset=PRESETS["quick"],
        )
        assert config.effort is None

    def test_model_override_sets_all_nodes(self, tmp_path: Path) -> None:
        """from_cli_args with --model overrides all model dict entries."""
        args = argparse.Namespace(
            query="test",
            output=tmp_path,
            preset="quick",
            auto=True,
            parallel=False,
            agents=None,
            deep=False,
            challenge=False,
            no_challenge=False,
            no_memory=False,
            date="2026-04-05",
            research_type="general",
            personal_context="",
            prior_knowledge="",
            plan_only=False,
            approved_plan=None,
            effort=None,
            model="test-model-override",
        )
        config = ResearchConfig.from_cli_args(args)
        for node_name, model_value in config.models.items():
            assert model_value == "test-model-override", (
                f"Node '{node_name}' should be overridden but got '{model_value}'"
            )

    def test_model_not_overridden_when_none(self, tmp_path: Path) -> None:
        """from_cli_args without --model keeps default model assignments."""
        args = argparse.Namespace(
            query="test",
            output=tmp_path,
            preset="quick",
            auto=True,
            parallel=False,
            agents=None,
            deep=False,
            challenge=False,
            no_challenge=False,
            no_memory=False,
            date="2026-04-05",
            research_type="general",
            personal_context="",
            prior_knowledge="",
            plan_only=False,
            approved_plan=None,
            effort=None,
            model=None,
        )
        config = ResearchConfig.from_cli_args(args)
        # All nodes should have the DEFAULT_MODEL, not None or empty
        for node_name, model_value in config.models.items():
            assert model_value != "test-model-override"
            assert model_value  # not empty/None


class TestCLIArgs:
    def test_cli_effort_arg(self) -> None:
        """--effort low parses correctly."""
        from research_pipeline.__main__ import build_parser

        args = build_parser().parse_args(["--query", "test", "--effort", "low"])
        assert args.effort == "low"

    def test_cli_effort_default_is_none(self) -> None:
        """--effort not provided defaults to None."""
        from research_pipeline.__main__ import build_parser

        args = build_parser().parse_args(["--query", "test"])
        assert args.effort is None

    def test_cli_model_arg(self) -> None:
        """--model claude-opus-4-6 parses correctly."""
        from research_pipeline.__main__ import build_parser

        args = build_parser().parse_args(
            ["--query", "test", "--model", "claude-opus-4-6"]
        )
        assert args.model == "claude-opus-4-6"

    def test_cli_model_default_is_none(self) -> None:
        """--model not provided defaults to None."""
        from research_pipeline.__main__ import build_parser

        args = build_parser().parse_args(["--query", "test"])
        assert args.model is None

    def test_cli_preset_micro(self) -> None:
        """--preset micro is a valid choice."""
        from research_pipeline.__main__ import build_parser

        args = build_parser().parse_args(["--query", "test", "--preset", "micro"])
        assert args.preset == "micro"
