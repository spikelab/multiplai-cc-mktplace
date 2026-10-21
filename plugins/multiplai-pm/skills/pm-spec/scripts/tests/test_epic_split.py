"""The epic split: the answer's checks, and child change dirs. Adapted from
buildme's `test_epic_split.py` (`check_children` cases, the retry with the
errors, `--no-epic-split` making no call)."""

from __future__ import annotations

import pytest

from pm_spec_pipeline.epic import SplitAnswer, check_answer, waves
from pm_spec_pipeline.state import EpicChild

from conftest import change_dir, meta, run, state, write

PLAN = "# Plan: {t}\n\n> File: {i}.md\n\n**Objective:** {t}.\n\n## Context\n\n## Outcome\n\n## Done means\n1. it works\n"


def child(cid, deps=(), plan=None, ref=""):
    return {"id": cid, "title": cid.title(), "depends_on": list(deps),
            "plan": PLAN.format(t=cid, i=cid) if plan is None else plan, "ticket_ref": ref}


def answer(*children):
    return SplitAnswer.model_validate({"single": False, "reason": "two deliverables", "children": list(children)})


SPLIT = {"single": False, "reason": "Two deliverables.",
         "children": [child("api", ref="T-1"), child("ui", deps=["api"], ref="T-2")]}
PLAIN_SPLIT = {"single": False, "reason": "Two deliverables.", "children": [child("api"), child("ui", deps=["api"])]}


class TestCheckAnswer:
    def test_a_valid_split_has_no_errors(self):
        assert check_answer(answer(child("a"), child("b", ["a"])), []) == []

    def test_one_change_needs_no_children(self):
        assert check_answer(SplitAnswer(single=True), []) == []

    @pytest.mark.parametrize("bad", ["Upper", "under_score", "-lead", "a--b"])
    def test_ids_are_lowercase_and_hyphenated(self, bad):
        assert any("lowercase" in e for e in check_answer(answer(child(bad), child("b")), []))

    def test_ids_are_unique(self):
        assert any("used twice" in e for e in check_answer(answer(child("a"), child("a")), []))

    def test_every_dependency_names_a_child(self):
        assert any("not a child" in e for e in check_answer(answer(child("a", ["z"]), child("b")), []))

    def test_a_cycle_is_named(self):
        errors = check_answer(answer(child("a", ["b"]), child("b", ["a"])), [])
        assert any("cycle" in e and "a -> b -> a" in e for e in errors)

    @pytest.mark.parametrize("plan,missing", [("## Done means\n1.\n", "# Plan:"), ("# Plan: x\n", "## Done means")])
    def test_each_plan_has_a_title_and_done_means(self, plan, missing):
        assert any(missing in e for e in check_answer(answer(child("a", plan=plan), child("b")), []))

    def test_a_split_needs_two_children(self):
        assert any("at least two" in e for e in check_answer(answer(child("a")), []))

    def test_a_ticket_ref_must_be_one_of_the_tickets_children(self):
        assert any("T-9" in e for e in check_answer(answer(child("a", ref="T-9"), child("b")), ["T-1"]))
        assert check_answer(answer(child("a", ref="T-1"), child("b")), ["T-1"]) == []

    def test_an_answer_that_drops_a_ticket_child_is_rejected(self):
        errors = check_answer(answer(child("a", ref="T-1"), child("b")), ["T-1", "T-2"])
        assert any("T-2" in e and "no child change" in e for e in errors)

    def test_one_change_drops_two_or_more_ticket_children(self):
        assert any("T-1" in e for e in check_answer(SplitAnswer(single=True), ["T-1", "T-2"]))
        assert check_answer(SplitAnswer(single=True), ["T-1"]) == []

    def test_a_ticket_child_given_to_two_children_is_rejected(self):
        errors = check_answer(answer(child("a", ref="T-1"), child("b", ref="T-1")), ["T-1"])
        assert any("T-1" in e and "twice" in e for e in errors)

    def test_waves_put_dependencies_first(self):
        kids = [EpicChild(**child("c", ["b"])), EpicChild(**child("b", ["a"])), EpicChild(**child("a"))]
        assert waves(kids) == ["a", "b", "c"]


def test_a_split_creates_one_change_dir_per_child_and_stops(repo, model, tmp_path, capsys):
    ticket = write(tmp_path / "t.md", "Throttle the API.\n")
    model["epic_split"] = SPLIT
    assert run(repo, "new", "throttle", "--auto", "--ticket-file", str(ticket), "--ticket-ref", "EPIC-9",
               "--ticket-child", "T-1", "--ticket-child", "T-2") == 0
    out = capsys.readouterr().out
    parent = change_dir(repo)
    kids = [change_dir(repo, "throttle-api"), change_dir(repo, "throttle-ui")]
    assert f"SPEC:SPLIT:{parent}" in out
    assert [line for line in out.splitlines() if line.startswith("SPEC:CHILD:")] == \
        [f"SPEC:CHILD:{k}" for k in kids]
    assert "spec:proposal" not in model.labels()
    for kid, ref in zip(kids, ("T-1", "T-2")):
        assert meta(kid)["parent"] == "throttle"
        assert meta(kid)["ticket_refs"] == {"epic": ref, "children": []}
        assert meta(kid)["spec_status"] == "draft"
        assert "## Done means" in (kid / "plan.md").read_text()
        s = state(kid)
        assert s["parent"] == "throttle" and s["options"]["epic_split"] is False
        assert s["options"]["ticket_ref"] == ref


def test_a_child_is_specified_by_its_own_resume_from_its_plan(repo, model):
    model["epic_split"] = PLAIN_SPLIT
    assert run(repo, "new", "throttle", "--auto") == 0
    n_split = model.calls_to("epic_split")
    assert run(repo, "resume", "throttle-api") == 0
    assert model.calls_to("epic_split") == n_split  # a child never splits again
    assert "# Plan: api" in model.prompts("interview:questions")[-1]
    assert (change_dir(repo, "throttle-api") / "proposal.md").is_file()


def test_a_rejected_answer_is_retried_once_with_the_errors(repo, model, capsys):
    bad = {"single": False, "reason": "", "children": [child("a", ["b"]), child("b", ["a"])]}
    model["epic_split"] = [bad, bad]
    assert run(repo, "new", "throttle", "--auto") == 1
    assert "rejected twice" in capsys.readouterr().out
    assert "cycle" in model.prompts("epic_split")[1]


def test_no_epic_split_makes_no_call(repo, model):
    assert run(repo, "new", "throttle", "--auto", "--no-epic-split") == 0
    assert "epic_split" not in model.labels()


def test_a_split_that_drops_a_ticket_child_is_retried_then_stops(repo, model, capsys):
    dropped = {"single": False, "reason": "", "children": [child("api", ref="T-1"), child("ui")]}
    model["epic_split"] = [dropped, dropped]
    assert run(repo, "new", "throttle", "--auto", "--ticket-ref", "EPIC-9", "--ticket-child", "T-1",
               "--ticket-child", "T-2") == 1
    assert model.calls_to("epic_split") == 2
    assert "T-2" in model.prompts("epic_split")[1]
    assert "rejected twice" in capsys.readouterr().out


def test_a_child_starts_with_its_own_budget(repo, model):
    model["epic_split"] = PLAIN_SPLIT
    assert run(repo, "new", "throttle", "--auto") == 0
    parent_calls = state(change_dir(repo))["budget"]["calls"]
    assert parent_calls == len(model.calls) > 0
    assert state(change_dir(repo, "throttle-api"))["budget"] is None
    before = len(model.calls)
    assert run(repo, "resume", "throttle-api") == 0
    assert state(change_dir(repo, "throttle-api"))["budget"]["calls"] == len(model.calls) - before


def test_the_split_follows_the_tickets_children_first(repo, model):
    model["epic_split"] = SPLIT
    assert run(repo, "new", "throttle", "--auto", "--ticket-ref", "EPIC-9", "--ticket-child", "T-1",
               "--ticket-child", "T-2") == 0
    prompt = model.prompts("epic_split")[0]
    assert "T-1, T-2" in prompt and "first" in prompt
