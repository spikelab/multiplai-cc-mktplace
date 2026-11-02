"""`resume` on a change whose specs pm-spec did not write (a change buildme
wrote before pm-spec existed, in the old layout, or one written by hand with
`specs.md`): the consistency check and the handoff only, no other model call,
no new spec or interview file."""

from __future__ import annotations

from conftest import (
    CAPS,
    TWO,
    change_dir,
    git,
    meta,
    proposal,
    requirements,
    run,
    source_dir,
    specs,
    worktree,
    write,
)
from multiplai_core.specs import is_ready
from pm_spec_pipeline import layout


def buildme_era_change(repo):
    """Written in the user's checkout; pm-spec runs on its copy in its worktree."""
    hand = source_dir(repo)
    write(hand / "proposal.md", proposal())
    write(hand / "use-cases.md", "# Use cases\n")
    for cap in CAPS:
        write(hand / "requirements" / f"{cap}.md", requirements(cap))
    write(hand / ".change.yaml", "schema: spec-driven\n")
    return change_dir(repo)


def files(cdir):
    return sorted(
        p.relative_to(cdir).as_posix()
        for p in cdir.rglob("*")
        if p.is_file() and ".pm-spec" not in p.parts
    )


def test_adopt_runs_only_the_check_then_marks_ready(repo, model, capsys):
    cdir = buildme_era_change(repo)
    before = files(source_dir(repo))
    model["components"] = TWO  # would split, if it were asked
    assert run(repo, "resume", "throttle", "--ticket-ref", "ENG-1") == 0
    assert model.labels() == ["component_check:throttle", "consistency"]
    assert "SPEC:READY:" in capsys.readouterr().out and is_ready(cdir) == (True, "")
    assert files(cdir) == sorted(before + ["tickets.json"])
    assert not (cdir / "interview.md").exists()


def test_only_pm_spec_files_are_adopted(repo, model):
    """A change dir also holds buildme's files and anyone else's; copied into
    pm-spec's worktree, they were left there uncommitted. Only the files
    pm-spec commits (`git_ops.owned`) and its run folder are copied; the
    originals all stay where they are."""
    cdir = buildme_era_change(repo)
    hand = source_dir(repo)
    owned = files(hand)
    foreign = [
        "design.md",
        "tasks.md",
        "notes.txt",
        ".board.json",
        ".build-state.json",
        "reviews/block-01-round-1.json",
        "requirements-draft/x.md",
    ]
    for rel in foreign:
        write(hand / rel, f"{rel}\n")
    assert run(repo, "resume", "throttle", "--ticket-ref", "ENG-1") == 0
    assert files(cdir) == sorted(owned + ["tickets.json"])
    assert all((hand / rel).is_file() for rel in foreign)
    rel = cdir.relative_to(worktree(repo)).as_posix()
    status = git(
        worktree(repo), "status", "--porcelain", "--untracked-files=all", "--", rel
    )
    assert [line for line in status.splitlines() if "/.pm-spec/" not in line] == []


def test_adopt_blocked_on_findings_stops_with_exit_4(repo, model, capsys):
    from test_consistency import finding, recheck

    cdir = buildme_era_change(repo)
    files_ = ("requirements/rate-limit.md", "requirements/retry-policy.md")
    model["consistency"] = [
        {"findings": [finding(files=files_)]},
        recheck("still_present"),
    ]
    assert run(repo, "resume", "throttle", "--ticket-ref", "ENG-1") == 4
    assert {label.split(":")[0] for label in model.labels()} == {
        "component_check",
        "consistency",
    }
    assert f"SPEC:BLOCKED:{cdir}" in capsys.readouterr().out
    assert meta(cdir).get("spec_status") != "ready"
    # Unchanged, again: refused with no call.
    calls = len(model.calls)
    assert run(repo, "resume", "throttle", "--ticket-ref", "ENG-1") == 4
    assert len(model.calls) == calls


def test_an_adopted_change_resumed_again_makes_no_call(repo, model):
    buildme_era_change(repo)
    assert run(repo, "resume", "throttle", "--ticket-ref", "ENG-1") == 0
    calls = len(model.calls)
    assert run(repo, "resume", "throttle", "--ticket-ref", "ENG-1") == 0
    assert len(model.calls) == calls


def test_an_old_handoff_with_no_ticket_is_refused_naming_ticket_ref(
    repo, model, capsys
):
    """Decision 9: with no ticket ref anywhere, nothing is copied, committed
    or pushed, and the advice names `--ticket-ref`."""
    cdir = buildme_era_change(repo)
    assert run(repo, "resume", "throttle") == 2
    out = capsys.readouterr().out
    assert "has no ticket ref" in out and "--ticket-ref" in out
    assert not cdir.exists() and model.calls == []


def test_a_hand_written_nested_change_is_adopted(repo, model, capsys):
    hand = source_dir(repo)
    write(hand / "proposal.md", proposal())
    for cid in ("api", "ui"):
        write(hand / "components" / cid / "plan.md", "# Plan: x\n")
        write(hand / "components" / cid / "specs.md", specs())
    assert run(repo, "resume", "throttle", "--ticket-ref", "ENG-1") == 0
    cdir = change_dir(repo)
    assert model.labels() == [
        "component_check:api",
        "component_check:ui",
        "consistency",
    ]
    assert layout.ready(cdir) == (True, "")
    assert is_ready(cdir / "components" / "ui") == (True, "")


def test_a_proposal_without_specs_is_not_adopted(repo, model, capsys):
    write(source_dir(repo) / "proposal.md", proposal())
    assert run(repo, "resume", "throttle", "--ticket-ref", "ENG-1") == 2
    assert "has no pm-spec run" in capsys.readouterr().out
