"""One test per row S1-S9 of the plan's "Stop and resume states" table.

Each test stops the run at its row, resumes it, and compares the change dir
with a run of the same inputs that never stopped (`baseline`), except where
the row's "Resume does" column says otherwise; those parts assert what the
column says instead. A kill is `Killed`, a BaseException raised from inside
the call or the write the row names, so no `except Exception` in the code
can swallow it."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import (
    FakeModel,
    Killed,
    change_dir,
    committed,
    git,
    meta,
    ready_line,
    requirements,
    run,
    snapshot,
    state,
    write,
)
from multiplai_core.specs import is_ready, set_spec_issues, spec_revision
from pm_spec_pipeline import git_ops, handoff, intake, prototype, publish, runlock, sdk
from test_prototype import NOTES_FINDINGS, agent, ui_proposal

TICKET = "# Throttle the API\n\nClients flood the server.\n"


def killer(then=None):
    """A handler that kills the run on its first call and answers like *then* after."""
    def kill(prompt, label, kw):
        raise Killed(label)
    return [kill, then] if then is not None else kill


def new_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    write(path / "README.md", "# proj\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


def baseline(tmp_path, monkeypatch, configure, *commands) -> tuple[Path, FakeModel]:
    """The same inputs in a fresh repository with a fresh fake model, never stopped.
    The test's own model is put back afterwards."""
    previous = sdk.run_agent
    fake = FakeModel()
    configure(fake)
    monkeypatch.setattr(sdk, "run_agent", fake)
    other = new_repo(tmp_path / f"baseline-{len(list(tmp_path.glob('baseline-*')))}")
    try:
        for argv, code in commands:
            argv = [str(a).replace("{tmp}", str(tmp_path)) for a in argv]
            assert run(other, *argv) == code, argv
    finally:
        monkeypatch.setattr(sdk, "run_agent", previous)
    return other, fake


def run_dir_files(cdir: Path) -> dict[str, bytes]:
    """Everything in the change dir, the run folder included, but the lock's holder note."""
    return {p.relative_to(cdir).as_posix(): p.read_bytes() for p in sorted(cdir.rglob("*"))
            if p.is_file() and p.name != runlock.LOCK_FILE}


# --- S1 ---------------------------------------------------------------------------

def test_S1_ticket_interview_waits_then_continues_with_the_replies(repo, model, tmp_path, monkeypatch, capsys):
    ticket = write(tmp_path / "ticket.md", TICKET)
    replies = write(tmp_path / "replies.md", "alice: sixty a minute, nobody exempt.\n")
    new = ["new", "throttle", "--interview-mode", "ticket", "--ticket-file", str(ticket), "--ticket-ref", "EPIC-9"]

    assert run(repo, *new) == 3
    out = capsys.readouterr().out
    cdir = change_dir(repo)
    questions = cdir / "interview-questions.md"
    assert f"INTERVIEW:WAITING:{questions}" in out
    assert state(cdir)["interview_waiting"] is True
    assert not (cdir / "interview.md").exists()

    # Without answers: the marker again, exit 3, nothing written, no call.
    before, calls = run_dir_files(cdir), len(model.calls)
    assert run(repo, "resume", "throttle") == 3
    assert f"INTERVIEW:WAITING:{questions}" in capsys.readouterr().out
    assert run_dir_files(cdir) == before
    assert len(model.calls) == calls

    # With answers: interview.md, and on to SPEC:READY.
    assert run(repo, "resume", "throttle", "--interview-replies", str(replies)) == 0
    assert "sixty a minute" in (cdir / "interview.md").read_text()
    assert model.calls_to("interview:questions") == 1

    other, _ = baseline(tmp_path, monkeypatch, lambda m: None,
                        (new, 3), (["resume", "throttle", "--interview-replies", str(replies)], 0))
    assert snapshot(cdir) == snapshot(change_dir(other))


# --- S2 ---------------------------------------------------------------------------

def test_S2_artifacts_killed_part_way_keep_what_is_finished(repo, model, tmp_path, monkeypatch):
    from conftest import default_requirements
    regen = lambda prompt, label, kw: default_requirements(prompt, label)
    model["spec:requirements:retry-policy"] = killer(regen)
    with pytest.raises(Killed):
        run(repo, "new", "throttle", "--auto")
    cdir = change_dir(repo)
    records = state(cdir)["artifacts"]
    assert all(records[k]["output"] for k in ("proposal", "use-cases", "requirements/rate-limit"))
    assert records["requirements/retry-policy"]["output"] is None  # started, not finished

    assert run(repo, "resume", "throttle") == 0
    assert [model.calls_to(p) for p in ("spec:proposal", "spec:use-cases", "spec:requirements:rate-limit",
                                        "spec:requirements:retry-policy")] == [1, 1, 1, 2]
    other, _ = baseline(tmp_path, monkeypatch, lambda m: None, (["new", "throttle", "--auto"], 0))
    assert snapshot(cdir) == snapshot(change_dir(other))

    # A finished artifact a person edited, with its inputs unchanged, is kept.
    second = new_repo(tmp_path / "second")
    model.count.clear()
    with pytest.raises(Killed):
        run(second, "new", "throttle", "--auto")
    edited = requirements("rate-limit", rule="the edited rule")
    write(change_dir(second) / "requirements" / "rate-limit.md", edited)
    assert run(second, "resume", "throttle") == 0
    assert (change_dir(second) / "requirements" / "rate-limit.md").read_text() == edited

    # A changed proposal regenerates the use cases and every requirements file.
    third = new_repo(tmp_path / "third")
    model.count.clear()
    with pytest.raises(Killed):
        run(third, "new", "throttle", "--auto")
    model.calls.clear()
    path = change_dir(third) / "proposal.md"
    path.write_text(path.read_text().replace("Clients flood the API server.", "Bots flood the API server."))
    assert run(third, "resume", "throttle") == 0
    assert "Bots flood" in path.read_text()  # the edit is the proposal now
    assert [model.calls_to(p) for p in ("spec:proposal", "spec:use-cases", "spec:requirements:rate-limit",
                                        "spec:requirements:retry-policy")] == [0, 1, 1, 1]
    assert "Bots flood" in model.prompts("spec:requirements:rate-limit")[0]


# --- S3 ---------------------------------------------------------------------------

def test_S3_split_killed_before_the_child_dirs_exist(repo, model, tmp_path, monkeypatch, capsys):
    from test_epic_split import PLAIN_SPLIT
    model["epic_split"] = PLAIN_SPLIT
    real = intake.create_change
    made: list[Path] = []

    def create_change(change_dir, *, parent=None):
        if parent and made:
            raise Killed("between children")
        if parent:
            made.append(change_dir)
        return real(change_dir, parent=parent)

    monkeypatch.setattr(intake, "create_change", create_change)
    with pytest.raises(Killed):
        run(repo, "new", "throttle", "--auto")
    monkeypatch.setattr(intake, "create_change", real)
    parent, api, ui = (change_dir(repo, n) for n in ("throttle", "throttle-api", "throttle-ui"))
    assert state(parent)["split"]["children"][1]["id"] == "ui"
    assert (api / ".pm-spec" / "state.json").is_file() and not (ui / ".pm-spec").exists()
    # Killed before the list: the parent names no child yet, and nothing is committed.
    assert "children" not in meta(parent)
    assert git(repo, "log", "-1", "--format=%s") == "initial"
    capsys.readouterr()

    assert run(repo, "resume", "throttle") == 0
    out = capsys.readouterr().out
    assert model.calls_to("epic_split") == 1  # never split again
    assert [line for line in out.splitlines() if line.startswith("SPEC:")] == \
        [f"SPEC:SPLIT:{parent}", f"SPEC:CHILD:{api}", f"SPEC:CHILD:{ui}"]

    other, _ = baseline(tmp_path, monkeypatch, lambda m: m.__setitem__("epic_split", PLAIN_SPLIT),
                        (["new", "throttle", "--auto"], 0))
    for name in ("throttle", "throttle-api", "throttle-ui"):
        assert snapshot(change_dir(repo, name)) == snapshot(change_dir(other, name))
        mine, theirs = state(change_dir(repo, name)), state(change_dir(other, name))
        assert {k: mine[k] for k in ("options", "parent", "split")} == \
            {k: theirs[k] for k in ("options", "parent", "split")}
    # The child killed before its dir existed gets its dependencies as the
    # child written before the kill did.
    assert meta(api)["depends_on"] == [] and meta(ui)["depends_on"] == ["throttle-api"]
    # The resume writes the parent's list and commits the parent once, as the
    # run that never stopped did.
    assert meta(parent)["children"] == ["throttle-api", "throttle-ui"]
    assert git(repo, "log", "--format=%s").splitlines() == ["specs(throttle): children updated", "initial"]
    assert git(other, "log", "--format=%s").splitlines() == ["specs(throttle): children updated", "initial"]

    # Unchanged inputs: no second split, and no record is written or committed again.
    before = {p: (p / ".change.yaml").stat().st_mtime_ns for p in (parent, api, ui)}
    head = git(repo, "rev-parse", "HEAD")
    assert run(repo, "resume", "throttle") == 0
    assert model.calls_to("epic_split") == 1
    assert {p: (p / ".change.yaml").stat().st_mtime_ns for p in (parent, api, ui)} == before
    assert git(repo, "rev-parse", "HEAD") == head
    # Changed parent inputs: split again. The children already created keep
    # their files, and their depends_on follows the new answer.
    from test_epic_split import child
    model["epic_split"] = {"single": False, "reason": "UI first.", "children": [child("api", deps=["ui"]), child("ui")]}
    plan_before = (api / "plan.md").read_text()
    interview_md = parent / "interview.md"
    interview_md.write_text(interview_md.read_text() + "\nAlso throttle websockets.\n")
    assert run(repo, "resume", "throttle") == 0
    assert model.calls_to("epic_split") == 2
    assert meta(api)["depends_on"] == ["throttle-ui"] and meta(ui)["depends_on"] == []
    assert (api / "plan.md").read_text() == plan_before
    # The parent's list follows the new wave order and is committed with the
    # changed interview; the children, never handed off, are not committed.
    assert meta(parent)["children"] == ["throttle-ui", "throttle-api"]
    assert git(repo, "log", "--format=%s", f"{head}..HEAD").splitlines() == ["specs(throttle): children updated"]
    assert git(repo, "status", "--porcelain", "--", "specs/changes/throttle") == ""


# --- S4 ---------------------------------------------------------------------------

def _ui(m):
    m["spec:proposal"] = lambda *a: ui_proposal()
    m["prototype"] = agent(NOTES_FINDINGS)


def test_S4_prototype_killed_in_the_agent_or_its_rewrite(repo, model, tmp_path, monkeypatch, capsys):
    other, _ = baseline(tmp_path, monkeypatch, _ui, (["new", "throttle", "--auto"], 0))
    expected = snapshot(change_dir(other))

    # (a) Killed during the agent: the agent runs again, then its one rewrite.
    _ui(model)
    model["prototype"] = killer(agent(NOTES_FINDINGS))
    with pytest.raises(Killed):
        run(repo, "new", "throttle", "--auto")
    assert run(repo, "resume", "throttle") == 0
    assert model.calls_to("prototype:rewrite:proposal") == 1 and model.count["prototype"] == 2
    assert snapshot(change_dir(repo)) == expected

    # (b) Killed during the rewrite, before it publishes: the agent runs again,
    # with no second rewrite, so its findings stop the run (exit 4).
    b = new_repo(tmp_path / "b")
    fake = FakeModel()
    _ui(fake)
    fake["prototype:rewrite:proposal"] = killer(lambda *a: ui_proposal())
    monkeypatch.setattr(sdk, "run_agent", fake)
    with pytest.raises(Killed):
        run(b, "new", "throttle", "--auto")
    cdir = change_dir(b)
    files_before = {k: v for k, v in snapshot(cdir).items() if not k.startswith("prototype/")}
    assert state(cdir)["steps"]["prototype"]["repair_attempts"] == 1
    capsys.readouterr()
    assert run(b, "resume", "throttle") == 4
    assert fake.count["prototype"] == 2
    assert fake.calls_to("prototype:rewrite") == 1
    assert "PT-F1" in capsys.readouterr().out
    assert {k: v for k, v in snapshot(cdir).items() if not k.startswith("prototype/")} == files_before

    # (c) Killed after the rewrite published: on to the consistency check,
    # with no prototype or rewrite call.
    c = new_repo(tmp_path / "c")
    fake = FakeModel()
    _ui(fake)
    monkeypatch.setattr(sdk, "run_agent", fake)
    real_publish = prototype.publish

    def publish_then_die(run_, step, files):
        real_publish(run_, step, files)
        raise Killed("after publish")

    monkeypatch.setattr(prototype, "publish", publish_then_die)
    with pytest.raises(Killed):
        run(c, "new", "throttle", "--auto")
    monkeypatch.setattr(prototype, "publish", real_publish)
    calls = len(fake.calls)
    assert run(c, "resume", "throttle") == 0
    assert [label for label, _, _ in fake.calls[calls:]] == ["consistency"]
    assert snapshot(change_dir(c)) == expected

    # (d) Killed while the published files were being written: the run that
    # finds the journal finishes the publication, then goes on.
    d = new_repo(tmp_path / "d")
    fake = FakeModel()
    _ui(fake)
    monkeypatch.setattr(sdk, "run_agent", fake)
    real_write = publish.atomic_write

    def write_then_die(path, text):
        journal = change_dir(d) / ".pm-spec" / publish.JOURNAL
        if journal.is_file() and "requirements" in Path(path).parts:
            raise Killed("mid publication")
        real_write(path, text)

    monkeypatch.setattr(publish, "atomic_write", write_then_die)
    with pytest.raises(Killed):
        run(d, "new", "throttle", "--auto")
    monkeypatch.setattr(publish, "atomic_write", real_write)
    assert (change_dir(d) / ".pm-spec" / publish.JOURNAL).is_file()
    calls = len(fake.calls)
    assert run(d, "resume", "throttle") == 0
    assert [label for label, _, _ in fake.calls[calls:]] == ["consistency"]
    assert not (change_dir(d) / ".pm-spec" / publish.JOURNAL).exists()
    assert snapshot(change_dir(d)) == expected


# --- S5 ---------------------------------------------------------------------------

def test_S5_consistency_blocked_after_its_rewrite_and_recheck(repo, model, capsys):
    from test_consistency import finding, recheck
    model["consistency"] = [{"findings": [finding()]}, recheck("still_present"),
                            {"findings": [finding()]}, recheck("resolved")]
    assert run(repo, "new", "throttle", "--auto") == 4
    cdir = change_dir(repo)
    step = state(cdir)["steps"]["consistency"]
    assert [(f["finding_id"], f["status"]) for f in step["repair_findings"]] == [("CC-F1", "still_present")]
    assert meta(cdir)["spec_status"] == "draft"
    capsys.readouterr()

    # Files unchanged: refused with no model call, the findings printed. Twice.
    calls = len(model.calls)
    for _ in range(2):
        assert run(repo, "resume", "throttle") == 4
        out = capsys.readouterr().out
        assert "CC-F1 still_present" in out and f"SPEC:BLOCKED:{cdir}" in out
    assert len(model.calls) == calls

    # Files edited: one rewrite allowed again, then the check.
    path = cdir / "requirements" / "retry-policy.md"
    path.write_text(requirements("retry-policy", rule="a retry after sixty seconds"))
    assert run(repo, "resume", "throttle") == 0
    assert len(model.exactly("consistency")) == 4
    assert model.calls_to("consistency:rewrite") == 4  # two files, twice
    assert is_ready(cdir) == (True, "")


# --- S6 ---------------------------------------------------------------------------

def test_S6_ready_record_written_and_the_commit_failed(repo, model, tmp_path, monkeypatch, capsys):
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
    assert meta(cdir)["spec_status"] == "ready"
    assert meta(cdir)["spec_revision"] == spec_revision(cdir)
    assert state(cdir)["handoff"]["committed"] is False
    assert git(repo, "status", "--porcelain", "--", "specs") != ""
    capsys.readouterr()

    # The revision still matches: only the commit is retried.
    calls = len(model.calls)
    assert run(repo, "resume", "throttle") == 0
    assert len(model.calls) == calls
    assert ready_line(capsys.readouterr().out) == f"SPEC:READY:{cdir}:{spec_revision(cdir)}"
    assert git(repo, "status", "--porcelain", "--", "specs") == ""
    other, _ = baseline(tmp_path, monkeypatch, lambda m: None, (["new", "throttle", "--auto"], 0))
    assert snapshot(cdir) == snapshot(change_dir(other))
    assert committed(repo, cdir) == committed(other, change_dir(other))

    # The files changed since: back to draft, the check again, then ready.
    second = new_repo(tmp_path / "second")
    failing["left"] = 1
    assert run(second, "new", "throttle", "--auto") == 1
    cdir2 = change_dir(second)
    old = meta(cdir2)["spec_revision"]
    write(cdir2 / "requirements" / "rate-limit.md", requirements("rate-limit", rule="fifty a minute"))
    checks = len(model.exactly("consistency"))
    assert run(second, "resume", "throttle") == 0
    assert len(model.exactly("consistency")) == checks + 1
    assert meta(cdir2)["spec_revision"] == spec_revision(cdir2) != old
    assert is_ready(cdir2) == (True, "")
    assert git(second, "status", "--porcelain", "--", "specs") == ""


# --- S7 ---------------------------------------------------------------------------

def test_S7_an_edit_after_ready_is_checked_and_marked_ready_again(repo, model, capsys):
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    old = meta(cdir)["spec_revision"]
    write(cdir / "requirements" / "retry-policy.md", requirements("retry-policy", rule="a sixty second wait"))
    assert is_ready(cdir)[0] is False
    capsys.readouterr()
    checks = len(model.exactly("consistency"))
    assert run(repo, "resume", "throttle") == 0
    assert len(model.exactly("consistency")) == checks + 1
    assert model.calls_to("spec:") == 4  # nothing regenerated: the edit is kept
    assert "a sixty second wait" in (cdir / "requirements" / "retry-policy.md").read_text()
    new = spec_revision(cdir)
    assert new != old and ready_line(capsys.readouterr().out) == f"SPEC:READY:{cdir}:{new}"
    assert is_ready(cdir) == (True, "")
    assert git(repo, "status", "--porcelain", "--", "specs") == ""


# --- S8 ---------------------------------------------------------------------------

def test_S8_a_second_process_is_refused_with_the_holders_pid(repo, model, tmp_path, monkeypatch, capsys):
    model["spec:requirements:retry-policy"] = killer(requirements("retry-policy"))
    with pytest.raises(Killed):
        run(repo, "new", "throttle", "--auto")
    cdir = change_dir(repo)
    before, calls = run_dir_files(cdir), len(model.calls)
    capsys.readouterr()
    with runlock.hold(cdir / ".pm-spec"):
        assert run(repo, "resume", "throttle") == 5
    out = capsys.readouterr().out
    assert f"pid {os.getpid()}" in out
    assert len(model.calls) == calls and run_dir_files(cdir) == before

    # Once the holder is gone the run resumes as if nothing happened.
    assert run(repo, "resume", "throttle") == 0
    other, _ = baseline(tmp_path, monkeypatch, lambda m: None, (["new", "throttle", "--auto"], 0))
    assert snapshot(cdir) == snapshot(change_dir(other))


# --- S9 ---------------------------------------------------------------------------

ISSUES = "# Spec issues\n\n## Retry header\n\nrequirements/retry-policy.md lacks the Retry-After header.\n"


def _resolved(m):
    m["consistency"] = [{"findings": [], "prior_assessments": []},
                        {"findings": [], "prior_assessments": [{"finding_id": "RV-F1", "status": "resolved"}]}]


def _issues(repo):
    cdir = change_dir(repo)
    write(cdir / "spec-issues.md", ISSUES)
    set_spec_issues(cdir, cdir / "spec-issues.md")


def test_S9_revise_killed_after_the_rewrite_published(repo, model, tmp_path, monkeypatch, capsys):
    _resolved(model)
    assert run(repo, "new", "throttle", "--auto") == 0
    _issues(repo)
    real = publish.publish

    def publish_then_die(run_, step, files):
        real(run_, step, files)
        raise Killed("after publish")

    monkeypatch.setattr(publish, "publish", publish_then_die)
    with pytest.raises(Killed):
        run(repo, "revise", "throttle")
    monkeypatch.setattr(publish, "publish", real)
    cdir = change_dir(repo)
    step = state(cdir)["steps"]["revise"]
    assert step["rewrite_published"] is True and step["repair_findings"][0]["finding_id"] == "RV-F1"
    assert meta(cdir)["spec_issues"] == "spec-issues.md"

    # The recheck alone, against the listed issue; no second rewrite.
    rewrites, checks = model.calls_to("revise:rewrite"), len(model.exactly("consistency"))
    assert run(repo, "revise", "throttle") == 0
    assert model.calls_to("revise:rewrite") == rewrites == 1
    assert len(model.exactly("consistency")) == checks + 1
    assert "RV-F1" in model.exactly("consistency")[-1]
    assert meta(cdir)["spec_issues"] is None and is_ready(cdir) == (True, "")

    def setup(m):
        _resolved(m)
    other, _ = baseline(tmp_path, monkeypatch, setup, (["new", "throttle", "--auto"], 0))
    _issues(other)
    previous = sdk.run_agent
    fake = FakeModel()
    _resolved(fake)
    fake.count["consistency"] = 1  # the baseline's first check already ran on its own model
    monkeypatch.setattr(sdk, "run_agent", fake)
    assert run(other, "revise", "throttle") == 0
    monkeypatch.setattr(sdk, "run_agent", previous)
    assert snapshot(cdir) == snapshot(change_dir(other))
