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


def test_refs_come_from_the_recorded_ticket_refs_not_the_proposal(repo, model, tmp_path):
    """The epic carries the ref the change came in with; no task carries a ref
    a model wrote, even when the proposal's `## Sources` names a child."""
    ticket = write(tmp_path / "t.md", "Throttle.\n")
    assert run(repo, "new", "throttle", "--auto", "--no-epic-split", "--ticket-file", str(ticket),
               "--ticket-ref", "EPIC-9", "--ticket-child", "T-1", "--ticket-child", "T-2") == 0
    cdir = change_dir(repo)
    assert "ticket T-1" in (cdir / "proposal.md").read_text()  # the model cited it
    items = load(cdir)
    validate(items)
    assert [i.get("ref") for i in items] == ["EPIC-9", None, None]


def test_a_child_change_carries_its_ticket_childs_ref(repo, model, tmp_path):
    from test_epic_split import SPLIT
    model["epic_split"] = SPLIT
    assert run(repo, "new", "throttle", "--auto", "--ticket-ref", "EPIC-9", "--ticket-child", "T-1",
               "--ticket-child", "T-2") == 0
    for name, ref in (("throttle-api", "T-1"), ("throttle-ui", "T-2")):
        assert run(repo, "resume", name) == 0
        items = load(change_dir(repo, name))
        validate(items)
        assert items[0]["ref"] == ref and all("ref" not in i for i in items[1:])


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
def test_the_priority_line(text, expected):
    assert tickets.stated_priority(text) == expected


def test_tickets_json_ids_are_stable_across_runs(repo, model):
    """A second handoff after an edit to a non-title field keeps every id."""
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    first = load(cdir)
    path = cdir / "proposal.md"
    path.write_text(path.read_text().replace("Clients flood the API server.", "Bots flood the API server."))
    assert run(repo, "resume", "throttle") == 0
    second = load(cdir)
    assert second[0]["description"] == "Bots flood the API server." != first[0]["description"]
    assert [i["id"] for i in second] == [i["id"] for i in first]
    assert [i["parent"] for i in second] == [i["parent"] for i in first]


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


# --- the ready record covers only files the check read --------------------------

def test_an_edit_while_the_check_runs_is_checked_before_ready(repo, model, capsys):
    from multiplai_core.specs import is_ready, spec_revision

    from conftest import meta, requirements, state
    cdir = change_dir(repo)
    read = []

    def check(prompt, label, kw):
        read.append(spec_revision(cdir))
        if len(read) == 1:  # the user saves an edit while the agent is still reading
            write(cdir / "requirements" / "rate-limit.md", requirements("rate-limit", rule="UNCHECKED EDIT"))
        return {"findings": []}

    model["consistency"] = check
    assert run(repo, "new", "throttle", "--auto") == 0
    out = capsys.readouterr().out
    assert len(read) == 2  # checked again, at the edited revision
    ready = [line for line in out.splitlines() if line.startswith("SPEC:READY:")]
    assert ready == [f"SPEC:READY:{cdir}:{read[1]}"]
    assert meta(cdir)["spec_revision"] == read[1] == spec_revision(cdir)
    assert state(cdir)["steps"]["consistency"]["revision"] == read[1]
    assert is_ready(cdir) == (True, "")


def test_the_recheck_passes_at_the_revision_it_read(repo, model):
    from multiplai_core.specs import spec_revision

    from conftest import meta, requirements, state
    from test_consistency import finding, recheck
    cdir = change_dir(repo)
    read = []

    def second(prompt, label, kw):
        read.append(spec_revision(cdir))
        write(cdir / "requirements" / "retry-policy.md", requirements("retry-policy", rule="EDIT DURING RECHECK"))
        return recheck()

    model["consistency"] = [{"findings": [finding()]}, second, {"findings": []}]
    assert run(repo, "new", "throttle", "--auto") == 0
    assert len(model.exactly("consistency")) == 3
    assert meta(cdir)["spec_revision"] == spec_revision(cdir) != read[0]
    assert state(cdir)["steps"]["consistency"]["revision"] == spec_revision(cdir)


def test_a_change_between_the_compare_and_the_ready_record_is_checked_again(repo, model, monkeypatch):
    from multiplai_core.specs import is_ready, spec_revision

    from conftest import meta, requirements
    real = tickets.write
    cdir = change_dir(repo)
    done = []

    def write_then_edit(change_dir_, project_dir):
        path = real(change_dir_, project_dir)
        if not done:
            done.append(1)
            write(cdir / "requirements" / "rate-limit.md", requirements("rate-limit", rule="LATE EDIT"))
        return path

    monkeypatch.setattr(tickets, "write", write_then_edit)
    assert run(repo, "new", "throttle", "--auto") == 0
    assert len(model.exactly("consistency")) == 2
    assert meta(cdir)["spec_revision"] == spec_revision(cdir) and is_ready(cdir)[0]


# --- never ready while requirements issues are open ------------------------------

ISSUES = "# Spec issues\n\n## Retry header\n\nrequirements/retry-policy.md lacks the Retry-After header.\n"


def test_resume_after_a_blocked_revise_refuses_and_never_hands_off(repo, model, capsys):
    from multiplai_core.specs import is_ready, set_spec_issues

    from conftest import meta
    model["consistency"] = [{"findings": []},
                            {"findings": [], "prior_assessments": [{"finding_id": "RV-F1",
                                                                    "status": "still_present"}]},
                            {"findings": []}]
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    write(cdir / "spec-issues.md", ISSUES)
    set_spec_issues(cdir, cdir / "spec-issues.md")
    assert run(repo, "revise", "throttle") == 4
    capsys.readouterr()
    calls, head = len(model.calls), git(repo, "rev-parse", "HEAD")
    assert run(repo, "resume", "throttle") == 4
    out = capsys.readouterr().out
    assert "SPEC:READY" not in out and "revise throttle" in out
    assert len(model.calls) == calls and git(repo, "rev-parse", "HEAD") == head
    assert meta(cdir)["spec_issues"] == "spec-issues.md" and not is_ready(cdir)[0]


def test_a_ready_change_with_open_issues_is_not_reported_ready(repo, model, capsys):
    from multiplai_core.specs import set_spec_issues
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    write(cdir / "spec-issues.md", ISSUES)
    set_spec_issues(cdir, cdir / "spec-issues.md")
    capsys.readouterr()
    assert run(repo, "resume", "throttle") == 4
    out = capsys.readouterr().out
    assert "SPEC:READY" not in out and "spec-issues.md" in out
