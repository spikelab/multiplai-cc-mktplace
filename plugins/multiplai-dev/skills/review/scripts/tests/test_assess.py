"""repeats and assess: earlier rounds, the person's decisions, labels and their gates.

Every agent call is replaced. All data is made up.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import KEYWORD_CITATION, KEYWORD_USE_CITATION, SCHEMA, cite, high_finding, medium_finding
from review_pipeline import rounds, sdk
from review_pipeline.config import ReviewConfig
from review_pipeline.export import _finding, to_findings_file
from review_pipeline.models import (AssessItem, AssessOutput, DuplicateSet, Finding, RepeatMatch, RepeatsOutput,
                                    ReviewState, Verdict)
from review_pipeline.render import render_review, render_summary
from review_pipeline.stages import RunContext
from review_pipeline.stages.assess import gate_assessments, run_assess
from review_pipeline.stages.repeats import gate_matches, run_repeats
from review_pipeline.state import load_state, save_state

OLD_HEAD = "1" * 40
OLDER_HEAD = "2" * 40


class Fake:
    """Stands in for sdk.agent_call_structured: answers by budget label, records prompts."""

    def __init__(self, answers: dict[str, list]):
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, prompt, schema, *, budget_label="", allowed_tools=(), **kwargs):
        assert list(allowed_tools) == ["Read", "Grep", "Glob"]  # no web for repeats or assess
        self.calls.append((budget_label, prompt))
        answer = self.answers[budget_label].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def fake(monkeypatch):
    def install(**answers) -> Fake:
        f = Fake(answers)
        monkeypatch.setattr(sdk, "agent_call_structured", f)
        return f
    return install


@pytest.fixture
def ctx(target_info, tmp_path) -> RunContext:
    snapshot = tmp_path / "tree"
    snapshot.mkdir()
    return RunContext(config=ReviewConfig(), snapshot=snapshot, diff="", target_dir=tmp_path / "out" / "t")


def at(claim: str, *citations, file: str = "rateplan_service.py", line: int = 1, severity: str = "LOW") -> Finding:
    return Finding(claim=claim, severity=severity, dimension="tests", file=file, line_start=line, line_end=line,
                   failure_scenario=f"{claim}: fails", citations=list(citations) or [KEYWORD_CITATION],
                   finder="tests", finders=["tests"])


def state_with(target_info, *findings: Finding, stage: str = "merge") -> ReviewState:
    return ReviewState(target=target_info, stage=stage, findings=list(findings),
                       verdicts={f.id: Verdict(finding_id=f.id, status="confirmed", reason=f"r {f.claim}")
                                 for f in findings})


def write_round(target_dir: Path, head: str, findings: list[Finding], *, when: str = "2026-01-01T00:00:00Z") -> None:
    d = target_dir / "rounds" / head[:12]
    d.mkdir(parents=True, exist_ok=True)
    rows = [_finding(f, "confirmed", "r", "e") for f in findings]
    (d / "findings.json").write_text(json.dumps({"schema_version": 1, "generated_at": when, "producer": "test",
                                                 "target": {"head_sha": head}, "findings": rows}))


def decide(target_dir: Path, **by_id: tuple[str, str]) -> None:
    path = target_dir / "viewer" / "decisions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text()) if path.exists() else {}
    for fid, (decision, note) in by_id.items():
        data[fid] = {"decision": decision, "note": note, "ts": "2026-01-01T00:00:00Z"}
    path.write_text(json.dumps(data))


# --- rounds ----------------------------------------------------------------------


def test_load_rounds_keeps_the_latest_copy_of_each_id_with_its_decision(tmp_path):
    a, b = at("a"), at("b")
    write_round(tmp_path, OLDER_HEAD, [a], when="2026-01-01T00:00:00Z")
    write_round(tmp_path, OLD_HEAD, [a, b], when="2026-01-02T00:00:00Z")
    decide(tmp_path, **{a.id: ("reject", "not a problem here"), b.id: ("accept", "")})
    earlier = {e.id: e for e in rounds.load_rounds(tmp_path)}
    assert earlier[a.id].head_sha == OLD_HEAD and earlier[a.id].note == "not a problem here"
    assert [e.id for e in rounds.rejected(list(earlier.values()))] == [a.id]
    assert [e.id for e in rounds.accepted_or_open(list(earlier.values()))] == [b.id]
    # A round on the current head counts: a rerun keeps one to compare with.
    assert {e.head_sha for e in rounds.load_rounds(tmp_path, current_head=OLD_HEAD)} == {OLD_HEAD}


def test_a_rerun_on_the_same_head_keeps_each_round_in_its_own_directory(tmp_path):
    def finish(when: str) -> None:
        (tmp_path / "findings.json").write_text(json.dumps({"generated_at": when, "target": {"head_sha": OLD_HEAD},
                                                            "findings": []}))
    finish("2026-01-01T00:00:00Z")
    first = rounds.keep_round(tmp_path, "t", OLD_HEAD)
    assert first == tmp_path / "rounds" / f"01-{OLD_HEAD[:12]}"
    assert rounds.keep_round(tmp_path, "t", OLD_HEAD) == first  # started again before it finished: one copy
    finish("2026-01-02T00:00:00Z")
    second = rounds.keep_round(tmp_path, "t", OLD_HEAD)
    assert second == tmp_path / "rounds" / f"02-{OLD_HEAD[:12]}"
    assert json.loads((first / "findings.json").read_text())["generated_at"] == "2026-01-01T00:00:00Z"
    assert json.loads((second / "findings.json").read_text())["generated_at"] == "2026-01-02T00:00:00Z"
    finish("2026-01-03T00:00:00Z")
    assert rounds.keep_round(tmp_path, "t", "3" * 40) == tmp_path / "rounds" / f"03-{OLD_HEAD[:12]}"


def test_a_same_head_rerun_reads_the_decisions_made_on_the_round_it_replaces(tmp_path):
    a = at("decided while the last run was shown")
    write_round(tmp_path, OLD_HEAD, [a], when="2026-01-01T00:00:00Z")
    # The rerun has not exported: findings.json is still the kept round's.
    (tmp_path / "findings.json").write_text(json.dumps({"generated_at": "2026-01-01T00:00:00Z",
                                                        "target": {"head_sha": OLD_HEAD}, "findings": []}))
    _decisions(tmp_path, **{a.id: ("reject", "2026-01-01T09:00:00Z")})
    assert [e.decision for e in rounds.load_rounds(tmp_path, OLD_HEAD)] == ["reject"]
    # Once the rerun has exported, a decision made on its findings is not read back as the earlier round's.
    (tmp_path / "findings.json").write_text(json.dumps({"generated_at": "2026-01-01T06:00:00Z",
                                                        "target": {"head_sha": OLD_HEAD}, "findings": []}))
    assert [e.decision for e in rounds.load_rounds(tmp_path, OLD_HEAD)] == [""]


def test_a_repeat_carries_the_earlier_rejection_onto_its_own_id(tmp_path):
    reworded = at("the same defect, worded another way")
    d = tmp_path / "rounds" / OLD_HEAD[:12]
    d.mkdir(parents=True)
    row = {**_finding(reworded, "confirmed", "r", "e"),
           "assessment": {"label": "repeat", "reason": "same", "earlier_id": "abcdef0123",
                          "earlier_round": OLDER_HEAD, "earlier_decision": "reject", "earlier_note": "by design"}}
    (d / "findings.json").write_text(json.dumps({"generated_at": "2026-01-02T00:00:00Z",
                                                 "target": {"head_sha": OLD_HEAD}, "findings": [row]}))
    (e,) = rounds.load_rounds(tmp_path)
    assert (e.id, e.decision, e.note) == (reworded.id, "reject", "by design")
    # The person's own decision on the new id wins.
    _decisions(tmp_path, **{reworded.id: ("accept", "2026-01-01T00:00:00Z")})
    assert rounds.load_rounds(tmp_path)[0].decision == "accept"


def test_keep_round_never_overwrites_a_round_already_kept(tmp_path):
    kept = tmp_path / "rounds" / OLD_HEAD[:12]
    kept.mkdir(parents=True)
    (kept / "findings.json").write_text('{"original": true}')
    (tmp_path / "findings.json").write_text(json.dumps({"generated_at": "2026-01-03T00:00:00Z",
                                                        "target": {"head_sha": OLD_HEAD}, "findings": []}))
    (tmp_path / "review-t.md").write_text("# review")
    assert rounds.keep_round(tmp_path, "t", "3" * 40) == tmp_path / "rounds" / f"02-{OLD_HEAD[:12]}"
    assert (kept / "findings.json").read_text() == '{"original": true}'
    assert (tmp_path / "rounds" / f"02-{OLD_HEAD[:12]}" / "review-t.md").read_text() == "# review"  # a file not kept yet is still copied


def _decisions(target_dir: Path, **by_id: tuple[str, str]) -> None:
    path = target_dir / "viewer" / "decisions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({fid: {"decision": d, "note": "", "ts": ts} for fid, (d, ts) in by_id.items()}))


def test_a_decision_made_while_a_later_round_was_shown_is_not_read_back_as_earlier(tmp_path):
    current_head = "3" * 40
    early, late, middle = at("decided in round one"), at("decided in the current round"), at("decided in round two")
    write_round(tmp_path, OLDER_HEAD, [middle], when="2026-01-01T00:00:00Z")
    write_round(tmp_path, OLD_HEAD, [early, late], when="2026-01-02T00:00:00Z")
    (tmp_path / "findings.json").write_text(json.dumps({"generated_at": "2026-01-03T00:00:00Z",
                                                        "target": {"head_sha": current_head}, "findings": []}))
    _decisions(tmp_path, **{early.id: ("reject", "2026-01-02T12:00:00Z"),
                            late.id: ("reject", "2026-01-03T09:00:00Z"),
                            middle.id: ("reject", "2026-01-01T18:00:00Z")})
    got = {e.id: e.decision for e in rounds.load_rounds(tmp_path, current_head)}
    assert got == {early.id: "reject", late.id: "", middle.id: "reject"}
    # Round one's copy of a finding decided only in round two's time does not count for round one.
    _decisions(tmp_path, **{middle.id: ("reject", "2026-01-02T06:00:00Z"), late.id: ("reject", "2026-01-03T09:00:00Z")})
    assert {e.id: e.decision for e in rounds.load_rounds(tmp_path, current_head)}[middle.id] == ""
    # A run that has not exported yet (findings.json is still the last round's): no limit on the last round.
    (tmp_path / "findings.json").write_text(json.dumps({"generated_at": "2026-01-02T00:00:00Z",
                                                        "target": {"head_sha": OLD_HEAD}, "findings": []}))
    assert {e.id: e.decision for e in rounds.load_rounds(tmp_path, current_head)}[late.id] == "reject"


# --- repeats ---------------------------------------------------------------------


async def test_an_exact_id_match_is_a_repeat_with_no_agent_call(target_info, ctx, fake):
    same = at("the keyword is a literal")
    write_round(ctx.target_dir, OLD_HEAD, [same])
    decide(ctx.target_dir, **{same.id: ("reject", "intended")})
    calls = fake()
    state = await run_repeats(state_with(target_info, same), ctx)
    assert calls.calls == []
    r = state.repeats[same.id]
    assert (r.by, r.rejected_id, r.round, r.note) == ("id", same.id, OLD_HEAD, "intended")
    assert state.stage == "repeats"


async def test_a_reworded_match_from_the_agent_is_kept(target_info, ctx, fake):
    earlier = at("the keyword is a literal")
    reworded = at("rate plans match a fixed word", KEYWORD_USE_CITATION, file="settings.py", line=3, severity="MEDIUM")
    write_round(ctx.target_dir, OLD_HEAD, [earlier])
    decide(ctx.target_dir, **{earlier.id: ("reject", "intended")})
    calls = fake(repeats=[RepeatsOutput(matches=[RepeatMatch(id=reworded.id, rejected_id=earlier.id,
                                                             reason="the same literal keyword")])])
    state = await run_repeats(state_with(target_info, reworded), ctx)
    (label, text), = calls.calls
    assert label == "repeats" and reworded.id in text and earlier.id in text and "intended" in text
    assert state.repeats[reworded.id].rejected_id == earlier.id and state.repeats[reworded.id].by == "agent"


def test_repeat_gate_drops_each_bad_match():
    good = RepeatMatch(id="a", rejected_id="x", reason="same defect")
    kept, dropped = gate_matches([
        RepeatMatch(id="zzz", rejected_id="x", reason="r"),      # not a shown finding
        RepeatMatch(id="b", rejected_id="nope", reason="r"),     # not on the rejected list
        good,
        RepeatMatch(id="a", rejected_id="y", reason="r"),        # a second match for a
        RepeatMatch(id="c", rejected_id="x", reason="  "),       # no reason
    ], {"a", "b", "c"}, {"x", "y"})
    assert kept == [good]
    assert len(dropped) == 4
    assert any("not a finding of this round" in d for d in dropped)
    assert any("not a rejected earlier finding" in d for d in dropped)
    assert any("more than one" in d for d in dropped)
    assert any("no reason" in d for d in dropped)


async def test_dropped_matches_are_listed_in_errors(target_info, ctx, fake):
    earlier, now = at("earlier"), at("now", file="settings.py", line=3)
    write_round(ctx.target_dir, OLD_HEAD, [earlier])
    decide(ctx.target_dir, **{earlier.id: ("reject", "")})
    fake(repeats=[RepeatsOutput(matches=[RepeatMatch(id=now.id, rejected_id="0000000000", reason="r")])])
    state = await run_repeats(state_with(target_info, now), ctx)
    assert state.repeats == {}
    assert state.errors == [f"repeats: dropped a match: {now.id}: 0000000000 is not a rejected earlier finding"]


async def test_no_rejected_findings_skips_the_stage(target_info, ctx, fake):
    accepted = at("accepted")
    write_round(ctx.target_dir, OLD_HEAD, [accepted])
    decide(ctx.target_dir, **{accepted.id: ("accept", "")})
    calls = fake()
    state = await run_repeats(state_with(target_info, at("new", file="settings.py")), ctx)
    assert calls.calls == [] and state.repeats == {} and state.stage == "repeats"


async def test_a_resumed_repeats_stage_does_not_ask_again(target_info, ctx, fake):
    earlier, now = at("earlier"), at("now", file="settings.py", line=3)
    write_round(ctx.target_dir, OLD_HEAD, [earlier])
    decide(ctx.target_dir, **{earlier.id: ("reject", "")})
    fake(repeats=[RepeatsOutput()])
    state = await run_repeats(state_with(target_info, now), ctx)
    assert state.repeats_checked == [now.id]
    state.stage = "merge"  # as a resume that stopped before the checkpoint after repeats
    calls = fake()
    state = await run_repeats(state, ctx)
    assert calls.calls == []


async def test_a_failed_repeats_call_leaves_every_finding_unmatched(target_info, ctx, fake):
    earlier, now = at("earlier"), at("now", file="settings.py", line=3)
    write_round(ctx.target_dir, OLD_HEAD, [earlier])
    decide(ctx.target_dir, **{earlier.id: ("reject", "")})
    fake(repeats=[sdk.AgentCallError("timeout\ntrace")])
    state = await run_repeats(state_with(target_info, now), ctx)
    assert state.repeats == {} and state.errors == ["repeats: timeout"]


async def test_a_repeat_is_not_sent_to_assess(target_info, ctx, fake):
    same, other, third = at("same"), at("other", file="settings.py", line=3), at("third", line=6)
    write_round(ctx.target_dir, OLD_HEAD, [same])
    decide(ctx.target_dir, **{same.id: ("reject", "by design")})
    calls = fake(repeats=[RepeatsOutput()],
                 assess=[AssessOutput(assessments=[AssessItem(id=other.id, label="useful"),
                                                   AssessItem(id=third.id, label="useful")])])
    state = await run_repeats(state_with(target_info, same, other, third), ctx)
    state = await run_assess(state, ctx)
    (r_label, r_text), (label, text) = calls.calls
    assert r_label == "repeats" and same.id not in r_text.split("rejected in earlier rounds")[0]
    assert label == "assess" and same.id not in text and other.id in text
    a = state.assessments[same.id]
    assert (a.label, a.earlier_id, a.earlier_decision, a.earlier_note) == ("repeat", same.id, "reject", "by design")


# --- assess ----------------------------------------------------------------------


def three(target_info) -> tuple[ReviewState, Finding, Finding, Finding]:
    a = at("a", cite("rateplan_service.py", 1, "q"))
    b = at("b", cite("settings.py", 3, "q"), file="settings.py", line=3)
    c = at("c", cite("direct_booking.py", 5, "q"), file="direct_booking.py", line=5)
    return state_with(target_info, a, b, c, stage="repeats"), a, b, c


async def test_assess_keeps_labels_that_pass_the_gate(target_info, ctx, fake):
    state, a, b, c = three(target_info)
    earlier = at("earlier accepted", file="settings.py", line=9)
    write_round(ctx.target_dir, OLD_HEAD, [earlier])
    decide(ctx.target_dir, **{earlier.id: ("accept", "fix later")})
    calls = fake(assess=[AssessOutput(assessments=[
        AssessItem(id=a.id, label="useful", reason="real"),
        AssessItem(id=b.id, label="still-open", reason="not fixed", earlier_id=earlier.id),
        AssessItem(id=c.id, label="low-value", reason="[speculative] needs a change nobody proposed"),
    ])])
    state = await run_assess(state, ctx)
    (_, text), = calls.calls
    assert earlier.id in text and "fix later" in text and "[speculative]" in text
    got = {k: (v.label, v.earlier_id, v.earlier_decision, v.earlier_note) for k, v in state.assessments.items()}
    assert got == {a.id: ("useful", "", "", ""), b.id: ("still-open", earlier.id, "accept", "fix later"),
                   c.id: ("low-value", "", "", "")}
    assert state.errors == [] and state.stage == "assess"
    assert [f.severity for f in state.findings] == ["LOW", "LOW", "LOW"]  # severities untouched


@pytest.mark.parametrize("item, error", [
    (lambda ids, e: AssessItem(id="0000000000", label="useful"), "is not a finding of this round"),
    (lambda ids, e: AssessItem(id=ids[0], label="noise"), "unknown label"),
    (lambda ids, e: AssessItem(id=ids[0], label="useful", earlier_id="9999999999"), "is not in an earlier round"),
    (lambda ids, e: AssessItem(id=ids[0], label="still-open", reason="r"), "still-open without an earlier_id"),
    (lambda ids, e: AssessItem(id=ids[0], label="low-value", reason="not worth it"), "without naming a rule"),
])
async def test_each_assess_gate_rule_turns_a_bad_label_into_useful(target_info, ctx, fake, item, error):
    state, a, b, c = three(target_info)
    deferred = at("deferred earlier", file="settings.py", line=9)
    write_round(ctx.target_dir, OLD_HEAD, [deferred])
    decide(ctx.target_dir, **{deferred.id: ("defer", "")})
    ids = [a.id, b.id, c.id]
    fake(assess=[AssessOutput(assessments=[item(ids, {"deferred": deferred.id})]
                              + [AssessItem(id=i, label="useful") for i in ids[1:]])])
    state = await run_assess(state, ctx)
    assert {v.label for v in state.assessments.values()} == {"useful"}
    assert any(error in e for e in state.errors), state.errors


async def test_a_finding_left_out_is_useful_with_an_error(target_info, ctx, fake):
    state, a, b, c = three(target_info)
    fake(assess=[AssessOutput(assessments=[AssessItem(id=a.id, label="low-value", reason="[covered] by b")])])
    state = await run_assess(state, ctx)
    assert state.assessments[a.id].label == "low-value"
    assert state.assessments[b.id].label == state.assessments[c.id].label == "useful"
    assert state.errors == [f"assess: no label for {b.id}, {c.id}; labelled useful"]


def near_pair(target_info) -> tuple[ReviewState, Finding, Finding, Finding]:
    """`three`, but b is anchored a line below a in a's file (still citing settings.py), so assess may merge them."""
    a = at("a", cite("rateplan_service.py", 1, "q"))
    b = at("b", cite("settings.py", 3, "q"), line=2)
    c = at("c", cite("direct_booking.py", 5, "q"), file="direct_booking.py", line=5)
    return state_with(target_info, a, b, c, stage="repeats"), a, b, c


async def test_assess_duplicates_are_merged_keeping_every_citation(target_info, ctx, fake):
    state, a, b, c = near_pair(target_info)
    fake(assess=[AssessOutput(assessments=[AssessItem(id=i, label="useful") for i in (a.id, b.id, c.id)],
                              duplicate_sets=[DuplicateSet(finding_ids=[a.id, b.id], reason="one defect")])])
    state = await run_assess(state, ctx)
    assert [f.id for f in state.findings] == [a.id, c.id]
    assert [x.path for x in state.findings[0].citations] == ["rateplan_service.py", "settings.py"]
    assert state.merged[0].finding.id == b.id and state.merged[0].reason.startswith("assess: ")
    assert set(state.assessments) == {a.id, c.id}


async def test_a_failed_assess_call_leaves_everything_useful(target_info, ctx, fake):
    state, a, b, c = three(target_info)
    fake(assess=[sdk.AgentCallError("timeout\ntrace")])
    state = await run_assess(state, ctx)
    assert {v.label for v in state.assessments.values()} == {"useful"}
    assert state.errors == ["assess: timeout"] and len(state.findings) == 3


async def test_a_resumed_assess_stage_reuses_the_stored_answer(target_info, ctx, fake):
    state, a, b, c = three(target_info)
    fake(assess=[AssessOutput(assessments=[AssessItem(id=a.id, label="low-value", reason="[context] r")])])
    state = await run_assess(state, ctx)
    state.stage = "repeats"
    calls = fake()
    state = await run_assess(state, ctx)
    assert calls.calls == [] and state.assessments[a.id].label == "low-value"


# --- which assess duplicates are merged --------------------------------------------

BENCHMARK = Path(__file__).parent / "fixtures" / "assess_merges"


def benchmark_case(target_info, name: str) -> tuple[ReviewState, AssessOutput]:
    """One assess duplicate set from a PR #295 benchmark run, with its findings and their verdicts.

    Copied from that run's `review-state.json`: `.findings` (the survivor, with
    the citations it took over), `.merged[].finding`, `.verdicts` and
    `.assess_answer.duplicate_sets`.
    """
    data = json.loads((BENCHMARK / name).read_text(encoding="utf-8"))
    findings = [Finding.model_validate(f) for f in data["findings"]]
    assert [f.id for f in findings] == [f["id"] for f in data["findings"]]  # the ids the run gave them
    state = ReviewState(target=target_info, stage="repeats", findings=findings,
                        verdicts={k: Verdict.model_validate(v) for k, v in data["verdicts"].items()})
    answer = AssessOutput(assessments=[AssessItem(id=f.id, label="useful") for f in findings],
                          duplicate_sets=[DuplicateSet.model_validate(s) for s in data["duplicate_sets"]])
    return state, answer


@pytest.mark.parametrize("name, root", [
    ("r1-r004.json", "157cbadf24"),  # R-004, the slug collision in template.py, set with fill.py and markers.py
    ("r2-r017.json", "312895fb3f"),  # R-017, `--head` reaching `git push`, set with a github.py finding
])
async def test_assess_does_not_merge_a_root_bug_into_a_finding_in_another_file(target_info, ctx, fake, caplog,
                                                                              name, root):
    state, answer = benchmark_case(target_info, name)
    ids = [f.id for f in state.findings]
    fake(assess=[answer])
    with caplog.at_level("INFO", logger="review_pipeline.stages.assess"):
        state = await run_assess(state, ctx)
    assert [f.id for f in state.findings] == ids and state.merged == []
    assert root in {f.id for f in state.findings}
    dropped = [r.getMessage() for r in caplog.records if "duplicate set" in r.getMessage()]
    assert len(dropped) == 1 and all(i in dropped[0] for i in ids), dropped


async def test_assess_merges_two_findings_anchored_within_two_lines_in_one_file(target_info, ctx, fake):
    a = at("a", cite("rateplan_service.py", 10, "q"), line=10)
    b = at("b", cite("rateplan_service.py", 12, "q"), line=12)
    state = state_with(target_info, a, b, stage="repeats")
    fake(assess=[AssessOutput(assessments=[AssessItem(id=a.id, label="useful"), AssessItem(id=b.id, label="useful")],
                              duplicate_sets=[DuplicateSet(finding_ids=[b.id, a.id], reason="one defect")])])
    state = await run_assess(state, ctx)
    assert [f.id for f in state.findings] == [a.id]
    assert [(m.finding.id, m.into) for m in state.merged] == [(b.id, a.id)]


async def test_assess_drops_only_the_set_members_anchored_away_from_the_others(target_info, ctx, fake, caplog):
    a = at("a", line=10)
    b = at("b", line=11)
    far = at("far, same file", line=40)
    other = at("other file", file="settings.py", line=10)
    c = at("c", file="direct_booking.py", line=5)
    d = at("d", file="direct_booking.py", line=7)
    state = state_with(target_info, a, b, far, other, c, d, stage="repeats")
    fake(assess=[AssessOutput(
        assessments=[AssessItem(id=f.id, label="useful") for f in (a, b, far, other, c, d)],
        duplicate_sets=[DuplicateSet(finding_ids=[a.id, far.id, b.id, other.id, c.id, d.id], reason="one")])])
    with caplog.at_level("INFO", logger="review_pipeline.stages.assess"):
        state = await run_assess(state, ctx)
    # Each run of members within two lines of each other in one file is merged on its own.
    assert sorted((m.finding.id, m.into) for m in state.merged) == sorted([(b.id, a.id), (d.id, c.id)])
    assert [f.id for f in state.findings] == [a.id, far.id, other.id, c.id]
    line, = [r.getMessage() for r in caplog.records if "duplicate set" in r.getMessage()]
    assert far.id in line and other.id in line


def test_findings_json_lists_the_findings_merged_into_each_survivor(target_info):
    import jsonschema

    from review_pipeline.models import Merged

    a, b, c, d = at("a", line=1), at("b", line=2), at("c", line=3), at("d", file="settings.py", line=9)
    state = state_with(target_info, c, d)
    # b went into a at merge, then a into c at assess: c carries both.
    state.merged = [Merged(finding=b, into=a.id, reason="r"), Merged(finding=a, into=c.id, reason="assess: r")]
    data = to_findings_file(state)
    jsonschema.validate(data, json.loads(SCHEMA.read_text(encoding="utf-8")))
    rows = {r["id"]: r for r in data["findings"]}
    assert rows[c.id]["absorbed"] == [{"id": b.id, "claim": "b"}, {"id": a.id, "claim": "a"}]
    assert "absorbed" not in rows[d.id]  # written only when there are some


# --- assess resume (the plan's S1-S3) ----------------------------------------------


def resume_case(target_info) -> tuple[ReviewState, AssessOutput]:
    """Three findings; the agent sets all three as one defect, but only a and b are anchored together."""
    a = at("a", line=10)
    b = at("b", line=11)
    c = at("c", file="settings.py", line=40)
    state = state_with(target_info, a, b, c, stage="repeats")
    answer = AssessOutput(assessments=[AssessItem(id=a.id, label="useful"), AssessItem(id=b.id, label="useful"),
                                       AssessItem(id=c.id, label="low-value", reason="[covered] by a")],
                          duplicate_sets=[DuplicateSet(finding_ids=[a.id, b.id, c.id], reason="one defect")])
    return state, answer


def outcome(state: ReviewState) -> tuple:
    return ([f.model_dump() for f in state.findings], [m.model_dump() for m in state.merged],
            {k: v.model_dump() for k, v in state.assessments.items()}, state.stage, state.errors)


async def never_stopped(target_info, ctx, fake) -> ReviewState:
    state, answer = resume_case(target_info)
    fake(assess=[answer])
    return await run_assess(state, ctx)


def saved_at_s1(target_info, ctx) -> Path:
    """review-state.json as a run leaves it once the assess answer is stored and before its sets are applied."""
    state, answer = resume_case(target_info)
    state.assess_answer = answer
    ctx.target_dir.mkdir(parents=True, exist_ok=True)
    return save_state(state, ctx.target_dir)


async def test_assess_resume_s1_applies_the_saved_answer_under_the_link_check(target_info, ctx, fake):
    expected = outcome(await never_stopped(target_info, ctx, fake))
    path = saved_at_s1(target_info, ctx)
    calls = fake()
    resumed = await run_assess(load_state(path), ctx)
    assert calls.calls == []
    assert outcome(resumed) == expected
    a, b, c = resume_case(target_info)[0].findings
    assert [(m.finding.id, m.into) for m in resumed.merged] == [(b.id, a.id)]
    assert [f.id for f in resumed.findings] == [a.id, c.id]


async def test_assess_resume_s2_keeps_merges_made_before_the_link_check(target_info, ctx, fake):
    from review_pipeline.stages.merge import apply_duplicate_sets

    state, answer = resume_case(target_info)
    a, b, c = state.findings
    # What a finished assess stage left before the link check: all three merged into a.
    apply_duplicate_sets(state, [(list(state.findings), answer.duplicate_sets)], by="assess")
    state.stage = "assess"
    ctx.target_dir.mkdir(parents=True, exist_ok=True)
    path = save_state(state, ctx.target_dir)
    calls = fake()
    resumed = await run_assess(load_state(path), ctx)
    assert calls.calls == []
    assert resumed.model_dump() == state.model_dump()
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert to_findings_file(resumed, generated_at=when) == to_findings_file(state, generated_at=when)
    row, = to_findings_file(resumed)["findings"]
    assert row["id"] == a.id and row["absorbed"] == [{"id": b.id, "claim": "b"}, {"id": c.id, "claim": "c"}]


async def test_assess_resume_s3_stopping_twice_at_s1_gives_the_same_result(target_info, ctx, fake):
    expected = outcome(await never_stopped(target_info, ctx, fake))
    path = saved_at_s1(target_info, ctx)
    calls = fake()
    await run_assess(load_state(path), ctx)  # stops again before anything is saved: the file is still at S1
    resumed = await run_assess(load_state(path), ctx)
    assert calls.calls == []
    assert outcome(resumed) == expected


async def test_assess_is_skipped_for_one_finding_and_no_earlier_rounds(target_info, ctx, fake):
    calls = fake()
    only = at("only")
    state = await run_assess(state_with(target_info, only, stage="repeats"), ctx)
    assert calls.calls == [] and state.assessments[only.id].label == "useful" and state.stage == "assess"


@pytest.mark.parametrize("decision", ["defer", ""])
async def test_still_open_on_a_deferred_or_undecided_earlier_finding_is_kept(target_info, ctx, fake, decision):
    state, a, b, c = three(target_info)
    earlier = at("earlier, not decided", file="settings.py", line=9)
    write_round(ctx.target_dir, OLD_HEAD, [earlier])
    if decision:
        decide(ctx.target_dir, **{earlier.id: (decision, "")})
    fake(assess=[AssessOutput(assessments=[AssessItem(id=b.id, label="still-open", reason="r", earlier_id=earlier.id)]
                              + [AssessItem(id=i, label="useful") for i in (a.id, c.id)])])
    state = await run_assess(state, ctx)
    assert (state.assessments[b.id].label, state.assessments[b.id].earlier_decision) == ("still-open", decision)
    assert state.errors == []


def test_still_open_on_a_rejected_earlier_finding_fails_the_gate():
    f = at("now")
    rejected = rounds.EarlierFinding(finding={"id": "e0e0e0e0e0", "status": "confirmed"}, head_sha=OLD_HEAD,
                                     decision="reject", note="")
    labels, errors = gate_assessments([AssessItem(id=f.id, label="still-open", earlier_id="e0e0e0e0e0")],
                                      [f], [rejected])
    assert labels[f.id].label == "useful"
    assert errors == [f"{f.id}: still-open, but the person's decision on e0e0e0e0e0 is reject; labelled useful"]


async def test_one_finding_with_an_earlier_round_still_goes_to_the_assess_agent(target_info, ctx, fake):
    only = at("only", file="settings.py", line=3)
    earlier = at("the same defect, accepted", file="settings.py", line=3, severity="MEDIUM")
    write_round(ctx.target_dir, OLD_HEAD, [earlier])
    decide(ctx.target_dir, **{earlier.id: ("accept", "")})
    calls = fake(assess=[AssessOutput(assessments=[
        AssessItem(id=only.id, label="still-open", reason="not fixed", earlier_id=earlier.id)])])
    state = await run_assess(state_with(target_info, only, stage="repeats"), ctx)
    assert [label for label, _ in calls.calls] == ["assess"]
    assert state.assessments[only.id].label == "still-open" and state.errors == []


# --- export, render, post ----------------------------------------------------------


def _labelled_state(target_info) -> ReviewState:
    high, medium = high_finding(), medium_finding()
    low = at("a low one", line=6)
    state = state_with(target_info, high, medium, low, stage="assess")
    state.assessments = {
        high.id: rounds_assessment("useful"),
        medium.id: rounds_assessment("repeat", reason="same as before", earlier_id="abcdef0123",
                                     earlier_round=OLD_HEAD, earlier_decision="reject", earlier_note="by design"),
        low.id: rounds_assessment("low-value", reason="[speculative] needs a future change"),
    }
    return state


def rounds_assessment(label, **kw):
    from review_pipeline.models import Assessment
    return Assessment(label=label, **kw)


def test_findings_json_with_and_without_assessment_validates(target_info, canned_state):
    import jsonschema

    schema = json.loads(SCHEMA.read_text())
    jsonschema.validate(to_findings_file(canned_state), schema)  # no assessment
    data = to_findings_file(_labelled_state(target_info))
    jsonschema.validate(data, schema)
    by_label = {f["assessment"]["label"]: f for f in data["findings"]}
    assert by_label["repeat"]["assessment"] == {
        "label": "repeat", "reason": "same as before", "earlier_id": "abcdef0123", "earlier_round": OLD_HEAD,
        "earlier_decision": "reject", "earlier_note": "by design"}


def test_repeats_and_low_value_go_only_to_the_appendix_and_are_not_counted(target_info):
    state = _labelled_state(target_info)
    review = render_review(state)
    appendix = review.index("## Appendix")
    assert "Repeats and low-value" not in review
    assert review.index("## HIGH") < appendix
    assert appendix < review.index(medium_finding().claim)
    assert appendix < review.index("a low one")
    assert "- **Findings:** Code 1 HIGH, 0 MEDIUM, 0 LOW; Tests none; Docs none" in review
    assert "repeat — repeats `abcdef0123` (round 111111111111), your decision reject: by design." in review
    assert "- **repeat** (MEDIUM)" in review and "- **low-value** (LOW)" in review
    summary = render_summary(state)
    assert "Findings: Code 1 HIGH, 0 MEDIUM, 0 LOW; Tests none; Docs none." in summary
    assert "repeat" not in summary and "low-value" not in summary
    assert medium_finding().claim[:30] not in summary
    assert "LOW in the full review" not in summary  # the only LOW is low-value


def test_post_leaves_out_repeats_unless_accepted(target_info):
    from review_pipeline.post import select

    data = to_findings_file(_labelled_state(target_info))
    medium = medium_finding().id
    assert medium not in [f["id"] for f in select(data["findings"], None)]
    assert medium in [f["id"] for f in select(data["findings"], {medium: {"decision": "accept"}})]


# --- two rounds through the CLI ----------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    import os
    import subprocess

    env = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="T",
               GIT_COMMITTER_EMAIL="t@example.com", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_DATE="2026-01-01T00:00:00Z", GIT_COMMITTER_DATE="2026-01-01T00:00:00Z")
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


def test_a_finding_rejected_in_round_one_comes_back_reworded_and_is_labelled_repeat(tmp_path, monkeypatch, capsys):
    """Round 1 finds a defect in gauge.py; the person rejects it. Round 2's finder reports the
    same defect in other words, anchored in alarm.py. Round 2 labels it `repeat`."""
    from review_pipeline import budget
    from review_pipeline.__main__ import main

    repo = tmp_path / "weather"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "gauge.py").write_text("def level():\n    return 1\n")
    (repo / "alarm.py").write_text("from gauge import level\n\ndef ring():\n    return level() > 0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "gauge.py").write_text("def level():\n    return -1\n")
    (repo / "alarm.py").write_text("from gauge import level\n\ndef ring():\n    return level() >= 0\n")
    _git(repo, "commit", "-q", "-am", "round one")
    _git(repo, "update-ref", "refs/remotes/origin/feature", "HEAD")

    first = Finding(claim="level() returns a negative number, so the alarm never rings", severity="MEDIUM",
                    file="gauge.py", line_start=2, line_end=2, failure_scenario="any reading: no alarm",
                    citations=[cite("gauge.py", 2, "return -1")])
    reworded = Finding(claim="ring() compares against a level that is always below zero", severity="LOW",
                       file="alarm.py", line_start=4, line_end=4, failure_scenario="the alarm is silent",
                       citations=[cite("alarm.py", 4, "return level() >= 0"), cite("gauge.py", 2, "return -1")])
    round_findings = {"one": first, "two": reworded}
    current = {"round": "one"}

    async def agent(prompt, schema, *, budget_label="", **kwargs):
        sdk.require_trusted_repo()
        budget.check(stage=budget_label)
        stage = budget_label.split(":")[0]
        if budget_label == "find:diff-bugs":
            return schema(findings=[round_findings[current["round"]]])
        if stage == "find":
            return schema()
        if stage == "verify":
            f = round_findings[current["round"]]
            return Verdict(status="confirmed", impact="breaks-users", reason="read it", citations=[f.citations[0]],
                           expected_behaviour="The alarm rings on a real reading.")
        if stage == "repeats":
            return RepeatsOutput(matches=[RepeatMatch(id=reworded.id, rejected_id=first.id,
                                                      reason="the same negative level, seen from the alarm")])
        raise AssertionError(budget_label)

    monkeypatch.setattr(sdk, "agent_call_structured", agent)
    out = tmp_path / "out"
    args = ["--out", str(out), "review", "--repo", str(repo), "--branch", "feature", "--base-branch", "main",
            "--trust-repo"]
    assert main(args) == 0
    target_dir = out / "weather" / "branch-feature"
    round_one_head = json.loads((target_dir / "findings.json").read_text())["target"]["head_sha"]
    decide(target_dir, **{first.id: ("reject", "negative levels are the sensor's idle state")})

    (repo / "gauge.py").write_text("def level():\n    return -1\n\n# idle\n")
    _git(repo, "commit", "-q", "-am", "round two")
    _git(repo, "update-ref", "refs/remotes/origin/feature", "HEAD")
    current["round"] = "two"
    assert main(args) == 0

    assert (target_dir / "rounds" / f"01-{round_one_head[:12]}" / "findings.json").is_file()
    data = json.loads((target_dir / "findings.json").read_text())
    (row,) = [f for f in data["findings"] if f["id"] == reworded.id]
    assert row["assessment"] == {
        "label": "repeat", "reason": "the same negative level, seen from the alarm", "earlier_id": first.id,
        "earlier_round": round_one_head, "earlier_decision": "reject",
        "earlier_note": "negative levels are the sensor's idle state"}
    summary = next(target_dir.glob("summary-*.md")).read_text()
    assert reworded.claim[:30] not in summary  # a repeat is neither listed nor counted

    # Round three reruns the same head and gets round two's wording back. Round two
    # is kept although the head did not change, and the rejection carried onto the
    # reworded id matches it in Python: no repeats agent is asked.
    round_two_head = data["target"]["head_sha"]
    current["round"] = "two"
    repeats_asked = []
    real_agent = agent

    async def agent_round_three(prompt, schema, *, budget_label="", **kwargs):
        if budget_label.startswith("repeats"):
            repeats_asked.append(budget_label)
        return await real_agent(prompt, schema, budget_label=budget_label, **kwargs)

    monkeypatch.setattr(sdk, "agent_call_structured", agent_round_three)
    assert main(args) == 0
    assert (target_dir / "rounds" / f"02-{round_two_head[:12]}" / "findings.json").is_file()
    assert repeats_asked == []
    data = json.loads((target_dir / "findings.json").read_text())
    (row,) = [f for f in data["findings"] if f["id"] == reworded.id]
    assert row["assessment"]["label"] == "repeat"
    assert row["assessment"]["earlier_id"] == reworded.id
    assert row["assessment"]["earlier_round"] == round_two_head
    assert row["assessment"]["earlier_decision"] == "reject"
    assert row["assessment"]["earlier_note"] == "negative levels are the sensor's idle state"


def test_assess_only_labels_a_saved_review_and_changes_nothing_in_it(tmp_path, monkeypatch, capsys):
    from review_pipeline import budget
    from review_pipeline.__main__ import main

    repo = tmp_path / "weather"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "gauge.py").write_text("def level():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "gauge.py").write_text("def level():\n    return -1\n\ndef unit():\n    return 'mm'\n")
    _git(repo, "commit", "-q", "-am", "change")
    head = _git(repo, "rev-parse", "HEAD")
    kept = Finding(claim="level() is negative", severity="MEDIUM", file="gauge.py", line_start=2, line_end=2,
                   failure_scenario="no alarm", citations=[cite("gauge.py", 2, "return -1")])
    noise = Finding(claim="unit() could one day return inches", severity="LOW", file="gauge.py", line_start=5,
                    line_end=5, failure_scenario="a future change", citations=[cite("gauge.py", 5, "return 'mm'")])
    answers = {"assess": AssessOutput(assessments=[
        AssessItem(id=kept.id, label="useful"),
        AssessItem(id=noise.id, label="low-value", reason="[speculative] nobody proposed inches")])}

    async def agent(prompt, schema, *, budget_label="", **kwargs):
        sdk.require_trusted_repo()
        budget.check(stage=budget_label)
        stage = budget_label.split(":")[0]
        if budget_label == "find:diff-bugs":
            return schema(findings=[kept, noise])
        if stage == "find":
            return schema()
        if stage == "verify":
            return Verdict(status="confirmed", impact="breaks-users", reason="r", citations=[cite("gauge.py", 2, "return -1")])
        if stage == "merge":
            return schema()
        return answers[stage]

    monkeypatch.setattr(sdk, "agent_call_structured", agent)
    out = tmp_path / "out"
    answers_during_review = AssessOutput(assessments=[AssessItem(id=kept.id, label="useful"),
                                                      AssessItem(id=noise.id, label="useful")])
    answers["assess"], saved = answers_during_review, answers["assess"]
    assert main(["--out", str(out), "review", "--repo", str(repo), "--range", f"{base}..{head}", "--trust-repo"]) == 0
    target_dir = next(out.glob("weather/range-*"))
    decide(target_dir, **{kept.id: ("accept", ""), noise.id: ("reject", "never")})
    before = {p.name: p.read_bytes() for p in target_dir.iterdir() if p.is_file()}
    answers["assess"] = saved
    no_decisions = out / "other"
    no_decisions.mkdir()
    report = tmp_path / "report.md"
    capsys.readouterr()
    assert main(["assess-only", str(target_dir), str(no_decisions), "--report", str(report), "--trust-repo"]) == 0
    assert f"report: {report}" in capsys.readouterr().out
    text = report.read_text()
    assert f"`{noise.id}`" in text and "| reject | low-value | [speculative] nobody proposed inches |" in text
    assert "Rejected findings labelled repeat or low-value: 1 of 1" in text
    assert "Accepted findings labelled repeat or low-value (wrongly folded): 0 of 1" in text
    assert "other: no viewer/decisions.json" in text
    after = {p.name: p.read_bytes() for p in target_dir.iterdir() if p.is_file()}
    assert after == before and not (target_dir / "assess-tree").exists()


def test_assess_only_keeps_the_report_when_one_review_goes_over_budget_or_fails(tmp_path, monkeypatch, capsys):
    from review_pipeline import budget, orchestrator
    from review_pipeline.__main__ import main

    dirs = {}
    for name in ("done", "costly", "broken"):
        d = tmp_path / "out" / name
        decide(d, **{"aaaaaaaaaa": ("accept", "")})
        dirs[name] = d

    async def assess_only(target_dir, config):
        if target_dir.name == "costly":
            raise budget.BudgetExceededError("spent $2.00 of $1.00", diagnosis="assess: $2.00")
        if target_dir.name == "broken":
            raise RuntimeError("snapshot failed\ntrace")
        return orchestrator.AssessOnly(target_dir=target_dir, cost_usd=0.5, rows=[
            {"id": "aaaaaaaaaa", "severity": "LOW", "claim": "c", "decision": "accept", "note": "",
             "label": "useful", "reason": ""}])

    monkeypatch.setattr(orchestrator, "assess_only", assess_only)
    report = tmp_path / "report.md"
    code = main(["assess-only", *(str(d) for d in dirs.values()), "--report", str(report), "--trust-repo"])
    out, err = capsys.readouterr()
    assert code == 0 and f"report: {report}" in out and "resume" not in (out + err).lower()
    text = report.read_text()
    assert "| done | `aaaaaaaaaa`" in text and "Cost: $0.50 over 1 reviews" in text
    assert "- costly: stopped at the cost limit (spent $2.00 of $1.00)" in text
    assert "- broken: failed (RuntimeError: snapshot failed)" in text


def test_assess_only_stops_at_a_usage_limit_and_keeps_the_report_so_far(tmp_path, monkeypatch, capsys):
    from review_pipeline import orchestrator
    from review_pipeline.__main__ import main

    dirs = {}
    for name in ("done", "limited", "later"):
        d = tmp_path / "out" / name
        decide(d, **{"aaaaaaaaaa": ("accept", "")})
        dirs[name] = d
    asked = []

    async def assess_only(target_dir, config):
        asked.append(target_dir.name)
        if target_dir.name != "done":
            raise sdk.UsageLimitError("the Claude weekly usage limit was hit; it resets Oct 19, 4am (UTC)",
                                      limit="weekly", resets="Oct 19, 4am (UTC)")
        return orchestrator.AssessOnly(target_dir=target_dir, cost_usd=0.5, rows=[
            {"id": "aaaaaaaaaa", "severity": "LOW", "claim": "c", "decision": "accept", "note": "",
             "label": "useful", "reason": ""}])

    monkeypatch.setattr(orchestrator, "assess_only", assess_only)
    report = tmp_path / "report.md"
    code = main(["assess-only", *(str(d) for d in dirs.values()), "--report", str(report), "--trust-repo"])
    out, err = capsys.readouterr()
    assert code == 6 and asked == ["done", "limited"]
    assert "STOPPED: the Claude weekly usage limit was hit; it resets Oct 19, 4am (UTC)" in err
    text = report.read_text()
    assert "| done | `aaaaaaaaaa`" in text
    assert "- limited: stopped: the Claude weekly usage limit was hit" in text
    assert "- later: not started: the Claude weekly usage limit was hit" in text


# --- the record of what was checked (checks.json) --------------------------------------


async def test_repeats_records_its_agent_call_and_the_matches(target_info, ctx, fake):
    earlier = at("the keyword is a literal")
    reworded = at("rate plans match a fixed word", KEYWORD_USE_CITATION, file="settings.py", line=3, severity="MEDIUM")
    write_round(ctx.target_dir, OLD_HEAD, [earlier])
    decide(ctx.target_dir, **{earlier.id: ("reject", "intended")})
    fake(repeats=[RepeatsOutput(matches=[RepeatMatch(id=reworded.id, rejected_id=earlier.id, reason="same")])])
    state = await run_repeats(state_with(target_info, reworded), ctx)
    check, = state.checks
    assert (check.stage, check.subject, check.error) == ("repeats", "repeats", "")
    assert check.given == ["findings (1)", "rejected earlier (1)"]
    assert check.outcome == f"{reworded.id} repeats {earlier.id}"
    # A resume asks nothing again, so it records nothing again.
    state.stage = "merge"
    fake()
    assert len((await run_repeats(state, ctx)).checks) == 1


async def test_a_failed_repeats_call_is_recorded_with_its_error(target_info, ctx, fake):
    earlier, now = at("earlier"), at("now", file="settings.py", line=3)
    write_round(ctx.target_dir, OLD_HEAD, [earlier])
    decide(ctx.target_dir, **{earlier.id: ("reject", "")})
    fake(repeats=[sdk.AgentCallError("timeout\ntrace")])
    check, = (await run_repeats(state_with(target_info, now), ctx)).checks
    assert check.error == "repeats: timeout" and check.outcome == "failed: timeout"


async def test_an_exact_id_repeat_makes_no_call_and_records_none(target_info, ctx, fake):
    same = at("the keyword is a literal")
    write_round(ctx.target_dir, OLD_HEAD, [same])
    decide(ctx.target_dir, **{same.id: ("reject", "intended")})
    fake()
    assert (await run_repeats(state_with(target_info, same), ctx)).checks == []


async def test_assess_records_its_labels_and_merges_and_marks_the_finder_entry(target_info, ctx, fake):
    from review_pipeline.models import AgentCheck

    state, a, b, c = near_pair(target_info)
    state.checks.append(AgentCheck(stage="find", subject="diff-bugs", findings=[
        {"id": i, "fate": "kept"} for i in (a.id, b.id, c.id)]))
    fake(assess=[AssessOutput(
        assessments=[AssessItem(id=a.id, label="useful"), AssessItem(id=b.id, label="useful"),
                     AssessItem(id=c.id, label="low-value", reason="[speculative] r")],
        duplicate_sets=[DuplicateSet(finding_ids=[a.id, b.id], reason="one defect")])])
    state = await run_assess(state, ctx)
    finder, check = state.checks
    assert (check.stage, check.subject, check.error) == ("assess", "assess", "")
    assert check.given[:2] == ["findings (3)", "earlier findings (0)"]
    assert check.outcome == f"1 low-value; 1 useful; merged {b.id} into {a.id}"
    assert [(e["id"], e["fate"], e.get("into")) for e in finder.findings] == [
        (a.id, "kept", None), (b.id, "merged", a.id), (c.id, "kept", None)]
    # A resume reuses the stored answer and records no second call.
    state.stage = "repeats"
    fake()
    assert [x.stage for x in (await run_assess(state, ctx)).checks] == ["find", "assess"]


async def test_a_failed_assess_call_is_recorded_with_its_error(target_info, ctx, fake):
    state, a, b, c = three(target_info)
    fake(assess=[sdk.AgentCallError("timeout\ntrace")])
    check, = (await run_assess(state, ctx)).checks
    assert check.error == "assess: timeout" and check.outcome == "failed: timeout"


async def test_checks_json_with_repeats_and_assess_entries_validates(target_info, ctx, fake):
    import jsonschema

    from review_pipeline.export import to_checks_file

    state, a, b, c = three(target_info)
    fake(assess=[AssessOutput(assessments=[AssessItem(id=i, label="useful") for i in (a.id, b.id, c.id)])])
    state = await run_assess(state, ctx)
    data = to_checks_file(state)
    jsonschema.validate(data, json.loads((SCHEMA.parent / "checks.v1.schema.json").read_text(encoding="utf-8")))
    assert [x["stage"] for x in data["agents"]] == ["assess"]
    from review_pipeline.render import render_checks
    assert "### assess: assess — 3 useful" in render_checks(state, checks_file=data)


def _limit() -> sdk.UsageLimitError:
    return sdk.UsageLimitError("the Claude session usage limit was hit", limit="session")


async def test_a_usage_limit_stops_repeats_instead_of_leaving_findings_unmatched(target_info, ctx, fake):
    earlier, now = at("earlier"), at("now", file="settings.py", line=3)
    write_round(ctx.target_dir, OLD_HEAD, [earlier])
    decide(ctx.target_dir, **{earlier.id: ("reject", "")})
    fake(repeats=[_limit()])
    state = state_with(target_info, now)
    with pytest.raises(sdk.UsageLimitError):
        await run_repeats(state, ctx)
    assert state.repeats == {} and state.errors == [] and not state.past("repeats")


async def test_a_usage_limit_stops_assess_instead_of_labelling_everything_useful(target_info, ctx, fake):
    state, *_ = three(target_info)
    fake(assess=[_limit()])
    with pytest.raises(sdk.UsageLimitError):
        await run_assess(state, ctx)
    assert state.assessments == {} and state.errors == [] and not state.past("assess")
