"""The handoff: the ready record on each component and on the change, and the
commit of the change dir alone."""

from __future__ import annotations

import pytest
from conftest import TWO, change_dir, git, meta, run, specs, state, write
from multiplai_core.specs import is_ready, spec_revision
from pm_spec_pipeline import branch, git_ops, layout


def test_each_component_and_the_change_get_the_ready_record(repo, model, capsys):
    model["components"] = TWO
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    for d in (cdir, cdir / "components" / "api", cdir / "components" / "ui"):
        assert is_ready(d) == (True, ""), d
        assert meta(d)["spec_revision"] == spec_revision(d)
    revision = layout.revision(cdir)
    assert f"SPEC:READY:{cdir}:{revision}" in capsys.readouterr().out
    assert state(cdir)["handoff"]["revision"] == revision


def test_an_edit_to_one_components_specs_after_ready_is_checked_again(repo, model, capsys):
    model["components"] = TWO
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    ui = cdir / "components" / "ui"
    write(ui / "specs.md", specs(rule="the edited rule"))
    assert is_ready(cdir)[0] is True and is_ready(ui)[0] is False
    checks = len(model.exactly("consistency"))
    capsys.readouterr()
    assert run(repo, "resume", "throttle") == 0
    assert len(model.exactly("consistency")) == checks + 1
    assert is_ready(ui) == (True, "") and layout.ready(cdir) == (True, "")
    assert f"SPEC:READY:{cdir}:{layout.revision(cdir)}" in capsys.readouterr().out
    assert git(change_dir(repo).parents[2], "status", "--porcelain", "--", "specs") == ""


class TestCommit:
    def test_other_staged_work_stays_staged_and_out_of_the_commit(self, repo):
        cdir = write(repo / "specs" / "changes" / "c" / "proposal.md", "p\n").parent
        write(repo / "staged.txt", "s\n")
        git(repo, "add", "staged.txt")
        sha = git_ops.commit_change_dir(repo, cdir, "specs(c): ready")
        assert sha == git(repo, "rev-parse", "HEAD")
        assert (
            git(repo, "show", "--name-only", "--format=", "HEAD")
            == "specs/changes/c/proposal.md"
        )
        assert git(repo, "diff", "--cached", "--name-only") == "staged.txt"

    def test_nothing_to_commit_returns_none(self, repo):
        cdir = write(repo / "specs" / "changes" / "c" / "proposal.md", "p\n").parent
        assert git_ops.commit_change_dir(repo, cdir, "m")
        assert git_ops.commit_change_dir(repo, cdir, "m") is None

    def test_a_file_staged_and_then_edited_is_refused(self, repo):
        path = write(repo / "specs" / "changes" / "c" / "proposal.md", "p\n")
        git(repo, "add", str(path))
        path.write_text("edited\n")
        with pytest.raises(
            git_ops.GitError, match="staged and working versions differ"
        ):
            git_ops.commit_change_dir(repo, path.parent, "m")

    def test_the_run_folder_is_never_committed(self, repo, model):
        assert run(repo, "new", "throttle", "--auto") == 0
        assert git(repo, "ls-files", "specs/changes/throttle/.pm-spec") == ""


# --- the ready record covers only files the check read --------------------------


def test_an_edit_while_the_check_runs_is_checked_before_ready(repo, model, capsys):
    cdir = change_dir(repo)
    read = []

    def check(prompt, label, kw):
        read.append(spec_revision(cdir))
        if len(read) == 1:  # the user saves an edit while the agent is still reading
            write(cdir / "specs.md", specs(rule="UNCHECKED EDIT"))
        return {"findings": []}

    model["consistency"] = check
    assert run(repo, "new", "throttle", "--auto") == 0
    out = capsys.readouterr().out
    assert len(read) == 2  # checked again, at the edited revision
    ready = [line for line in out.splitlines() if line.startswith("SPEC:READY:")]
    assert ready == [f"SPEC:READY:{cdir}:{read[1]}"]
    assert meta(cdir)["spec_revision"] == read[1] == spec_revision(cdir)
    assert state(cdir)["steps"]["consistency"]["revision"] == read[1]
    assert is_ready(cdir) == (True, "")


def test_the_recheck_passes_at_the_revision_it_read(repo, model):
    from test_consistency import finding, recheck

    cdir = change_dir(repo)
    read = []

    def second(prompt, label, kw):
        read.append(spec_revision(cdir))
        write(cdir / "specs.md", specs(rule="EDIT DURING RECHECK"))
        return recheck()

    model["consistency"] = [
        {"findings": [finding(files=("specs.md", "proposal.md"))]},
        second,
        {"findings": []},
    ]
    assert run(repo, "new", "throttle", "--auto") == 0
    assert len(model.exactly("consistency")) == 3
    assert meta(cdir)["spec_revision"] == spec_revision(cdir) != read[0]
    assert state(cdir)["steps"]["consistency"]["revision"] == spec_revision(cdir)


def test_a_change_between_the_compare_and_the_ready_record_is_checked_again(
    repo, model, monkeypatch
):
    real = branch.record
    cdir = change_dir(repo)
    done = []

    def record_then_edit(change_dir_, name):
        real(change_dir_, name)
        if model.exactly("consistency") and not done:
            done.append(1)
            write(cdir / "specs.md", specs(rule="LATE EDIT"))

    monkeypatch.setattr(branch, "record", record_then_edit)
    assert run(repo, "new", "throttle", "--auto") == 0
    assert len(model.exactly("consistency")) == 2
    assert meta(cdir)["spec_revision"] == spec_revision(cdir) and is_ready(cdir)[0]


# --- never ready while requirements issues are open ------------------------------

ISSUES = "# Spec issues\n\n## Retry header\n\n`retry-policy` lacks the Retry-After header.\n"


def test_resume_after_a_blocked_revise_refuses_and_never_hands_off(repo, model, capsys):
    from multiplai_core.specs import set_spec_issues

    model["consistency"] = [
        {"findings": []},
        {
            "findings": [],
            "prior_assessments": [{"finding_id": "RV-F1", "status": "still_present"}],
        },
        {"findings": []},
    ]
    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    write(cdir / "spec-issues.md", ISSUES)
    set_spec_issues(cdir, cdir / "spec-issues.md")
    assert run(repo, "revise", "throttle") == 4
    capsys.readouterr()
    calls, head = len(model.calls), git(repo, "rev-parse", "HEAD")
    assert run(repo, "resume", "throttle") == 4
    out = capsys.readouterr().out
    assert "SPEC:READY" not in out and "revise throttle" in out
    assert len(model.calls) == calls and git(repo, "rev-parse", "HEAD") == head
    assert meta(cdir)["spec_issues"] == "spec-issues.md" and not is_ready(cdir)[0]


def test_a_ready_change_with_open_issues_is_not_reported_ready(repo, model, capsys):
    from multiplai_core.specs import set_spec_issues

    assert run(repo, "new", "throttle", "--auto") == 0
    cdir = change_dir(repo)
    write(cdir / "spec-issues.md", ISSUES)
    set_spec_issues(cdir, cdir / "spec-issues.md")
    capsys.readouterr()
    assert run(repo, "resume", "throttle") == 4
    out = capsys.readouterr().out
    assert "SPEC:READY" not in out and "spec-issues.md" in out
