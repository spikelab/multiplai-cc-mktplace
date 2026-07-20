"""Tests for the structured adversarial (challenge) review node."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from research_pipeline.config import PRESETS, ResearchConfig
from research_pipeline.models import Confidence, Finding
from research_pipeline.nodes import challenge as challenge_node
from research_pipeline.nodes.challenge import (
    ChallengeReview,
    adversarial_review,
    render_review,
)


def _mk_config(tmp_path: Path) -> ResearchConfig:
    return ResearchConfig(
        query="test", output_dir=tmp_path, preset=PRESETS["quick"], date="2026-07-20"
    )


def _review(**overrides) -> ChallengeReview:
    base = dict(
        evidence_strength=4,
        argument_coherence=3,
        counter_argument_resistance=2,
        weakest_claims=["claim A rests on one blog post"],
        review_markdown="# Adversarial Review: Test\n\nBody.",
    )
    base.update(overrides)
    return ChallengeReview(**base)


class TestChallengeReviewModel:
    def test_overall_is_mean_of_three_scores(self) -> None:
        assert _review().overall == pytest.approx(3.0)

    def test_scores_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _review(evidence_strength=6)
        with pytest.raises(ValidationError):
            _review(argument_coherence=0)


class TestRenderReview:
    def test_file_starts_with_code_generated_score_table(self) -> None:
        rendered = render_review(_review())
        lines = rendered.splitlines()
        assert lines[0] == "| Dimension | Score (1-5) |"
        assert "| Evidence strength | 4 |" in rendered
        assert "| Argument coherence | 3 |" in rendered
        assert "| Counter-argument resistance | 2 |" in rendered
        assert "| **Overall** | **3.0** |" in rendered
        # The LLM body follows the table
        assert rendered.index("| **Overall**") < rendered.index("# Adversarial Review")


class TestAdversarialReviewNode:
    @pytest.mark.asyncio
    async def test_structured_parse_and_grounding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}

        async def fake_structured(prompt, schema, **kwargs):
            captured["prompt"] = prompt
            captured["schema"] = schema
            return _review()

        monkeypatch.setattr(challenge_node, "llm_call_structured", fake_structured)

        findings = [
            Finding(
                fact="grounding fact",
                source_url="https://g.example",
                source_title="Grounding Source",
                confidence=Confidence.HIGH,
                quote="the exact words",
            )
        ]
        review = await adversarial_review(_mk_config(tmp_path), "# Report", findings)

        assert review.overall == pytest.approx(3.0)
        assert captured["schema"] is ChallengeReview
        # Grounded: findings (fact + quote) are in the prompt
        assert "grounding fact" in captured["prompt"]
        assert "the exact words" in captured["prompt"]
        # Short report → no truncation note
        assert "truncated at 50,000 chars" not in captured["prompt"]

    @pytest.mark.asyncio
    async def test_truncation_note_appended_for_long_reports(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}

        async def fake_structured(prompt, schema, **kwargs):
            captured["prompt"] = prompt
            return _review()

        monkeypatch.setattr(challenge_node, "llm_call_structured", fake_structured)

        long_report = "x" * 60_000
        await adversarial_review(_mk_config(tmp_path), long_report, [])

        assert "NOTE: report truncated at 50,000 chars for review" in captured["prompt"]
        # And the report itself was capped
        assert "x" * 50_001 not in captured["prompt"]
