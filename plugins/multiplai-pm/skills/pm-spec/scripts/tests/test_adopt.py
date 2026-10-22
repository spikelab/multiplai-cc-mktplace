"""`resume` on a change whose specs pm-spec did not write (plan item 21's advice
for a change buildme wrote before pm-spec existed): the consistency check and
the handoff only, no other model call, no new spec or interview file."""

from __future__ import annotations

from conftest import (
    CAPS,
    change_dir,
    meta,
    proposal,
    requirements,
    run,
    source_dir,
    write,
)
from multiplai_core.specs import is_ready
from test_epic_split import PLAIN_SPLIT


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
    model["epic_split"] = PLAIN_SPLIT  # would split, if it were asked
    assert run(repo, "resume", "throttle", "--ticket-ref", "ENG-1") == 0
    assert model.labels() == ["consistency"]
    assert "SPEC:READY:" in capsys.readouterr().out and is_ready(cdir) == (True, "")
    assert files(cdir) == sorted(before + ["tickets.json"])
    assert not (cdir / "interview.md").exists()


def test_adopt_blocked_on_findings_stops_with_exit_4(repo, model, capsys):
    from test_consistency import finding, recheck

    cdir = buildme_era_change(repo)
    model["consistency"] = [{"findings": [finding()]}, recheck("still_present")]
    assert run(repo, "resume", "throttle", "--ticket-ref", "ENG-1") == 4
    assert {label.split(":")[0] for label in model.labels()} == {"consistency"}
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
