"""Plan items 56-58: the consistency check reads in full only what changed
since the last check (row S40), the rewrite reads less and makes the smallest
change, and a table copied from `ticket.md` is checked in code."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    BRANCH,
    Killed,
    change_dir,
    component,
    git,
    rewrite_answer,
    run,
    specs,
    state,
    write,
)
from pm_spec_pipeline import consistency
from pm_spec_pipeline import findings as F
from pm_spec_pipeline.config import SpecConfig
from pm_spec_pipeline.run import Run
from pm_spec_pipeline.state import RunState
from test_resume_rows import FLOW, RESUME, baseline, clone, push_edit

# Two components with capabilities of their own, so neither specs.md names the
# other by a capability name; ui depends on api.
SPLIT = {
    "reason": "Each is a pull request on its own.",
    "components": [component("api"), component("ui", deps=["api"])],
}
CAPS_OF = {"api": ("quota-store",), "ui": ("limits-page",)}
API, UI = "components/api/specs.md", "components/ui/specs.md"
EDITED_UI = specs(("limits-page",), rule="the edited limits page")


def configure(model) -> None:
    model["components"] = SPLIT
    model["spec:specs"] = lambda prompt, label, kw: rewrite_answer(
        specs(CAPS_OF[label.rsplit(":", 1)[1]])
    )


def finding(severity="major", files=(API, "components/api/plan.md"), type_="gap"):
    return {
        "type": type_,
        "severity": severity,
        "description": f"a {type_} in {files[0]}",
        "places": [{"file": f, "where": "Requirement"} for f in files],
        "suggestion": "state it",
    }


def resolved(*new):
    return {
        "findings": list(new),
        "prior_assessments": [
            {"finding_id": "CC-F1", "status": "resolved", "evidence": "the lines"}
        ],
    }


NO_FINDINGS = {"findings": [], "prior_assessments": []}


def ready_then_edit_ui(repo: Path, model, then) -> None:
    """Hand off through the review stops (the first, full check finds nothing),
    then a person edits ui's specs.md and pushes; *then* answers the next check."""
    configure(model)
    model["consistency"] = [NO_FINDINGS, then]
    for argv, code in FLOW:
        assert run(repo, *argv) == code, argv
    push_edit(repo, repo.parent / "reviewer", UI, EDITED_UI)


def checks(model) -> list[str]:
    return model.exactly("consistency")


# --- item 56 ----------------------------------------------------------------------


def test_the_first_check_reads_everything_and_saves_the_digests(repo, model):
    configure(model)
    for argv, code in FLOW:
        assert run(repo, *argv) == code, argv
    first = checks(model)[0]
    assert consistency.UNCHANGED_MARK not in first
    assert f"### {API}\n# Capability: quota-store" in first
    saved = state(change_dir(repo))["checked_digests"]
    assert set(saved) == {
        "proposal.md",
        "use-cases.md",
        "components/api/plan.md",
        "components/ui/plan.md",
        API,
        UI,
    }


def test_after_an_edit_the_check_reads_only_the_changed_spec_in_full(repo, model):
    ready_then_edit_ui(repo, model, NO_FINDINGS)
    assert run(repo, *RESUME) == 0
    second = checks(model)[1]
    assert f"### {UI}\n# Capability: limits-page" in second
    assert "the edited limits page" in second
    assert f"### {API} {consistency.UNCHANGED_MARK}\n" in second
    assert "### Requirement: Quota store holds" in second  # its title stays
    assert "The system SHALL apply quota-store" not in second  # its body does not
    cdir = change_dir(repo)
    assert (
        state(cdir)["checked_digests"][UI]
        == consistency.document_digests(_run_for(cdir))[UI]
    )


def test_a_spec_the_changed_one_cites_is_read_in_full(repo, model):
    configure(model)
    model["consistency"] = [NO_FINDINGS, NO_FINDINGS]
    for argv, code in FLOW:
        assert run(repo, *argv) == code, argv
    cites = specs(("limits-page",), rule="the quota `quota-store` keeps")
    push_edit(repo, repo.parent / "reviewer", UI, cites)
    assert run(repo, *RESUME) == 0
    assert consistency.UNCHANGED_MARK not in checks(model)[1]


def test_an_edited_proposal_makes_a_full_check(repo, model):
    configure(model)
    model["consistency"] = [NO_FINDINGS, NO_FINDINGS]
    for argv, code in FLOW:
        assert run(repo, *argv) == code, argv
    cdir = change_dir(repo)
    text = (cdir / "proposal.md").read_text() + "\nAlso log each refusal.\n"
    push_edit(repo, repo.parent / "reviewer", "proposal.md", text)
    assert run(repo, *RESUME) == 0
    assert consistency.UNCHANGED_MARK not in checks(model)[1]


def test_a_new_major_finding_on_unchanged_files_only_is_advisory(repo, model, capsys):
    ready_then_edit_ui(repo, model, {"findings": [finding()]})
    assert run(repo, *RESUME) == 0
    step = state(change_dir(repo))["steps"]["consistency"]
    assert step["outcome"] == "passed"
    [advisory] = step["advisory_findings"]
    assert advisory["severity"] == "major"
    assert consistency.UNCHANGED_NOTE.strip() in advisory["text"]
    assert "SPEC:WARN:" in capsys.readouterr().out


def test_a_new_critical_finding_on_unchanged_files_still_blocks(repo, model):
    ready_then_edit_ui(repo, model, {"findings": [finding("critical")]})
    assert run(repo, *RESUME) == 4
    assert state(change_dir(repo))["steps"]["consistency"]["outcome"] == "blocked"


def test_a_major_finding_on_a_changed_file_blocks(repo, model):
    ready_then_edit_ui(repo, model, {"findings": [finding(files=(UI, API))]})
    assert run(repo, *RESUME) == 4


def _run_for(cdir: Path) -> Run:
    config = SpecConfig(project_dir=cdir.parents[2], change_name=cdir.name)
    return Run(config=config, state=RunState(change=cdir.name))


def _two_components(root: Path) -> Path:
    cdir = root / "specs" / "changes" / "c"
    write(cdir / "proposal.md", "# P\n")
    for cid in ("api", "ui"):
        write(cdir / "components" / cid / "plan.md", "# Plan\n")
        write(cdir / "components" / cid / ".change.yaml", "depends_on: []\n")
        write(cdir / "components" / cid / "specs.md", specs(CAPS_OF[cid]))
    return cdir


def test_a_major_finding_an_earlier_check_raised_still_blocks(tmp_path):
    cdir = _two_components(tmp_path)
    r = _run_for(cdir)
    r.state.checked_digests = consistency.document_digests(r)
    write(cdir / UI, EDITED_UI)
    reading = consistency.what_to_read(r)
    assert reading.changed == {UI} and reading.full == {UI}
    raised = F.number_findings("consistency", [{**finding(), "category": "gap"}])
    found = consistency.as_dicts(consistency.ConsistencyAnswer(findings=[finding()]))
    kept, quiet = consistency.quiet_unchanged(r, found, reading, raised)
    assert kept == found and quiet == []
    kept, quiet = consistency.quiet_unchanged(r, found, reading, [])
    assert kept == [] and len(quiet) == 1


def test_with_no_saved_digests_the_check_is_full_and_quiets_nothing(tmp_path):
    cdir = _two_components(tmp_path)
    r = _run_for(cdir)
    reading = consistency.what_to_read(r)
    assert reading.changed is None and reading.full == {API, UI}
    found = consistency.as_dicts(consistency.ConsistencyAnswer(findings=[finding()]))
    assert consistency.quiet_unchanged(r, found, reading, []) == (found, [])


def test_the_files_a_listed_finding_cites_are_read_in_full(tmp_path):
    cdir = _two_components(tmp_path)
    r = _run_for(cdir)
    r.state.checked_digests = consistency.document_digests(r)
    write(cdir / UI, EDITED_UI)
    listed = F.number_findings("consistency", [finding()])
    assert consistency.what_to_read(r, listed).full == {UI, API}


# --- S40 --------------------------------------------------------------------------


def _stop_after_the_answer(monkeypatch) -> None:
    """The check answers, and the run is killed before its outcome (and so its
    digests) is saved."""
    real = F.first_check_verdict

    def die(found):
        monkeypatch.setattr(F, "first_check_verdict", real)
        raise Killed("after the check answered")

    monkeypatch.setattr(F, "first_check_verdict", die)


@pytest.mark.parametrize("where", ["during the call", "after the answer"])
def test_S40_a_check_stopped_before_its_digests_were_saved_runs_a_full_check(
    repo, model, tmp_path, monkeypatch, where
):
    def answers(m):
        configure(m)
        m["consistency"] = [NO_FINDINGS, NO_FINDINGS]

    ready_then_edit_ui(repo, model, NO_FINDINGS)
    cdir = change_dir(repo)
    assert state(cdir)["checked_digests"]  # the first check's
    if where == "during the call":

        def kill(prompt, label, kw):
            raise Killed(label)

        model["consistency"] = [NO_FINDINGS, kill, NO_FINDINGS]
    else:
        _stop_after_the_answer(monkeypatch)
    with pytest.raises(Killed):
        run(repo, *RESUME)
    assert state(cdir)["checked_digests"] is None  # old digests cleared

    assert run(repo, *RESUME) == 0
    last = checks(model)[-1]
    assert consistency.UNCHANGED_MARK not in last  # a full check
    assert f"### {API}\n# Capability: quota-store" in last
    saved = state(cdir)["checked_digests"]
    assert saved == consistency.document_digests(_run_for(cdir))

    # As a run that never stopped: the same outcome and digests.
    other, _ = baseline(tmp_path, monkeypatch, answers, FLOW)
    push_edit(other, other.parent / "reviewer", UI, EDITED_UI)
    model["consistency"] = NO_FINDINGS
    assert run(other, *RESUME) == 0
    theirs, mine = state(change_dir(other)), state(cdir)
    assert mine["checked_digests"] == theirs["checked_digests"]
    assert mine["steps"]["consistency"] == theirs["steps"]["consistency"]
    assert (cdir / UI).read_text() == (change_dir(other) / UI).read_text()


# --- item 57 ----------------------------------------------------------------------


def test_the_rewrite_reads_the_cited_specs_in_full_and_the_rest_by_names(repo, model):
    configure(model)
    model["consistency"] = [
        {"findings": [finding(files=(API, "proposal.md"))]},
        resolved(),
    ]
    assert run(repo, "new", "throttle", "--auto") == 0
    [prompt] = model.prompts("consistency:rewrite:api")
    assert f"## The file to revise: {API}" in prompt
    assert "The system SHALL apply quota-store" in prompt  # the file itself
    assert f"### {UI} (capability names" in prompt
    assert "### Requirement: Limits page holds" in prompt
    assert "The system SHALL apply limits-page" not in prompt
    assert "add a requirement only when none covers it" in " ".join(prompt.split())


def test_the_rewrite_reads_in_full_a_second_spec_its_finding_cites(repo, model):
    configure(model)
    model["consistency"] = [{"findings": [finding(files=(API, UI))]}, resolved()]
    assert run(repo, "new", "throttle", "--auto") == 0
    [prompt] = model.prompts("consistency:rewrite:api")
    assert f"### {UI}\n# Capability: limits-page" in prompt
    assert "The system SHALL apply limits-page" in prompt


# --- item 58 ----------------------------------------------------------------------

TICKET_TABLE = """\
| Section | Owner | Staff |
|---|---|---|
| Dashboard | yes | Front Office |
| Immobili | yes | Rendimenti, Task di Maya |
"""


def _ticket_change(root: Path, copied: str) -> Path:
    cdir = root / "specs" / "changes" / "c"
    write(cdir / "ticket.md", "# Ticket ENG-1\n\nThe sections:\n\n" + TICKET_TABLE)
    write(cdir / "specs.md", specs(("rate-limit",)) + "\n" + copied)
    return cdir


def test_a_table_copied_from_the_ticket_with_a_cell_dropped_is_critical(tmp_path):
    dropped = TICKET_TABLE.replace("| Front Office |", "| |")
    cdir = _ticket_change(tmp_path, dropped)
    [f] = consistency.table_findings(_run_for(cdir))
    assert f["category"] == "format" and f["severity"] == "critical"
    assert f["places"][0]["file"] == "specs.md"
    assert f["places"][1] == {"file": "ticket.md", "where": "the table at line 5"}
    assert "| Dashboard | yes | Front Office |" in f["description"]


def test_a_table_copied_exactly_whitespace_aside_is_no_finding(tmp_path):
    spaced = TICKET_TABLE.replace("| yes |", "|   yes   |").replace("|---|", "| :-- |")
    assert consistency.table_findings(_run_for(_ticket_change(tmp_path, spaced))) == []


def test_a_table_with_another_header_is_not_compared(tmp_path):
    other = TICKET_TABLE.replace("| Section |", "| Area |").replace("yes", "no")
    assert consistency.table_findings(_run_for(_ticket_change(tmp_path, other))) == []


def test_a_wrong_copy_blocks_the_check_after_the_specs_review(repo, model):
    configure(model)
    for argv, code in FLOW[:4]:
        assert run(repo, *argv) == code, argv
    # A reviewer adds the ticket's table to ui's specs with one cell changed.
    mine = clone(repo, repo.parent / "reviewer")
    cdir = mine / "specs" / "changes" / "throttle"
    write(cdir / "ticket.md", "# Ticket ENG-1\n\n" + TICKET_TABLE)
    with (cdir / UI).open("a") as fh:
        fh.write("\n" + TICKET_TABLE.replace("Front Office", "Back Office"))
    git(mine, "add", "-A")
    git(mine, "commit", "-qm", "copy the table")
    git(mine, "push", "-q", "origin", BRANCH)
    assert run(repo, *RESUME) == 4
    step = state(change_dir(repo))["steps"]["consistency"]
    [blocking] = step["blocking_findings"]
    assert blocking["category"] == "format" and blocking["severity"] == "critical"
    assert blocking["places"][0]["file"] == UI
