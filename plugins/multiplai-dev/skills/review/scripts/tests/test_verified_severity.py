"""The verifier answers a severity, code holds it to what the impact means, and `depends-on-data`.

plan-factory-merged-2026-10-29.md, items 19-21, and Stop/resume rows S12-S14.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import KEYWORD_USE_CITATION, high_finding, medium_finding
from review_pipeline import answers, sdk
from review_pipeline.config import ReviewConfig
from review_pipeline.models import (
    CRITICAL_IMPACTS,
    DEPENDS_ON_DATA,
    IMPACTS,
    ReviewState,
    Verdict,
    VerifierAnswer,
)
from review_pipeline.prompts import verify as prompt
from review_pipeline.prompts.find import SEVERITY_GUIDE
from review_pipeline.render import finding_section, listed
from review_pipeline.stages import RunContext
from review_pipeline.stages.verify import (
    VERIFY_TRIES,
    VerifyIncomplete,
    run_verify,
    verified_severity,
)
from review_pipeline.state import STATE_FILE, load_state, save_state


class Canned:
    """Stands in for `sdk.agent_call_structured`: verify answers consumed in order; an Exception is raised."""

    def __init__(self, answers: list):
        self.answers = answers
        self.calls: list[str] = []

    async def __call__(self, text, schema, *, budget_label="", **kwargs):
        assert budget_label == "verify"
        self.calls.append(text)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return schema.model_validate(answer.model_dump())


def use(monkeypatch, answers: list) -> Canned:
    canned = Canned(answers)
    monkeypatch.setattr(sdk, "agent_call_structured", canned)
    return canned


@pytest.fixture
def ctx(target_info, tmp_path, monkeypatch) -> RunContext:
    monkeypatch.setenv("REVIEW_TRUST_REPO", "1")
    snapshot = tmp_path / "tree"
    snapshot.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    return RunContext(
        config=ReviewConfig(concurrency=1, dimensions=("diff-bugs",)),
        snapshot=snapshot,
        diff=Path(target_info.diff_path).read_text(),
        target_dir=out,
    )


def verdict(
    status: str = "confirmed",
    impact: str = "correctness-only",
    severity: str | None = None,
    topic: str = "code",
) -> Verdict:
    return Verdict(
        status=status,
        impact=impact,
        topic=topic,
        severity=severity,
        reason="it does",
        citations=[KEYWORD_USE_CITATION],
        expected_behaviour="It matches.",
    )


def outcome(state: ReviewState) -> tuple:
    """What the severity pass decides: each finding's severity, the verdicts and `original_severity`."""
    return (
        [(f.id, f.severity) for f in state.findings],
        {k: v.model_dump() for k, v in state.verdicts.items()},
        dict(state.original_severity),
        state.stage,
    )


# --- item 21: the new impact ---------------------------------------------------------


def test_depends_on_data_is_a_critical_impact():
    assert DEPENDS_ON_DATA == "depends-on-data"
    assert DEPENDS_ON_DATA in IMPACTS and DEPENDS_ON_DATA in CRITICAL_IMPACTS
    assert Verdict(status="confirmed", reason="", impact="depends-on-data").impact == (
        "depends-on-data"
    )


def test_critical_mode_lists_depends_on_data_findings():
    rows = [
        {"status": "confirmed", "impact": "depends-on-data"},
        {"status": "confirmed", "impact": "correctness-only"},
    ]
    assert listed(rows, "critical") == rows[:1]


def test_verifier_prompt_defines_depends_on_data(target_info):
    text = " ".join(prompt.build(target_info, high_finding()).split())
    assert '"depends-on-data"' in prompt.SCHEMA
    assert "- `depends-on-data`: default settings reach the path" in text
    assert "Name that data in `reason`." in text


async def test_a_tests_finding_rated_depends_on_data_is_lowered_to_correctness_only(
    target_info, ctx, monkeypatch
):
    use(monkeypatch, [verdict(impact="depends-on-data", topic="tests")])
    state = await run_verify(
        ReviewState(target=target_info, stage="find", findings=[medium_finding()]), ctx
    )
    assert state.verdicts[medium_finding().id].impact == "correctness-only"


# --- item 19: the verifier answers a severity ----------------------------------------


def test_verifier_prompt_asks_for_a_severity_with_the_severity_guide(target_info):
    text = prompt.build(target_info, high_finding())
    assert '"severity": "HIGH" | "MEDIUM" | "LOW"' in text
    assert SEVERITY_GUIDE in text
    assert "set `severity` from the consequence you verified" in text
    assert "do not flag" not in text.lower()


def test_verdict_severity_is_optional_and_normalised():
    assert Verdict(status="confirmed", reason="").severity is None
    assert (
        Verdict(status="confirmed", reason="", severity="medium").severity == "MEDIUM"
    )
    assert Verdict(status="confirmed", reason="", severity="urgent").severity is None
    # An answer without one still parses: the finding keeps the finder's severity.
    answer = VerifierAnswer.model_validate(
        {"status": "confirmed", "reason": "", "impact": "hygiene", "topic": "code"}
    )
    assert answer.severity is None


async def test_the_verifier_severity_replaces_the_finders(
    target_info, ctx, monkeypatch
):
    high, medium = high_finding(), medium_finding()
    use(
        monkeypatch,
        [
            verdict(impact="correctness-only", severity="LOW"),
            verdict(impact="breaks-users", severity="HIGH"),
        ],
    )
    state = await run_verify(
        ReviewState(target=target_info, stage="find", findings=[high, medium]), ctx
    )
    assert [f.severity for f in state.findings] == ["LOW", "HIGH"]
    assert state.original_severity == {high.id: "HIGH", medium.id: "MEDIUM"}
    assert [state.verdicts[f.id].status for f in state.findings] == [
        "confirmed",
        "confirmed",
    ]


async def test_a_confirmed_finding_whose_severity_stays_is_not_recorded(
    target_info, ctx, monkeypatch
):
    use(monkeypatch, [verdict(severity="MEDIUM")])
    state = await run_verify(
        ReviewState(target=target_info, stage="find", findings=[medium_finding()]), ctx
    )
    assert state.findings[0].severity == "MEDIUM" and state.original_severity == {}


def test_a_raised_severity_says_raised_in_the_review():
    row = {
        "severity": "HIGH",
        "file": "a.py",
        "line_start": 1,
        "line_end": 1,
        "claim": "c",
        "status": "confirmed",
        "citations": [],
        "failure_scenario": "s",
    }
    text = finding_section(
        row, web_base=None, head_sha="0" * 40, original_severity="LOW"
    )
    assert "confirmed (raised from LOW)" in text


# --- item 20: code holds the severity to what the impact means -----------------------


@pytest.mark.parametrize(
    ("status", "impact", "given", "finder", "need", "expected"),
    [
        ("confirmed", "hygiene", "HIGH", "HIGH", False, "LOW"),
        ("confirmed", "breaks-users", "LOW", "LOW", False, "MEDIUM"),
        ("confirmed", "depends-on-data", "LOW", "HIGH", False, "MEDIUM"),
        ("confirmed", "breaks-business", "HIGH", "LOW", False, "HIGH"),
        # No other cap: a correctness-only finding may stay HIGH, or rise.
        ("confirmed", "correctness-only", "HIGH", "LOW", False, "HIGH"),
        ("confirmed", "correctness-only", None, "MEDIUM", False, "MEDIUM"),
        # Unverifiable: today's rule after the impact rules.
        ("unverifiable", "breaks-users", "HIGH", "HIGH", False, "MEDIUM"),
        ("unverifiable", "breaks-users", "MEDIUM", "HIGH", False, "LOW"),
        ("unverifiable", "breaks-users", "MEDIUM", "HIGH", True, "MEDIUM"),
        ("unverifiable", "hygiene", "HIGH", "HIGH", True, "LOW"),
    ],
)
def test_verified_severity_rules(status, impact, given, finder, need, expected):
    v = verdict(status=status, impact=impact, severity=given)
    assert verified_severity(finder, v, has_need=need) == expected


async def test_the_impact_rules_change_no_status(target_info, ctx, monkeypatch):
    high, medium = high_finding(), medium_finding()
    use(
        monkeypatch,
        [
            verdict(impact="hygiene", severity="HIGH"),
            verdict(status="unverifiable", impact="breaks-users", severity="LOW"),
        ],
    )
    state = await run_verify(
        ReviewState(target=target_info, stage="find", findings=[high, medium]), ctx
    )
    assert [state.verdicts[f.id].status for f in state.findings] == [
        "confirmed",
        "unverifiable",
    ]
    # breaks-users lifts LOW to MEDIUM, then unverifiable lowers it one step, as today.
    assert [f.severity for f in state.findings] == ["LOW", "LOW"]


def test_answers_settle_the_severity_from_the_finders_and_the_new_verdict(target_info):
    medium = medium_finding()
    state = ReviewState(
        target=target_info,
        stage="export",
        findings=[medium.model_copy(update={"severity": "LOW"})],
        verdicts={medium.id: verdict(severity="HIGH", impact="breaks-users")},
        original_severity={medium.id: "MEDIUM"},
    )
    answers.settle_severity(state, medium.id)
    assert state.findings[0].severity == "HIGH"
    assert state.original_severity == {medium.id: "MEDIUM"}
    state.verdicts[medium.id] = verdict(severity="MEDIUM")
    answers.settle_severity(state, medium.id)
    assert state.findings[0].severity == "MEDIUM" and state.original_severity == {}


# --- Stop/resume rows -----------------------------------------------------------------


def three_answers() -> list[Verdict]:
    return [
        verdict(impact="correctness-only", severity="LOW"),
        verdict(status="unverifiable", impact="breaks-users", severity="HIGH"),
    ]


async def test_s12_stop_while_verifiers_run_asks_only_findings_with_no_verdict(
    target_info, ctx, tmp_path, monkeypatch
):
    """S12: stopped while verifiers run; resume asks only the findings with no verdict, and the
    severity pass runs once, after all verdicts."""
    high, medium = high_finding(), medium_finding()
    first, second = three_answers()

    use(monkeypatch, [first, second])
    clean = await run_verify(
        ReviewState(target=target_info, stage="find", findings=[high, medium]),
        RunContext(
            config=ctx.config, snapshot=ctx.snapshot, diff=ctx.diff, target_dir=None
        ),
    )

    use(monkeypatch, [first, *[sdk.AgentCallError("down")] * VERIFY_TRIES])
    with pytest.raises(VerifyIncomplete):
        await run_verify(
            ReviewState(target=target_info, stage="find", findings=[high, medium]), ctx
        )
    saved = load_state(ctx.target_dir / STATE_FILE)
    assert saved.verdicts[high.id].severity == "LOW" and medium.id not in saved.verdicts
    # The pass has not run: every finding still has the finder's severity.
    assert [f.severity for f in saved.findings] == ["HIGH", "MEDIUM"]
    assert saved.original_severity == {}

    canned = use(monkeypatch, [second])
    resumed = await run_verify(saved, ctx)
    assert len(canned.calls) == 1 and medium.claim in canned.calls[0]
    assert outcome(resumed) == outcome(clean)
    # Run once: the unverifiable HIGH is MEDIUM, not lowered twice.
    assert [f.severity for f in resumed.findings] == ["LOW", "MEDIUM"]


async def test_s13_old_verdicts_without_severity_keep_the_finders_severity(
    target_info, ctx, monkeypatch
):
    """S13: a state saved before R2a, whose verdicts have no `severity`: those findings keep the
    finder's severity, and item 20's rules still apply."""
    high, medium = high_finding(), medium_finding()
    low = medium.with_location(severity="LOW", claim="A low finding about the keyword")
    old = {
        high.id: {"status": "confirmed", "reason": "r", "impact": "correctness-only"},
        medium.id: {"status": "confirmed", "reason": "r", "impact": "hygiene"},
        low.id: {"status": "confirmed", "reason": "r", "impact": "breaks-users"},
    }
    saved = ReviewState(target=target_info, stage="find", findings=[high, medium, low])
    saved.verdicts = {k: Verdict.model_validate(v) for k, v in old.items()}
    save_state(saved, ctx.target_dir)
    state = load_state(ctx.target_dir / STATE_FILE)
    assert all(v.severity is None for v in state.verdicts.values())

    canned = use(monkeypatch, [])
    ctx.config.verify_low = True
    state = await run_verify(state, ctx)
    assert canned.calls == []
    assert [f.severity for f in state.findings] == ["HIGH", "LOW", "MEDIUM"]
    assert state.original_severity == {medium.id: "MEDIUM", low.id: "LOW"}


async def test_s14_stop_during_the_severity_pass_resumes_to_the_same_result(
    target_info, ctx, monkeypatch
):
    """S14: stopped during the severity pass, before the save, with `original_severity` partly
    written: the pass runs again, skips findings already in it, and ends as a run that never
    stopped."""
    high, medium = high_finding(), medium_finding()
    first, second = three_answers()
    verdicts = {high.id: first, medium.id: second}
    for v, f in zip(verdicts.values(), (high, medium), strict=True):
        v.finding_id = f.id

    clean = await run_verify(
        ReviewState(
            target=target_info,
            stage="find",
            findings=[high, medium],
            verdicts=dict(verdicts),
        ),
        ctx,
    )

    # The pass had settled the first finding when it stopped.
    partial = ReviewState(
        target=target_info,
        stage="find",
        findings=[high.model_copy(update={"severity": "LOW"}), medium],
        verdicts=dict(verdicts),
        original_severity={high.id: "HIGH"},
    )
    save_state(partial, ctx.target_dir)
    canned = use(monkeypatch, [])
    resumed = await run_verify(load_state(ctx.target_dir / STATE_FILE), ctx)
    assert canned.calls == []
    assert outcome(resumed) == outcome(clean)
