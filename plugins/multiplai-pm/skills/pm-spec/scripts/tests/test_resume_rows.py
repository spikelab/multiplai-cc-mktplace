"""One test per row S1-S17 of the plan's "Stop and resume states" table.

Each test stops the run at its row, resumes it, and compares the result with a
run of the same inputs that never stopped (`baseline`): the same files, the
same commits and refs on origin, and the same run state (`same_as`), except
where the row's "Resume does" column says otherwise; those parts assert what
the column says instead. A kill is `Killed`, a BaseException raised from
inside the call or the write the row names, so no `except Exception` in the
code can swallow it.

Most rows use a change with two components (`TWO`) and the stops a person
sees: `new` stops for the proposal's review, `resume` for the components',
`resume` at the ticket wait, `resume --ticket ...` for the specs' review,
and `resume` hands off (`FLOW`)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from conftest import (
    BRANCH,
    CAPS,
    TWO,
    FakeModel,
    Killed,
    agent,
    change_dir,
    git,
    make_repo,
    meta,
    origin,
    proposal,
    ready_line,
    requirements,
    run,
    snapshot,
    source_dir,
    specs,
    state,
    worktree,
    write,
)
from multiplai_core.specs import is_ready, mark_ready, write_change_meta
from pm_spec_pipeline import epic, git_ops, handoff, layout, runlock, sdk, stages
from pm_spec_pipeline import state as state_mod
from pm_spec_pipeline.state import (
    EpicChild,
    Handoff,
    RunState,
    SplitRecord,
    StepOutcome,
)

NEW = ["new", "throttle", "--interview-summary", "Throttle it."]
RESUME = ["resume", "throttle"]
TICKETS = [*RESUME, "--ticket", "api=ENG-2", "--ticket", "ui=ENG-3"]
FLOW = [(NEW, 3), (RESUME, 3), (RESUME, 3), (TICKETS, 3), (RESUME, 0)]
AUTO = [(["new", "throttle", "--auto"], 0)]
AUTO_LIKE_NEW = [([*NEW, "--auto", "--interview-mode", "user"], 0)]


def two(m: FakeModel) -> None:
    m["components"] = TWO


def killer(then=None, times: int = 1):
    """A handler that kills the run on its first *times* calls and answers like *then* after."""

    def kill(prompt, label, kw):
        raise Killed(label)

    return [kill] * times + [then] if then is not None else kill


def baseline(tmp_path, monkeypatch, configure, commands) -> tuple[Path, FakeModel]:
    """The same inputs in a fresh repository with a fresh fake model, never stopped.
    The test's own model is put back afterwards."""
    previous = sdk.run_agent
    fake = FakeModel()
    configure(fake)
    monkeypatch.setattr(sdk, "run_agent", fake)
    other = make_repo(
        tmp_path / f"baseline-{len(list(tmp_path.glob('baseline-*')))}" / "proj"
    )
    try:
        for argv, code in commands:
            assert run(other, *argv) == code, argv
    finally:
        monkeypatch.setattr(sdk, "run_agent", previous)
    return other, fake


def pushed(repo: Path) -> list[str]:
    """Origin's feature branch, newest first: each commit's subject and the files it changed."""
    out = git(origin(repo), "log", "--format=@ %s", "--name-only", BRANCH)
    return [line for line in out.splitlines() if line]


def refs(repo: Path) -> list[str]:
    return git(origin(repo), "for-each-ref", "--format=%(refname)").splitlines()


def run_state(cdir: Path) -> dict:
    """The run state, but the handoff commit's sha (`.change.yaml`'s timestamps differ)."""
    data = json.loads((cdir / ".pm-spec" / "state.json").read_text())
    if data.get("handoff"):
        data["handoff"].pop("commit", None)
    return data


def same_as(
    repo: Path, other: Path, *, commits: bool = True, run_state_too: bool = True
) -> None:
    """The change dir, origin's commits and refs, and the run state match *other*'s."""
    cdir, theirs = change_dir(repo), change_dir(other)
    assert snapshot(cdir) == snapshot(theirs)
    assert refs(repo) == refs(other)
    if commits:
        assert pushed(repo) == pushed(other)
    if run_state_too:
        assert run_state(cdir) == run_state(theirs)


def run_dir_files(cdir: Path) -> dict[str, bytes]:
    """Everything in the change dir, the run folder included, but the lock's holder note."""
    return {
        p.relative_to(cdir).as_posix(): p.read_bytes()
        for p in sorted(cdir.rglob("*"))
        if p.is_file() and p.name != runlock.LOCK_FILE
    }


def clone(repo: Path, at: Path) -> Path:
    """Another person's clone of origin, on the feature branch."""
    git(repo.parent, "clone", "-q", "-b", BRANCH, str(origin(repo)), str(at))
    return at


def push_edit(repo: Path, at: Path, rel: str, text: str) -> None:
    """A person edits *rel* (under the change dir) in their clone and pushes it."""
    mine = clone(repo, at)
    write(mine / "specs" / "changes" / "throttle" / rel, text)
    git(mine, "commit", "-qam", f"edit {rel}")
    git(mine, "push", "-q", "origin", BRANCH)


def go(repo: Path, steps) -> None:
    for argv, code in steps:
        assert run(repo, *argv) == code, argv


# --- S1 ---------------------------------------------------------------------------


def test_S1_the_review_stop_after_the_proposal(
    repo, model, tmp_path, monkeypatch, capsys
):
    two(model)
    assert run(repo, *NEW) == 3
    out = capsys.readouterr().out
    cdir = change_dir(repo)
    assert "SPEC:REVIEW:proposal" in out and f"SPEC:BRANCH:{BRANCH}" in out
    assert pushed(repo) == [
        "@ specs(throttle): proposal for review",
        "specs/changes/throttle/.change.yaml",
        "specs/changes/throttle/interview.md",
        "specs/changes/throttle/proposal.md",
        "@ initial",
        "README.md",
    ]
    assert state(cdir)["steps"]["proposal"]["outcome"] == "passed"
    assert not (cdir / "use-cases.md").exists()
    assert git(worktree(repo), "status", "--porcelain") == ""

    # Resume pulls, then runs stage 2 only.
    calls = len(model.calls)
    assert run(repo, *RESUME) == 3
    assert model.labels()[calls:] == ["spec:use-cases", "components"]
    assert "SPEC:REVIEW:components" in capsys.readouterr().out

    # The files are the ones a run with no stop writes; the commits are not
    # (a stop pushes for review).
    go(repo, [(RESUME, 3), ([*RESUME, "--tickets-later"], 3), (RESUME, 0)])
    other, _ = baseline(tmp_path, monkeypatch, two, AUTO_LIKE_NEW)
    same_as(repo, other, commits=False, run_state_too=False)


# --- S2 ---------------------------------------------------------------------------


def test_S2_killed_after_the_proposal_before_the_commit(
    repo, model, tmp_path, monkeypatch, capsys
):
    two(model)
    real = git_ops.commit_change_dir
    kills = {"left": 1}

    def commit(*a, **k):
        if kills["left"]:
            kills["left"] -= 1
            raise Killed("commit")
        return real(*a, **k)

    monkeypatch.setattr(git_ops, "commit_change_dir", commit)
    with pytest.raises(Killed):
        run(repo, *NEW)
    cdir = change_dir(repo)
    assert (cdir / "proposal.md").is_file()
    assert git(worktree(repo), "status", "--porcelain", "--", "specs") != ""
    assert git(origin(repo), "branch", "--list", BRANCH) == ""
    capsys.readouterr()

    # Commits and pushes what is there, then stops at S1; nothing regenerated.
    assert run(repo, *RESUME) == 3
    assert "SPEC:REVIEW:proposal" in capsys.readouterr().out
    assert model.calls_to("spec:proposal") == 1
    assert pushed(repo)[0] == "@ specs(throttle): proposal for review"

    go(repo, FLOW[1:])
    other, _ = baseline(tmp_path, monkeypatch, two, FLOW)
    same_as(repo, other)


# --- S3 ---------------------------------------------------------------------------


def test_S3_the_push_failed(repo, model, tmp_path, monkeypatch, capsys):
    two(model)
    hook = origin(repo) / "hooks" / "pre-receive"
    write(hook, "#!/bin/sh\necho refused by the test >&2\nexit 1\n")
    hook.chmod(0o755)
    assert run(repo, *NEW) == 1
    assert f"`git push origin {BRANCH}` failed" in capsys.readouterr().out
    wt = worktree(repo)
    head = git(wt, "rev-parse", "HEAD")
    assert git(wt, "log", "-1", "--format=%s") == "specs(throttle): proposal for review"
    assert git(origin(repo), "branch", "--list", BRANCH) == ""
    assert state(change_dir(repo))["steps"].get("proposal") is None

    # Retries the push, then stops at S1: no second commit, no call.
    hook.unlink()
    calls = len(model.calls)
    assert run(repo, *RESUME) == 3
    assert "SPEC:REVIEW:proposal" in capsys.readouterr().out
    assert len(model.calls) == calls
    assert (
        git(wt, "rev-parse", "HEAD") == head == git(origin(repo), "rev-parse", BRANCH)
    )

    go(repo, FLOW[1:])
    other, _ = baseline(tmp_path, monkeypatch, two, FLOW)
    same_as(repo, other)


# --- S4 ---------------------------------------------------------------------------


def test_S4_the_review_stop_after_the_components(
    repo, model, tmp_path, monkeypatch, capsys
):
    two(model)
    go(repo, FLOW[:2])
    out = capsys.readouterr().out
    cdir = change_dir(repo)
    assert "SPEC:REVIEW:components" in out and f"SPEC:BRANCH:{BRANCH}" in out
    assert pushed(repo)[:9] == [
        "@ specs(throttle): components for review",
        "specs/changes/throttle/.change.yaml",
        "specs/changes/throttle/components.md",
        "specs/changes/throttle/components/api/.change.yaml",
        "specs/changes/throttle/components/api/plan.md",
        "specs/changes/throttle/components/ui/.change.yaml",
        "specs/changes/throttle/components/ui/plan.md",
        "specs/changes/throttle/use-cases.md",
        "@ specs(throttle): proposal for review",
    ]
    assert state(cdir)["steps"]["components"]["outcome"] == "passed"
    assert not (cdir / "prototype").exists()

    # Resume pulls, then runs stage 3 from the prototype.
    calls = len(model.calls)
    assert run(repo, *RESUME) == 3
    assert model.labels()[calls:] == ["prototype"]

    go(repo, [([*RESUME, "--tickets-later"], 3), (RESUME, 0)])
    other, _ = baseline(tmp_path, monkeypatch, two, AUTO_LIKE_NEW)
    same_as(repo, other, commits=False, run_state_too=False)


# --- S5 ---------------------------------------------------------------------------


def test_S5_killed_part_way_through_the_component_plans(
    repo, model, tmp_path, monkeypatch, capsys
):
    two(model)
    real = epic.atomic_write
    kills = {"left": 1}

    def write_or_die(path, text):
        if path.name == "plan.md" and path.parent.name == "ui" and kills["left"]:
            kills["left"] -= 1
            raise Killed("ui plan")
        return real(path, text)

    monkeypatch.setattr(epic, "atomic_write", write_or_die)
    assert run(repo, *NEW) == 3
    with pytest.raises(Killed):
        run(repo, *RESUME)
    cdir = change_dir(repo)
    assert (cdir / "components" / "api" / "plan.md").is_file()
    assert not (cdir / "components" / "ui" / "plan.md").exists()
    assert not (cdir / "components.md").exists()
    capsys.readouterr()

    # Writes what is missing from the saved answer, keeps the rest, stops at S4.
    assert run(repo, *RESUME) == 3
    assert "SPEC:REVIEW:components" in capsys.readouterr().out
    assert model.calls_to("components") == 1 and model.calls_to("spec:use-cases") == 1

    go(repo, FLOW[2:])
    other, _ = baseline(tmp_path, monkeypatch, two, FLOW)
    same_as(repo, other)


# --- S6 ---------------------------------------------------------------------------


def test_S6_the_ticket_wait(repo, model, tmp_path, monkeypatch, capsys):
    two(model)
    go(repo, FLOW[:3])
    out = capsys.readouterr().out
    cdir = change_dir(repo)
    assert f"SPEC:TICKETS:{cdir / 'tickets.json'}" in out
    assert pushed(repo)[:5] == [
        "@ specs(throttle): prototype and tickets",
        "specs/changes/throttle/prototype/NOTES.md",
        "specs/changes/throttle/prototype/mockup.html",
        "specs/changes/throttle/tickets.json",
        "@ specs(throttle): components for review",
    ]
    assert state(cdir)["steps"]["tickets"]["outcome"] == "pending"

    # Without a ticket for every component: exit 2, the flags printed, nothing changed.
    before, calls = run_dir_files(cdir), len(model.calls)
    for argv in (RESUME, [*RESUME, "--ticket", "api=ENG-2"]):
        assert run(repo, *argv) == 2
        assert (
            "resume throttle --ticket api=<ID> --ticket ui=<ID>"
            in capsys.readouterr().out
        )
    assert run_dir_files(cdir) == before and len(model.calls) == calls

    # With them: the refs written, then the specs, then their review stop.
    assert run(repo, *TICKETS) == 3
    assert "SPEC:REVIEW:specs" in capsys.readouterr().out
    assert model.labels()[calls:] == ["spec:specs:api", "spec:specs:ui"]
    assert run(repo, *RESUME) == 0
    other, _ = baseline(tmp_path, monkeypatch, two, FLOW)
    same_as(repo, other)

    # --tickets-later instead: the same, with the tickets pending.
    later = make_repo(tmp_path / "later" / "proj")
    go(later, [*FLOW[:3], ([*RESUME, "--tickets-later"], 3), (RESUME, 0)])
    assert state(change_dir(later))["steps"]["tickets"]["outcome"] == "skipped"
    assert "ticket_refs" not in meta(change_dir(later) / "components" / "ui")


@pytest.mark.parametrize("bad", ["ENG 2", "ENG..2", "ENG~2", "-ENG-2"])
def test_S6_a_ticket_ref_git_refuses_is_refused_and_nothing_is_written(
    repo, model, tmp_path, monkeypatch, capsys, bad
):
    two(model)
    go(repo, FLOW[:3])
    cdir, wt = change_dir(repo), worktree(repo)
    before, calls = run_dir_files(cdir), len(model.calls)
    head = git(wt, "rev-parse", "HEAD")
    capsys.readouterr()

    assert run(repo, *RESUME, "--ticket", f"api={bad}", "--ticket", "ui=ENG-3") == 2
    out = capsys.readouterr().out
    assert f"--ticket 'api={bad}'" in out and "Nothing was recorded" in out
    assert run_dir_files(cdir) == before and len(model.calls) == calls
    assert git(wt, "rev-parse", "HEAD") == head

    # Good refs afterwards: as if the bad one was never given.
    go(repo, [(TICKETS, 3), (RESUME, 0)])
    other, _ = baseline(tmp_path, monkeypatch, two, FLOW)
    same_as(repo, other)


# --- S7 ---------------------------------------------------------------------------


def test_S7_killed_during_the_specs(repo, model, tmp_path, monkeypatch, capsys):
    two(model)
    model["spec:specs:ui"] = killer(specs())
    go(repo, FLOW[:3])
    with pytest.raises(Killed):
        run(repo, *TICKETS)
    cdir = change_dir(repo)
    assert (cdir / "components" / "api" / "specs.md").is_file()
    assert not (cdir / "components" / "ui" / "specs.md").exists()

    # Writes the missing one only, then stops for the specs' review (S13).
    calls = len(model.calls)
    assert run(repo, *RESUME) == 3
    assert model.labels()[calls:] == ["spec:specs:ui"]
    assert "SPEC:REVIEW:specs" in capsys.readouterr().out
    assert run(repo, *RESUME) == 0
    assert model.labels()[calls:] == ["spec:specs:ui", "consistency"]
    assert (
        ready_line(capsys.readouterr().out)
        == f"SPEC:READY:{cdir}:{layout.revision(cdir)}"
    )
    other, _ = baseline(tmp_path, monkeypatch, two, FLOW)
    same_as(repo, other)


# --- S8 ---------------------------------------------------------------------------


def test_S8_the_handoff_commit_failed(repo, model, tmp_path, monkeypatch, capsys):
    two(model)
    real = git_ops.commit_change_dir
    failing = {"left": 1}

    def commit(*a, **k):
        if failing["left"]:
            failing["left"] -= 1
            raise git_ops.GitError("index.lock exists")
        return real(*a, **k)

    monkeypatch.setattr(handoff, "commit_change_dir", commit)
    assert run(repo, "new", "throttle", "--auto") == 1
    cdir = change_dir(repo)
    assert layout.ready(cdir) == (True, "")
    assert state(cdir)["handoff"]["committed"] is False
    assert git(worktree(repo), "status", "--porcelain", "--", "specs") != ""
    capsys.readouterr()

    # The revision still matches: only the commit is retried.
    calls = len(model.calls)
    assert run(repo, *RESUME) == 0
    assert len(model.calls) == calls
    assert (
        ready_line(capsys.readouterr().out)
        == f"SPEC:READY:{cdir}:{layout.revision(cdir)}"
    )
    assert git(worktree(repo), "status", "--porcelain", "--", "specs") == ""
    other, _ = baseline(tmp_path, monkeypatch, two, AUTO)
    same_as(repo, other)

    # The files changed since: back to draft, the check again, then ready.
    second = make_repo(tmp_path / "second" / "proj")
    failing["left"] = 1
    assert run(second, "new", "throttle", "--auto") == 1
    cdir2 = change_dir(second)
    old = layout.revision(cdir2)
    write(cdir2 / "components" / "ui" / "specs.md", specs(rule="fifty a minute"))
    checks = len(model.exactly("consistency"))
    assert run(second, *RESUME) == 0
    assert len(model.exactly("consistency")) == checks + 1
    assert layout.revision(cdir2) != old and layout.ready(cdir2) == (True, "")
    assert git(worktree(second), "status", "--porcelain", "--", "specs") == ""


# --- S9 ---------------------------------------------------------------------------


EDITED_PLAN = (
    "# Plan: Build the limits endpoint\n\n**Objective:** EDITED objective.\n\n"
    "## Done means\n1. EDITED: a client over 60 a minute gets 429.\n"
)


def test_S9_edits_between_a_stop_and_the_resume_are_kept(repo, model, tmp_path, capsys):
    two(model)
    go(repo, FLOW[:2])
    cdir = change_dir(repo)
    push_edit(repo, tmp_path / "pm", "components/api/plan.md", EDITED_PLAN)

    # Pulled, kept, and an input to stage 3 from then on.
    go(repo, FLOW[2:])
    assert (cdir / "components" / "api" / "plan.md").read_text() == EDITED_PLAN
    assert model.calls_to("components") == 1
    assert "EDITED objective" in model.exactly("prototype")[0]
    assert "EDITED objective" in model.exactly("spec:specs:api")[0]
    task = json.loads((cdir / "tickets.json").read_text())[1]
    assert task["title"] == "Build the limits endpoint"
    assert task["acceptance"] == ["EDITED: a client over 60 a minute gets 429."]

    # An edit after the ready record makes it stale: checked again, ready again.
    old = layout.revision(cdir)
    push_edit(
        repo, tmp_path / "dev", "components/ui/specs.md", specs(rule="EDITED rule")
    )
    capsys.readouterr()
    checks, written = len(model.exactly("consistency")), model.calls_to("spec:specs")
    assert run(repo, *RESUME) == 0
    assert "EDITED rule" in (cdir / "components" / "ui" / "specs.md").read_text()
    assert len(model.exactly("consistency")) == checks + 1
    assert model.calls_to("spec:specs") == written
    assert layout.revision(cdir) != old
    assert (
        ready_line(capsys.readouterr().out)
        == f"SPEC:READY:{cdir}:{layout.revision(cdir)}"
    )
    assert is_ready(cdir / "components" / "ui") == (True, "")
    assert git(worktree(repo), "rev-parse", "HEAD") == git(
        origin(repo), "rev-parse", BRANCH
    )


# --- S10 --------------------------------------------------------------------------


def test_S10_a_diverged_branch_is_refused_and_nothing_changes(
    repo, model, tmp_path, capsys
):
    two(model)
    assert run(repo, *NEW) == 3
    wt, cdir = worktree(repo), change_dir(repo)
    write(cdir / "notes.md", "local\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "mine")
    mine = git(wt, "rev-parse", "HEAD")
    push_edit(
        repo, tmp_path / "other", "proposal.md", "# Request throttling\n\nTheirs.\n"
    )
    capsys.readouterr()
    before, calls = run_dir_files(cdir), len(model.calls)

    assert run(repo, *RESUME) == 2
    out = capsys.readouterr().out
    assert f"origin/{BRANCH}" in out and "Nothing was changed" in out
    assert f"git -C {wt} pull --rebase origin {BRANCH}" in out
    assert git(wt, "rev-parse", "HEAD") == mine
    assert run_dir_files(cdir) == before and len(model.calls) == calls


# --- S11 --------------------------------------------------------------------------


def test_S11_the_same_step_stopped_twice(repo, model, tmp_path, monkeypatch):
    two(model)
    model["spec:specs:ui"] = killer(specs(), times=2)
    go(repo, FLOW[:3])
    with pytest.raises(Killed):
        run(repo, *TICKETS)
    with pytest.raises(Killed):
        run(repo, *RESUME)
    go(repo, [(RESUME, 3), (RESUME, 0)])
    assert model.calls_to("spec:specs:api") == 1 and model.calls_to("prototype") == 1
    other, _ = baseline(tmp_path, monkeypatch, two, FLOW)
    same_as(repo, other)


# --- S12 --------------------------------------------------------------------------


def test_S12_a_second_process_is_refused_with_the_holders_pid(
    repo, model, tmp_path, monkeypatch, capsys
):
    two(model)
    model["spec:specs:ui"] = killer(specs())
    go(repo, FLOW[:3])
    with pytest.raises(Killed):
        run(repo, *TICKETS)
    cdir = change_dir(repo)
    before, calls = run_dir_files(cdir), len(model.calls)
    capsys.readouterr()
    with runlock.hold(cdir / ".pm-spec"):
        assert run(repo, *RESUME) == 5
    assert f"pid {os.getpid()}" in capsys.readouterr().out
    assert len(model.calls) == calls and run_dir_files(cdir) == before

    # Once the holder is gone the run resumes as if nothing happened.
    go(repo, [(RESUME, 3), (RESUME, 0)])
    other, _ = baseline(tmp_path, monkeypatch, two, FLOW)
    same_as(repo, other)


# --- S13 --------------------------------------------------------------------------


def test_S13_the_review_stop_after_the_specs(
    repo, model, tmp_path, monkeypatch, capsys
):
    two(model)
    go(repo, FLOW[:4])
    out = capsys.readouterr().out
    cdir = change_dir(repo)
    assert "SPEC:REVIEW:specs" in out and f"SPEC:BRANCH:{BRANCH}" in out
    assert "SPEC:READY" not in out
    # The specs, and the tickets the resume recorded.
    assert pushed(repo)[:8] == [
        "@ specs(throttle): specs for review",
        "specs/changes/throttle/components.md",
        "specs/changes/throttle/components/api/.change.yaml",
        "specs/changes/throttle/components/api/specs.md",
        "specs/changes/throttle/components/ui/.change.yaml",
        "specs/changes/throttle/components/ui/specs.md",
        "specs/changes/throttle/tickets.json",
        "@ specs(throttle): prototype and tickets",
    ]
    assert state(cdir)["steps"]["specs"]["outcome"] == "passed"
    assert "consistency" not in model.labels() and state(cdir)["handoff"] is None
    assert meta(cdir)["spec_status"] == "draft"

    # Resume pulls, then runs the check and the handoff only.
    calls = len(model.calls)
    assert run(repo, *RESUME) == 0
    assert model.labels()[calls:] == ["consistency"]
    assert (
        ready_line(capsys.readouterr().out)
        == f"SPEC:READY:{cdir}:{layout.revision(cdir)}"
    )
    other, _ = baseline(tmp_path, monkeypatch, two, FLOW)
    same_as(repo, other)

    # A reviewer's edit to a specs.md and to the prototype, pushed at the
    # stop: kept, and what the check reads.
    edited = make_repo(tmp_path / "edited" / "proj")
    go(edited, FLOW[:4])
    rule, mock = specs(rule="EDITED by a reviewer"), "<html>the reviewer's</html>\n"
    mine = clone(edited, tmp_path / "reviewer")
    write(
        mine / "specs" / "changes" / "throttle" / "components" / "ui" / "specs.md", rule
    )
    write(mine / "specs" / "changes" / "throttle" / "prototype" / "mockup.html", mock)
    git(mine, "commit", "-qam", "review edits")
    git(mine, "push", "-q", "origin", BRANCH)
    written = model.calls_to("spec:specs")
    assert run(edited, *RESUME) == 0
    ecdir = change_dir(edited)
    assert (ecdir / "components" / "ui" / "specs.md").read_text() == rule
    assert (ecdir / "prototype" / "mockup.html").read_text() == mock
    assert "EDITED by a reviewer" in model.exactly("consistency")[-1]
    assert model.calls_to("spec:specs") == written
    assert layout.ready(ecdir) == (True, "")


CONTRADICTION = {
    "type": "contradiction",
    "severity": "major",
    "description": "api says 60 a minute, ui says 100",
    "places": [
        {"file": "components/api/specs.md", "where": "rate-limit"},
        {"file": "components/ui/specs.md", "where": "rate-limit"},
    ],
    "suggestion": "make them agree",
}
NIT = {**CONTRADICTION, "severity": "minor", "description": "a wording nit"}


def two_with_check(*answers):
    """Two components, and the consistency agent's answers in order."""

    def configure(m: FakeModel) -> None:
        two(m)
        m["consistency"] = [{"findings": a} for a in answers]

    return configure


def test_S13_after_the_review_a_major_finding_blocks_and_rewrites_nothing(
    repo, model, tmp_path, monkeypatch, capsys
):
    configure = two_with_check([CONTRADICTION])
    configure(model)
    go(repo, FLOW[:4])
    cdir = change_dir(repo)
    before = {u: (cdir / u).read_text() for u in layout.units(cdir)}
    capsys.readouterr()

    calls = len(model.calls)
    assert run(repo, *RESUME) == 4
    out = capsys.readouterr().out
    assert model.labels()[calls:] == ["consistency"]
    assert f"SPEC:BLOCKED:{cdir}" in out and "SPEC:READY" not in out
    assert "api says 60 a minute, ui says 100" in out
    assert "no file was rewritten" in out
    assert {u: (cdir / u).read_text() for u in layout.units(cdir)} == before
    step = state(cdir)["steps"]["consistency"]
    assert step["outcome"] == "blocked" and step["repair_attempts"] == 0
    assert [f["severity"] for f in step["blocking_findings"]] == ["major"]
    assert meta(cdir)["spec_status"] == "draft" and state(cdir)["handoff"] is None

    other, _ = baseline(tmp_path, monkeypatch, configure, [*FLOW[:4], (RESUME, 4)])
    same_as(repo, other)


def test_S13_after_the_review_a_minor_finding_is_reported_and_handed_off(
    repo, model, tmp_path, monkeypatch, capsys
):
    configure = two_with_check([NIT])
    configure(model)
    go(repo, FLOW[:4])
    capsys.readouterr()
    assert run(repo, *RESUME) == 0
    out = capsys.readouterr().out
    cdir = change_dir(repo)
    assert "advisory findings" in out and "a wording nit" in out
    assert ready_line(out) == f"SPEC:READY:{cdir}:{layout.revision(cdir)}"
    assert not any(":rewrite:" in label for label in model.labels())
    other, _ = baseline(tmp_path, monkeypatch, configure, FLOW)
    same_as(repo, other)


# --- S17 --------------------------------------------------------------------------


def reviewer_edit(repo: Path, at: Path, rule: str) -> str:
    """A person fixes the ui specs in their own clone and pushes."""
    text = specs(rule=rule)
    mine = clone(repo, at)
    write(
        mine / "specs" / "changes" / "throttle" / "components" / "ui" / "specs.md", text
    )
    git(mine, "commit", "-qam", "fix the contradiction")
    git(mine, "push", "-q", "origin", BRANCH)
    return text


def test_S17_resume_after_a_blocked_check_rechecks_the_edited_files(
    repo, model, tmp_path, monkeypatch, capsys
):
    configure = two_with_check([CONTRADICTION], [])
    configure(model)
    go(repo, [*FLOW[:4], (RESUME, 4)])
    cdir = change_dir(repo)
    capsys.readouterr()

    # Unchanged: refused with no model call, the findings printed again.
    calls = len(model.calls)
    assert run(repo, *RESUME) == 4
    out = capsys.readouterr().out
    assert len(model.calls) == calls
    assert "api says 60 a minute, ui says 100" in out
    assert f"SPEC:BLOCKED:{cdir}" in out and "one rewrite" not in out

    # A person edits and pushes: pulled, checked again (no rewrite), handed off.
    rule = reviewer_edit(repo, tmp_path / "reviewer", "60 a minute everywhere")
    assert run(repo, *RESUME) == 0
    out = capsys.readouterr().out
    assert model.labels()[calls:] == ["consistency"]
    assert "60 a minute everywhere" in model.exactly("consistency")[-1]
    assert (cdir / "components" / "ui" / "specs.md").read_text() == rule
    assert ready_line(out) == f"SPEC:READY:{cdir}:{layout.revision(cdir)}"
    assert state(cdir)["steps"]["consistency"]["outcome"] == "passed"

    # The same edit made at the review stop, in a run that never blocked.
    def at_review(m: FakeModel) -> None:
        two(m)
        m["consistency"] = {"findings": []}

    other, _ = baseline(tmp_path, monkeypatch, at_review, FLOW[:4])
    reviewer_edit(other, tmp_path / "reviewer-2", "60 a minute everywhere")
    monkeypatch.setattr(sdk, "run_agent", FakeModel())
    assert run(other, *RESUME) == 0
    monkeypatch.setattr(sdk, "run_agent", model)
    # Files, commits and refs as if it never blocked; the run state keeps
    # the check's history.
    same_as(repo, other, run_state_too=False)


def test_S17_an_edit_that_still_disagrees_blocks_again(repo, model, capsys):
    two_with_check([CONTRADICTION], [CONTRADICTION])(model)
    go(repo, [*FLOW[:4], (RESUME, 4)])
    reviewer_edit(repo, repo.parent / "reviewer", "still 100 in the ui")
    capsys.readouterr()
    assert run(repo, *RESUME) == 4
    cdir = change_dir(repo)
    assert f"SPEC:BLOCKED:{cdir}" in capsys.readouterr().out
    assert model.calls_to("consistency") == 2
    assert not any(":rewrite:" in label for label in model.labels())
    assert state(cdir)["steps"]["consistency"]["outcome"] == "blocked"


def test_auto_keeps_the_one_rewrite(repo, model):
    """No review stop, no reviewer: the check still rewrites what it cites."""
    two(model)
    model["consistency"] = [
        {"findings": [CONTRADICTION]},
        {
            "findings": [],
            "prior_assessments": [
                {"finding_id": "CC-F1", "status": "resolved", "evidence": "x"}
            ],
        },
    ]
    assert run(repo, "new", "throttle", "--auto") == 0
    assert [label for label in model.labels() if ":rewrite:" in label] == [
        "consistency:rewrite:api",
        "consistency:rewrite:ui",
    ]


# --- S14 --------------------------------------------------------------------------


def old_layout_change(repo: Path, *, handed_off: bool) -> None:
    """A change pm-spec 0.8.0 or earlier wrote in the user's checkout:
    `requirements/`, no `specs.md`, and its run state, which `branch.adopt`
    copies into pm-spec's worktree with it. *handed_off*: the old run checked
    it and wrote the ready record; else it stopped before the check."""
    hand = source_dir(repo)
    write(hand / "proposal.md", proposal())
    write(hand / "use-cases.md", "# Use cases\n")
    for cap in CAPS:
        write(hand / "requirements" / f"{cap}.md", requirements(cap))
    write_change_meta(hand, {"ticket_refs": {"epic": "ENG-1", "children": []}})
    steps = {"prototype": StepOutcome(outcome="skipped")}
    handoff_record = None
    if handed_off:
        rev = mark_ready(hand)
        steps["consistency"] = StepOutcome(outcome="passed", revision=rev)
        handoff_record = Handoff(revision=rev, committed=True, commit="abc")
    old = RunState(
        change="throttle",
        intake_done=True,
        interview_done=True,
        steps=steps,
        handoff=handoff_record,
    )
    state_mod.save(old, hand / ".pm-spec", ledger=False)


STAGE_FILES = ("components.md", "specs.md", "plan.md", "components", "prototype")


@pytest.mark.parametrize("handed_off", [True, False])
def test_S14_a_run_from_before_the_staged_flow_only_checks_and_hands_off(
    repo, model, capsys, handed_off
):
    two(model)
    old_layout_change(repo, handed_off=handed_off)
    # Edited after the old handoff: the ready record is stale (S9).
    hand = source_dir(repo)
    write(hand / "requirements" / f"{CAPS[0]}.md", requirements(CAPS[0], rule="EDITED"))

    assert run(repo, *RESUME) == 0
    out = capsys.readouterr().out
    cdir = change_dir(repo)
    assert model.labels() == ["consistency"]
    assert "EDITED" in model.exactly("consistency")[0]
    assert "SPEC:REVIEW" not in out and "SPEC:TICKETS" not in out
    assert ready_line(out) == f"SPEC:READY:{cdir}:{layout.revision(cdir)}"
    assert layout.ready(cdir) == (True, "")
    assert [n for n in STAGE_FILES if (cdir / n).exists()] == []
    # The handoff writes it when missing, as for an adopted change.
    assert (cdir / "tickets.json").is_file()
    steps = state(cdir)["steps"]
    for name in ("proposal", "components", "tickets", "specs"):
        assert steps[name]["outcome"] == "skipped"
        assert steps[name]["reason"] == stages.BEFORE_STAGED_FLOW
    # The old run's own record of its prototype step is kept as it was.
    assert steps["prototype"] == {
        **steps["prototype"],
        "outcome": "skipped",
        "reason": "",
    }
    assert git(worktree(repo), "rev-parse", "HEAD") == git(
        origin(repo), "rev-parse", BRANCH
    )

    # Resumed again: nothing to do, nothing regenerated.
    assert run(repo, *RESUME) == 0
    assert model.labels() == ["consistency"]


# --- S15 --------------------------------------------------------------------------

BLOCKED_NOTES = "- mockup.html: half of it\n\nQUESTIONS: which page?\nSTATUS: BLOCKED\n"


def test_S15_a_blocked_prototype_is_checked_again_not_taken_as_the_pms(
    repo, model, tmp_path, monkeypatch, capsys
):
    model["prototype"] = [agent(BLOCKED_NOTES), agent()]
    go(repo, [(AUTO[0][0], 1)])
    cdir = change_dir(repo)
    assert state(cdir)["steps"]["prototype"]["outcome"] == "blocked"
    capsys.readouterr()

    # Still BLOCKED: fails again with the reason; no agent, nothing kept.
    assert run(repo, *RESUME) == 1
    assert "STATUS: BLOCKED" in capsys.readouterr().out
    assert model.calls_to("prototype") == 1
    assert state(cdir)["steps"]["prototype"]["outcome"] == "blocked"
    assert "spec:specs" not in " ".join(model.labels())

    # A person answers the agent's question: checked again, kept, and on.
    write(
        cdir / "prototype" / "NOTES.md",
        "- mockup.html: the limits page\nSTATUS: DONE\n",
    )
    assert run(repo, *RESUME) == 0
    assert model.calls_to("prototype") == 1
    assert state(cdir)["steps"]["prototype"]["outcome"] == "passed"
    assert "STATUS: DONE" in (cdir / "prototype" / "NOTES.md").read_text()


def test_S15_a_prototype_without_notes_runs_the_agent_again(
    repo, model, tmp_path, monkeypatch
):
    model["prototype"] = [agent(notes=None), agent(notes=None), agent()]
    go(repo, [(AUTO[0][0], 1)])
    cdir = change_dir(repo)
    assert (cdir / "prototype" / "mockup.html").is_file()
    assert not (cdir / "prototype" / "NOTES.md").exists()

    assert run(repo, *RESUME) == 0
    assert model.calls_to("prototype") == 3
    assert (cdir / "prototype" / "NOTES.md").is_file()
    other, _ = baseline(tmp_path, monkeypatch, lambda m: None, AUTO)
    same_as(repo, other, commits=False, run_state_too=False)


def test_S15_files_are_the_pms_only_when_no_agent_ran(repo, model):
    # A write outside prototype/ failed the run; what it left inside is
    # checked by the gate, never kept unchecked.
    model["prototype"] = [agent(notes=None, outside=repo.parent / "x.txt"), agent()]
    go(repo, [(AUTO[0][0], 1)])
    cdir = change_dir(repo)
    assert (cdir / "prototype" / "mockup.html").is_file()
    assert run(repo, *RESUME) == 0
    assert model.calls_to("prototype") == 2
    reason = state(cdir)["steps"]["prototype"]["reason"]
    assert not reason.startswith("Kept the")


# --- S16 --------------------------------------------------------------------------

OLD_CHILDREN = ("throttle-api", "throttle-ui")


def old_split(repo: Path) -> dict:
    """The parent of a pm-spec 0.7.2 epic split in the user's checkout: its
    `interview.md` and `.change.yaml` (`children`), and a run state holding
    the split; each child a sibling change in the old layout, handed off by
    its own run. Returns the parent's run state as saved."""
    parent = source_dir(repo)
    write(parent / "interview.md", "# Interview\n\nThrottle it.\n")
    write_change_meta(
        parent,
        {
            "children": list(OLD_CHILDREN),
            "ticket_refs": {"epic": "ENG-1", "children": []},
        },
    )
    split = SplitRecord(
        inputs="old",
        single=False,
        children=[
            EpicChild(id="api", title="API", plan="# Plan: api\n"),
            EpicChild(id="ui", title="UI", depends_on=["api"], plan="# Plan: ui\n"),
        ],
    )
    old = RunState(
        change="throttle", intake_done=True, interview_done=True, split=split
    )
    state_mod.save(old, parent / ".pm-spec", ledger=False)
    for name, deps in zip(OLD_CHILDREN, ([], ["throttle-api"]), strict=True):
        child = source_dir(repo, name)
        write(child / "proposal.md", proposal())
        write(child / "use-cases.md", "# Use cases\n")
        write(child / "plan.md", f"# Plan: {name}\n\n## Done means\n1. x\n")
        for cap in CAPS:
            write(child / "requirements" / f"{cap}.md", requirements(cap))
        write_change_meta(child, {"parent": "throttle", "depends_on": deps})
        rev = mark_ready(child)
        state_mod.save(
            RunState(
                change=name,
                parent="throttle",
                intake_done=True,
                interview_done=True,
                steps={"consistency": StepOutcome(outcome="passed", revision=rev)},
                handoff=Handoff(revision=rev, committed=True, commit="abc"),
            ),
            child / ".pm-spec",
            ledger=False,
        )
    return json.loads((parent / ".pm-spec" / "state.json").read_text())


def change_files(cdir: Path) -> dict[str, object]:
    """Every file of a change dir, the run folder included, each
    `.change.yaml` as a mapping without the branch the move to the feature
    branch records (`branch.adopt`, before any step runs)."""
    out: dict[str, object] = {}
    for path in sorted(cdir.rglob("*")):
        if not path.is_file() or path.name == runlock.LOCK_FILE:
            continue
        rel = path.relative_to(cdir).as_posix()
        if path.name == ".change.yaml":
            out[rel] = {k: v for k, v in meta(path.parent).items() if k != "branch"}
        else:
            out[rel] = path.read_bytes()
    return out


def test_S16_the_parent_of_an_old_split_points_at_its_children(repo, model, capsys):
    two(model)
    saved = old_split(repo)
    names = ("throttle", *OLD_CHILDREN)
    before = {n: change_files(source_dir(repo, n)) for n in names}

    assert run(repo, *RESUME) == 0
    out = capsys.readouterr().out
    assert model.calls == []
    assert "specs live in the children" in out and "Nothing was changed" in out
    assert "  resume throttle-api\n  resume throttle-ui" in out
    assert "SPEC:" not in out

    # As the run that never stopped left it: every file, the children's run
    # states too, but the parent's state, which gains the stages skipped.
    after = {n: change_files(change_dir(repo, n)) for n in names}
    state_rel = ".pm-spec/state.json"
    for n in names:
        theirs, mine = dict(before[n]), dict(after[n])
        if n == "throttle":
            theirs.pop(state_rel), mine.pop(state_rel)
        assert mine == theirs, n
    now = state(change_dir(repo))
    assert now["steps"] == {
        name: {**now["steps"][name], "outcome": "skipped", "reason": stages.OLD_SPLIT}
        for name in stages.STAGES
    }
    assert {k: v for k, v in now.items() if k not in ("steps", "budget")} == {
        k: v for k, v in saved.items() if k not in ("steps", "budget")
    }
    # Only the move to the branch was committed, and nothing was pushed by the run.
    wt = worktree(repo)
    subjects = git(wt, "log", "--format=%s", "origin/main..HEAD").splitlines()
    assert subjects and all(s.endswith("moved to ENG-1-throttle") for s in subjects)

    # Resumed again: nothing at all changes.
    whole = run_dir_files(change_dir(repo).parent)
    head = git(wt, "rev-parse", "HEAD")
    assert run(repo, *RESUME) == 0
    assert run_dir_files(change_dir(repo).parent) == whole
    assert git(wt, "rev-parse", "HEAD") == head and model.calls == []
