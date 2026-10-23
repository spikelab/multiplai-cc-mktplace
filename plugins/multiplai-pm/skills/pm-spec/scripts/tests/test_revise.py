"""`revise`: requirements issues from `spec-issues.md` or `respec.md`, fixed in
the same specs files stage 3 writes."""

from __future__ import annotations

from conftest import (
    CAPS,
    TWO,
    change_dir,
    git,
    meta,
    ready_line,
    requirements,
    run,
    source_dir,
    specs,
    state,
    worktree,
    write,
)
from multiplai_core.specs import is_ready, set_spec_issues
from pm_spec_pipeline import layout
from pm_spec_pipeline import revise as R

ISSUES = """\
# Spec issues

## Retry header missing

components/ui/specs.md says nothing about the Retry-After header the
reviewer asked for (https://review.example/1).
"""

RESPEC = """\
## ADDED Requirements

### Requirement: Retry-After header
The system SHALL send Retry-After.

#### Scenario: refused
- **WHEN** a request is refused
- **THEN** Retry-After is set

## MODIFIED Requirements

### Requirement: Rate limit holds
The system SHALL apply 100 a minute.

## REMOVED Requirements

## Notes considered and not proposed
- a tool quirk
"""


def resolved(n=1):
    return {
        "findings": [],
        "prior_assessments": [
            {"finding_id": f"RV-F{i}", "status": "resolved"} for i in range(1, n + 1)
        ],
    }


def ready_change(repo, model):
    model["components"] = TWO
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    write(cdir / "spec-issues.md", ISSUES)
    set_spec_issues(cdir, cdir / "spec-issues.md")
    return cdir


def test_revise_rewrites_the_named_file_rechecks_clears_and_hands_off(
    repo, model, capsys
):
    cdir = ready_change(repo, model)
    capsys.readouterr()
    model["consistency"] = resolved()
    assert run(repo, "revise", "throttle") == 0
    out = capsys.readouterr().out
    assert [label for label in model.labels() if ":rewrite:" in label] == [
        "revise:rewrite:ui"
    ]
    assert "Retry-After" in model.prompts("revise:rewrite:ui")[0]
    assert "the rewritten ui" in (cdir / "components" / "ui" / "specs.md").read_text()
    assert "RV-F1" in model.exactly("consistency")[-1]
    assert meta(cdir)["spec_issues"] is None
    assert is_ready(cdir) == (True, "")
    assert ready_line(out).endswith(layout.revision(cdir))
    assert not (cdir / "spec-issues.md").exists()
    assert len(list(cdir.glob("spec-issues.resolved-*.md"))) == 1
    assert git(repo, "status", "--porcelain", "--", "specs") == ""
    assert state(cdir)["steps"]["revise"]["repair_findings"][0]["status"] == "resolved"


def test_an_issue_not_resolved_blocks_and_keeps_spec_issues(repo, model, capsys):
    cdir = ready_change(repo, model)
    model["consistency"] = {
        "findings": [],
        "prior_assessments": [{"finding_id": "RV-F1", "status": "still_present"}],
    }
    assert run(repo, "revise", "throttle") == 4
    assert "RV-F1 still_present" in capsys.readouterr().out
    assert meta(cdir)["spec_issues"] == "spec-issues.md"
    assert meta(cdir)["spec_status"] == "draft"
    assert (cdir / "spec-issues.md").is_file()
    # Again, unchanged: refused with no model call.
    calls = len(model.calls)
    assert run(repo, "revise", "throttle") == 4
    assert len(model.calls) == calls


def test_from_respec_reads_each_requirement_block_as_an_issue(repo, model):
    model["components"] = TWO
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    write(cdir / "respec.md", RESPEC)
    model["consistency"] = resolved(2)
    assert run(repo, "revise", "throttle", "--from-respec") == 0
    # The ADDED block names no file: every file is rewritten.
    assert sorted(label for label in model.labels() if ":rewrite:" in label) == [
        "revise:rewrite:api",
        "revise:rewrite:ui",
    ]
    # Resolved, it is moved aside like spec-issues.md.
    assert not (cdir / "respec.md").exists()
    assert [p.read_text() for p in cdir.glob("respec.resolved-*.md")] == [RESPEC]


def test_the_same_issue_raised_again_after_a_passed_revise_is_a_new_round(repo, model):
    model["consistency"] = {
        "findings": [],
        "prior_assessments": [{"finding_id": "RV-F1", "status": "resolved"}],
    }
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    for round_ in (1, 2):
        write(cdir / "spec-issues.md", ISSUES)
        set_spec_issues(cdir, cdir / "spec-issues.md")
        assert run(repo, "revise", "throttle") == 0, round_
        assert model.calls_to("revise:rewrite") == round_
        assert meta(cdir)["spec_issues"] is None


def test_a_second_respec_after_a_passed_one_is_a_new_round(repo, model):
    model["components"] = TWO
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    model["consistency"] = resolved(2)
    for round_ in (1, 2):
        write(cdir / "respec.md", RESPEC)
        assert run(repo, "revise", "throttle", "--from-respec") == 0, round_
        assert model.calls_to("revise:rewrite") == 2 * round_


def test_parse_respec_skips_notes():
    issues = R.parse_respec(RESPEC)
    assert len(issues) == 2
    assert issues[0].startswith("ADDED requirement") and issues[1].startswith(
        "MODIFIED requirement"
    )


def test_parse_spec_issues_without_sections_is_one_issue():
    assert R.parse_spec_issues("# Title\n\nOne problem.\n") == ["One problem."]
    assert R.parse_spec_issues("") == []


def test_an_issue_naming_a_requirement_or_capability_targets_its_file(tmp_path):
    cdir = tmp_path / "c"
    for cid, caps in (("api", ("rate-limit",)), ("ui", ("retry-policy",))):
        write(cdir / "components" / cid / "plan.md", "# Plan: x\n")
        write(cdir / "components" / cid / "specs.md", specs(caps))
    assert R.issue_units(cdir, "The 'Rate limit holds' requirement is wrong") == [
        "components/api/specs.md"
    ]
    assert R.issue_units(cdir, "`retry-policy` is vague") == ["components/ui/specs.md"]
    # The old layout: one file per capability.
    old = tmp_path / "old"
    for cap in CAPS:
        write(old / "requirements" / f"{cap}.md", requirements(cap))
    assert R.issue_units(old, "The 'Rate limit holds' requirement is wrong") == [
        "requirements/rate-limit.md"
    ]


def test_an_issues_file_in_a_component_goes_to_that_components_specs(repo, model):
    """buildme writes a component's issues into its own dir."""
    model["components"] = TWO
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    ui = cdir / "components" / "ui"
    write(
        ui / "spec-issues.md", "# Spec issues\n\n## Vague\n\nThe wait is not stated.\n"
    )
    set_spec_issues(ui, ui / "spec-issues.md")
    model["consistency"] = resolved()
    assert run(repo, "revise", "throttle") == 0
    assert [label for label in model.labels() if ":rewrite:" in label] == [
        "revise:rewrite:ui"
    ]
    assert (
        meta(ui)["spec_issues"] is None
        and len(list(ui.glob("spec-issues.resolved-*.md"))) == 1
    )
    assert is_ready(ui) == (True, "")


def test_no_issues_file_is_refused(repo, model, capsys):
    assert run(repo, "new", "throttle", "--auto") == 0
    assert run(repo, "revise", "throttle") == 2
    assert "nothing to revise" in capsys.readouterr().out


def test_revise_adopts_a_change_pm_spec_did_not_write(repo, model):
    cdir, hand = change_dir(repo), source_dir(repo)
    from conftest import proposal

    write(hand / "proposal.md", proposal())
    for cap in CAPS:
        write(hand / "requirements" / f"{cap}.md", requirements(cap))
    write(hand / "spec-issues.md", ISSUES)
    model["consistency"] = resolved()
    assert run(repo, "revise", "throttle", "--ticket-ref", "ENG-1") == 0
    assert is_ready(cdir) == (True, "")
    assert model.calls_to("spec:") == 0


def test_revise_finds_an_archived_change(repo, model, capsys):
    """An `--auto` build archives the change when its PR is ready, and
    buildme-findings writes later issues into the archived copy (item 22a):
    with specs/changes/<name>/ gone, revise works on the newest archive."""
    cdir = ready_change(repo, model)
    wt = worktree(repo)
    git(wt, "add", "-A", "specs")
    git(wt, "commit", "-qm", "issues")
    archive = wt / "specs" / "archive"
    archive.mkdir(parents=True)
    older = archive / "2026-01-01-throttle"
    older.mkdir()
    git(wt, "mv", "specs/changes/throttle", "specs/archive/2026-10-22-throttle")
    git(wt, "commit", "-qm", "archive throttle")
    archived = archive / "2026-10-22-throttle"
    capsys.readouterr()
    model["consistency"] = resolved()

    assert run(repo, "revise", "throttle") == 0

    out = capsys.readouterr().out
    assert not cdir.exists() and not (older / ".change.yaml").exists()
    assert meta(archived)["spec_issues"] is None
    assert is_ready(archived) == (True, "")
    assert f"SPEC:READY:{archived}:" in out
    assert git(wt, "status", "--porcelain", "--", "specs") == ""
