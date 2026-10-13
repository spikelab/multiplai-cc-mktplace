"""The explore step with a fake `CallFn`: the dispatch rule, the budget rule, and
`facts.json`'s ids, which do not depend on the order answers came back in."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from code_facts import UNTRUSTED_RULES

from conftest import FIXED, git, make_repo, make_units_repo, read_state, run
from spec_extract_pipeline import explore
from spec_extract_pipeline.scope import TargetScope
from spec_extract_pipeline.state import State
from spec_extract_pipeline.target import Target

# These tests are about resolve, scope and explore: the run ends after explore, as it did
# before the group and write steps existed.
pytestmark = pytest.mark.usefixtures("stops_after_explore")


def _repo_with_units(tmp_path, n: int = 3):
    return make_units_repo(tmp_path / "widgets", n)


def _go(repo, root, *extra, now=FIXED) -> int:
    return run("run", "--repo", repo, "--tree", "HEAD", "--out", root, *extra, now=now)


def _folder(root):
    (folder,) = (root / "widgets").glob("tree@*")
    return folder


def test_explore_writes_facts_and_report(tmp_path, capsys, fake_call):
    repo = make_repo(tmp_path / "widgets")
    root = tmp_path / "out"
    assert _go(repo, root) == 0
    assert "SPEC_EXTRACT:EXPLORED 1/1" in capsys.readouterr().out
    folder = _folder(root)
    facts = json.loads((folder / "facts.json").read_text())
    assert facts["kind"] == "tree" and facts["head_sha"] == git(repo, "rev-parse", "HEAD")
    assert facts["base_sha"] == explore.EMPTY_TREE
    assert facts["generated_at"] == "2026-10-13T12:00:00Z"
    assert [f["id"] for f in facts["facts"]][:3] == ["f1", "f2", "f3"]
    assert {f["path"] for f in facts["files"]} == {"src/a/one.py", "src/billing/charge.py", "src_a/two.py"}
    state = read_state(folder)
    assert state["steps_done"] == ["resolve", "scope", "explore"]
    assert state["explore_attempts"] == {"unit:u1": 1}
    assert state["budget"]["cost_usd"] == 1.0
    # The agent gets read-only tools, on the snapshot, and a prompt that says the code is data.
    (kw,) = fake_call.kwargs
    assert kw["allowed_tools"] == ["Read", "Glob", "Grep"]
    assert kw["cwd"] == str(folder / "snapshot")
    assert not (folder / "facts.json.tmp").exists() and not (folder / "report.md.tmp").exists()


def test_prompt_carries_the_untrusted_rules(tmp_path):
    repo = make_repo(tmp_path / "widgets")
    root = tmp_path / "out"
    assert _go(repo, root) == 0
    state = State.model_validate(read_state(_folder(root)))
    prompt = explore.unit_prompt(state.target, state.scope.units[0])
    assert UNTRUSTED_RULES in prompt


def test_the_prompt_names_the_code(tmp_path):
    t = Target(kind="tree", repo=str(tmp_path / "widgets"), head_sha="a" * 40)
    assert explore.label(t) == "widgets"
    assert explore.label(t.model_copy(update={"path": "src/a"})) == "widgets/src/a"
    assert explore.label(t.model_copy(update={"kind": "pr", "pr": 7})) == "widgets (PR #7)"
    d = Target(kind="dir", repo=str(tmp_path / "run" / "source"), head_sha="a" * 40, source=str(tmp_path / "x" / "lib"))
    assert explore.label(d) == "lib"


def test_a_failed_call_is_stored_as_an_error_and_counted(tmp_path, capsys, fake_call):
    repo = _repo_with_units(tmp_path)
    root = tmp_path / "out"
    fake_call.script["d1/mod.py"] = ["fail", "fail"]  # both attempts fail
    fake_call.script["d2/mod.py"] = ["fail"]  # the first fails, round 2 answers
    assert _go(repo, root) == 0
    assert "SPEC_EXTRACT:EXPLORED 2/3" in capsys.readouterr().out
    state = read_state(_folder(root))
    assert state["explore_attempts"] == {"unit:u1": 1, "unit:u2": 2, "unit:u3": 2}
    assert state["explore_answers"]["unit:u2"] == {"error": "fake failure for d1/mod.py"}
    assert "facts" in state["explore_answers"]["unit:u3"]
    assert fake_call.calls == {"d0/mod.py": 1, "d1/mod.py": 2, "d2/mod.py": 2}
    report = (_folder(root) / "report.md").read_text()
    assert "- `d1/mod.py`: not analysed: explore gave no answer in 2 attempts" in report
    # Final after 2 attempts: a re-run asks nothing and writes the same bytes, on a later
    # clock too: facts.json's generated_at is the time explore first finished, kept in state.
    before = {n: (_folder(root) / n).read_bytes() for n in ("facts.json", "report.md", "plan.txt")}
    assert _go(repo, root, now=FIXED + timedelta(days=1)) == 0
    assert fake_call.calls == {"d0/mod.py": 1, "d1/mod.py": 2, "d2/mod.py": 2}
    assert {n: (_folder(root) / n).read_bytes() for n in before} == before


def test_budget_error_from_the_call_gives_exit_3_and_no_error_answer(tmp_path, capsys, fake_call):
    repo = _repo_with_units(tmp_path)
    root = tmp_path / "out"
    fake_call.script["d1/mod.py"] = ["over"]
    assert _go(repo, root) == 3
    assert "higher --max-usd" in capsys.readouterr().err
    state = read_state(_folder(root))
    assert "unit:u2" not in state["explore_answers"]  # no error answer
    assert "unit:u2" not in state["explore_attempts"]  # the attempt was taken back: unchanged
    assert "explore" not in state["steps_done"]
    assert not (_folder(root) / "facts.json").exists()
    # The units in flight finished and were saved.
    assert set(state["explore_answers"]) == {"unit:u1", "unit:u3"}


def test_budget_error_on_a_retry_takes_back_only_that_attempt(tmp_path, fake_call):
    repo = _repo_with_units(tmp_path)
    root = tmp_path / "out"
    fake_call.script["d1/mod.py"] = ["fail", "over"]
    assert _go(repo, root) == 3
    state = read_state(_folder(root))
    # Round 1 counted u2's first attempt and stored its error; round 2 deleted the error,
    # counted a second attempt, was stopped by the budget and took that attempt back.
    assert state["explore_attempts"]["unit:u2"] == 1
    assert "unit:u2" not in state["explore_answers"]
    # Resumed: the dispatch rule still allows u2 one more attempt.
    assert _go(repo, root) == 0
    state = read_state(_folder(root))
    assert state["explore_attempts"]["unit:u2"] == 2
    assert "facts" in state["explore_answers"]["unit:u2"]


def test_budget_check_comes_before_counting(tmp_path, fake_call):
    repo = _repo_with_units(tmp_path)
    root = tmp_path / "out"
    assert _go(repo, root, "--max-usd", "1.5") == 3
    state = read_state(_folder(root))
    # u1 ($1) and u2 ($2) passed the check; u3's check saw $2 >= $1.5 and counted nothing.
    assert state["explore_attempts"] == {"unit:u1": 1, "unit:u2": 1}
    assert fake_call.calls == {"d0/mod.py": 1, "d1/mod.py": 1}
    assert state["budget"]["cost_usd"] == 2.0


def test_ids_do_not_depend_on_answer_order(tmp_path, fake_call):
    repo = _repo_with_units(tmp_path)
    root = tmp_path / "out"
    assert _go(repo, root) == 0
    folder = _folder(root)
    state = State.model_validate(read_state(folder))
    scope: TargetScope = state.scope
    answers = state.explore_answers
    forward = explore.facts_document(state.target, scope, answers, folder, FIXED)
    backward = explore.facts_document(state.target, scope, dict(reversed(list(answers.items()))), folder, FIXED)
    assert list(answers) != list(reversed(list(answers)))
    assert forward == backward
    assert [f["id"] for f in forward["facts"]] == [f"f{i}" for i in range(1, len(forward["facts"]) + 1)]



def test_the_gate_drops_a_made_up_citation_and_lists_uncited_files(tmp_path):
    repo = make_repo(tmp_path / "widgets")
    root = tmp_path / "out"
    assert _go(repo, root) == 0
    folder = _folder(root)
    state = State.model_validate(read_state(folder))
    (key,) = state.explore_answers
    answer = state.explore_answers[key]
    # One fact cites a line that does not say what its quote says; nothing cites src_a/two.py.
    made_up = next(f for f in answer["facts"] if f["citations"][0]["path"] == "src/a/one.py")
    made_up["citations"][0]["quote"] = "def three():"
    answer["facts"] = [f for f in answer["facts"] if f["citations"][0]["path"] != "src_a/two.py"]
    answer["files"] = [f for f in answer["files"] if f["path"] != "src_a/two.py"]

    doc = explore.facts_document(state.target, state.scope, state.explore_answers, folder, FIXED)
    assert [d["what"] for d in doc["dropped"]] == [made_up["claim"]]
    assert all(c["quote"] != "def three():" for f in doc["facts"] for c in f["citations"])
    assert doc["uncovered"] == ["src_a/two.py"]
    one = next(f for f in doc["files"] if f["path"] == "src/a/one.py")
    assert one["purpose"] == "holds one"
    assert (one["citation"]["path"], one["citation"]["line_start"], one["citation"]["quote"]) == (
        "src/a/one.py", 1, "def one():")
