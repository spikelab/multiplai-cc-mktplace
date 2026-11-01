"""The implementation plan of a change of several components
(`epic.change_plan`): the change's build writes one plan for every
component, pushes it to the feature branch, and launches no component until
an engineer approves it there (stop 4), unless it runs with --auto."""

from __future__ import annotations

import subprocess

import pytest
from build_pipeline.config import EpicSettings
from build_pipeline.epic import lock as epic_lock

from .epic_row_helpers import (
    CHANGE_PLAN,
    Fakes,
    _git,
    make_build,
    make_project,
    real_git,
    run_coordinator,
    use_workspace,
)
from .test_nested_components import _launched, _push_nested

PLAN = "specs/changes/e/implementation-plan.md"
REQUEST = "specs/changes/e/plan-approval.md"


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


@pytest.fixture
def change(tmp_path, monkeypatch):
    """Change `e` with components a and b (b depends on a), pushed to
    origin's ENG-40-e, built without --auto."""
    use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)
    fakes = Fakes(tmp_path, monkeypatch, {"a": [{"commit": {"a.txt": "a\n"}}]})
    config = make_build(project)
    config.auto = False
    config.epic = EpicSettings(max_parallel=2, merge="manual")
    _git(project, "config", "user.name", "Ada Lovelace")
    _push_nested(config)
    return config, fakes, tmp_path / "o" / "r.git"


def _show(origin, path: str) -> str:
    return _git(origin, "show", f"ENG-40-e:{path}")


def _launches(root) -> list[str]:
    return _launched(root) if (root / "calls.log").exists() else []


def _revision(origin) -> str:
    for line in _show(origin, REQUEST).splitlines():
        if line.startswith("Plan revision: `"):
            return line[len("Plan revision: `") : -1]
    raise AssertionError("no revision in plan-approval.md")


def _push_edit(config, origin, path: str, text: str) -> None:
    """Edit *path* on origin's ENG-40-e, as an engineer or pm-spec would."""
    project = config.project_dir
    _git(project, "fetch", "-q", "origin")
    _git(project, "checkout", "-q", "-B", "edit", "origin/ENG-40-e")
    (project / path).write_text(text)
    _git(project, "add", "--", path)
    _git(project, "-c", "commit.gpgsign=false", "commit", "-qm", "edit")
    subprocess.run(
        [real_git(), "push", "-q", "origin", "HEAD:refs/heads/ENG-40-e"],
        cwd=project,
        check=True,
        capture_output=True,
    )


def test_the_plan_is_written_once_and_waits_for_its_approval(change, capsys):
    config, fakes, origin = change
    capsys.readouterr()
    assert run_coordinator(config, auto=False) == 3
    out = capsys.readouterr().out
    assert "PLAN:WAITING:" in out and out.split("PLAN:WAITING:")[1].split()[0].endswith(
        REQUEST
    )
    assert fakes.plans_written == ["e"]
    assert _show(origin, PLAN) == CHANGE_PLAN
    request = _show(origin, REQUEST)
    assert (
        "`implementation-plan.md`: how the whole change will be implemented" in request
    )
    assert "- `components/a/`" in request and "- `components/b/`" in request
    assert _launches(fakes.root) == []

    # Again with no approval: the plan is not written again, nothing launches.
    head = _git(origin, "rev-parse", "ENG-40-e")
    assert run_coordinator(config, auto=False) == 3
    assert fakes.plans_written == ["e"]
    assert _git(origin, "rev-parse", "ENG-40-e") == head
    assert _launches(fakes.root) == []


def test_approve_plan_commits_the_approval_on_the_branch_and_builds(change):
    config, fakes, origin = change
    assert run_coordinator(config, auto=False) == 3
    rev = _revision(origin)
    assert run_coordinator(config, auto=False, approve_plan=True) == 3
    trailers = _git(origin, "log", "-1", "--format=%(trailers:only,unfold)", "ENG-40-e")
    assert trailers.split("\n")[:2] == ["Stop: 4", f"Revision: {rev}"]
    assert "Approved-by: Ada Lovelace" in trailers
    assert _launches(fakes.root) == ["e/a"]
    # The components build from the plan on the branch, and write none.
    assert fakes.plans_written == ["e"]


def test_an_approval_someone_pushed_counts(change):
    config, fakes, origin = change
    assert run_coordinator(config, auto=False) == 3
    rev = _revision(origin)
    project = config.project_dir
    _git(project, "fetch", "-q", "origin")
    _git(project, "checkout", "-q", "-B", "ok", "origin/ENG-40-e")
    _git(
        project,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "review(e): approve the implementation plan",
        "--trailer",
        "Stop: 4",
        "--trailer",
        f"Revision: {rev}",
        "--trailer",
        "Approved-by: Ada Lovelace",
    )
    subprocess.run(
        [real_git(), "push", "-q", "origin", "HEAD:refs/heads/ENG-40-e"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    assert run_coordinator(config, auto=False) == 3
    assert _launches(fakes.root) == ["e/a"]


def test_an_edit_to_the_plan_after_the_approval_asks_again(change, capsys):
    config, fakes, origin = change
    assert run_coordinator(config, auto=False) == 3
    first = _revision(origin)
    assert run_coordinator(config, auto=False, approve_plan=True) == 3
    assert _launches(fakes.root) == ["e/a"]
    _push_edit(config, origin, PLAN, CHANGE_PLAN + "\nA second store.\n")
    capsys.readouterr()
    assert run_coordinator(config, auto=False) == 3
    assert "PLAN:WAITING:" in capsys.readouterr().out
    assert _revision(origin) != first
    # The edited plan is kept, never written again.
    assert fakes.plans_written == ["e"]
    assert "A second store." in _show(origin, PLAN)


def test_with_auto_the_plan_is_written_and_the_components_build(change):
    config, fakes, origin = change
    config.auto = True
    assert run_coordinator(config, auto=True) == 3
    assert _show(origin, PLAN) == CHANGE_PLAN
    assert "plan-approval.md" not in _git(
        origin, "ls-tree", "--name-only", "ENG-40-e:specs/changes/e"
    )
    assert _launches(fakes.root) == ["e/a"]


def test_a_change_planned_per_component_keeps_its_plans(tmp_path, monkeypatch):
    """A component on the branch has its own plan, the change none: the
    change was planned one component at a time, and builds on that way."""
    use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)
    fakes = Fakes(tmp_path, monkeypatch, {"a": [{"commit": {"a.txt": "a\n"}}]})
    config = make_build(project)
    config.auto = False
    config.epic = EpicSettings(max_parallel=2, merge="manual")
    _push_nested(config)
    origin = tmp_path / "o" / "r.git"
    _push_edit(
        config, origin, "specs/changes/e/components/a/design.md", "# Design of a\n"
    )
    assert run_coordinator(config, auto=False) == 3
    assert fakes.plans_written == []
    assert "implementation-plan.md" not in _git(
        origin, "ls-tree", "--name-only", "ENG-40-e:specs/changes/e"
    )
    assert _launches(fakes.root) == ["e/a"]


def test_a_spec_issue_in_the_plan_goes_back_to_pm_spec(change, monkeypatch, capsys):
    config, fakes, origin = change
    from build_pipeline.llm_steps import spec_steps

    async def with_issue(change_dir, config, **_):
        return (
            CHANGE_PLAN
            + "SPEC_ISSUE: components/b/specs.md#cap-b: cap-b: two stores contradict\n"
        )

    monkeypatch.setattr(spec_steps, "generate_change_plan", with_issue)
    capsys.readouterr()
    assert run_coordinator(config, auto=False) == 4
    out = capsys.readouterr().out
    assert "SPEC:ISSUES:" in out and "pm-spec revise e" in out
    assert "two stores contradict" in _show(origin, "specs/changes/e/spec-issues.md")
    assert "implementation-plan.md" not in _git(
        origin, "ls-tree", "--name-only", "ENG-40-e:specs/changes/e"
    )
    assert _launches(fakes.root) == []
