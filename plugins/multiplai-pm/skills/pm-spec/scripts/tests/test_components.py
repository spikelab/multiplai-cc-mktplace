"""The components step: the answer's checks, and the files it writes. Adapted
from buildme's `test_epic_split.py` (`check_children` cases, the retry with
the errors)."""

from __future__ import annotations

import pytest
from conftest import TWO, change_dir, component, meta, run, state, write
from pm_spec_pipeline.epic import SplitAnswer, check_answer, components_table, waves
from pm_spec_pipeline.state import EpicChild


def answer(*components):
    return SplitAnswer.model_validate({"reason": "two deliverables", "components": list(components)})


TICKETED = {
    "reason": "Two deliverables.",
    "components": [component("api", ticket_ref="T-1"), component("ui", deps=["api"], ticket_ref="T-2")],
}


class TestCheckAnswer:
    def test_a_valid_answer_has_no_errors(self):
        assert check_answer(answer(component("a"), component("b", ["a"])), []) == []
        assert check_answer(answer(component("a")), []) == []

    def test_no_component_is_rejected(self):
        assert any("no component" in e for e in check_answer(answer(), []))

    @pytest.mark.parametrize("bad", ["Upper", "under_score", "-lead", "a--b"])
    def test_ids_are_lowercase_and_hyphenated(self, bad):
        assert any("lowercase" in e for e in check_answer(answer(component(bad), component("b")), []))

    def test_ids_are_unique(self):
        assert any("used twice" in e for e in check_answer(answer(component("a"), component("a")), []))

    def test_every_dependency_names_a_component(self):
        errors = check_answer(answer(component("a", ["z"]), component("b")), [])
        assert any("not a component" in e for e in errors)

    def test_a_cycle_is_named(self):
        errors = check_answer(answer(component("a", ["b"]), component("b", ["a"])), [])
        assert any("cycle" in e and "a -> b -> a" in e for e in errors)

    @pytest.mark.parametrize("plan,missing", [("## Done means\n1.\n", "# Plan:"), ("# Plan: x\n", "## Done means")])
    def test_each_plan_has_a_title_and_done_means(self, plan, missing):
        bad = {**component("a"), "plan": plan}
        assert any(missing in e for e in check_answer(answer(bad, component("b")), []))

    def test_a_ticket_ref_must_be_one_of_the_tickets_children(self):
        assert any("T-9" in e for e in check_answer(answer(component("a", ticket_ref="T-9")), ["T-1"]))
        assert check_answer(answer(component("a", ticket_ref="T-1")), ["T-1"]) == []

    def test_an_answer_that_drops_a_ticket_child_is_rejected(self):
        errors = check_answer(answer(component("a", ticket_ref="T-1"), component("b")), ["T-1", "T-2"])
        assert any("T-2" in e and "no component" in e for e in errors)

    def test_a_ticket_child_given_to_two_components_is_rejected(self):
        errors = check_answer(
            answer(component("a", ticket_ref="T-1"), component("b", ticket_ref="T-1")), ["T-1"]
        )
        assert any("T-1" in e and "twice" in e for e in errors)

    def test_one_component_when_kept_as_one(self):
        errors = check_answer(answer(component("a"), component("b")), [], one=True)
        assert any("one component" in e for e in errors)

    def test_waves_put_dependencies_first(self):
        kids = [EpicChild(**component("c", ["b"])), EpicChild(**component("b", ["a"])), EpicChild(**component("a"))]
        assert waves(kids) == ["a", "b", "c"]


def test_several_components_nest_under_the_change(repo, model):
    model["components"] = TWO
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    assert meta(cdir)["children"] == ["api", "ui"]
    assert not (cdir / "plan.md").exists() and not (cdir / "specs.md").exists()
    for cid, deps in (("api", []), ("ui", ["api"])):
        comp = cdir / "components" / cid
        assert (comp / "plan.md").read_text().startswith(f"# Plan: Build {cid}")
        assert (comp / "specs.md").is_file()
        m = meta(comp)
        assert m["depends_on"] == deps and m["branch"] == "ENG-1-throttle"
        assert "parent" not in m and "ticket_refs" not in m
    table = (cdir / "components.md").read_text()
    assert table.startswith("# Components: Request throttling\n\n| Component  | Depends on  | Ticket   |")
    assert "| ui         | api         | pending  |" in table
    assert table.rstrip().endswith("Why two: Each is a pull request on its own.")
    # One run covers every component: no run of their own, no sibling dirs.
    assert sorted(p.name for p in (cdir.parent).iterdir()) == ["throttle"]
    assert not (cdir / "components" / "api" / ".pm-spec").exists()
    assert model.calls_to("spec:specs:api") == 1 and model.calls_to("spec:specs:ui") == 1
    assert "## Other components of this change" in model.prompts("spec:specs:api")[0]
    assert "- `ui`: Build ui" in model.prompts("spec:specs:api")[0]


def test_one_component_keeps_its_plan_in_the_change_dir(repo, model):
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    assert (cdir / "plan.md").read_text().startswith("# Plan: Throttle the API")
    assert not (cdir / "components").exists() and not (cdir / "components.md").exists()
    assert "children" not in meta(cdir)
    assert state(cdir)["split"]["single"] is True


def test_the_components_call_reads_the_approved_proposal_and_use_cases(repo, model):
    assert run(repo, "new", "throttle", "--auto") == 0
    prompt = model.exactly("components")[0]
    assert "## Proposal (approved)\n# Request throttling" in prompt
    assert "## Use cases\n# Use cases" in prompt


def test_a_rejected_answer_is_retried_once_with_the_errors(repo, model):
    bad = {"reason": "x", "components": [component("a", ["zzz"])]}
    model["components"] = [bad, TWO]
    assert run(repo, "new", "throttle", "--auto") == 0
    assert model.calls_to("components") == 2
    assert "Your previous answer was rejected" in model.exactly("components")[1]
    assert "'zzz'" in model.exactly("components")[1]


def test_no_epic_split_asks_for_one_component(repo, model):
    model["components"] = [TWO, {"reason": "one", "components": [component("throttle")]}]
    assert run(repo, "new", "throttle", "--auto", "--no-epic-split") == 0
    assert "Keep the work as exactly one component." in model.exactly("components")[0]
    assert model.calls_to("components") == 2
    assert (change_dir(repo) / "plan.md").is_file()


def test_the_components_follow_the_tickets_children(repo, model, tmp_path):
    ticket = write(tmp_path / "t.md", "Throttle the API.\n")
    model["components"] = TICKETED
    argv = ["new", "throttle", "--auto", "--ticket-file", str(ticket), "--ticket-ref", "EPIC-9"]
    assert run(repo, *argv, "--ticket-child", "T-1", "--ticket-child", "T-2") == 0
    cdir = change_dir(repo)
    assert "The ticket already lists these child items: T-1, T-2" in model.exactly("components")[0]
    assert meta(cdir / "components" / "api")["ticket_refs"] == {"epic": "T-1", "children": []}
    assert "| api        |             | T-1     |" in (cdir / "components.md").read_text()


def test_a_component_answer_dropping_a_ticket_child_is_retried_then_fails(repo, model, tmp_path, capsys):
    model["components"] = TWO
    assert run(repo, "new", "throttle", "--auto", "--ticket-child", "T-1") == 1
    assert model.calls_to("components") == 2
    assert "T-1" in capsys.readouterr().out


def test_components_table_pads_each_column():
    assert components_table([("token-retry", "", "pending"), ("timeout-setting", "token-retry", "pending")]) == (
        "| Component        | Depends on   | Ticket   |\n"
        "|------------------|--------------|----------|\n"
        "| token-retry      |              | pending  |\n"
        "| timeout-setting  | token-retry  | pending  |"
    )
