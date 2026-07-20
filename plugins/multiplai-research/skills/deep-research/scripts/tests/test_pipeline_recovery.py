"""Wiring tests for pipeline recovery paths.

Covers the P0 hardening fixes: sequential reassess cycle with loud failures,
coverage-recovery URL dedup, diversity-gate recheck, and min-sources honesty.
All LLM nodes and the search router are stubbed — no network, no SDK calls.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest

from research_pipeline import pipeline
from research_pipeline.search_router import SearchRouter
from research_pipeline.config import PRESETS, ResearchConfig
from research_pipeline.models import (
    GateResult,
    PlanResult,
    QualityCheckResult,
    ReassessResult,
    SearchResult,
    Source,
    SourceStatus,
)
from research_pipeline.progress import ProgressWriter
from research_pipeline.state import ResearchState, Stage


# ---------------------------------------------------------------------------
# Stubs / helpers
# ---------------------------------------------------------------------------


def _as_router(r: "StubRouter") -> SearchRouter:
    """Duck-typed stub → SearchRouter for the type checker."""
    return cast(SearchRouter, r)


class StubRouter:
    """Search router stub: pops one canned result batch per call, records calls."""

    def __init__(self, result_batches: list[list[SearchResult]] | None = None):
        self.result_batches = list(result_batches or [])
        self.batch_search_calls: list[list[str]] = []

    async def batch_search(
        self, queries, max_results: int = 10, strategy: str = "keyword"
    ) -> list[SearchResult]:
        self.batch_search_calls.append(list(queries))
        if self.result_batches:
            return self.result_batches.pop(0)
        return []

    async def aclose(self) -> None:
        pass


def _mk_config(tmp_path: Path, preset: str = "quick", **kw) -> ResearchConfig:
    return ResearchConfig(
        query="test query",
        output_dir=tmp_path,
        preset=PRESETS[preset],
        date="2026-07-20",
        auto=True,
        **kw,
    )


def _mk_state(config: ResearchConfig) -> ResearchState:
    return ResearchState.new(
        query=config.query,
        output_file=config.output_file_path(),
        state_file=config.state_file_path(),
    )


def _mk_progress(tmp_path: Path) -> ProgressWriter:
    progress = ProgressWriter(tmp_path / "progress.md")
    progress.initialize(query="test query", preset="quick", fetch_budget=10)
    return progress


def _sr(url: str) -> SearchResult:
    return SearchResult(url=url, title=url, snippet="s", source_api="stub")


# ---------------------------------------------------------------------------
# Sequential reassess cycle + failure visibility (P0: refinement‖verification race)
# ---------------------------------------------------------------------------


class TestReassessCycleSequentialAndFailureVisibility:
    @pytest.mark.asyncio
    async def test_reassess_cycle_sequential_and_verification_failure_visible(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(a) Refinement completes before verification starts; (b) a raising
        verification leg writes a progress line + state field and does not
        propagate."""
        config = _mk_config(tmp_path)
        state = _mk_state(config)
        state.reassessment = ReassessResult(
            refinement_needed=True,
            refinement_queries=["rq"],
            verify_queries=["vq"],
        )
        progress = _mk_progress(tmp_path)

        order: list[str] = []

        async def fake_refinement(config, state, router, progress) -> None:
            order.append("refinement:start")
            await asyncio.sleep(0.01)
            order.append("refinement:end")

        async def fake_verification(config, state, router, progress) -> None:
            order.append("verification:start")
            raise RuntimeError("boom")

        monkeypatch.setattr(pipeline, "_run_refinement", fake_refinement)
        monkeypatch.setattr(pipeline, "_run_verification", fake_verification)

        # Must not raise despite the verification failure
        await pipeline._run_reassess_cycle(config, state, _as_router(StubRouter()), progress)

        assert order == ["refinement:start", "refinement:end", "verification:start"]
        progress_text = (tmp_path / "progress.md").read_text()
        assert "VERIFICATION FAILED" in progress_text
        assert "boom" in progress_text
        assert state.verification_error == "boom"
        assert state.refinement_error == ""

    @pytest.mark.asyncio
    async def test_refinement_failure_recorded_and_verification_still_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _mk_config(tmp_path)
        state = _mk_state(config)
        state.reassessment = ReassessResult(
            refinement_needed=True,
            refinement_queries=["rq"],
            verify_queries=["vq"],
        )
        progress = _mk_progress(tmp_path)

        ran: list[str] = []

        async def fake_refinement(config, state, router, progress) -> None:
            raise ValueError("refinement broke")

        async def fake_verification(config, state, router, progress) -> None:
            ran.append("verification")

        monkeypatch.setattr(pipeline, "_run_refinement", fake_refinement)
        monkeypatch.setattr(pipeline, "_run_verification", fake_verification)

        await pipeline._run_reassess_cycle(config, state, _as_router(StubRouter()), progress)

        assert ran == ["verification"]
        assert state.refinement_error == "refinement broke"
        progress_text = (tmp_path / "progress.md").read_text()
        assert "REFINEMENT FAILED" in progress_text

    def test_no_gather_in_reassess_cycle(self) -> None:
        """The reassess cycle must not use asyncio.gather (the old race)."""
        import inspect

        src = inspect.getsource(pipeline._run_reassess_cycle)
        assert "asyncio.gather" not in src

    def test_failure_rendered_in_reassessment_for_synthesis(
        self, tmp_path: Path
    ) -> None:
        """A recorded verification failure reaches the synthesis prompt."""
        from research_pipeline.nodes.synthesize import _format_reassessment

        config = _mk_config(tmp_path)
        state = _mk_state(config)
        state.reassessment = ReassessResult(verify_claims=["claim X"])
        state.verification_error = "search exploded"

        text = _format_reassessment(state)
        assert "FAILED" in text
        assert "search exploded" in text
        assert "unverified" in text


# ---------------------------------------------------------------------------
# Coverage-recovery URL dedup + min-sources honesty (full-stage harness)
# ---------------------------------------------------------------------------


def _stub_llm_nodes(monkeypatch: pytest.MonkeyPatch, triage_calls: list, read_calls: list):
    """Stub every LLM node so _run_main_stages runs from INIT without SDK calls.

    The stub read marks pending sources EXTRACTED (no findings), so the
    coverage gate fails and coverage recovery fires.
    """
    plan = PlanResult(sub_questions=["what is quantum foo?"], primary_queries=["p1"])

    async def fake_plan(config):
        return plan

    async def fake_plan_passthrough(config, p):
        return p

    async def fake_triage(
        config, results, sub_questions, target_domains, authority_domains=None
    ):
        triage_calls.append([r.url for r in results])
        return [Source(url=r.url, title=r.title, snippet=r.snippet) for r in results]

    async def fake_read(config, state, router=None):
        pending = state.pending_sources()
        read_calls.append([s.url for s in pending])
        for s in pending:
            s.status = SourceStatus.EXTRACTED

    async def fake_reassess(config, state, findings_override=None):
        return ReassessResult()

    async def fake_quality_check(config, state):
        return QualityCheckResult(go=True, confidence=0.9, reasoning="fine")

    async def fake_synthesize(config, state):
        return "# report"

    monkeypatch.setattr(pipeline.plan_node, "plan", fake_plan)
    monkeypatch.setattr(pipeline.plan_node, "diverge", fake_plan_passthrough)
    monkeypatch.setattr(pipeline.plan_node, "challenge", fake_plan_passthrough)
    monkeypatch.setattr(pipeline.triage_node, "triage", fake_triage)
    monkeypatch.setattr(pipeline.read_node, "read", fake_read)
    monkeypatch.setattr(pipeline.reassess_node, "reassess", fake_reassess)
    monkeypatch.setattr(pipeline.quality_check_node, "quality_check", fake_quality_check)
    monkeypatch.setattr(pipeline.synthesize_node, "synthesize", fake_synthesize)
    monkeypatch.setattr(
        pipeline,
        "query_diversity_gate",
        lambda queries, min_clusters=3: GateResult(passed=True, reason="stubbed"),
    )


class TestCoverageRecoveryDedup:
    @pytest.mark.asyncio
    async def test_known_url_not_readded_or_refetched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A targeted result whose URL is already in state.sources is filtered
        out before triage — not re-added, not re-fetched."""
        config = _mk_config(tmp_path)
        state = _mk_state(config)
        # SEARCH returns the known URL; recovery returns known + new
        router = StubRouter(
            result_batches=[
                [_sr("https://known.example/a")],
                [_sr("https://known.example/a"), _sr("https://new.example/b")],
            ]
        )
        triage_calls: list = []
        read_calls: list = []
        _stub_llm_nodes(monkeypatch, triage_calls, read_calls)

        progress = _mk_progress(tmp_path)
        await pipeline._run_main_stages(config, state, _as_router(router), progress)

        # Main triage saw the known URL; recovery triage saw ONLY the new URL
        assert triage_calls == [
            ["https://known.example/a"],
            ["https://new.example/b"],
        ]
        # The known URL was not re-added
        urls = [s.url for s in state.sources]
        assert urls.count("https://known.example/a") == 1
        assert "https://new.example/b" in urls
        # Recovery read only fetched the new source
        assert read_calls == [
            ["https://known.example/a"],
            ["https://new.example/b"],
        ]

    @pytest.mark.asyncio
    async def test_all_known_urls_skips_triage_and_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When every targeted result is already known, recovery is a no-op."""
        config = _mk_config(tmp_path)
        state = _mk_state(config)
        router = StubRouter(
            result_batches=[
                [_sr("https://known.example/a")],
                [_sr("https://known.example/a")],  # recovery returns only known
            ]
        )
        triage_calls: list = []
        read_calls: list = []
        _stub_llm_nodes(monkeypatch, triage_calls, read_calls)

        progress = _mk_progress(tmp_path)
        await pipeline._run_main_stages(config, state, _as_router(router), progress)

        # Only the main-stage triage/read ran — recovery filtered everything
        assert triage_calls == [["https://known.example/a"]]
        assert read_calls == [["https://known.example/a"]]
        assert [s.url for s in state.sources] == ["https://known.example/a"]


class TestMinSourcesGateHonesty:
    @pytest.mark.asyncio
    async def test_thin_run_logs_proceeding_under_minimum(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """quick preset needs 5 sources; with 1 the gate logs the shortfall
        loudly and the pipeline still completes."""
        config = _mk_config(tmp_path)
        state = _mk_state(config)
        router = StubRouter(result_batches=[[_sr("https://only.example")]])
        triage_calls: list = []
        read_calls: list = []
        _stub_llm_nodes(monkeypatch, triage_calls, read_calls)

        progress = _mk_progress(tmp_path)
        await pipeline._run_main_stages(config, state, _as_router(router), progress)

        progress_text = (tmp_path / "progress.md").read_text()
        assert "FAILED — proceeding under minimum (1/5)" in progress_text
        assert state.is_complete(Stage.SYNTHESIZE_COMPLETE)


# ---------------------------------------------------------------------------
# Diversity gate recheck (P0: recheck existed only as a comment)
# ---------------------------------------------------------------------------


class TestDiversityGateRecheck:
    @pytest.mark.asyncio
    async def test_recheck_runs_and_logs_after_diverge_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _mk_config(tmp_path)
        config.plan_only = True  # stop right after the diversity gate
        state = _mk_state(config)

        plan = PlanResult(sub_questions=["q"], primary_queries=["p1"])

        async def fake_plan(config):
            return plan

        async def fake_diverge(config, p):
            return plan

        async def fake_challenge(config, p):
            return plan

        monkeypatch.setattr(pipeline.plan_node, "plan", fake_plan)
        monkeypatch.setattr(pipeline.plan_node, "diverge", fake_diverge)
        monkeypatch.setattr(pipeline.plan_node, "challenge", fake_challenge)

        gate_calls: list[int] = []

        def fake_gate(queries, min_clusters=3):
            gate_calls.append(1)
            if len(gate_calls) == 1:
                return GateResult(passed=False, reason="only 1 cluster")
            return GateResult(passed=True, reason="4 clusters")

        monkeypatch.setattr(pipeline, "query_diversity_gate", fake_gate)

        progress = _mk_progress(tmp_path)
        await pipeline._run_main_stages(config, state, _as_router(StubRouter()), progress)

        # Gate ran twice: initial + recheck after the DIVERGE retry
        assert len(gate_calls) == 2
        progress_text = (tmp_path / "progress.md").read_text()
        assert "DIVERSITY GATE (recheck)" in progress_text
        assert "passed=True" in progress_text

    @pytest.mark.asyncio
    async def test_no_recheck_when_gate_passes_first_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _mk_config(tmp_path)
        config.plan_only = True
        state = _mk_state(config)

        plan = PlanResult(sub_questions=["q"], primary_queries=["p1"])

        async def fake_plan(config):
            return plan

        async def fake_diverge(config, p):
            raise AssertionError("DIVERGE retry must not run when gate passes")

        async def fake_challenge(config, p):
            return plan

        monkeypatch.setattr(pipeline.plan_node, "plan", fake_plan)
        monkeypatch.setattr(pipeline.plan_node, "challenge", fake_challenge)

        gate_calls: list[int] = []

        def fake_gate(queries, min_clusters=3):
            gate_calls.append(1)
            return GateResult(passed=True, reason="diverse")

        monkeypatch.setattr(pipeline, "query_diversity_gate", fake_gate)

        state.plan = plan
        state.advance_to(Stage.CHALLENGE_COMPLETE)  # skip PLAN/DIVERGE/CHALLENGE
        progress = _mk_progress(tmp_path)
        await pipeline._run_main_stages(config, state, _as_router(StubRouter()), progress)

        assert len(gate_calls) == 1
        assert "recheck" not in (tmp_path / "progress.md").read_text()
