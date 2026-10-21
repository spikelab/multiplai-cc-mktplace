"""The handoff (item 13): the ready record, `tickets.json` and its schema, and
the commit of the change dir alone."""

from __future__ import annotations

import json

import jsonschema
import pytest

from pm_spec_pipeline import git_ops, tickets

from conftest import CAPS, PRD, SCHEMA, change_dir, git, run, write


def load(cdir):
    return json.loads((cdir / "tickets.json").read_text())


def validate(items):
    jsonschema.validate(items, json.loads(SCHEMA.read_text()))


def test_tickets_json_validates_and_has_one_epic_and_one_task_per_capability(repo, model):
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    items = load(cdir)
    validate(items)
    epic, *tasks = items
    assert epic == {
        "id": "throttle", "type": "epic", "title": "Request throttling",
        "description": "Clients flood the API server.", "priority": "none", "labels": ["throttle"],
        "parent": None, "acceptance": [],
        "links": ["specs/changes/throttle/proposal.md", "specs/changes/throttle/use-cases.md"]}
    assert [t["id"] for t in tasks] == [f"throttle/{c}" for c in CAPS]
    for task, cap in zip(tasks, CAPS):
        name = cap.replace("-", " ").capitalize()
        assert task["type"] == "task" and task["parent"] == "throttle" and task["title"] == cap
        assert task["labels"] == ["throttle", cap]
        assert task["acceptance"] == [f"Requirement: {name} holds", f"Scenario: {name} applies"]
        assert task["links"] == [f"specs/changes/throttle/requirements/{cap}.md"]
        assert "ref" not in task
    assert "ref" not in epic


def test_items_that_came_in_as_refs_carry_them(repo, model, tmp_path):
    ticket = write(tmp_path / "t.md", "Throttle.\n")
    assert run(repo, "new", "throttle", "--auto", "--ticket-file", str(ticket), "--ticket-ref", "EPIC-9",
               "--ticket-child", "T-1", "--ticket-child", "T-2") == 0
    items = load(change_dir(repo))
    validate(items)
    assert [i.get("ref") for i in items] == ["EPIC-9", "T-1", "T-2"]


def test_priority_comes_from_the_prd_and_tasks_take_the_epics(repo, model, tmp_path):
    prd = write(tmp_path / "prd.md", PRD)
    assert run(repo, "new", "throttle", "--auto", "--prd", str(prd)) == 0
    assert {i["priority"] for i in load(change_dir(repo))} == {"high"}


def test_priority_from_the_ticket_when_the_prd_states_none(repo, model, tmp_path):
    ticket = write(tmp_path / "t.md", "Priority: Urgent\n\nThrottle.\n")
    assert run(repo, "new", "throttle", "--auto", "--ticket-file", str(ticket)) == 0
    assert {i["priority"] for i in load(change_dir(repo))} == {"urgent"}


@pytest.mark.parametrize("text,expected", [
    ("**Priority:** P1\n", "high"), ("priority = low\n", "low"), ("Priority: whenever\n", None), ("", None)])
def test_stated_priority(text, expected):
    assert tickets.stated_priority(text) == expected


def test_tickets_json_is_stable_across_runs(repo, model):
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    first = (cdir / "tickets.json").read_text()
    assert tickets.build(cdir, repo) == json.loads(first)


def test_the_schema_rejects_a_ticket_system_shaped_item():
    schema = json.loads(SCHEMA.read_text())
    bad = [{"id": "x", "type": "story", "title": "t", "description": "", "priority": "P1", "labels": [],
            "parent": None, "acceptance": [], "links": ["/abs/path"]}]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


class TestCommit:
    def test_other_staged_work_stays_staged_and_out_of_the_commit(self, repo):
        cdir = write(repo / "specs" / "changes" / "c" / "proposal.md", "p\n").parent
        write(repo / "staged.txt", "s\n")
        git(repo, "add", "staged.txt")
        sha = git_ops.commit_change_dir(repo, cdir, "specs(c): ready")
        assert sha == git(repo, "rev-parse", "HEAD")
        assert git(repo, "show", "--name-only", "--format=", "HEAD") == "specs/changes/c/proposal.md"
        assert git(repo, "diff", "--cached", "--name-only") == "staged.txt"

    def test_nothing_to_commit_returns_none(self, repo):
        cdir = write(repo / "specs" / "changes" / "c" / "proposal.md", "p\n").parent
        assert git_ops.commit_change_dir(repo, cdir, "m")
        assert git_ops.commit_change_dir(repo, cdir, "m") is None

    def test_a_file_staged_and_then_edited_is_refused(self, repo):
        path = write(repo / "specs" / "changes" / "c" / "proposal.md", "p\n")
        git(repo, "add", str(path))
        path.write_text("edited\n")
        with pytest.raises(git_ops.GitError, match="staged and working versions differ"):
            git_ops.commit_change_dir(repo, path.parent, "m")

    def test_the_run_folder_is_never_committed(self, repo, model):
        assert run(repo, "new", "throttle", "--auto") == 0
        assert git(repo, "ls-files", "specs/changes/throttle/.pm-spec") == ""
