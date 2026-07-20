"""Tests for the VERIFY node — per-claim verdicts closing the verification loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from research_pipeline.config import PRESETS, ResearchConfig
from research_pipeline.models import Confidence, Finding, ReassessResult
from research_pipeline.nodes import verify as verify_node
from research_pipeline.nodes.verify import ClaimVerdict, VerifyVerdicts, verify_verdicts
from research_pipeline.state import ResearchState


def _mk_config(tmp_path: Path) -> ResearchConfig:
    return ResearchConfig(
        query="test", output_dir=tmp_path, preset=PRESETS["quick"], date="2026-07-20"
    )


def _finding(fact: str, quote: str | None = None) -> Finding:
    return Finding(
        fact=fact,
        source_url="https://v.example",
        source_title="Verification Source",
        confidence=Confidence.HIGH,
        quote=quote,
    )


class TestVerifyVerdictsNode:
    @pytest.mark.asyncio
    async def test_verdicts_from_stubbed_llm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}

        async def fake_structured(prompt, schema, **kwargs):
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return VerifyVerdicts(
                verdicts=[
                    ClaimVerdict(
                        claim="X is true",
                        verdict="refuted",
                        evidence=["finding 1: X is false"],
                    )
                ]
            )

        monkeypatch.setattr(verify_node, "llm_call_structured", fake_structured)

        verdicts = await verify_verdicts(
            _mk_config(tmp_path),
            claims=["X is true"],
            new_findings=[_finding("X is false", quote="X is definitely false")],
        )

        assert len(verdicts) == 1
        assert verdicts[0].verdict == "refuted"
        # The prompt carries both the claim and the new evidence
        assert "X is true" in captured["prompt"]
        assert "X is false" in captured["prompt"]
        assert captured["kwargs"]["label"] == "verify:verdicts"

    @pytest.mark.asyncio
    async def test_empty_new_findings_short_circuits_without_llm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def exploding_structured(*args, **kwargs):
            raise AssertionError("LLM must not be called with zero new findings")

        monkeypatch.setattr(verify_node, "llm_call_structured", exploding_structured)

        verdicts = await verify_verdicts(
            _mk_config(tmp_path),
            claims=["claim A", "claim B"],
            new_findings=[],
        )

        assert [v.claim for v in verdicts] == ["claim A", "claim B"]
        assert all(v.verdict == "unresolved" for v in verdicts)
        assert all(v.evidence == [] for v in verdicts)

    @pytest.mark.asyncio
    async def test_no_claims_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def exploding_structured(*args, **kwargs):
            raise AssertionError("LLM must not be called with zero claims")

        monkeypatch.setattr(verify_node, "llm_call_structured", exploding_structured)

        assert await verify_verdicts(_mk_config(tmp_path), [], [_finding("f")]) == []

    def test_verdict_literal_rejects_unknown_value(self) -> None:
        with pytest.raises(Exception):
            ClaimVerdict(claim="c", verdict="maybe")  # type: ignore[arg-type]


class TestVerdictsRenderedForSynthesis:
    def test_format_reassessment_renders_table_and_instruction(
        self, tmp_path: Path
    ) -> None:
        from research_pipeline.nodes.synthesize import _format_reassessment

        config = _mk_config(tmp_path)
        state = ResearchState.new(
            query=config.query,
            output_file=config.output_file_path(),
            state_file=config.state_file_path(),
        )
        state.reassessment = ReassessResult(verify_claims=["X is true"])
        state.verdicts = [
            ClaimVerdict(claim="X is true", verdict="refuted", evidence=["finding 1"]),
            ClaimVerdict(claim="Y holds", verdict="unresolved", evidence=[]),
        ]

        text = _format_reassessment(state)
        assert "VERDICTS" in text
        assert "| X is true | refuted | finding 1 |" in text
        assert "| Y holds | unresolved |" in text
        # Binding instructions for the synthesis LLM
        assert "refuted MUST be corrected or removed" in text
        assert "tagged UNVERIFIED" in text

    def test_no_table_without_verdicts(self, tmp_path: Path) -> None:
        from research_pipeline.nodes.synthesize import _format_reassessment

        config = _mk_config(tmp_path)
        state = ResearchState.new(
            query=config.query,
            output_file=config.output_file_path(),
            state_file=config.state_file_path(),
        )
        state.reassessment = ReassessResult(verify_claims=["X"])
        assert "VERDICTS" not in _format_reassessment(state)

    def test_verdicts_survive_checkpoint_roundtrip(self, tmp_path: Path) -> None:
        config = _mk_config(tmp_path)
        state = ResearchState.new(
            query=config.query,
            output_file=config.output_file_path(),
            state_file=config.state_file_path(),
        )
        state.verdicts = [
            ClaimVerdict(claim="c", verdict="confirmed", evidence=["e"])
        ]
        state.checkpoint()
        loaded = ResearchState.load(Path(state.state_file))
        assert loaded.verdicts[0].claim == "c"
        assert loaded.verdicts[0].verdict == "confirmed"
