"""Wiring tests for the adversarial-review trigger and verify-verdict loop.

Covers: auto-challenge on reassess-flagged claims (standard preset),
--no-challenge suppression, the CHALLENGE: stdout line, and _run_verification
issuing per-claim verdicts. All LLM nodes and the router are stubbed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from research_pipeline import pipeline
from research_pipeline.models import Confidence, Finding, ReassessResult, Source
from research_pipeline.nodes.challenge import ChallengeReview
from research_pipeline.nodes.verify import ClaimVerdict
from research_pipeline.state import Stage

from tests.test_pipeline_recovery import (
    StubRouter,
    _as_router,
    _mk_config,
    _mk_progress,
    _mk_state,
    _sr,
)


def _stub_review() -> ChallengeReview:
    return ChallengeReview(
        evidence_strength=4,
        argument_coherence=3,
        counter_argument_resistance=2,
        weakest_claims=["w"],
        review_markdown="# Adversarial Review\n\nBody.",
    )


async def _run_pipeline_with_stubbed_stages(
    config,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reassessment: ReassessResult,
) -> tuple[int, list]:
    """Run the real run_pipeline with _run_main_stages replaced by a stub that
    leaves the state as a completed synthesis with the given reassessment."""
    review_calls: list = []

    async def fake_main_stages(config, state, router, progress) -> None:
        state.reassessment = reassessment
        Path(state.output_file).write_text("# Report\n\nSynthesized.")
        state.advance_to(Stage.SYNTHESIZE_COMPLETE)

    async def fake_adversarial_review(config, report, findings) -> ChallengeReview:
        review_calls.append((report, findings))
        return _stub_review()

    monkeypatch.setattr(
        pipeline, "build_default_router", lambda **kw: _as_router(StubRouter())
    )
    monkeypatch.setattr(pipeline, "_run_main_stages", fake_main_stages)
    monkeypatch.setattr(
        pipeline.challenge_node, "adversarial_review", fake_adversarial_review
    )

    rc = await pipeline.run_pipeline(config)
    return rc, review_calls


class TestAutoChallengeTrigger:
    @pytest.mark.asyncio
    async def test_standard_preset_flagged_claims_triggers_challenge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """standard preset (challenge_enabled=False) + non-empty
        load_bearing_claims ⇒ adversarial review runs, review file written,
        CHALLENGE: line printed."""
        config = _mk_config(tmp_path, preset="standard")
        assert not config.challenge_enabled  # precondition: not flag/preset driven

        rc, review_calls = await _run_pipeline_with_stubbed_stages(
            config,
            monkeypatch,
            reassessment=ReassessResult(load_bearing_claims=["big claim"]),
        )

        assert rc == 0
        assert len(review_calls) == 1
        review_path = config.output_dir / (
            config.output_file_path().stem + "-challenge.md"
        )
        assert review_path.exists()
        assert review_path.read_text().startswith("| Dimension | Score (1-5) |")

        out = capsys.readouterr().out
        assert f"CHALLENGE: {review_path} | overall=3.0" in out

    @pytest.mark.asyncio
    async def test_no_challenge_flag_suppresses_auto_trigger(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        config = _mk_config(tmp_path, preset="standard", no_challenge=True)

        rc, review_calls = await _run_pipeline_with_stubbed_stages(
            config,
            monkeypatch,
            reassessment=ReassessResult(load_bearing_claims=["big claim"]),
        )

        assert rc == 0
        assert review_calls == []
        out = capsys.readouterr().out
        assert "CHALLENGE:" not in out
        assert "SUMMARY:" in out  # pipeline still completed normally

    @pytest.mark.asyncio
    async def test_no_flagged_claims_no_challenge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        config = _mk_config(tmp_path, preset="standard")

        rc, review_calls = await _run_pipeline_with_stubbed_stages(
            config, monkeypatch, reassessment=ReassessResult()
        )

        assert rc == 0
        assert review_calls == []
        assert "CHALLENGE:" not in capsys.readouterr().out


class TestVerificationVerdictWiring:
    @pytest.mark.asyncio
    async def test_flagged_claims_get_unresolved_verdicts_without_new_evidence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verification search finds nothing new ⇒ every flagged claim gets an
        unresolved verdict (no LLM call) and a VERIFY VERDICTS progress line."""
        config = _mk_config(tmp_path)
        state = _mk_state(config)
        state.reassessment = ReassessResult(
            verify_queries=["vq"],
            verify_claims=["claim V"],
            conflation_claims=["claim C"],
            convenience_bias_claims=["claim B"],
        )
        progress = _mk_progress(tmp_path)

        await pipeline._run_verification(
            config, state, _as_router(StubRouter()), progress
        )

        assert [v.claim for v in state.verdicts] == ["claim V", "claim C", "claim B"]
        assert all(v.verdict == "unresolved" for v in state.verdicts)
        progress_text = (tmp_path / "progress.md").read_text()
        assert "VERIFY VERDICTS" in progress_text
        assert "3 claims: 3 unresolved" in progress_text

    @pytest.mark.asyncio
    async def test_verdict_node_judges_only_findings_from_verification_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from research_pipeline.models import PlanResult

        config = _mk_config(tmp_path)
        state = _mk_state(config)
        state.plan = PlanResult(sub_questions=["q"], primary_queries=["p"])
        old = Finding(
            fact="old fact",
            source_url="https://old.example",
            source_title="Old",
            confidence=Confidence.HIGH,
        )
        state.findings.append(old)
        state.reassessment = ReassessResult(
            verify_queries=["vq"], verify_claims=["claim V"]
        )
        router = StubRouter(result_batches=[[_sr("https://fresh.example")]])

        async def fake_triage(
            config, results, sub_questions, target_domains, authority_domains=None
        ):
            return [Source(url=r.url, title=r.title, snippet=r.snippet) for r in results]

        new = Finding(
            fact="new fact",
            source_url="https://fresh.example",
            source_title="Fresh",
            confidence=Confidence.HIGH,
        )

        async def fake_read(config, state, router=None):
            state.findings.append(new)

        captured: dict = {}

        async def fake_verdicts(config, claims, new_findings):
            captured["claims"] = claims
            captured["new_findings"] = new_findings
            return [ClaimVerdict(claim=c, verdict="confirmed", evidence=["new fact"]) for c in claims]

        monkeypatch.setattr(pipeline.triage_node, "triage", fake_triage)
        monkeypatch.setattr(pipeline.read_node, "read", fake_read)
        monkeypatch.setattr(pipeline.verify_node, "verify_verdicts", fake_verdicts)

        progress = _mk_progress(tmp_path)
        await pipeline._run_verification(config, state, _as_router(router), progress)

        assert captured["claims"] == ["claim V"]
        # Only the finding added by the verification read — not pre-existing ones
        assert captured["new_findings"] == [new]
        assert [v.verdict for v in state.verdicts] == ["confirmed"]
