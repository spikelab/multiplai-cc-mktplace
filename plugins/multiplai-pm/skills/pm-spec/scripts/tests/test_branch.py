"""The change's feature branch: pm-spec writes the specs in its own worktree
for `<TICKET>-<change>` and pushes it (`pm_spec_pipeline.branch`). Each test
runs against a real bare `origin`; the user's checkout is left as it was."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from conftest import (
    TWO,
    change_dir,
    git,
    meta,
    origin,
    ready_line,
    run,
    source_dir,
    state,
    worktree,
    write,
)
from multiplai_core.specs import is_ready, set_spec_issues
from test_revise import ISSUES, resolved


def tree(repo: Path) -> dict[str, bytes]:
    """Every file in the checkout but `.git`, with its bytes."""
    return {
        p.relative_to(repo).as_posix(): p.read_bytes()
        for p in sorted(repo.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(repo).parts
    }


def on_origin(repo: Path, rev: str, path: str) -> str:
    return git(origin(repo), "show", f"{rev}:{path}")


def clone(repo: Path, at: Path, branch: str = "ENG-1-throttle") -> Path:
    """Another person's clone of origin, on *branch*."""
    git(repo.parent, "clone", "-q", "-b", branch, str(origin(repo)), str(at))
    return at


def test_new_puts_the_specs_on_the_ticket_branch_from_origins_main_and_pushes_it(
    repo, model, capsys
):
    """T1, and T10's first rule: ticket ENG-42 makes `ENG-42-throttle`."""
    before, head = tree(repo), git(repo, "rev-parse", "HEAD")
    assert run(repo, "new", "throttle", "--auto", "--ticket-ref", "ENG-42") == 0
    out = capsys.readouterr().out
    wt = worktree(repo)
    cdir = change_dir(repo)
    # Written only in pm-spec's worktree, at a detached HEAD cut from
    # origin/main (no local branch, so a build can check the branch out).
    assert wt == worktree(repo, "ENG-42-throttle")
    assert tree(repo) == before
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert git(repo, "status", "--porcelain") == ""
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert git(repo, "branch", "--list", "ENG-42-throttle") == ""
    assert git(wt, "merge-base", "--is-ancestor", head, "HEAD") == ""
    assert meta(cdir)["branch"] == "ENG-42-throttle"
    assert meta(cdir)["ticket_refs"]["epic"] == "ENG-42"
    assert is_ready(cdir) == (True, "")
    # Pushed: origin's ENG-42-throttle is the worktree's HEAD and holds the ready record.
    assert git(origin(repo), "rev-parse", "ENG-42-throttle") == git(
        wt, "rev-parse", "HEAD"
    )
    pushed = yaml.safe_load(
        on_origin(repo, "ENG-42-throttle", "specs/changes/throttle/.change.yaml")
    )
    assert pushed["spec_status"] == "ready" and pushed["branch"] == "ENG-42-throttle"
    assert git(origin(repo), "rev-parse", "main") == head
    assert state(cdir)["handoff"]["pushed"] is True
    lines = out.splitlines()
    ready = lines.index(ready_line(out))
    assert lines[ready + 1] == "SPEC:BRANCH:ENG-42-throttle"


def test_branch_names_the_feature_branch(repo, model, capsys):
    assert (
        run(repo, "new", "throttle", "--auto", "--branch", "ENG-1-login-timeout") == 0
    )
    assert "SPEC:BRANCH:ENG-1-login-timeout" in capsys.readouterr().out
    cdir = worktree(repo, "ENG-1-login-timeout") / "specs" / "changes" / "throttle"
    assert meta(cdir)["branch"] == "ENG-1-login-timeout"
    assert git(origin(repo), "rev-parse", "ENG-1-login-timeout") == git(
        worktree(repo, "ENG-1-login-timeout"), "rev-parse", "HEAD"
    )
    assert not worktree(repo, "ENG-1-throttle").exists()


def _nothing_written(repo, before, model):
    assert tree(repo) == before and model.calls == []
    assert not (repo.parent / ".worktrees").exists()
    assert git(origin(repo), "for-each-ref", "--format=%(refname)", "refs/heads") == (
        "refs/heads/main"
    )


def test_a_branch_not_starting_with_the_ticket_is_refused(repo, model, capsys):
    """T10: `--branch` must still start with `<TICKET>-`."""
    before = tree(repo)
    assert run(repo, "new", "throttle", "--auto", "--branch", "fix/login-timeout") == 2
    out = capsys.readouterr().out
    assert (
        "--branch 'fix/login-timeout' does not start with the ticket id (ENG-1-)" in out
    )
    _nothing_written(repo, before, model)


@pytest.mark.parametrize("ticket, why", [("ENG[42]", "'['"), ("ENG 42", "' '")])
def test_a_ticket_id_git_refuses_in_a_branch_name_is_refused_with_the_reason(
    repo, model, capsys, ticket, why
):
    """T10: git rejects `[` and a space in a ref name."""
    before = tree(repo)
    assert run(repo, "new", "throttle", "--auto", "--ticket-ref", ticket) == 2
    out = capsys.readouterr().out
    assert f"the ticket id {ticket!r} cannot start the branch name" in out
    assert f"it holds {why}, which git refuses in a branch name" in out
    _nothing_written(repo, before, model)


def test_no_ticket_refuses_the_handoff_naming_ticket_ref(repo, model, capsys):
    """T11: a run from a prompt only, with no `--ticket-ref`: refused before
    any model call, nothing committed or pushed."""
    from pm_spec_pipeline.__main__ import main

    before = tree(repo)
    assert main(["new", "throttle", "--project-dir", str(repo), "--auto"]) == 2
    out = capsys.readouterr().out
    assert "throttle has no ticket ref" in out and "pass --ticket-ref" in out
    assert "SPEC:READY" not in out
    _nothing_written(repo, before, model)


def test_a_childs_tickets_are_its_own_then_its_parents(tmp_path):
    """T10: a child with no ticket of its own takes its nearest parent's; one
    with its own puts it first."""
    from pm_spec_pipeline import branch

    changes = tmp_path / "specs" / "changes"
    write(
        changes / "auth" / ".change.yaml",
        yaml.safe_dump({"ticket_refs": {"epic": "ENG-40"}}),
    )
    write(changes / "auth-a" / ".change.yaml", yaml.safe_dump({"parent": "auth"}))
    write(
        changes / "auth-b" / ".change.yaml",
        yaml.safe_dump({"parent": "auth", "ticket_refs": {"epic": "ENG-41"}}),
    )
    write(changes / "lone" / ".change.yaml", "schema: spec-driven\n")
    assert branch.tickets(changes / "auth-a") == ["ENG-40"]
    assert branch.tickets(changes / "auth-b") == ["ENG-41", "ENG-40"]
    assert branch.tickets(changes / "lone") == []
    assert branch.name("ENG-40", "auth") == "ENG-40-auth"


def test_a_branch_origin_has_already_is_started_from_origin(repo, model, tmp_path):
    other = clone(repo, tmp_path / "other", "main")
    git(other, "checkout", "-q", "-b", "ENG-1-throttle")
    write(other / "notes.md", "on the branch already\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "notes")
    git(other, "push", "-q", "origin", "ENG-1-throttle")
    assert run(repo, "new", "throttle", "--auto") == 0
    wt = worktree(repo)
    assert (wt / "notes.md").read_text() == "on the branch already\n"
    assert git(origin(repo), "rev-parse", "ENG-1-throttle") == git(
        wt, "rev-parse", "HEAD"
    )


def test_a_second_run_reuses_the_worktree_and_fast_forwards_it(repo, model, tmp_path):
    """T2, first half."""
    assert run(repo, "new", "throttle", "--auto") == 0
    wt = worktree(repo)
    other = clone(repo, tmp_path / "other")
    write(other / "notes.md", "pushed by someone else\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "notes")
    git(other, "push", "-q", "origin", "ENG-1-throttle")
    theirs = git(other, "rev-parse", "HEAD")
    calls = len(model.calls)
    assert run(repo, "resume", "throttle") == 0
    assert len(model.calls) == calls  # handed off already: nothing to do
    assert git(wt, "rev-parse", "HEAD") == theirs
    assert (wt / "notes.md").is_file()
    assert sorted(p.name for p in (repo.parent / ".worktrees").iterdir()) == [
        "pm-spec-ENG-1-throttle"
    ]


def test_a_worktree_diverged_from_origin_is_refused_naming_both_shas(
    repo, model, tmp_path, capsys
):
    """T2, second half."""
    assert run(repo, "new", "throttle", "--auto") == 0
    wt = worktree(repo)
    write(wt / "mine.md", "local\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "mine")
    mine = git(wt, "rev-parse", "HEAD")
    other = clone(repo, tmp_path / "other")
    write(other / "theirs.md", "remote\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "theirs")
    git(other, "push", "-q", "origin", "ENG-1-throttle")
    theirs = git(other, "rev-parse", "HEAD")
    capsys.readouterr()
    calls = len(model.calls)
    assert run(repo, "resume", "throttle") == 2
    out = capsys.readouterr().out
    assert "diverged" in out and mine in out and theirs in out
    assert git(wt, "rev-parse", "HEAD") == mine
    assert len(model.calls) == calls


def test_a_failed_push_keeps_the_commit_and_resume_retries_only_the_push(
    repo, model, capsys
):
    """T3."""
    hook = origin(repo) / "hooks" / "pre-receive"
    write(hook, "#!/bin/sh\necho refused by the test >&2\nexit 1\n")
    hook.chmod(0o755)
    assert run(repo, "new", "throttle", "--auto") == 1
    out = capsys.readouterr().out
    cdir, wt = change_dir(repo), worktree(repo)
    assert "git push origin HEAD:refs/heads/ENG-1-throttle` failed" in out
    assert "SPEC:READY" not in out
    assert is_ready(cdir) == (True, "")
    handoff = state(cdir)["handoff"]
    assert handoff["committed"] is True and handoff["pushed"] is False
    assert git(wt, "rev-parse", "HEAD") == handoff["commit"]
    assert git(origin(repo), "branch", "--list", "ENG-1-throttle") == ""

    hook.unlink()
    calls = len(model.calls)
    assert run(repo, "resume", "throttle") == 0
    out = capsys.readouterr().out
    assert len(model.calls) == calls
    assert git(wt, "rev-parse", "HEAD") == handoff["commit"]  # no second commit
    assert git(origin(repo), "rev-parse", "ENG-1-throttle") == handoff["commit"]
    assert ready_line(out) and "SPEC:BRANCH:ENG-1-throttle" in out
    assert state(cdir)["handoff"]["pushed"] is True


def test_a_repository_without_origin_is_refused(tmp_path, model, capsys):
    path = tmp_path / "lone"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    write(path / "README.md", "x\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "i")
    assert run(path, "new", "throttle", "--auto") == 2
    assert "has no origin remote" in capsys.readouterr().out
    assert model.calls == []
    assert not (tmp_path / ".worktrees").exists()


def test_a_change_handed_off_before_branches_is_moved_onto_its_branch(
    repo, model, capsys
):
    """`pm-spec resume <name>` on a change ready in the user's checkout, on
    main, recording no branch: copied into the worktree, committed and pushed;
    the original stays."""
    assert run(repo, "new", "throttle", "--auto") == 0
    wt = worktree(repo)
    # Make it the old handoff: the same files on the user's main, no branch.
    shutil.copytree(change_dir(repo), source_dir(repo))
    data = yaml.safe_load((source_dir(repo) / ".change.yaml").read_text())
    del data["branch"]
    (source_dir(repo) / ".change.yaml").write_text(yaml.safe_dump(data))
    saved = source_dir(repo) / ".pm-spec" / "state.json"
    old = json.loads(saved.read_text())
    del old["handoff"]["pushed"]
    saved.write_text(json.dumps(old))
    git(repo, "add", "specs")
    git(repo, "commit", "-q", "-m", "specs(throttle): ready for build")
    git(repo, "worktree", "remove", "--force", str(wt))
    git(repo, "push", "-q", "origin", "--delete", "ENG-1-throttle")
    assert is_ready(source_dir(repo)) == (True, "")
    before = tree(repo)
    capsys.readouterr()
    calls = len(model.calls)

    assert run(repo, "resume", "throttle") == 0

    out = capsys.readouterr().out
    assert len(model.calls) == calls
    assert tree(repo) == before  # the original is never deleted
    assert meta(change_dir(repo))["branch"] == "ENG-1-throttle"
    assert is_ready(change_dir(repo)) == (True, "")
    assert (
        git(wt, "log", "-1", "--format=%s")
        == "specs(throttle): moved to ENG-1-throttle"
    )
    assert git(origin(repo), "rev-parse", "ENG-1-throttle") == git(
        wt, "rev-parse", "HEAD"
    )
    assert f"SPEC:READY:{change_dir(repo)}:" in out
    assert "SPEC:BRANCH:ENG-1-throttle" in out


@pytest.mark.parametrize("on", ["ENG-1-throttle", "ENG-1-throttle-a"])
def test_revise_in_a_builds_worktree_commits_there_and_pushes_nothing(
    repo, model, capsys, on
):
    """buildme's advice, `--project-dir <build worktree>`: the revision is
    committed in the build's worktree, on the feature branch itself (a single
    change's build) or on a child's own branch, and reaches the feature
    branch with that build's push or through its pull request."""
    assert run(repo, "new", "throttle", "--auto") == 0
    pushed = git(origin(repo), "rev-parse", "ENG-1-throttle")
    build = repo.parent / ".worktrees" / "buildme-throttle"
    git(
        repo,
        "worktree",
        "add",
        "-q",
        "-b",
        on,
        str(build),
        "origin/ENG-1-throttle",
    )
    cdir = build / "specs" / "changes" / "throttle"
    write(cdir / "spec-issues.md", ISSUES)
    set_spec_issues(cdir, cdir / "spec-issues.md")
    model["consistency"] = resolved()
    capsys.readouterr()

    assert run(build, "revise", "throttle") == 0

    out = capsys.readouterr().out
    assert f"SPEC:READY:{cdir}:" in out
    assert (
        "reaches ENG-1-throttle with that build's push, or through its pull request"
        in out
    )
    assert git(build, "symbolic-ref", "--short", "HEAD") == on
    assert git(build, "log", "-1", "--format=%s").startswith("specs(throttle)")
    assert git(build, "status", "--porcelain", "--", "specs") == ""
    assert git(origin(repo), "rev-parse", "ENG-1-throttle") == pushed
    assert git(origin(repo), "branch", "--list", "ENG-1-throttle-a") == ""


def test_revise_in_a_builds_worktree_commits_none_of_buildmes_files(
    repo, model, capsys
):
    """A revise on 2026-10-23 committed the component's `.build-state.json`
    and `.board.json` with the specs: pm-spec commits only its own files, and
    buildme's stay as buildme left them."""
    model["components"] = TWO
    assert run(repo, "new", "throttle", "--auto") == 0
    build = repo.parent / ".worktrees" / "buildme-throttle-api"
    git(
        repo,
        "worktree",
        "add",
        "-q",
        "-b",
        "ENG-1-throttle-api",
        str(build),
        "origin/ENG-1-throttle",
    )
    cdir = build / "specs" / "changes" / "throttle"
    comp = cdir / "components" / "api"
    buildmes = {
        comp / ".build-state.json": "{}\n",
        comp / ".board.json": "{}\n",
        comp / "design.md": "# Design\n",
        comp / "build-progress.md": "progress\n",
    }
    for path, text in buildmes.items():
        write(path, text)
    write(
        comp / "spec-issues.md",
        "# Spec issues\n\n## Vague\n\nThe wait is not stated.\n",
    )
    set_spec_issues(comp, comp / "spec-issues.md")
    model["consistency"] = resolved()
    capsys.readouterr()

    assert run(build, "revise", "throttle") == 0

    committed = git(build, "show", "--name-only", "--format=", "HEAD").split()
    assert committed, "the revise committed nothing"
    rel = "specs/changes/throttle/components/api/"
    assert not [p for p in committed if Path(p).name in {p.name for p in buildmes}]
    assert f"{rel}specs.md" in committed and f"{rel}.change.yaml" in committed
    assert any(p.startswith(f"{rel}spec-issues.resolved-") for p in committed)
    untracked = git(build, "status", "--porcelain", "-uall", "--", "specs").splitlines()
    assert sorted(line.split()[-1] for line in untracked) == sorted(
        str(p.relative_to(build)) for p in buildmes
    )
    assert all(p.read_text() == text for p, text in buildmes.items())


def test_revise_in_a_checkout_that_is_not_a_build_works_in_pm_specs_worktree(
    repo, model, capsys, tmp_path
):
    """A checkout on a branch of its own whose copy records the feature
    branch (not a build's worktree) is left alone: the revision is written in
    pm-spec's worktree and pushed."""
    assert run(repo, "new", "throttle", "--auto") == 0
    mine = clone(repo, tmp_path / "elsewhere" / "mine")
    git(mine, "checkout", "-q", "-b", "my-notes")
    cdir = mine / "specs" / "changes" / "throttle"
    write(cdir / "spec-issues.md", ISSUES)
    set_spec_issues(cdir, cdir / "spec-issues.md")
    git(mine, "add", "-A")
    git(mine, "commit", "-q", "-m", "issues")
    before, head = tree(mine), git(mine, "rev-parse", "HEAD")
    model["consistency"] = resolved()
    capsys.readouterr()

    # pm-spec cuts its own worktree from origin/ENG-1-throttle, whose copy
    # has no issues: the clone's were never read.
    assert run(mine, "revise", "throttle") == 2
    assert "nothing to revise" in capsys.readouterr().out
    assert (tmp_path / "elsewhere" / ".worktrees" / "pm-spec-ENG-1-throttle").is_dir()
    assert tree(mine) == before and git(mine, "rev-parse", "HEAD") == head
    assert git(mine, "symbolic-ref", "--short", "HEAD") == "my-notes"


def test_resume_with_a_ticket_ref_commits_and_pushes_it(repo, model, capsys):
    """A change handed off under its parent's ticket gets its own on
    `resume --ticket-ref`: the `.change.yaml` recording it is committed and
    pushed, not left in pm-spec's worktree."""
    assert run(repo, "new", "throttle", "--auto", "--ticket-ref", "ENG-42") == 0
    wt = worktree(repo, "ENG-42-throttle")
    cdir = change_dir(repo)
    # As pm-spec's split hands a child off: no ticket of its own, the parent's.
    data = meta(cdir)
    data.pop("ticket_refs")
    data["parent"] = "auth"
    write(cdir / ".change.yaml", yaml.safe_dump(data))
    write(
        cdir.parent / "auth" / ".change.yaml",
        yaml.safe_dump({"ticket_refs": {"epic": "ENG-42", "children": ["throttle"]}}),
    )
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "specs: a child of auth")
    git(wt, "push", "-q", "origin", "HEAD:refs/heads/ENG-42-throttle")
    capsys.readouterr()

    assert run(repo, "resume", "throttle", "--ticket-ref", "ENG-45") == 0

    out = capsys.readouterr().out
    assert "SPEC:BRANCH:ENG-42-throttle" in out
    assert git(wt, "status", "--porcelain") == ""
    pushed = yaml.safe_load(
        on_origin(repo, "ENG-42-throttle", "specs/changes/throttle/.change.yaml")
    )
    assert pushed["ticket_refs"]["epic"] == "ENG-45"
