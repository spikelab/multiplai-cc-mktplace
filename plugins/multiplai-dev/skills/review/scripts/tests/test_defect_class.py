"""`defect_class` and `other_locations`: what a finder names, `locations_gate`, merge, export, render, post.

Every test name holds `defect_class`, `other_locations` or `locations_gate`, so
`pytest -k "defect_class or other_locations or locations_gate"` selects them all
(Done-means item 10). Row S13 of the plan's stop-and-resume table is
`test_defect_class_s13_*`. No agent is called: the finders and the merger are canned.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from conftest import KEYWORD_CITATION, KEYWORD_USE_CITATION, SCHEMA, high_finding, medium_finding
from fixture_repo import KEYWORD_USE_LINE
from review_pipeline import sdk
from review_pipeline.config import ReviewConfig
from review_pipeline.export import to_findings_file, write_findings_file
from review_pipeline.gates import gated_locations, locations_gate
from review_pipeline.models import (ConvertOutput, Finding, FinderOutput, FinderReport, FinderResult, Piece,
                                    ReviewState, Verdict)
from review_pipeline.paths_io import PathView
from review_pipeline.post import inline_comment, review_body
from review_pipeline.prompts import convert as convert_prompt
from review_pipeline.prompts import find as find_prompt
from review_pipeline.prompts.path import build_path
from review_pipeline.prompts.piece import CLASS_AND_PLACES, build_piece
from review_pipeline.render import class_line, finding_section
from review_pipeline.stages import RunContext
from review_pipeline.stages.find import map_converted, run_find
from review_pipeline.stages.merge import merge_set
from review_pipeline.state import STATE_FILE, load_state, save_state

GOOD = f"rateplan_service.py:{KEYWORD_USE_LINE}"
CLASS = "keyword matched without case folding"
SENTENCE = ("Name the kind of defect in a few words in `defect_class`, and list every other place with the "
            "same defect as `path:line` in `other_locations`.")


class Canned:
    """Stands in for `sdk.agent_call_structured`; `answers[label]` is consumed in order."""

    def __init__(self, answers: dict[str, list]):
        self.answers = answers
        self.calls: list[str] = []

    async def __call__(self, prompt, schema, *, budget_label="", **kwargs):
        self.calls.append(budget_label)
        answer = self.answers[budget_label].pop(0)
        return schema.model_validate(answer.model_dump())


@pytest.fixture
def ctx(target_info, tmp_path) -> RunContext:
    snapshot = tmp_path / "tree"
    snapshot.mkdir()
    return RunContext(config=ReviewConfig(concurrency=2, dimensions=("diff-bugs", "callers")),
                      snapshot=snapshot, diff=Path(target_info.diff_path).read_text())


# --- the finding and its parse ----------------------------------------------------


def test_defect_class_and_other_locations_default_empty_and_parse_loosely():
    plain = high_finding()
    assert plain.defect_class == "" and plain.other_locations == []
    loose = Finding.model_validate({**plain.model_dump(), "defect_class": None, "other_locations": " a.py:3 "})
    assert loose.defect_class == "" and loose.other_locations == ["a.py:3"]
    listed = Finding.model_validate({**plain.model_dump(), "other_locations": ["a.py:3", "", None, 7]})
    assert listed.other_locations == ["a.py:3"]  # a non-string is no place; the gate judges the rest
    assert loose.id == plain.id  # neither field is part of the v1 id


def test_defect_class_and_other_locations_carry_through_convert():
    out = ConvertOutput.model_validate({"items": [{
        "kind": "defect", "severity": "MEDIUM", "file": "rateplan_service.py", "line_start": 1, "line_end": 1,
        "claim": "c", "failure_scenario": "f", "citations": [KEYWORD_CITATION.model_dump()],
        "defect_class": CLASS, "other_locations": GOOD}]})
    _, (finding,) = map_converted("piece@1", out)
    assert finding.defect_class == CLASS and finding.other_locations == [GOOD]


# --- prompts ----------------------------------------------------------------------


def test_defect_class_sentence_is_in_both_file_rules_and_both_schemas(target_info):
    assert SENTENCE in find_prompt.FILE_RULE and SENTENCE in find_prompt.FILE_RULE_TREE
    change = find_prompt.build(target_info, "diff-bugs", "diff")
    tree = find_prompt.build(target_info.model_copy(update={"kind": "tree"}), "diff-bugs", "", files=["a.py"])
    for text in (change, tree):
        assert SENTENCE in text and '"defect_class"' in text and '"other_locations"' in text
    assert '"defect_class"' in convert_prompt.SCHEMA and '"other_locations"' in convert_prompt.SCHEMA
    assert "`Also at:`" in convert_prompt.build_convert("piece@1", "report")


def test_defect_class_and_other_locations_are_asked_of_piece_and_path_finders():
    piece = build_piece(Piece(id=1, group="", files=["a.py"]), "diff", workspace="ws")
    view = PathView(id="p1", name="entry", file="a.py", line=1, handler=None, invokers=("cli",), hops=(),
                    changed_lines=(), e2e=(), broken_at=None)
    path = build_path(view, workspace="ws")
    for text in (piece, path):
        assert CLASS_AND_PLACES in text
    assert "in the diff or the snapshot" in CLASS_AND_PLACES


# --- locations_gate ---------------------------------------------------------------


@pytest.mark.parametrize("location, passed, rule", [
    (GOOD, True, ""),
    ("rateplan_service.py:1-6", True, ""),  # the file's last line
    ("rateplan_service.py:7", False, "line past the end of the file"),  # the empty string after the last newline
    ("rateplan_service.py:5-9", False, "line past the end of the file"),
    ("gone.py:3", False, "path not at head"),
    ("rateplan_service.py", False, "not a path:line location"),
    ("rateplan_service.py:0", False, "not a path:line location"),
    ("rateplan_service.py:4-2", False, "not a path:line location"),
    ("https://example.com/x:3", False, "path not at head"),
])
def test_locations_gate_checks_each_location_at_head(target_info, location, passed, rule):
    result = locations_gate(target_info, location)
    assert result.passed is passed
    assert rule in result.reason


def test_locations_gate_keeps_the_finding_and_drops_only_bad_locations(target_info):
    finding = high_finding().model_copy(update={"other_locations": [GOOD, "gone.py:3"], "defect_class": CLASS})
    kept, dropped = gated_locations(target_info, finding)
    assert kept.id == finding.id and kept.defect_class == CLASS and kept.other_locations == [GOOD]
    assert len(dropped) == 1 and "gone.py:3" in dropped[0]
    unchanged, none = gated_locations(target_info, high_finding())
    assert none == [] and unchanged.other_locations == []


async def test_locations_gate_runs_in_find_with_a_line_in_errors(target_info, ctx, monkeypatch):
    high = high_finding().with_location(defect_class=CLASS, other_locations=[
        str(ctx.snapshot / GOOD), "gone.py:3", "rateplan_service.py:99"])
    canned = Canned({"find:diff-bugs": [FinderOutput(findings=[high])], "find:callers": [FinderOutput()],
                     "find:trim": [FinderOutput()]})
    monkeypatch.setattr(sdk, "agent_call_structured", canned)
    state = await run_find(ReviewState(target=target_info), ctx)
    (kept,) = state.findings
    assert kept.id == high.id and kept.defect_class == CLASS
    assert kept.other_locations == [GOOD]  # made repo-relative, then checked
    lines = [e for e in state.errors if e.startswith("locations_gate")]
    assert len(lines) == 2 and all(high.id in e for e in lines)
    assert "path not at head" in lines[0] and "line past the end of the file" in lines[1]


# --- merge ------------------------------------------------------------------------


def test_merge_set_takes_the_union_of_other_locations_and_the_survivors_defect_class(target_info):
    keep = high_finding().model_copy(update={"defect_class": CLASS, "other_locations": ["a.py:1", "b.py:2"]})
    copy = medium_finding().model_copy(update={"defect_class": "another name", "other_locations": ["b.py:2", "c.py:3"]})
    state = ReviewState(target=target_info, findings=[keep, copy], verdicts={
        keep.id: Verdict(finding_id=keep.id, status="confirmed", reason="r", citations=[KEYWORD_USE_CITATION]),
        copy.id: Verdict(finding_id=copy.id, status="unverifiable", reason="r")})
    merged = merge_set([copy, keep], state)
    assert merged.id == keep.id and merged.defect_class == CLASS
    assert merged.other_locations == ["a.py:1", "b.py:2", "c.py:3"]


# --- export, render, post ---------------------------------------------------------


def test_defect_class_and_other_locations_round_trip_through_the_schema(canned_state, tmp_path):
    high = canned_state.findings[0]
    canned_state.findings[0] = high.model_copy(update={"defect_class": CLASS, "other_locations": [GOOD]})
    path = write_findings_file(canned_state, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.validate(data, json.loads(SCHEMA.read_text(encoding="utf-8")))
    rows = {f["id"]: f for f in data["findings"]}
    assert rows[high.id]["defect_class"] == CLASS and rows[high.id]["other_locations"] == [GOOD]
    others = [f for fid, f in rows.items() if fid != high.id]
    assert others and all("defect_class" not in f and "other_locations" not in f for f in others)


def test_other_locations_null_or_empty_fails_the_schema(canned_state):
    data = to_findings_file(canned_state)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    for bad in ({"other_locations": []}, {"other_locations": None}, {"defect_class": ""},
                {"other_locations": ["no line here"]}):
        data["findings"][0].update(bad)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(data, schema)
        for key in bad:
            del data["findings"][0][key]


def _row(**extra) -> dict:
    return {"id": "0123456789", "severity": "HIGH", "status": "confirmed", "claim": "the claim",
            "file": "rateplan_service.py", "line_start": 1, "line_end": 1, "failure_scenario": "it fails",
            "citations": [KEYWORD_CITATION.model_dump()], "verdict_reason": "r", "expected_behaviour": None,
            **extra}


def test_defect_class_line_in_the_review_under_the_claim():
    text = finding_section(_row(defect_class=CLASS, other_locations=[GOOD, "a.py:2-4"]), web_base=None,
                           head_sha="abc")
    lines = text.splitlines()
    assert lines[2] == f"class: {CLASS}; also at: {GOOD}, a.py:2-4"
    assert class_line(_row(other_locations=["a.py:2"])) == "also at: a.py:2"
    assert class_line(_row()) == ""
    assert "class:" not in finding_section(_row(), web_base=None, head_sha="abc")


def test_defect_class_line_in_the_pr_comment_and_the_review_body():
    row = _row(defect_class=CLASS, other_locations=[GOOD])
    comment = inline_comment(row, {"rateplan_service.py": {1}}, "abcdef012345")
    body = comment["body"].splitlines()
    assert body[0] == "**HIGH** — the claim" and body[2] == f"class: {CLASS}; also at: {GOOD}"
    assert body[4] == "it fails"
    outside = review_body("https://github.com/o/r", "abc", 1, 0, [row], "abcdef012345")
    assert f"class: {CLASS}; also at: {GOOD}" in outside
    plain = inline_comment(_row(), {"rateplan_service.py": {1}}, "abcdef012345")["body"]
    assert "class:" not in plain and "also at:" not in plain


# --- S13: a checkpoint written before 0.95 ------------------------------------------


def _strip_new_keys(value):
    """*value* with every `defect_class` and `other_locations` key removed, at any depth."""
    if isinstance(value, dict):
        return {k: _strip_new_keys(v) for k, v in value.items() if k not in ("defect_class", "other_locations")}
    if isinstance(value, list):
        return [_strip_new_keys(v) for v in value]
    return value


async def test_defect_class_s13_old_checkpoint_loads_empty_and_asks_nothing_again(target_info, ctx, tmp_path,
                                                                                  monkeypatch):
    high = high_finding()
    stored = ReviewState(target=target_info, stage="paths", finder_results={
        "diff-bugs": FinderResult(findings=[high]), "callers": FinderResult(), "trim": FinderResult()},
        finder_reports={"piece@1": FinderReport(key="piece@1", text="r", converted=True, findings=[high])})
    target_dir = tmp_path / "checkpoint"
    target_dir.mkdir()
    save_state(stored, target_dir)
    path = target_dir / STATE_FILE
    old = _strip_new_keys(json.loads(path.read_text(encoding="utf-8")))
    assert '"defect_class":' not in json.dumps(old) and '"other_locations":' not in json.dumps(old)
    path.write_text(json.dumps(old), encoding="utf-8")

    state = load_state(path)
    assert state is not None
    loaded = state.finder_results["diff-bugs"].findings[0]
    assert loaded.id == high.id and loaded.defect_class == "" and loaded.other_locations == []
    assert state.finder_reports["piece@1"].findings[0].other_locations == []

    async def no_call(*args, **kwargs):
        raise AssertionError("a stored finder was asked again")

    monkeypatch.setattr(sdk, "agent_call_structured", no_call)
    state = await run_find(state, ctx)
    assert [f.id for f in state.findings] == [high.id]
    assert state.findings[0].defect_class == "" and state.findings[0].other_locations == []
    assert not [e for e in state.errors if e.startswith("locations_gate")]


# --- malformed other_locations in a raw model answer ------------------------------

_MISSING = object()
_PLACE = "direct_booking.py:5"


@pytest.mark.parametrize("route", ["finder", "converter"])
@pytest.mark.parametrize(("value", "expected"), [
    (_MISSING, []), (None, []), (False, []), (True, []), (42, []), (1.25, []),
    ({_PLACE: True}, []), ({}, []), ("  " + _PLACE + "  ", [_PLACE]),
    ([None, False, 8, {_PLACE: True}, [_PLACE], " ", _PLACE], [_PLACE]),
], ids=["missing", "null", "false", "true", "integer", "float", "object", "empty-object", "string", "mixed-list"])
async def test_other_locations_of_any_json_type_keep_the_finding(tmp_path, route, value, expected):
    """A model's answer is parsed by the real SDK path: only `sdk._run` is canned."""
    from unittest.mock import patch

    from fixture_repo import build
    from review_pipeline.models import TargetInfo

    repo = tmp_path / "repo"
    base, head = build(repo)
    target = TargetInfo(repo_path=str(repo), base_sha=base, head_sha=head, slug="test",
                        files=["rateplan_service.py"])
    original = high_finding()
    data = original.model_dump()
    data.pop("other_locations")
    if value is not _MISSING:
        data["other_locations"] = value
    calls = []

    async def answer(prompt, **kwargs):
        calls.append(kwargs.get("budget_label"))
        return json.dumps({("findings" if route == "finder" else "items"): [data]})

    with patch.object(sdk, "_run", answer):
        if route == "finder":
            ctx = RunContext(config=ReviewConfig(concurrency=1, dimensions=("diff-bugs",), trim=False),
                             snapshot=repo, diff="")
            found = (await run_find(ReviewState(target=target), ctx)).findings
        else:
            out = await sdk.agent_call_structured("convert", ConvertOutput, allowed_tools=[], cwd=str(repo))
            _, found = map_converted("piece@1", out)
            found = [gated_locations(target, f)[0] for f in found]
    assert len(calls) == 1
    (kept,) = found
    assert (kept.claim, kept.severity, kept.citations) == (original.claim, original.severity, original.citations)
    if route == "finder":
        assert kept.id == original.id
    assert kept.other_locations == expected


def test_other_locations_never_reads_an_object_key_as_a_place():
    plain = high_finding().model_dump()
    assert Finding.model_validate({**plain, "other_locations": {"a.py:3": "b.py:4"}}).other_locations == []
    assert Finding.model_validate({**plain, "other_locations": [{"a.py:3": 1}, ["b.py:4"]]}).other_locations == []
