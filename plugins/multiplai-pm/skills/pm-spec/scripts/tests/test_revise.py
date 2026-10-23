"""`revise`: requirements issues from `spec-issues.md` or `respec.md`, fixed in
the same specs files stage 3 writes."""

from __future__ import annotations

import pytest
from conftest import (
    CAPS,
    TWO,
    change_dir,
    git,
    meta,
    ready_line,
    requirements,
    rewrite_answer,
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
from pm_spec_pipeline import rewrite as W

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


def test_a_component_path_is_refused_naming_the_change(repo, model, capsys):
    """buildme's spec-issues.md told the real-model run of 2026-10-23 to run
    `revise word-freq/word-frequencies`; pm-spec ran the name together and
    refused `there is no change word-freqword-frequencies`. revise reads each
    component's issues file through the change, so the change is named."""
    model["components"] = TWO
    assert run(repo, "new", "throttle", "--auto") == 0
    capsys.readouterr()
    calls = len(model.calls)
    assert run(repo, "revise", "throttle/ui") == 2
    out = capsys.readouterr().out
    assert "throttle/ui names the component ui of the change throttle" in out
    assert "run `revise throttle`" in out
    assert "throttleui" not in out
    assert len(model.calls) == calls


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


# --- the rewrite's output contract (real-model run, 2026-10-23) -----------------

# What the model answered for a file no finding touched: its note, then the file.
# The note became line 1 of the specs.md, and `valid_specs_text` passed it.
E2E_NOTE = (
    "The finding belongs to the top-words component (it concerns missing coverage "
    "in `components/top-words/specs.md`). No finding touches this file's content, "
    "so it is returned unchanged.\n\n"
)

VAGUE = "# Spec issues\n\n## Vague\n\nThe wait is not stated.\n"


def _issue_naming_no_file(repo, model):
    """A change with two components and an issue naming no file: revise asks
    for a rewrite of every specs file."""
    model["components"] = TWO
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    write(cdir / "spec-issues.md", VAGUE)
    set_spec_issues(cdir, cdir / "spec-issues.md")
    return cdir


def test_parse_answer_keeps_only_the_delimited_file():
    answer = f"Here it is.\n{W.BEGIN}\n# Capability: a\nbody\n{W.END}\nDone.\n"
    assert W.parse_answer(answer) == "# Capability: a\nbody\n"


def test_parse_answer_reads_unchanged_and_drops_the_note_around_it():
    assert W.parse_answer("UNCHANGED") is None
    assert W.parse_answer(E2E_NOTE + "UNCHANGED\n") is None


@pytest.mark.parametrize(
    "answer",
    [
        E2E_NOTE + specs(),  # the real-model answer: a note, then the file
        specs(),  # the file with no delimiters
        f"{W.BEGIN}\nx\n{W.END}\n{W.BEGIN}\ny\n{W.END}\n",
        f"{W.BEGIN}\nx\n",
        f"{W.END}\nx\n{W.BEGIN}\n",
        f"UNCHANGED\n{W.BEGIN}\nx\n{W.END}\n",
    ],
)
def test_parse_answer_refuses_any_other_shape(answer):
    with pytest.raises(W.ContractError):
        W.parse_answer(answer)


def test_a_file_the_answer_leaves_unchanged_is_byte_identical(repo, model):
    cdir = _issue_naming_no_file(repo, model)
    api = cdir / "components" / "api" / "specs.md"
    before = api.read_bytes()
    model["revise:rewrite:api"] = E2E_NOTE + "UNCHANGED\n"
    model["consistency"] = resolved()
    assert run(repo, "revise", "throttle") == 0
    assert [label for label in model.labels() if ":rewrite:" in label] == [
        "revise:rewrite:api",
        "revise:rewrite:ui",
    ]
    assert api.read_bytes() == before
    ui = (cdir / "components" / "ui" / "specs.md").read_text()
    assert ui.startswith("# Capability:") and "the rewritten ui" in ui


def test_a_note_outside_the_delimiters_is_not_written(repo, model):
    cdir = _issue_naming_no_file(repo, model)
    model["revise:rewrite:ui"] = (
        "I added the wait.\n"
        + rewrite_answer(specs(rule="the wait"))
        + "Hope it helps.\n"
    )
    model["revise:rewrite:api"] = "UNCHANGED"
    model["consistency"] = resolved()
    assert run(repo, "revise", "throttle") == 0
    ui = (cdir / "components" / "ui" / "specs.md").read_text()
    assert ui == specs(rule="the wait")
    assert "I added" not in ui and "Hope it helps" not in ui


def test_the_real_model_answer_is_refused_and_writes_nothing(repo, model, capsys):
    """A note, then the file, with no delimiters: not written; the revise
    blocks on it (its one rewrite is spent), and both files stay as they were."""
    cdir = _issue_naming_no_file(repo, model)
    files = [cdir / "components" / c / "specs.md" for c in ("api", "ui")]
    before = [f.read_bytes() for f in files]
    model["revise:rewrite:api"] = E2E_NOTE + specs()
    model["revise:rewrite:ui"] = "UNCHANGED"
    capsys.readouterr()
    assert run(repo, "revise", "throttle") == 4
    out = capsys.readouterr().out
    assert "the rewrite of components/api/specs.md was not written" in out
    assert [f.read_bytes() for f in files] == before
    assert meta(cdir)["spec_issues"] == "spec-issues.md"


def test_a_rewrite_adding_text_above_the_first_capability_is_refused(
    repo, model, capsys
):
    cdir = _issue_naming_no_file(repo, model)
    ui = cdir / "components" / "ui" / "specs.md"
    before = ui.read_bytes()
    model["revise:rewrite:ui"] = rewrite_answer("A note.\n\n" + specs(rule="x"))
    model["revise:rewrite:api"] = "UNCHANGED"
    capsys.readouterr()
    assert run(repo, "revise", "throttle") == 4
    assert "changes the text above the first `# Capability:` line" in (
        capsys.readouterr().out
    )
    assert ui.read_bytes() == before
