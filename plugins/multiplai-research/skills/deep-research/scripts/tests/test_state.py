"""Tests for ResearchState checkpointing, resume, and per-source tracking."""

from __future__ import annotations

from pathlib import Path

import pytest

from research_pipeline.models import (
    Finding,
    PlanResult,
    ReputationTier,
    SearchResult,
    Source,
    SourceStatus,
)
from research_pipeline.state import ResearchState, Stage


@pytest.fixture
def state_paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "out.md", tmp_path / "state.json"


@pytest.fixture
def fresh_state(state_paths: tuple[Path, Path]) -> ResearchState:
    output, state = state_paths
    return ResearchState.new(query="test query", output_file=output, state_file=state)


class TestCheckpointRoundTrip:
    def test_fresh_state_has_init_stage(self, fresh_state: ResearchState) -> None:
        assert fresh_state.stage == Stage.INIT
        assert fresh_state.query == "test query"
        assert fresh_state.plan is None

    def test_checkpoint_creates_file(self, fresh_state: ResearchState) -> None:
        fresh_state.checkpoint()
        assert Path(fresh_state.state_file).exists()

    def test_checkpoint_is_valid_json(self, fresh_state: ResearchState) -> None:
        fresh_state.checkpoint()
        loaded = ResearchState.load(Path(fresh_state.state_file))
        assert loaded.query == fresh_state.query
        assert loaded.stage == fresh_state.stage

    def test_checkpoint_preserves_findings(self, fresh_state: ResearchState) -> None:
        fresh_state.findings.append(
            Finding(
                fact="The sky is blue",
                source_url="https://example.com",
                source_title="Example",
                reputation=ReputationTier.AUTHORITATIVE,
            )
        )
        fresh_state.checkpoint()
        loaded = ResearchState.load(Path(fresh_state.state_file))
        assert len(loaded.findings) == 1
        assert loaded.findings[0].fact == "The sky is blue"


class TestResumeFromStage:
    def test_resume_after_search_complete(self, fresh_state: ResearchState) -> None:
        fresh_state.plan = PlanResult(sub_questions=["q1", "q2"])
        fresh_state.search_results = [
            SearchResult(url="https://a", title="A", snippet="a", source_api="tavily"),
        ]
        fresh_state.advance_to(Stage.SEARCH_COMPLETE)

        loaded = ResearchState.load(Path(fresh_state.state_file))
        assert loaded.stage == Stage.SEARCH_COMPLETE
        assert loaded.is_complete(Stage.SEARCH_COMPLETE)
        assert loaded.is_complete(Stage.PLAN_COMPLETE)
        assert not loaded.is_complete(Stage.READ_COMPLETE)

    def test_stage_order(self, fresh_state: ResearchState) -> None:
        fresh_state.advance_to(Stage.READ_COMPLETE)
        assert fresh_state.is_complete(Stage.PLAN_COMPLETE)
        assert fresh_state.is_complete(Stage.SEARCH_COMPLETE)
        assert fresh_state.is_complete(Stage.READ_COMPLETE)
        assert not fresh_state.is_complete(Stage.SYNTHESIZE_COMPLETE)


class TestPerSourceTracking:
    def test_mark_source_extracted(self, fresh_state: ResearchState) -> None:
        fresh_state.sources = [
            Source(url="https://a", title="A", snippet="a"),
            Source(url="https://b", title="B", snippet="b"),
        ]
        fresh_state.mark_source_extracted(
            "https://a",
            content="# A\nContent",
            findings=[Finding(fact="fact1", source_url="https://a", source_title="A")],
        )
        assert fresh_state.sources[0].status == SourceStatus.EXTRACTED
        assert fresh_state.sources[0].extracted_content == "# A\nContent"
        assert fresh_state.sources[1].status == SourceStatus.PENDING
        assert len(fresh_state.findings) == 1

    def test_mark_source_failed(self, fresh_state: ResearchState) -> None:
        fresh_state.sources = [Source(url="https://a", title="A", snippet="a")]
        fresh_state.mark_source_failed("https://a", error="timeout")
        assert fresh_state.sources[0].status == SourceStatus.FAILED
        assert fresh_state.sources[0].error == "timeout"

    def test_pending_sources_excludes_completed(self, fresh_state: ResearchState) -> None:
        fresh_state.sources = [
            Source(url="https://a", title="A", snippet="a", status=SourceStatus.EXTRACTED),
            Source(url="https://b", title="B", snippet="b", status=SourceStatus.PENDING),
            Source(url="https://c", title="C", snippet="c", status=SourceStatus.FAILED),
        ]
        pending = fresh_state.pending_sources()
        assert len(pending) == 1
        assert pending[0].url == "https://b"

    def test_resume_with_partial_read(self, fresh_state: ResearchState) -> None:
        """After crash mid-READ, resume should skip completed sources."""
        fresh_state.sources = [
            Source(url=f"https://{i}", title=str(i), snippet="") for i in range(5)
        ]
        fresh_state.mark_source_extracted(
            "https://0", "content0", [Finding(fact="f0", source_url="https://0", source_title="0")]
        )
        fresh_state.mark_source_extracted(
            "https://1", "content1", [Finding(fact="f1", source_url="https://1", source_title="1")]
        )

        loaded = ResearchState.load(Path(fresh_state.state_file))
        assert len(loaded.completed_sources()) == 2
        assert len(loaded.pending_sources()) == 3
        assert len(loaded.findings) == 2


class TestCleanup:
    def test_cleanup_removes_state_file(self, fresh_state: ResearchState) -> None:
        fresh_state.checkpoint()
        assert Path(fresh_state.state_file).exists()
        fresh_state.cleanup()
        assert not Path(fresh_state.state_file).exists()

    def test_cleanup_is_idempotent(self, fresh_state: ResearchState) -> None:
        fresh_state.cleanup()  # doesn't exist yet — should not raise
        fresh_state.checkpoint()
        fresh_state.cleanup()
        fresh_state.cleanup()  # second call is no-op
