"""The record of what was checked: tool calls, AgentCheck/GateCheck, checks.json, checks-<slug>.md."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from conftest import KEYWORD_CITATION, KEYWORD_USE_CITATION, SCHEMA, high_finding, medium_finding, rejected_finding
from multiplai_core.agent_runner import ToolCall
from review_pipeline import checks, sdk
from review_pipeline.config import ReviewConfig
from review_pipeline.export import to_checks_file, to_findings_file, write_checks_file
from review_pipeline.models import (AgentCheck, Citation, DuplicateSet, FinderOutput, GateCheck, MergeOutput,
                                    ReviewState, Verdict)
from review_pipeline.render import render_checks
from review_pipeline.stages import RunContext
from review_pipeline.stages.find import run_find
from review_pipeline.stages.merge import run_merge
from review_pipeline.stages.verify import run_verify
from review_pipeline.state import load_state, save_state

CHECKS_SCHEMA = SCHEMA.parent / "checks.v1.schema.json"


def _schema(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --- sdk: tool calls reach the recorder ---------------------------------------------


def _result(text: str, *calls: ToolCall, cost: float = 0.5, turns: int = 3):
    return SimpleNamespace(text=text, tool_calls=tuple(calls), turns=turns,
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1, cost_usd=cost))


@pytest.fixture
def fake_run(monkeypatch):
    monkeypatch.setenv("REVIEW_TRUST_REPO", "1")
    replies: list = []

    async def run_agent(prompt, **kwargs):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(sdk, "run_agent", run_agent)
    return replies


async def test_recording_collects_tool_calls_turns_and_cost_from_run_agent(fake_run):
    from pydantic import BaseModel

    class Value(BaseModel):
        value: int

    fake_run.append(_result('{"value": 1}', ToolCall("Read", {"file_path": "/s/a.py"}),
                            ToolCall("Grep", {"pattern": "x"})))
    with sdk.recording() as rec:
        assert (await sdk.agent_call_structured("p", Value, allowed_tools=sdk.FINDER_TOOLS)).value == 1
    assert [c.name for c in rec.tool_calls] == ["Read", "Grep"]
    assert (rec.turns, rec.cost_usd) == (3, 0.5)


async def test_a_reask_concatenates_both_attempts(fake_run):
    from pydantic import BaseModel

    class Value(BaseModel):
        value: int

    fake_run += [_result('{"value": "nope"}', ToolCall("Read", {"file_path": "a.py"}), turns=4),
                 _result('{"value": 2}', ToolCall("Glob", {"pattern": "*.py"}), turns=1, cost=0.1)]
    with sdk.recording() as rec:
        assert (await sdk.agent_call_structured("p", Value, allowed_tools=sdk.FINDER_TOOLS)).value == 2
    assert [c.name for c in rec.tool_calls] == ["Read", "Glob"]
    assert rec.turns == 5 and rec.cost_usd == pytest.approx(0.6)


async def test_a_failed_run_still_records_its_partial_calls_turns_and_cost(fake_run):
    from pydantic import BaseModel

    from multiplai_core.agent_runner import AgentRunTimeout

    class Value(BaseModel):
        value: int

    partial = _result("", ToolCall("Read", {"file_path": "a.py"}), turns=7, cost=0.9)
    fake_run += [AgentRunTimeout("timed out", reason="timed out after 600s", partial=partial),
                 AgentRunTimeout("timed out", reason="timed out after 600s", partial=partial)]
    with sdk.recording() as rec:
        with pytest.raises(sdk.AgentCallError):
            await sdk.agent_call_structured("p", Value, allowed_tools=sdk.FINDER_TOOLS)
    # Both attempts timed out, and each one's partial result is recorded.
    assert [c.name for c in rec.tool_calls] == ["Read", "Read"]
    assert rec.turns == 14 and rec.cost_usd == pytest.approx(1.8)


async def test_without_a_recording_block_nothing_is_kept(fake_run):
    from pydantic import BaseModel

    class Value(BaseModel):
        value: int

    fake_run.append(_result('{"value": 1}', ToolCall("Read", {"file_path": "a.py"})))
    await sdk.agent_call_structured("p", Value, allowed_tools=sdk.FINDER_TOOLS)  # no error, no record


# --- pure functions -------------------------------------------------------------------


def test_summarise_call_makes_paths_repo_relative_and_keeps_inputs_only(target_info, tmp_path):
    snap = tmp_path / "tree"
    s = lambda name, **data: checks.summarise_call(ToolCall(name, data), target_info, snap)  # noqa: E731
    assert s("Read", file_path=str(snap / "pkg/a.py")) == {"tool": "Read", "target": "pkg/a.py", "detail": "whole file"}
    assert s("Read", file_path="a.py", offset=10, limit=5)["detail"] == "lines 10-14"
    assert s("Read", file_path="a.py", offset=10)["detail"] == "lines 10-end"
    assert s("Grep", pattern="def x", path=str(snap / "pkg"), glob="*.py") == \
        {"tool": "Grep", "target": "def x", "detail": "in pkg, glob *.py"}
    assert s("Glob", pattern="**/*.md") == {"tool": "Glob", "target": "**/*.md", "detail": "in ."}
    assert s("WebFetch", url="https://example.com/x", prompt="p")["target"] == "https://example.com/x"
    assert s("WebSearch", query="q") == {"tool": "WebSearch", "target": "q", "detail": ""}
    assert s("Bash", command="rm -rf /") == {"tool": "Bash", "target": "", "detail": "input keys: command"}


def test_diff_hunks_reads_new_side_ranges():
    diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +10,3 @@\n a\n+b\n c\n"
            "@@ -20 +30 @@\n-x\n+y\n--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n")
    assert checks.diff_hunks(diff) == {"x.py": [(10, 12), (30, 30)]}


@pytest.mark.parametrize("calls, hunks, cite_, expected", [
    ([ToolCall("Read", {"file_path": "/snap/a.py"})], {}, ("a.py", 50, 50), "read"),
    ([ToolCall("Read", {"file_path": "a.py", "offset": 40, "limit": 20})], {}, ("a.py", 50, 52), "read"),
    # A read whose range misses the cited lines does not count as reading them.
    ([ToolCall("Read", {"file_path": "a.py", "offset": 1, "limit": 20})], {}, ("a.py", 50, 50), "not-seen"),
    ([ToolCall("Read", {"file_path": "a.py", "offset": 1, "limit": 20}), ToolCall("Grep", {"pattern": "x"})],
     {}, ("a.py", 50, 50), "searched"),
    ([ToolCall("Grep", {"pattern": "x", "path": "/snap/pkg"})], {}, ("pkg/a.py", 5, 5), "searched"),
    ([ToolCall("Grep", {"pattern": "x", "path": "other"})], {"a.py": [(48, 60)]}, ("a.py", 50, 50), "diff"),
    ([], {"a.py": [(1, 10)]}, ("a.py", 50, 50), "not-seen"),
    ([ToolCall("WebFetch", {"url": "https://ex.com/doc/"})], {}, ("https://ex.com/doc", 1, 1), "fetched"),
    ([ToolCall("WebSearch", {"query": "doc"})], {}, ("https://ex.com/doc", 1, 1), "not-seen"),
])
def test_seen(target_info, calls, hunks, cite_, expected):
    path, start, end = cite_
    citation = Citation(path=path, line_start=start, line_end=end, quote="q")
    assert checks.seen(citation, calls, hunks, target_info, Path("/snap")) == expected


def test_seen_marks_lines_the_prompt_showed_another_way(target_info):
    citation = Citation(path="a.py", line_start=50, line_end=51, quote="q")
    shown = checks.cited_ranges([Citation(path="a.py", line_start=49, line_end=50, quote="q"),
                                 Citation(path="https://ex.com/doc", line_start=1, line_end=1, quote="q")])
    assert shown == {"a.py": [(49, 50)]}
    assert checks.seen(citation, [], {}, target_info, Path("/snap"), shown) == "prompt"
    assert checks.seen(citation, [], {}, target_info, Path("/snap"), {"a.py": [(1, 10)]}) == "not-seen"
    # A read still wins over what the prompt held.
    assert checks.seen(citation, [ToolCall("Read", {"file_path": "a.py"})], {}, target_info, Path("/snap"),
                       shown) == "read"


def test_prompt_labels(target_info):
    pr = target_info.model_copy(update={"title": "t", "base_ref": "main", "branch_rules": []})
    labels = checks.prompt_labels(pr, diff="x" * 10, diff_limit=5, rules=[("CLAUDE.md", True), ("a/CLAUDE.md", False)])
    assert labels == [f"diff ({len(pr.files)} files, cut at 5 characters)", "commit subjects (1)", "PR description",
                      "branch rules", "rules: CLAUDE.md", "rules skipped: a/CLAUDE.md"]
    unread = target_info.model_copy(update={"base_ref": "main", "branch_rules": None})
    assert checks.prompt_labels(unread, extra=["finding abc"]) == ["finding abc", "branch rules (could not be read)"]


# --- stages ---------------------------------------------------------------------------


class Recorded:
    """Stands in for sdk.agent_call_structured and feeds the recorder like `_run` does."""

    def __init__(self, answers: dict[str, list], calls: dict[str, list[ToolCall]] | None = None):
        self.answers, self.tool_calls, self.labels = answers, calls or {}, []

    async def __call__(self, prompt, schema, *, budget_label="", **kwargs):
        self.labels.append(budget_label)
        sdk._note(_result("", *self.tool_calls.get(budget_label, ()), cost=0.25, turns=2))
        answer = self.answers[budget_label].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return schema.model_validate(answer.model_dump())


@pytest.fixture
def ctx(target_info, tmp_path) -> RunContext:
    snapshot = tmp_path / "tree"
    snapshot.mkdir()
    return RunContext(config=ReviewConfig(concurrency=1, dimensions=("diff-bugs", "callers")),
                      snapshot=snapshot, diff=Path(target_info.diff_path).read_text())


async def test_find_records_one_check_per_finder_with_fates_and_gate_results(target_info, ctx, monkeypatch):
    high, medium, bad = high_finding(), medium_finding(), rejected_finding()
    reads = [ToolCall("Read", {"file_path": str(ctx.snapshot / "rateplan_service.py")}),
             ToolCall("Grep", {"pattern": "KEYWORD", "path": str(ctx.snapshot)})]
    recorded = Recorded({"find:diff-bugs": [FinderOutput(findings=[high, bad])],
                         "find:callers": [FinderOutput(findings=[high, medium])]},  # high again: deduped
                        {"find:diff-bugs": reads})
    monkeypatch.setattr(sdk, "agent_call_structured", recorded)
    state = await run_find(ReviewState(target=target_info), ctx)

    assert [(c.stage, c.subject) for c in state.checks] == [("find", "diff-bugs"), ("find", "callers")]
    first, second = state.checks
    assert first.calls[0] == {"tool": "Read", "target": "rateplan_service.py", "detail": "whole file"}
    assert first.calls[1]["detail"] == "in ."
    assert (first.turns, first.cost_usd, first.outcome) == (2, 0.25, "2 findings")
    assert first.given[0].startswith("diff (") and "commit subjects (1)" in first.given
    assert first.started_at <= first.ended_at
    by_id = {e["id"]: e for e in first.findings}
    assert by_id[high.id]["fate"] == "kept"
    assert by_id[bad.id]["fate"] == "rejected" and by_id[bad.id]["rule"] == "quote not at cited lines"
    assert [c["seen"] for c in by_id[high.id]["citations"]] == ["read", "read"]
    assert by_id[bad.id]["citations"][0]["gate"] == "fail"
    assert second.findings[0] == {**second.findings[0], "fate": "deduped", "into": high.id}
    # The callers finder read nothing, but its diff showed the lines.
    assert second.findings[1]["citations"][0]["seen"] == "diff"

    gates = {(g.finding_id, g.passed, g.rule) for g in state.gate_checks}
    assert gates == {(high.id, True, ""), (bad.id, False, "quote not at cited lines"), (medium.id, True, "")}


async def test_a_failed_finder_is_recorded_with_its_error(target_info, ctx, monkeypatch):
    monkeypatch.setattr(sdk, "agent_call_structured", Recorded({
        "find:diff-bugs": [sdk.AgentCallError("agent call failed (timeout)")],
        "find:callers": [FinderOutput()]}))
    state = await run_find(ReviewState(target=target_info), ctx)
    failed = state.checks[0]
    assert failed.subject == "diff-bugs" and failed.error.startswith("finder diff-bugs: agent call failed")
    assert failed.outcome == "failed: timeout" and failed.findings == []
    assert state.checks[1].outcome == "0 findings"


async def test_a_resumed_find_does_not_record_a_finder_twice(target_info, ctx, monkeypatch):
    from review_pipeline import budget

    state = ReviewState(target=target_info)
    monkeypatch.setattr(sdk, "agent_call_structured", Recorded({
        "find:diff-bugs": [FinderOutput(findings=[high_finding()])],
        "find:callers": [budget.BudgetExceededError("stop")]}))
    with pytest.raises(budget.BudgetExceededError):
        await run_find(state, ctx)
    assert [c.subject for c in state.checks] == ["diff-bugs"]
    reloaded = load_state(save_state(state, ctx.snapshot.parent))
    monkeypatch.setattr(sdk, "agent_call_structured", Recorded({"find:callers": [FinderOutput()]}))
    reloaded = await run_find(reloaded, ctx)
    assert [c.subject for c in reloaded.checks] == ["diff-bugs", "callers"]


async def test_verify_records_the_verdict_its_citations_and_the_gate(target_info, ctx, monkeypatch):
    high, medium = high_finding(), medium_finding()
    reads = [ToolCall("Read", {"file_path": "rateplan_service.py", "offset": 1, "limit": 3})]
    monkeypatch.setattr(sdk, "agent_call_structured", Recorded({"verify": [
        Verdict(status="confirmed", reason="r", citations=[KEYWORD_CITATION, KEYWORD_USE_CITATION]),
        Verdict(status="confirmed", reason="no cite"),  # lowered by verdict_gate
    ]}, {"verify": reads}))
    state = await run_verify(ReviewState(target=target_info, stage="find", findings=[high, medium]), ctx)
    one, two = state.checks
    assert (one.stage, one.subject, one.outcome) == ("verify", high.id, "confirmed")
    assert one.given == [f"finding {high.id}"]
    # The read covered lines 1-3, so line 1 was read. Line 6 was not read, but the finding cites it,
    # so the verifier's prompt showed it.
    assert [(c["gate"], c["seen"]) for c in one.verdict["citations"]] == [("pass", "read"), ("pass", "prompt")]
    assert one.verdict["lowered"] is False
    assert two.verdict == {"status": "unverifiable", "reason": two.verdict["reason"], "citations": [], "lowered": True}
    assert two.outcome == "unverifiable (answered confirmed)"
    assert [(g.gate, g.passed, g.rule) for g in state.gate_checks] == [
        ("verdict_gate", True, ""), ("verdict_gate", False, "confirmed without citing")]


async def test_a_failed_verifier_is_recorded_and_gets_no_gate_result(target_info, ctx, monkeypatch):
    monkeypatch.setattr(sdk, "agent_call_structured", Recorded({"verify": [sdk.AgentCallError("boom")]}))
    state = await run_verify(ReviewState(target=target_info, stage="find", findings=[high_finding()]), ctx)
    assert state.checks[0].error.startswith("the verifier failed") and state.checks[0].verdict is None
    assert state.gate_checks == []


async def test_merge_records_the_group_and_marks_merged_findings_in_their_finder_entry(
        target_info, ctx, monkeypatch):
    a, b = high_finding(), medium_finding().with_location(line_start=1, line_end=1)
    monkeypatch.setattr(sdk, "agent_call_structured", Recorded({
        "find:diff-bugs": [FinderOutput(findings=[a])], "find:callers": [FinderOutput(findings=[b])]}))
    state = await run_find(ReviewState(target=target_info), ctx)
    state.verdicts = {f.id: Verdict(finding_id=f.id, status="confirmed", reason="r") for f in state.findings}
    state.stage = "verify"
    ids = [f.id for f in state.findings]
    monkeypatch.setattr(sdk, "agent_call_structured", Recorded({
        "merge": [MergeOutput(duplicate_sets=[DuplicateSet(finding_ids=ids, reason="same")])]}))
    state = await run_merge(state, ctx)
    merge = state.checks[-1]
    assert (merge.stage, merge.given) == ("merge", ["findings (2)"])
    assert merge.outcome == f"merged {ids[1]} into {ids[0]}"
    fates = {e["id"]: (e["fate"], e.get("into")) for c in state.checks for e in c.findings}
    assert fates == {ids[0]: ("kept", None), ids[1]: ("merged", ids[0])}


# --- export, render, state --------------------------------------------------------------


def _recorded_state(canned_state) -> ReviewState:
    high = canned_state.findings[0]
    marked = {**KEYWORD_CITATION.model_dump(), "gate": "pass", "seen": "read"}
    canned_state.checks = [
        AgentCheck(stage="verify", subject=high.id, given=[f"finding {high.id}"],
                              calls=[{"tool": "Read", "target": "rateplan_service.py", "detail": "whole file"}],
                              outcome="confirmed", turns=2, cost_usd=0.1, started_at="2026-10-09T10:00:02.000Z",
                              ended_at="2026-10-09T10:00:03.000Z",
                              verdict={"status": "confirmed", "reason": "r", "citations": [marked], "lowered": False}),
        AgentCheck(stage="find", subject="diff-bugs", given=["diff (3 files)"],
                              calls=[{"tool": "WebFetch", "target": "https://example.com", "detail": ""}],
                              outcome="1 finding", turns=4, cost_usd=0.2, started_at="2026-10-09T10:00:00.000Z",
                              ended_at="2026-10-09T10:00:01.000Z",
                              findings=[{"id": high.id, "claim": high.claim, "severity": "HIGH",
                                         "failure_scenario": "s", "citations": [{**marked, "seen": "not-seen"}],
                                         "fate": "kept"}]),
    ]
    canned_state.gate_checks = [GateCheck(finding_id=high.id, gate="finding_gate", passed=True)]
    return canned_state


def test_checks_file_validates_and_orders_agents_by_start(canned_state, tmp_path):
    state = _recorded_state(canned_state)
    path = write_checks_file(state, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    schema = _schema(CHECKS_SCHEMA)
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(data, schema)
    assert [a["stage"] for a in data["agents"]] == ["find", "verify"]
    assert data["gates"] == [{"finding_id": state.findings[0].id, "gate": "finding_gate", "passed": True, "rule": ""}]
    assert data["target"] == to_findings_file(state)["target"]


def test_checks_file_holds_no_tool_results(canned_state):
    text = json.dumps(to_checks_file(_recorded_state(canned_state)))
    assert "tool_result" not in text and '"content"' not in text


def test_findings_file_validates_with_and_without_verifier_citations(canned_state):
    schema = _schema(SCHEMA)
    data = to_findings_file(canned_state)
    by_claim = {f["claim"]: f for f in data["findings"]}
    high = canned_state.findings[0]
    assert by_claim[high.claim]["verifier_citations"] == [KEYWORD_USE_CITATION.model_dump()]
    assert "verifier_citations" not in [f for f in data["findings"] if f["status"] == "rejected"][0]
    jsonschema.validate(data, schema)
    for f in data["findings"]:
        f.pop("verifier_citations", None)
    jsonschema.validate(data, schema)  # a file written before 0.26


def test_an_old_state_without_checks_still_loads(canned_state, tmp_path):
    path = save_state(canned_state, tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("checks", None)
    raw.pop("gate_checks", None)
    path.write_text(json.dumps(raw), encoding="utf-8")
    state = load_state(path)
    assert state is not None and state.checks == [] and state.gate_checks == []


def test_render_checks_has_the_checklist_then_each_agent_with_marked_citations(canned_state):
    text = render_checks(_recorded_state(canned_state))
    high = canned_state.findings[0]
    assert text.index("## Checklist") < text.index("## Agents")
    assert "| diff-bugs | ran | 0 | 1 | 1 | 0 | 0 | 0 |" in text
    assert f"| `{high.id}` | diff-bugs | HIGH | confirmed | passed | kept |" in text
    assert text.index("### find: diff-bugs") < text.index(f"### verify: {high.id}")
    assert "- WebFetch `https://example.com`" in text
    assert "gate: pass, seen: not-seen ⚠" in text and "gate: pass, seen: read" in text
    assert "KEYWORD = 'dolcebot'" in text
    assert "**Verdict:** confirmed. r" in text
