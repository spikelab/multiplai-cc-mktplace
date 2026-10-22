"""The change's feature branch: pm-spec writes the specs in its own worktree of
`feat/<change>` and pushes it (`pm_spec_pipeline.branch`). Each test runs
against a real bare `origin`; the user's checkout is left as it was."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml
from conftest import (
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


def clone(repo: Path, at: Path, branch: str = "feat/throttle") -> Path:
    """Another person's clone of origin, on *branch*."""
    git(repo.parent, "clone", "-q", "-b", branch, str(origin(repo)), str(at))
    return at


def test_new_puts_the_specs_on_feat_change_from_origins_main_and_pushes_it(
    repo, model, capsys
):
    """T1."""
    before, head = tree(repo), git(repo, "rev-parse", "HEAD")
    assert run(repo, "new", "throttle", "--auto") == 0
    out = capsys.readouterr().out
    wt = worktree(repo)
    cdir = change_dir(repo)
    # Written only in pm-spec's worktree, on feat/throttle cut from origin/main.
    assert tree(repo) == before
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert git(repo, "status", "--porcelain") == ""
    assert git(wt, "symbolic-ref", "--short", "HEAD") == "feat/throttle"
    assert git(wt, "merge-base", "--is-ancestor", head, "HEAD") == ""
    assert meta(cdir)["branch"] == "feat/throttle"
    assert is_ready(cdir) == (True, "")
    # Pushed: origin's feat/throttle is the worktree's HEAD and holds the ready record.
    assert git(origin(repo), "rev-parse", "feat/throttle") == git(
        wt, "rev-parse", "HEAD"
    )
    pushed = yaml.safe_load(
        on_origin(repo, "feat/throttle", "specs/changes/throttle/.change.yaml")
    )
    assert pushed["spec_status"] == "ready" and pushed["branch"] == "feat/throttle"
    assert git(origin(repo), "rev-parse", "main") == head
    assert state(cdir)["handoff"]["pushed"] is True
    lines = out.splitlines()
    ready = lines.index(ready_line(out))
    assert lines[ready + 1] == "SPEC:BRANCH:feat/throttle"


def test_branch_names_the_feature_branch(repo, model, capsys):
    assert run(repo, "new", "throttle", "--auto", "--branch", "feat/limits") == 0
    assert "SPEC:BRANCH:feat/limits" in capsys.readouterr().out
    cdir = worktree(repo, "feat/limits") / "specs" / "changes" / "throttle"
    assert meta(cdir)["branch"] == "feat/limits"
    assert git(origin(repo), "rev-parse", "feat/limits") == git(
        worktree(repo, "feat/limits"), "rev-parse", "HEAD"
    )
    assert not worktree(repo).exists()


def test_a_branch_origin_has_already_is_started_from_origin(repo, model, tmp_path):
    other = clone(repo, tmp_path / "other", "main")
    git(other, "checkout", "-q", "-b", "feat/throttle")
    write(other / "notes.md", "on the branch already\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "notes")
    git(other, "push", "-q", "origin", "feat/throttle")
    assert run(repo, "new", "throttle", "--auto") == 0
    wt = worktree(repo)
    assert (wt / "notes.md").read_text() == "on the branch already\n"
    assert git(origin(repo), "rev-parse", "feat/throttle") == git(
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
    git(other, "push", "-q", "origin", "feat/throttle")
    theirs = git(other, "rev-parse", "HEAD")
    calls = len(model.calls)
    assert run(repo, "resume", "throttle") == 0
    assert len(model.calls) == calls  # handed off already: nothing to do
    assert git(wt, "rev-parse", "HEAD") == theirs
    assert (wt / "notes.md").is_file()
    assert sorted(p.name for p in (repo.parent / ".worktrees").iterdir()) == [
        "pm-spec-feat-throttle"
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
    git(other, "push", "-q", "origin", "feat/throttle")
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
    assert "git push origin feat/throttle` failed" in out and "SPEC:READY" not in out
    assert is_ready(cdir) == (True, "")
    handoff = state(cdir)["handoff"]
    assert handoff["committed"] is True and handoff["pushed"] is False
    assert git(wt, "rev-parse", "HEAD") == handoff["commit"]
    assert git(origin(repo), "branch", "--list", "feat/throttle") == ""

    hook.unlink()
    calls = len(model.calls)
    assert run(repo, "resume", "throttle") == 0
    out = capsys.readouterr().out
    assert len(model.calls) == calls
    assert git(wt, "rev-parse", "HEAD") == handoff["commit"]  # no second commit
    assert git(origin(repo), "rev-parse", "feat/throttle") == handoff["commit"]
    assert ready_line(out) and "SPEC:BRANCH:feat/throttle" in out
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
    git(repo, "branch", "-D", "feat/throttle")
    git(repo, "push", "-q", "origin", "--delete", "feat/throttle")
    assert is_ready(source_dir(repo)) == (True, "")
    before = tree(repo)
    capsys.readouterr()
    calls = len(model.calls)

    assert run(repo, "resume", "throttle") == 0

    out = capsys.readouterr().out
    assert len(model.calls) == calls
    assert tree(repo) == before  # the original is never deleted
    assert meta(change_dir(repo))["branch"] == "feat/throttle"
    assert is_ready(change_dir(repo)) == (True, "")
    assert (
        git(wt, "log", "-1", "--format=%s") == "specs(throttle): moved to feat/throttle"
    )
    assert git(origin(repo), "rev-parse", "feat/throttle") == git(
        wt, "rev-parse", "HEAD"
    )
    assert f"SPEC:READY:{change_dir(repo)}:" in out
    assert "SPEC:BRANCH:feat/throttle" in out


def test_revise_in_a_builds_worktree_commits_there_and_pushes_nothing(
    repo, model, capsys
):
    """buildme's advice, `--project-dir <build worktree>`: the revision is
    committed on the build's branch and reaches the feature branch through
    that build's pull request."""
    assert run(repo, "new", "throttle", "--auto") == 0
    pushed = git(origin(repo), "rev-parse", "feat/throttle")
    build = repo.parent / ".worktrees" / "buildme-throttle"
    git(
        repo,
        "worktree",
        "add",
        "-q",
        "-b",
        "buildme/throttle",
        str(build),
        "origin/feat/throttle",
    )
    cdir = build / "specs" / "changes" / "throttle"
    write(cdir / "spec-issues.md", ISSUES)
    set_spec_issues(cdir, cdir / "spec-issues.md")
    model["consistency"] = resolved()
    capsys.readouterr()

    assert run(build, "revise", "throttle") == 0

    out = capsys.readouterr().out
    assert f"SPEC:READY:{cdir}:" in out
    assert "reaches feat/throttle through that branch's pull request" in out
    assert git(build, "symbolic-ref", "--short", "HEAD") == "buildme/throttle"
    assert git(build, "log", "-1", "--format=%s").startswith("specs(throttle)")
    assert git(build, "status", "--porcelain", "--", "specs") == ""
    assert git(origin(repo), "rev-parse", "feat/throttle") == pushed
    assert git(origin(repo), "branch", "--list", "buildme/throttle") == ""
