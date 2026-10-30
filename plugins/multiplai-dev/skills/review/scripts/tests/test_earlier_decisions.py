"""`--earlier-decisions`: findings an earlier reviewer of the change decided on (plan
factory-merged-2026-10-29, item 36, stop rows S31-S33).

Every agent call is replaced. All data is made up.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import KEYWORD_USE_CITATION, SCHEMA
from review_pipeline import orchestrator, sdk
from review_pipeline.__main__ import main
from review_pipeline.config import ReviewConfig
from review_pipeline.export import to_findings_file
from review_pipeline.models import (
    EarlierDecision,
    EarlierMatch,
    EarlierMatchesOutput,
    Finding,
    ReviewState,
    Verdict,
)
from review_pipeline.prompts import repeats as repeats_prompt
from review_pipeline.render import listed, render_review
from review_pipeline.stages import RunContext
from review_pipeline.stages.repeats import load_earlier_decisions, run_repeats
from review_pipeline.state import load_state, save_state

WHEN = datetime(2026, 1, 1, tzinfo=UTC)


class Fake:
    """Stands in for sdk.agent_call_structured: answers in order, records each prompt."""

    def __init__(self, answers: list):
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, prompt, schema, *, budget_label="", **kwargs):
        self.calls.append((budget_label, prompt))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def fake(monkeypatch):
    def install(*answers) -> Fake:
        f = Fake(list(answers))
        monkeypatch.setattr(sdk, "agent_call_structured", f)
        return f

    return install


def decision(
    did: str = "B03-R1-F1",
    kind: str = "rejected",
    reason: str = "the caller checks the role first",
) -> EarlierDecision:
    return EarlierDecision(
        id=did,
        file="settings.py",
        line_start=3,
        line_end=3,
        claim="rate plans match a fixed word",
        decision=kind,
        reason=reason,
    )


def ctx_with(tmp_path: Path, decisions: list[EarlierDecision]) -> RunContext:
    snapshot = tmp_path / "tree"
    snapshot.mkdir(exist_ok=True)
    return RunContext(
        config=ReviewConfig(earlier_decisions=decisions),
        snapshot=snapshot,
        diff="",
        target_dir=tmp_path / "out" / "t",
    )


def finding(claim: str, *, line: int = 3, severity: str = "MEDIUM") -> Finding:
    return Finding(
        claim=claim,
        severity=severity,
        dimension="correctness",
        file="settings.py",
        line_start=line,
        line_end=line,
        failure_scenario=f"{claim}: fails",
        citations=[KEYWORD_USE_CITATION],
        finder="correctness",
        finders=["correctness"],
    )


def state_with(target_info, *findings: Finding, stage: str = "merge") -> ReviewState:
    return ReviewState(
        target=target_info,
        stage=stage,
        findings=list(findings),
        verdicts={
            f.id: Verdict(
                finding_id=f.id,
                status="confirmed",
                reason=f"r {f.claim}",
                impact="correctness-only",
                topic="code",
            )
            for f in findings
        },
    )


def two(target_info) -> tuple[ReviewState, Finding, Finding]:
    same = finding("rate plans match a fixed word")
    other = finding("the cache never expires", line=1)
    return state_with(target_info, same, other), same, other


def answer_for(f: Finding, did: str = "B03-R1-F1") -> EarlierMatchesOutput:
    return EarlierMatchesOutput(
        matches=[EarlierMatch(id=f.id, decision_id=did, reason="the same fixed word")]
    )


def outcome(state: ReviewState) -> tuple:
    return (
        {k: v.model_dump() for k, v in state.earlier_decisions.items()},
        sorted(state.earlier_checked),
        state.repeats,
        state.stage,
        state.errors,
        to_findings_file(state, generated_at=WHEN)["findings"],
    )


async def never_stopped(target_info, tmp_path, fake) -> ReviewState:
    state, same, _ = two(target_info)
    fake(answer_for(same))
    return await run_repeats(state, ctx_with(tmp_path, [decision()]))


# --- the file ----------------------------------------------------------------------


def test_a_good_file_loads(tmp_path):
    path = tmp_path / "d.json"
    path.write_text(json.dumps([decision().model_dump()]))
    assert load_earlier_decisions(path) == [decision()]


@pytest.mark.parametrize(
    "content, says",
    [
        ("not json", "is not JSON"),
        ('{"id": "x"}', "the top level"),
        (json.dumps([{**decision().model_dump(), "decision": "fixed"}]), "decision"),
        (json.dumps([{**decision().model_dump(), "extra": 1}]), "extra"),
        (json.dumps([{**decision().model_dump(), "claim": ""}]), "claim"),
        (json.dumps([decision().model_dump(), decision().model_dump()]), "twice"),
    ],
)
def test_a_bad_file_names_what_is_wrong(tmp_path, content, says):
    path = tmp_path / "d.json"
    path.write_text(content)
    with pytest.raises(ValueError, match=says):
        load_earlier_decisions(path)


def test_a_bad_file_ends_review_and_resume_with_exit_2(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text('[{"id": "x"}]')

    async def never(*args, **kwargs):
        raise AssertionError("no review runs on a bad file")

    monkeypatch.setattr(orchestrator, "resume", never)
    target_dir = tmp_path / "out" / "proj" / "pr-12"
    target_dir.mkdir(parents=True)
    code = main(
        ["resume", str(target_dir), "--trust-repo", "--earlier-decisions", str(bad)]
    )
    assert code == 2
    assert "--earlier-decisions" in capsys.readouterr().err


def test_resume_reads_the_file_into_the_config(tmp_path, monkeypatch):
    good = tmp_path / "d.json"
    good.write_text(json.dumps([decision().model_dump()]))
    seen = {}

    async def capture(target_dir, config, **kwargs):
        seen["config"] = config
        raise orchestrator.ReviewError("stop here")

    monkeypatch.setattr(orchestrator, "resume", capture)
    target_dir = tmp_path / "out" / "proj" / "pr-12"
    target_dir.mkdir(parents=True)
    main(["resume", str(target_dir), "--trust-repo", "--earlier-decisions", str(good)])
    assert seen["config"].earlier_decisions == [decision()]


# --- matching ----------------------------------------------------------------------


async def test_no_file_asks_nothing(target_info, tmp_path, fake):
    state, _, _ = two(target_info)
    calls = fake()
    state = await run_repeats(state, ctx_with(tmp_path, []))
    assert calls.calls == [] and state.earlier_decisions == {}
    assert state.earlier_checked == [] and state.stage == "repeats"


async def test_an_equal_id_matches_with_no_agent_call(target_info, tmp_path, fake):
    state, same, other = two(target_info)
    calls = fake(EarlierMatchesOutput())
    state = await run_repeats(state, ctx_with(tmp_path, [decision(did=same.id)]))
    m = state.earlier_decisions[same.id]
    assert (m.id, m.by, m.decision) == (same.id, "id", "rejected")
    # Only the finding with no id match is asked about.
    ((_, prompt),) = calls.calls
    assert other.id in prompt and same.id not in prompt.split("earlier decisions")[0]


async def test_an_agent_match_is_kept_listed_and_not_a_repeat(matched):
    state = matched
    same = state.findings[0]
    m = state.earlier_decisions[same.id]
    assert (m.id, m.by, m.decision, m.reason, m.match_reason) == (
        "B03-R1-F1",
        "agent",
        "rejected",
        "the caller checks the role first",
        "the same fixed word",
    )
    assert state.repeats == {}
    data = to_findings_file(state, generated_at=WHEN)
    row = next(r for r in data["findings"] if r["id"] == same.id)
    assert row["earlier_decision"] == {
        "id": "B03-R1-F1",
        "decision": "rejected",
        "reason": "the caller checks the role first",
    }
    assert "assessment" not in row or row["assessment"]["label"] != "repeat"
    assert same.id in {r["id"] for r in listed(data["findings"])}
    other = next(r for r in data["findings"] if r["id"] != same.id)
    assert "earlier_decision" not in other


@pytest.fixture
async def matched(target_info, tmp_path, fake) -> ReviewState:
    return await never_stopped(target_info, tmp_path, fake)


def test_findings_json_with_an_earlier_decision_validates(matched):
    import jsonschema

    state = matched
    jsonschema.validate(
        to_findings_file(state, generated_at=WHEN), json.loads(SCHEMA.read_text())
    )


def test_the_review_markdown_shows_the_earlier_decision(matched):
    text = render_review(matched)
    assert (
        "**Earlier decision:** `B03-R1-F1` rejected: the caller checks the role first"
        in text
    )


async def test_a_bad_match_is_dropped_with_an_error(target_info, tmp_path, fake):
    state, same, _ = two(target_info)
    fake(answer_for(same, did="B09-R9-F9"))
    state = await run_repeats(state, ctx_with(tmp_path, [decision()]))
    assert state.earlier_decisions == {}
    assert state.errors == [
        f"earlier decisions: dropped a match: {same.id}: B09-R9-F9 is not a known earlier decision"
    ]


async def test_a_failed_call_leaves_every_finding_unmatched(
    target_info, tmp_path, fake
):
    state, _, _ = two(target_info)
    fake(sdk.AgentCallError("timeout\ntrace"))
    state = await run_repeats(state, ctx_with(tmp_path, [decision()]))
    assert state.earlier_decisions == {}
    assert state.errors == ["earlier decisions: timeout"]


def test_the_prompt_holds_both_lists_and_no_instruction_to_leave_findings_out(
    target_info,
):
    state, same, _ = two(target_info)
    text = repeats_prompt.build_earlier(
        target_info, state.findings, [decision(kind="accepted-unfixed")]
    )
    assert same.id in text and "B03-R1-F1" in text and "accepted-unfixed" in text
    assert "do not flag" not in text.lower() and "do not report" not in text.lower()


# --- stop and resume rows ------------------------------------------------------------


async def test_s31_stopped_after_the_match_call_before_the_save_asks_again_once(
    target_info, tmp_path, fake
):
    expected = outcome(await never_stopped(target_info, tmp_path, fake))
    state, same, _ = two(target_info)
    ctx = ctx_with(tmp_path, [decision()])
    ctx.target_dir.mkdir(parents=True, exist_ok=True)
    # The checkpoint the orchestrator saved after merge; the stage then runs and is killed
    # after its answer arrives, before the checkpoint after repeats is written.
    path = save_state(state, ctx.target_dir)
    calls = fake(answer_for(same))
    await run_repeats(load_state(path), ctx)
    assert len(calls.calls) == 1
    assert load_state(path).earlier_checked == []
    calls = fake(answer_for(same))
    resumed = await run_repeats(load_state(path), ctx)
    assert len(calls.calls) == 1  # asks again, once
    assert outcome(resumed) == expected


async def test_s32_stopped_after_the_save_makes_no_call(target_info, tmp_path, fake):
    done = await never_stopped(target_info, tmp_path, fake)
    expected = outcome(done)
    ctx = ctx_with(tmp_path, [decision()])
    ctx.target_dir.mkdir(parents=True, exist_ok=True)
    path = save_state(done, ctx.target_dir)
    calls = fake()
    resumed = await run_repeats(load_state(path), ctx)
    assert calls.calls == []
    assert outcome(resumed) == expected
    # A checkpoint whose ids were all asked about also asks nothing, whatever its stage.
    again = load_state(path)
    again.stage = "merge"
    resumed = await run_repeats(again, ctx)
    assert calls.calls == []
    assert outcome(resumed) == expected


async def test_s33_a_changed_file_on_resume_keeps_the_saved_matches(
    target_info, tmp_path, fake
):
    done = await never_stopped(target_info, tmp_path, fake)
    expected = outcome(done)
    ctx = ctx_with(tmp_path, [decision()])
    ctx.target_dir.mkdir(parents=True, exist_ok=True)
    path = save_state(done, ctx.target_dir)
    changed = ctx_with(
        tmp_path, [decision(did="B07-R2-X1", kind="accepted-unfixed", reason="new")]
    )
    calls = fake()
    resumed = await run_repeats(load_state(path), changed)
    assert calls.calls == []
    assert outcome(resumed) == expected
    # The next review round starts from a fresh state and reads the new file.
    state, same, _ = two(target_info)
    calls = fake(answer_for(same, did="B07-R2-X1"))
    next_round = await run_repeats(state, changed)
    assert len(calls.calls) == 1
    assert next_round.earlier_decisions[same.id].id == "B07-R2-X1"
    assert next_round.earlier_decisions[same.id].decision == "accepted-unfixed"
