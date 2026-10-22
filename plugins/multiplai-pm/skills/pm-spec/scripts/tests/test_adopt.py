"""`resume` on a change whose specs pm-spec did not write (plan item 21's advice
for a change buildme wrote before pm-spec existed): the consistency check and
the handoff only, no other model call, no new spec or interview file."""

from __future__ import annotations

from conftest import CAPS, change_dir, meta, proposal, requirements, run, write
from multiplai_core.specs import is_ready
from test_epic_split import PLAIN_SPLIT


def buildme_era_change(repo):
    cdir = change_dir(repo)
    write(cdir / "proposal.md", proposal())
    write(cdir / "use-cases.md", "# Use cases\n")
    for cap in CAPS:
        write(cdir / "requirements" / f"{cap}.md", requirements(cap))
    write(cdir / ".change.yaml", "schema: spec-driven\n")
    return cdir


def files(cdir):
    return sorted(
        p.relative_to(cdir).as_posix()
        for p in cdir.rglob("*")
        if p.is_file() and ".pm-spec" not in p.parts
    )


def test_adopt_runs_only_the_check_then_marks_ready(repo, model, capsys):
    cdir = buildme_era_change(repo)
    before = files(cdir)
    model["epic_split"] = PLAIN_SPLIT  # would split, if it were asked
    assert run(repo, "resume", "throttle") == 0
    assert model.labels() == ["consistency"]
    assert "SPEC:READY:" in capsys.readouterr().out and is_ready(cdir) == (True, "")
    assert files(cdir) == sorted(before + ["tickets.json"])
    assert not (cdir / "interview.md").exists()


def test_adopt_blocked_on_findings_stops_with_exit_4(repo, model, capsys):
    from test_consistency import finding, recheck

    cdir = buildme_era_change(repo)
    model["consistency"] = [{"findings": [finding()]}, recheck("still_present")]
    assert run(repo, "resume", "throttle") == 4
    assert {label.split(":")[0] for label in model.labels()} == {"consistency"}
    assert f"SPEC:BLOCKED:{cdir}" in capsys.readouterr().out
    assert meta(cdir).get("spec_status") != "ready"
    # Unchanged, again: refused with no call.
    calls = len(model.calls)
    assert run(repo, "resume", "throttle") == 4
    assert len(model.calls) == calls


def test_an_adopted_change_resumed_again_makes_no_call(repo, model):
    buildme_era_change(repo)
    assert run(repo, "resume", "throttle") == 0
    calls = len(model.calls)
    assert run(repo, "resume", "throttle") == 0
    assert len(model.calls) == calls
