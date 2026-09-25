"""Each stage with `agent_call_structured` replaced by canned answers."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    DEF_CITATION, KEYWORD_CITATION, KEYWORD_USE_CITATION, USE_CITATION, cite, high_finding, medium_finding,
    verified_fix,
)
from review_pipeline import sdk
from review_pipeline.config import ReviewConfig
from review_pipeline.models import (
    NO_VERIFIED_FIX, FinderOutput, Fix, FixCheck, Premise, ReviewState, Verdict,
)
from review_pipeline.stages import RunContext
from review_pipeline.stages.check_fix import run_check_fix
from review_pipeline.stages.find import conventions_chain, run_find
from review_pipeline.stages.prescribe import run_prescribe
from review_pipeline.stages.verify import run_verify


class Canned:
    """Stands in for sdk.agent_call_structured. `answers[label]` is a list
    consumed in order; an Exception in it is raised."""

    def __init__(self, answers: dict[str, list]):
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, prompt, schema, *, budget_label="", **kwargs):
        self.calls.append((budget_label, prompt))
        assert kwargs["allowed_tools"] == ["Read", "Grep", "Glob"]
        answer = self.answers[budget_label].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return schema.model_validate(answer.model_dump()) if hasattr(answer, "model_dump") else answer


@pytest.fixture
def ctx(target_info, tmp_path) -> RunContext:
    snapshot = tmp_path / "tree"
    snapshot.mkdir()
    return RunContext(config=ReviewConfig(concurrency=2, dimensions=("diff-bugs", "callers")),
                      snapshot=snapshot, diff=Path(target_info.diff_path).read_text())


def use(monkeypatch, answers) -> Canned:
    canned = Canned(answers)
    monkeypatch.setattr(sdk, "agent_call_structured", canned)
    return canned


# --- find ------------------------------------------------------------------------


async def test_find_dedupes_gates_and_normalises_paths(target_info, ctx, monkeypatch):
    high = high_finding()
    absolute = high.with_location(file=str(ctx.snapshot / "rateplan_service.py"),
                                  citations=[c.model_copy(update={"path": f"./{c.path}"}).model_dump()
                                             for c in high.citations])
    bad = medium_finding().with_location(citations=[cite("rateplan_service.py", 2, "KEYWORD = 'dolcebot'").model_dump()])
    canned = use(monkeypatch, {
        "find:diff-bugs": [FinderOutput(findings=[absolute, bad])],
        "find:callers": [FinderOutput(findings=[high])],  # duplicate of the first
    })
    state = await run_find(ReviewState(target=target_info), ctx)
    assert [f.id for f in state.findings] == [high.id]
    assert state.findings[0].file == "rateplan_service.py"
    assert state.findings[0].citations[0].path == "rateplan_service.py"
    assert state.findings[0].finder == "diff-bugs"
    assert len(state.rejected) == 1 and "quote not at cited lines" in state.rejected[0].reason
    assert state.stage == "find" and len(canned.calls) == 2


async def test_find_survives_one_failed_finder_but_not_all(target_info, ctx, monkeypatch):
    use(monkeypatch, {"find:diff-bugs": [sdk.AgentCallError("x")], "find:callers": [FinderOutput(findings=[])]})
    state = await run_find(ReviewState(target=target_info), ctx)
    assert state.stage == "find" and state.errors and "finder diff-bugs" in state.errors[0]

    use(monkeypatch, {"find:diff-bugs": [sdk.AgentCallError("x")], "find:callers": [sdk.AgentCallError("y")]})
    with pytest.raises(sdk.AgentCallError):
        await run_find(ReviewState(target=target_info), ctx)


async def test_trust_error_escapes(target_info, ctx, monkeypatch):
    use(monkeypatch, {"find:diff-bugs": [sdk.RepoTrustError("no")], "find:callers": [FinderOutput()]})
    with pytest.raises(sdk.RepoTrustError):
        await run_find(ReviewState(target=target_info), ctx)


async def test_stage_already_done_makes_no_calls(target_info, ctx, monkeypatch):
    canned = use(monkeypatch, {})
    state = ReviewState(target=target_info, stage="check_fix")
    for fn in (run_find, run_verify, run_prescribe, run_check_fix):
        assert await fn(state, ctx) is state
    assert canned.calls == []


def test_conventions_chain_reads_claude_md_at_head(target_info):
    assert conventions_chain(target_info) == ""  # the fixture has none


# --- verify ----------------------------------------------------------------------


async def test_verify_downgrades_uncited_confirmation_and_keeps_refuted(target_info, ctx, monkeypatch):
    high, medium = high_finding(), medium_finding()
    use(monkeypatch, {"verify": [
        Verdict(status="confirmed", reason="looks right"),  # no citation -> unverifiable
        Verdict(status="refuted", reason="line 6 handles it", citations=[KEYWORD_USE_CITATION]),
    ]})
    ctx.config.concurrency = 1  # answers are consumed in finding order
    state = await run_verify(ReviewState(target=target_info, stage="find", findings=[high, medium]), ctx)
    assert state.verdicts[high.id].status == "unverifiable"
    assert "without citing" in state.verdicts[high.id].reason
    assert state.findings[0].severity == "MEDIUM" and state.original_severity[high.id] == "HIGH"
    assert state.verdicts[medium.id].status == "refuted"
    assert ctx.counts == {"confirmed": 0, "refuted": 1, "unverifiable": 1}


async def test_verify_keeps_a_grounded_confirmation(target_info, ctx, monkeypatch):
    high = high_finding()
    use(monkeypatch, {"verify": [Verdict(status="confirmed", reason="r", citations=[KEYWORD_CITATION])]})
    state = await run_verify(ReviewState(target=target_info, stage="find", findings=[high]), ctx)
    assert state.verdicts[high.id].status == "confirmed" and state.findings[0].severity == "HIGH"


async def test_verifier_failure_is_unverifiable(target_info, ctx, monkeypatch):
    high = high_finding()
    use(monkeypatch, {"verify": [sdk.AgentCallError("timeout")]})
    state = await run_verify(ReviewState(target=target_info, stage="find", findings=[high]), ctx)
    assert state.verdicts[high.id].status == "unverifiable"


# --- prescribe -------------------------------------------------------------------


def _confirmed_state(target_info) -> ReviewState:
    high = high_finding()
    return ReviewState(target=target_info, stage="verify", findings=[high],
                       verdicts={high.id: Verdict(finding_id=high.id, status="confirmed", reason="r",
                                                  citations=[KEYWORD_CITATION])})


def _definition_fix() -> Fix:
    """The DB2038 mistake: derive the keyword from CHANNEX_OC_OTA_NAME, citing its definition."""
    return Fix(description="Use settings.CHANNEX_OC_OTA_NAME as the keyword", premises=[
        Premise(statement="CHANNEX_OC_OTA_NAME is the channel title", kind="in_repo",
                symbol="CHANNEX_OC_OTA_NAME", citation=DEF_CITATION)])


async def test_prescribe_reasks_once_with_the_gate_reason(target_info, ctx, monkeypatch):
    state = _confirmed_state(target_info)
    fid = state.findings[0].id
    canned = use(monkeypatch, {"prescribe": [_definition_fix(), verified_fix(fid)]})
    state = await run_prescribe(state, ctx)
    assert len(canned.calls) == 2
    assert "premise cites the definition of CHANNEX_OC_OTA_NAME" in canned.calls[1][1]
    assert state.fixes[fid].description.startswith("Make the keyword a setting")
    assert ctx.counts["gate_rejects"] == 1


async def test_prescribe_gives_up_after_two_gate_failures(target_info, ctx, monkeypatch):
    state = _confirmed_state(target_info)
    fid = state.findings[0].id
    use(monkeypatch, {"prescribe": [_definition_fix(), _definition_fix()]})
    state = await run_prescribe(state, ctx)
    fix = state.fixes[fid]
    assert fix.description == NO_VERIFIED_FIX and fix.premises == []
    assert "definition of CHANNEX_OC_OTA_NAME" in fix.open_questions[0]


async def test_prescribe_only_confirmed(target_info, ctx, monkeypatch):
    state = _confirmed_state(target_info)
    state.verdicts[state.findings[0].id].status = "unverifiable"
    canned = use(monkeypatch, {"prescribe": []})
    state = await run_prescribe(state, ctx)
    assert canned.calls == [] and state.fixes == {}


# --- check_fix -------------------------------------------------------------------


async def test_check_fix_drops_a_refuted_fix(target_info, ctx, monkeypatch):
    state = _confirmed_state(target_info)
    fid = state.findings[0].id
    state.fixes[fid] = verified_fix(fid)
    state.stage = "prescribe"
    canned = use(monkeypatch, {"check_fix": [FixCheck(status="refuted", reason="revision_ingest.py:170 compares ota_name")]})
    state = await run_check_fix(state, ctx)
    assert "direct_booking.py:6" in canned.calls[0][1]  # consumers are handed to the checker
    assert state.fixes[fid].description == NO_VERIFIED_FIX
    assert "revision_ingest.py:170" in state.fixes[fid].open_questions[0]
    assert state.fix_checks[fid].status == "refuted"


async def test_check_fix_keeps_a_confirmed_fix_and_drops_an_unchecked_one(target_info, ctx, monkeypatch):
    state = _confirmed_state(target_info)
    fid = state.findings[0].id
    state.fixes[fid] = verified_fix(fid)
    state.stage = "prescribe"
    use(monkeypatch, {"check_fix": [FixCheck(status="confirmed", reason="nothing else reads KEYWORD")]})
    kept = await run_check_fix(state.model_copy(deep=True), ctx)
    assert kept.fixes[fid].description != NO_VERIFIED_FIX

    use(monkeypatch, {"check_fix": [sdk.AgentCallError("timeout")]})
    dropped = await run_check_fix(state.model_copy(deep=True), ctx)
    assert dropped.fixes[fid].description == NO_VERIFIED_FIX


async def test_premise_paths_are_normalised(target_info, ctx, monkeypatch):
    state = _confirmed_state(target_info)
    fid = state.findings[0].id
    fix = verified_fix(fid)
    fix.premises[0].citation = USE_CITATION.model_copy(update={"path": str(ctx.snapshot / "direct_booking.py")})
    use(monkeypatch, {"prescribe": [fix]})
    state = await run_prescribe(state, ctx)
    assert state.fixes[fid].premises[0].citation.path == "direct_booking.py"
