"""The group step: one call with no tools, its Python gate, and `capabilities.json` (plan, item 14)."""

from __future__ import annotations

import json

import pytest
from code_facts import UNTRUSTED_RULES
from pydantic import ValidationError

from conftest import make_repo, read_state, run
from spec_extract_pipeline import group, write
from spec_extract_pipeline.scope import Symbol


def sym(path: str, name: str, line: int = 1) -> Symbol:
    return Symbol(path=path, name=name, kind="function", line=line)


def ids(caps) -> dict[str, list[str]]:
    return {c.name: c.symbols for c in caps}


@pytest.mark.parametrize("schema, answer", [
    (group.GroupOutput, {}), (group.GroupOutput, {"groups": [{"name": "b", "symbols": ["a.py::x"]}]}),
    (group.GroupOutput, {"capabilities": []}), (write.WriteOutput, {"reqs": [{"name": "r"}]})])
def test_an_answer_with_the_wrong_key_or_no_capability_does_not_parse(schema, answer):
    # So the wrapper sends it back to be reformatted, then counts it as a failed attempt.
    with pytest.raises(ValidationError):
        schema.model_validate(answer)


def test_gate_names_are_kebab_case_and_unique():
    symbols = [sym("a.py", "x"), sym("b.py", "y")]
    answer = {"capabilities": [{"name": "Billing Rules!", "summary": "", "symbols": ["a.py::x"]},
                               {"name": "billing_rules", "summary": "", "symbols": ["b.py::y"]}]}
    assert ids(group.gate(answer, symbols).capabilities) == {"billing-rules": ["a.py::x"],
                                                             "billing-rules-2": ["b.py::y"]}


def test_gate_puts_every_symbol_in_exactly_one_capability():
    symbols = [sym("a.py", "x"), sym("a.py", "y"), sym("b.py", "z")]
    answer = {"capabilities": [{"name": "one", "summary": "", "symbols": ["a.py::x", "nope.py::q"]},
                               {"name": "two", "summary": "", "symbols": ["a.py::x", "b.py::z"]},
                               {"name": "empty", "summary": "", "symbols": []}]}
    g = group.gate(answer, symbols)
    # a duplicate stays in the first; a missing one goes to `unassigned`, last; an empty one is dropped
    assert ids(g.capabilities) == {"one": ["a.py::x"], "two": ["b.py::z"], "unassigned": ["a.py::y"]}
    assert g.unassigned == ["a.py::y"]
    assert g.dropped == ["group named nope.py::q, which is not a public symbol of this target",
                         "capability empty: no symbol, so no file"]


def test_gate_splits_a_capability_over_40_symbols_by_module():
    symbols = [sym("pkg/a.py", f"a{i}", i + 1) for i in range(30)]
    symbols += [sym("pkg/__init__.py", f"b{i}", i + 1) for i in range(30)]
    answer = {"capabilities": [{"name": "big", "summary": "s",
                                "symbols": [f"{s.path}::{s.name}" for s in symbols]}]}
    caps = group.gate(answer, symbols).capabilities
    assert [(c.name, len(c.symbols)) for c in caps] == [("big-a", 30), ("big-pkg", 30)]


def test_gate_splits_one_module_over_40_symbols_into_chunks():
    symbols = [sym("m.py", f"f{i}", i + 1) for i in range(90)]
    answer = {"capabilities": [{"name": "big", "summary": "", "symbols": [f"m.py::f{i}" for i in range(90)]}]}
    caps = group.gate(answer, symbols).capabilities
    assert [(c.name, len(c.symbols)) for c in caps] == [("big-m-1", 40), ("big-m-2", 40), ("big-m-3", 10)]


def test_gate_puts_a_name_defined_twice_where_its_first_def_is():
    symbols = [sym("a.py", "C.x", 3), sym("a.py", "C.y", 5), sym("a.py", "C.x", 7)]
    answer = {"capabilities": [{"name": "c", "symbols": ["a.py::C.y", "a.py::C.x"]}]}
    assert ids(group.gate(answer, symbols).capabilities) == {"c": ["a.py::C.x", "a.py::C.y"]}


def test_gate_orders_symbols_as_the_scope_lists_them():
    symbols = [sym("a.py", "x", 1), sym("a.py", "y", 5)]
    answer = {"capabilities": [{"name": "c", "summary": "", "symbols": ["a.py::y", "a.py::x"]}]}
    assert ids(group.gate(answer, symbols).capabilities) == {"c": ["a.py::x", "a.py::y"]}


def test_group_call_has_no_tools_and_writes_capabilities_json(tmp_path, fake_call):
    repo = make_repo(tmp_path / "widgets")
    root = tmp_path / "out"
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
    (run_dir,) = [p for p in (root / "widgets").iterdir() if p.name != ".locks"]
    kwargs = next(k for k in fake_call.kwargs if k["budget_label"] == "group")
    assert kwargs["allowed_tools"] == []
    prompt = fake_call.prompts["group"]
    assert UNTRUSTED_RULES in prompt
    assert "- src/billing/charge.py::charge (function, line 1)" in prompt
    assert "f1:" in prompt  # the gated facts
    doc = json.loads((run_dir / "capabilities.json").read_text())
    assert [c["name"] for c in doc["capabilities"]] == ["one", "charge", "two"]
    state = read_state(run_dir)
    assert state["group_attempts"] == 1
    assert state["steps_done"][:4] == ["resolve", "scope", "explore", "group"]


def test_no_group_answer_after_two_attempts_exits_1(tmp_path, capsys, fake_call):
    repo = make_repo(tmp_path / "widgets")
    root = tmp_path / "out"
    fake_call.script["group"] = ["fail", "fail"]
    argv = ("run", "--repo", repo, "--tree", "HEAD", "--out", root)
    assert run(*argv) == 1
    (run_dir,) = [p for p in (root / "widgets").iterdir() if p.name != ".locks"]
    assert "- not grouped: group gave no answer in 2 attempts" in (run_dir / "report.md").read_text()
    assert not (run_dir / "registry").exists()
    assert "error" in read_state(run_dir)["group_answer"]
    assert run(*argv) == 1  # a re-run makes no call and exits 1 again
    assert fake_call.calls["group"] == 2
    assert "SPEC_EXTRACT:NOT_GROUPED" in capsys.readouterr().out


def test_group_budget_stop_takes_the_attempt_back(tmp_path, fake_call):
    repo = make_repo(tmp_path / "widgets")
    root = tmp_path / "out"
    fake_call.script["group"] = ["over"]
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 3
    (run_dir,) = [p for p in (root / "widgets").iterdir() if p.name != ".locks"]
    state = read_state(run_dir)
    assert state["group_attempts"] == 0 and state["group_answer"] is None
    assert run("run", "--repo", repo, "--tree", "HEAD", "--out", root) == 0
