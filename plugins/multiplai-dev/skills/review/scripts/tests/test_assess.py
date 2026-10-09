"""repeats and assess: earlier rounds, the person's decisions, labels and their gates.

Every agent call is replaced. All data is made up.
"""

from __future__ import annotations

import json
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
from review_pipeline.stages.assess import run_assess
from review_pipeline.stages.repeats import gate_matches, run_repeats

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
    assert rounds.load_rounds(tmp_path, current_head=OLD_HEAD)[0].head_sha == OLDER_HEAD


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
    (lambda ids, e: AssessItem(id=ids[0], label="still-open", earlier_id=e["deferred"]),
     "decision on"),
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


async def test_assess_duplicates_are_merged_keeping_every_citation(target_info, ctx, fake):
    state, a, b, c = three(target_info)
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


async def test_assess_is_skipped_for_one_finding_and_no_earlier_rounds(target_info, ctx, fake):
    calls = fake()
    only = at("only")
    state = await run_assess(state_with(target_info, only, stage="repeats"), ctx)
    assert calls.calls == [] and state.assessments[only.id].label == "useful" and state.stage == "assess"


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


def test_review_lists_repeats_and_low_value_last_and_summary_counts_them(target_info):
    state = _labelled_state(target_info)
    review = render_review(state)
    folded = review.index("## Repeats and low-value findings")
    appendix = review.index("## Appendix")
    assert review.index("## HIGH") < folded < appendix
    assert folded < review.index(medium_finding().claim) < appendix
    assert folded < review.index("a low one") < appendix
    assert "**Assessment:** repeat — repeats `abcdef0123` (round 111111111111), your decision reject: by design." in review
    summary = render_summary(state)
    assert "Assessed: 1 repeats of rejected findings, 1 low-value, 0 still open" in summary
    assert medium_finding().claim[:30] not in summary  # a repeat MEDIUM moves to the count
    assert "LOW findings are in the full review" not in summary  # the only LOW is low-value


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
            return Verdict(status="confirmed", reason="read it", citations=[f.citations[0]],
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
    target_dir = out / "weather--feature"
    round_one_head = json.loads((target_dir / "findings.json").read_text())["target"]["head_sha"]
    decide(target_dir, **{first.id: ("reject", "negative levels are the sensor's idle state")})

    (repo / "gauge.py").write_text("def level():\n    return -1\n\n# idle\n")
    _git(repo, "commit", "-q", "-am", "round two")
    _git(repo, "update-ref", "refs/remotes/origin/feature", "HEAD")
    current["round"] = "two"
    assert main(args) == 0

    assert (target_dir / "rounds" / round_one_head[:12] / "findings.json").is_file()
    data = json.loads((target_dir / "findings.json").read_text())
    (row,) = [f for f in data["findings"] if f["id"] == reworded.id]
    assert row["assessment"] == {
        "label": "repeat", "reason": "the same negative level, seen from the alarm", "earlier_id": first.id,
        "earlier_round": round_one_head, "earlier_decision": "reject",
        "earlier_note": "negative levels are the sensor's idle state"}
    summary = next(target_dir.glob("summary-*.md")).read_text()
    assert "Assessed: 1 repeats of rejected findings" in summary
