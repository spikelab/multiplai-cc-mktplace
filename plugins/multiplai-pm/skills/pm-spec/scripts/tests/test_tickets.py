"""`tickets.json` (stage 3) and the wait for the components' tickets (row S6's step)."""

from __future__ import annotations

import json

import jsonschema
import pytest
from conftest import PRD, SCHEMA, TWO, change_dir, component, meta, run, state, write
from pm_spec_pipeline import tickets


def load(cdir):
    return json.loads((cdir / "tickets.json").read_text())


def validate(items):
    jsonschema.validate(items, json.loads(SCHEMA.read_text()))


def to_ticket_wait(repo, capsys, *new_args) -> str:
    """`new` without --auto, then the two review resumes; the ticket wait's output."""
    assert (
        run(repo, "new", "throttle", "--interview-summary", "Throttle it.", *new_args)
        == 3
    )
    assert "SPEC:REVIEW:proposal" in capsys.readouterr().out
    assert run(repo, "resume", "throttle") == 3
    assert "SPEC:REVIEW:components" in capsys.readouterr().out
    assert run(repo, "resume", "throttle") == 3
    return capsys.readouterr().out


def test_several_components_are_an_epic_and_one_task_each(repo, model):
    model["components"] = TWO
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    items = load(cdir)
    validate(items)
    epic, *tasks = items
    assert epic == {
        "id": "throttle",
        "type": "epic",
        "title": "Request throttling",
        "description": "Clients flood the API server.",
        "priority": "none",
        "labels": ["throttle"],
        "parent": None,
        "acceptance": [],
        "links": ["specs/changes/throttle/proposal.md"],
        "ref": "ENG-1",
    }
    assert tasks == [
        {
            "id": f"throttle/{cid}",
            "type": "task",
            "title": f"Build {cid}",
            "description": f"Build {cid}, so the server stays up.",
            "priority": "none",
            "labels": ["throttle", cid],
            "parent": "throttle",
            "acceptance": [f"{cid} works."],
            "links": [f"specs/changes/throttle/components/{cid}/plan.md"],
        }
        for cid in ("api", "ui")
    ]
    # --auto skips the wait: the components' tickets stay pending.
    assert state(cdir)["steps"]["tickets"]["outcome"] == "skipped"
    assert (
        "| api        |             | pending  |"
        in (cdir / "components.md").read_text()
    )


def test_one_component_is_the_changes_own_ticket_rewritten(repo, model):
    assert run(repo, "new", "throttle", "--auto") == 0
    items = load(change_dir(repo))
    validate(items)
    assert items == [
        {
            "id": "throttle",
            "type": "task",
            "title": "Request throttling",
            "description": "Clients flood the API server.",
            "priority": "none",
            "labels": ["throttle"],
            "parent": None,
            "acceptance": ["throttle works."],
            "links": [
                "specs/changes/throttle/proposal.md",
                "specs/changes/throttle/plan.md",
            ],
            "ref": "ENG-1",
        }
    ]


def test_the_wait_takes_every_components_ticket_then_writes_the_specs(
    repo, model, capsys
):
    model["components"] = TWO
    out = to_ticket_wait(repo, capsys)
    cdir = change_dir(repo)
    assert (
        f"SPEC:TICKETS:{cdir / 'tickets.json'}" in out
        and "SPEC:BRANCH:ENG-1-throttle" in out
    )
    assert state(cdir)["steps"]["tickets"]["outcome"] == "pending"
    assert (cdir / "prototype" / "mockup.html").is_file() and not (
        cdir / "components" / "api" / "specs.md"
    ).exists()

    # No flags: refused, naming the flags; nothing written, no call.
    calls = len(model.calls)
    assert run(repo, "resume", "throttle") == 2
    out = capsys.readouterr().out
    assert "resume throttle --ticket api=<ID> --ticket ui=<ID>" in out
    assert "resume throttle --tickets-later" in out
    # One of two: refused, and nothing recorded.
    before = (cdir / "components.md").read_text()
    assert run(repo, "resume", "throttle", "--ticket", "api=ENG-2") == 2
    assert "ui have no ticket yet. Nothing was recorded" in capsys.readouterr().out
    assert "ticket_refs" not in meta(cdir / "components" / "api")
    assert (cdir / "components.md").read_text() == before
    assert len(model.calls) == calls
    assert (
        run(repo, "resume", "throttle", "--ticket", "api=ENG-2", "--ticket", "ui=ENG-3")
        == 3
    )
    for cid, ref in (("api", "ENG-2"), ("ui", "ENG-3")):
        assert meta(cdir / "components" / cid)["ticket_refs"] == {
            "epic": ref,
            "children": [],
        }
    table = (cdir / "components.md").read_text()
    assert (
        "| api        |             | ENG-2    |" in table
        and "| ui         | api         | ENG-3    |" in table
    )
    assert [i.get("ref") for i in load(cdir)] == ["ENG-1", "ENG-2", "ENG-3"]
    assert state(cdir)["steps"]["tickets"]["outcome"] == "passed"


def test_tickets_later_goes_on_with_the_tickets_pending(repo, model, capsys):
    model["components"] = TWO
    to_ticket_wait(repo, capsys)
    assert run(repo, "resume", "throttle", "--tickets-later") == 3
    cdir = change_dir(repo)
    step = state(cdir)["steps"]["tickets"]
    assert step["outcome"] == "skipped" and "api, ui pending" in step["reason"]
    assert all("ref" not in i for i in load(cdir)[1:])


def test_a_ticket_for_no_such_component_is_refused(repo, model, capsys):
    model["components"] = TWO
    to_ticket_wait(repo, capsys)
    assert run(repo, "resume", "throttle", "--ticket", "web=ENG-9") == 2
    assert "web, which is not a component" in capsys.readouterr().out
    assert run(repo, "resume", "throttle", "--ticket", "no-equals-sign") == 2


def test_one_component_waits_but_needs_no_flag(repo, model, capsys):
    out = to_ticket_wait(repo, capsys)
    assert "SPEC:TICKETS:" in out
    assert run(repo, "resume", "throttle") == 3
    assert state(change_dir(repo))["steps"]["tickets"]["outcome"] == "passed"


def test_components_with_ticket_children_need_no_flag(repo, model, capsys):
    model["components"] = {
        "reason": "two",
        "components": [
            component("api", ticket_ref="T-1"),
            component("ui", ticket_ref="T-2"),
        ],
    }
    to_ticket_wait(repo, capsys, "--ticket-child", "T-1", "--ticket-child", "T-2")
    assert run(repo, "resume", "throttle") == 3
    assert [i.get("ref") for i in load(change_dir(repo))] == ["ENG-1", "T-1", "T-2"]


def test_priority_comes_from_the_prd_and_tasks_take_the_epics(repo, model, tmp_path):
    model["components"] = TWO
    prd = write(tmp_path / "prd.md", PRD)
    assert run(repo, "new", "throttle", "--auto", "--prd", str(prd)) == 0
    assert {i["priority"] for i in load(change_dir(repo))} == {"high"}


def test_priority_from_the_ticket_when_the_prd_states_none(repo, model, tmp_path):
    ticket = write(tmp_path / "t.md", "Priority: Urgent\n\nThrottle.\n")
    assert run(repo, "new", "throttle", "--auto", "--ticket-file", str(ticket)) == 0
    assert {i["priority"] for i in load(change_dir(repo))} == {"urgent"}


@pytest.mark.parametrize(
    "text,expected",
    [
        ("**Priority:** P1\n", "high"),
        ("priority = low\n", "low"),
        ("Priority: whenever\n", None),
        ("", None),
    ],
)
def test_the_priority_line(text, expected):
    assert tickets.stated_priority(text) == expected


def test_done_means_and_objective_are_read_from_a_plan():
    plan = (
        "# Plan: Keep people signed in\n\n**Objective:** A laptop that wakes keeps its\nsession.\n\n"
        "## Done means\n1. A laptop asleep for 2 hours wakes with the form intact.\n"
        "2. A revoked token still logs out, and the typed form is\n   kept as a draft.\n"
    )
    assert tickets.objective(plan) == "A laptop that wakes keeps its session."
    assert tickets.done_means(plan) == [
        "A laptop asleep for 2 hours wakes with the form intact.",
        "A revoked token still logs out, and the typed form is kept as a draft.",
    ]


def test_a_nested_list_under_a_criterion_keeps_its_nesting():
    """A real-model plan: a criterion with a list under it, and a list under
    that. Each criterion stays one acceptance entry, its sub-items on their own
    indented lines, not run together with " - "."""
    plan = (
        "# Plan: Report\n\n## Done means\n\n"
        "1. The header has a Due column.\n"
        "2. With today injected as 2026-10-23:\n"
        "   - the row for an open task due 2026-10-22 has class `overdue`\n"
        "   - the rows for these tasks do not have class\n"
        "     `overdue`:\n"
        "     - an open task due 2026-10-23\n"
        "     - a completed task due 2026-10-01\n"
        "   1. a numbered sub-item\n"
        "3. The tests pass whatever the system date.\n"
    )
    assert tickets.done_means(plan) == [
        "The header has a Due column.",
        (
            "With today injected as 2026-10-23:\n"
            "- the row for an open task due 2026-10-22 has class `overdue`\n"
            "- the rows for these tasks do not have class `overdue`:\n"
            "  - an open task due 2026-10-23\n"
            "  - a completed task due 2026-10-01\n"
            "1. a numbered sub-item"
        ),
        "The tests pass whatever the system date.",
    ]
    schema = json.loads(SCHEMA.read_text())
    item = {
        "id": "x",
        "type": "task",
        "title": "t",
        "description": "",
        "priority": "none",
        "labels": [],
        "parent": None,
        "acceptance": tickets.done_means(plan),
        "links": [],
    }
    jsonschema.validate([item], schema)


def test_the_schema_rejects_a_ticket_system_shaped_item():
    schema = json.loads(SCHEMA.read_text())
    bad = [
        {
            "id": "x",
            "type": "story",
            "title": "t",
            "description": "",
            "priority": "P1",
            "labels": [],
            "parent": None,
            "acceptance": [],
            "links": ["/abs/path"],
        }
    ]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
