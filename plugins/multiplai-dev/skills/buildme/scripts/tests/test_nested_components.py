"""Components nested in the change (plan-pm-spec-staged-flow-2026-10-23.md,
items 24-27): pm-spec writes a change of several components as
`specs/changes/<change>/components/<id>/`, each with its own `plan.md`,
`specs.md` (`# Capability: <name>` sections) and `.change.yaml`, and keeps the
proposal, use cases and prototype at the change. buildme builds each
component as `<change>/<id>` on its own `<TICKET>-<change>-<id>` branch, reads
its capabilities from `specs.md`, hands the prototype's files to the agents
that write the code, and archives the change once, after its last component
merged into the feature branch. A change in the old layout
(`requirements/<cap>.md`) is read as before."""

from __future__ import annotations

import os
import subprocess
from datetime import date
from pathlib import Path

import pytest
from build_pipeline import feature_branch, git_ops, readiness
from build_pipeline.change_manager import (
    ChangeManager,
    normalize_change_name,
    registry_names,
)
from build_pipeline.config import BuildConfig, EpicSettings
from build_pipeline.epic import lock as epic_lock
from build_pipeline.llm_steps.research_steps import specs_summary
from build_pipeline.llm_steps.spec_steps import _read_dep, _read_specs
from build_pipeline.models import BlockInfo
from build_pipeline.orchestrator import pr_title
from build_pipeline.rubric import _gather_spec_summaries
from build_pipeline.spec_generator import _plan_hashes
from build_pipeline.tdd_engine import assemble_context
from multiplai_core.specs import is_ready, mark_ready, write_change_meta

from .epic_row_helpers import (
    READY_AT,
    REQUIREMENT,
    Fakes,
    _git,
    args_for,
    make_build,
    make_project,
    merge_waiting,
    pr_url,
    real_git,
    run_build,
    run_coordinator,
    use_workspace,
)


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


def _specs_md(*caps: str) -> str:
    return "\n".join(
        f"# Capability: {cap}\n\n" + REQUIREMENT.format(title=cap) for cap in caps
    )


# Each component: (its capabilities, the ids it depends on, its own ticket).
CHAIN = {
    "a": (("cap-a",), (), "ENG-41"),
    "b": (("cap-b", "cap-shared"), ("a",), None),
}


def write_nested(change_dir: Path, components=CHAIN, *, prototype: bool = True):
    """The change pm-spec hands off with its components nested: the change's
    proposal (no `## Capabilities`), use cases, `components.md` and
    prototype; each component's `plan.md`, `specs.md` and `.change.yaml`
    (`depends_on` ids, the change's branch, its own ticket when it has one),
    marked ready, and the change's `children` and ready record."""
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "proposal.md").write_text(
        "# Stop logging people out\n\n## Why\n\nPeople lose typed work.\n"
    )
    (change_dir / "use-cases.md").write_text(
        "# Use cases\n\n## UC-1\n\nA user keeps the form.\n"
    )
    (change_dir / "components.md").write_text(
        "| Component | Depends on | Ticket |\n|---|---|---|\n"
        + "".join(
            f"| {cid} | {', '.join(deps)} | {ticket or 'pending'} |\n"
            for cid, (_, deps, ticket) in components.items()
        )
    )
    if prototype:
        (change_dir / "prototype").mkdir(exist_ok=True)
        (change_dir / "prototype" / "settings.html").write_text("<form></form>\n")
        (change_dir / "prototype" / "NOTES.md").write_text("What each file shows.\n")
    for cid, (caps, deps, ticket) in components.items():
        d = change_dir / "components" / cid
        d.mkdir(parents=True, exist_ok=True)
        (d / "plan.md").write_text(f"# Plan: Part {cid}\n\n**Objective:** {cid}.\n")
        (d / "specs.md").write_text(_specs_md(*caps))
        meta = {
            "spec_status": "draft",
            "depends_on": list(deps),
            "branch": f"ENG-40-{change_dir.name}",
        }
        if ticket:
            meta["ticket_refs"] = {"epic": ticket, "children": []}
        write_change_meta(d, meta)
        mark_ready(d, now=READY_AT)
    write_change_meta(
        change_dir,
        {
            "children": list(components),
            "branch": f"ENG-40-{change_dir.name}",
            "ticket_refs": {"epic": "ENG-40", "children": []},
        },
    )
    mark_ready(change_dir, now=READY_AT)


def write_old_layout(change_dir: Path) -> None:
    """A change pm-spec wrote before `specs.md`: the proposal names its
    capabilities and each has `requirements/<cap>.md`."""
    (change_dir / "requirements").mkdir(parents=True, exist_ok=True)
    (change_dir / "proposal.md").write_text(
        "# Old\n\n## Why\n\nx\n\n## Capabilities\n\n- `old-cap`: x\n"
    )
    (change_dir / "use-cases.md").write_text("# Use cases\n\n## UC-1\n\nx\n")
    (change_dir / "requirements" / "old-cap.md").write_text(
        REQUIREMENT.format(title="old")
    )


def _config(project: Path, change: str) -> BuildConfig:
    config = BuildConfig(project_dir=project, change_name=change)
    config.specs_dir = project / "specs"
    return config


def _block() -> BlockInfo:
    return BlockInfo(number=1, name="Form", description="Keep the form.")


# --- item 24: a nested component resolves ------------------------------------------


def test_a_nested_component_resolves_to_its_dir_and_is_named_as_a_child(tmp_path):
    config = _config(tmp_path, "auth-session/token-retry")
    assert config.change_dir == (
        tmp_path / "specs" / "changes" / "auth-session" / "components" / "token-retry"
    )
    # Its branches are named as a sibling child's: `<TICKET>-<change>-<id>`.
    assert normalize_change_name("auth-session/token-retry") == (
        "auth-session-token-retry"
    )
    assert feature_branch.name("ENG-41", "auth-session/token-retry") == (
        "ENG-41-auth-session-token-retry"
    )
    assert git_ops.branch_name_for("auth-session/token-retry") == (
        "buildme/auth-session-token-retry"
    )
    # The setter takes a component's dir; anything deeper is still refused.
    config.change_dir = config.change_dir
    assert config.change_dir.name == "token-retry"
    with pytest.raises(ValueError):
        config.change_dir = config.specs_dir / "changes" / "a" / "b" / "c"
    # A plain change, and the path guard, are as before.
    assert _config(tmp_path, "feat").change_dir == tmp_path / "specs/changes/feat"
    assert normalize_change_name("../../etc") == "etc"


def test_a_components_tickets_are_its_own_then_its_changes(tmp_path):
    change = tmp_path / "specs" / "changes" / "e"
    write_nested(change)
    assert feature_branch.tickets_in(change / "components" / "a") == [
        "ENG-41",
        "ENG-40",
    ]
    # A component whose ticket is pending takes the change's.
    assert feature_branch.tickets_in(change / "components" / "b") == ["ENG-40"]


def test_tickets_and_readiness_on_a_commit_read_the_component_and_its_change(
    tmp_path,
):
    """An epic's component build reads its tickets and its ready record as
    the feature branch holds them; the ready record covers the change's
    proposal and use cases, so the copy holds the change too."""
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    write_nested(repo / "specs" / "changes" / "e")
    _git(repo, "add", "specs")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "specs",
    )
    assert feature_branch.tickets_at(repo, "HEAD", "specs/changes", "e/a") == [
        "ENG-41",
        "ENG-40",
    ]
    assert feature_branch.tickets_at(repo, "HEAD", "specs/changes", "e/b") == ["ENG-40"]
    copy = readiness.specs_at(
        repo, "HEAD", "specs/changes/e/components/b", tmp_path / "copy"
    )
    assert copy == tmp_path / "copy" / "specs/changes/e/components/b"
    assert is_ready(copy) == (True, "")
    assert (
        readiness.specs_at(
            repo, "HEAD", "specs/changes/e/components/zz", tmp_path / "none"
        )
        is None
    )


# --- item 25: specs.md capabilities are read ---------------------------------------


def test_a_components_specs_md_and_its_changes_proposal_are_its_inputs(tmp_path):
    write_nested(tmp_path / "specs" / "changes" / "e")
    config = _config(tmp_path, "e/b")
    cm = ChangeManager(config.specs_dir)
    assert cm.missing_inputs(config.change_dir) == []
    deps = cm.artifact_context(config.change_dir, "tasks")["dependencies"]
    assert deps["requirements"] == "specs.md"
    assert deps["proposal"] == os.path.join("..", "..", "proposal.md")
    context = {"dependencies": deps}
    assert "Stop logging people out" in _read_dep(context, "proposal", config)
    assert "A user keeps the form." in _read_dep(context, "use-cases", config)

    specs = _read_specs(config)
    assert "### cap-b\n" in specs and "### cap-shared\n" in specs
    assert "# Capability:" not in specs
    assert "### cap-b" in _gather_spec_summaries(config.change_dir)
    summary = specs_summary(config.change_dir)
    assert "### proposal.md\n# Stop logging people out" in summary
    assert "### specs.md#cap-shared" in summary
    implementer = assemble_context(_block(), config, "implementer")
    assert "## Requirements: specs.md#cap-b\n" in implementer
    assert "## Requirements: specs.md#cap-shared\n" in implementer
    assert registry_names(config.change_dir) == ["cap-b", "cap-shared"]
    # The change's proposal and use cases are hashed into the component's plan.
    hashes = _plan_hashes(config)
    assert {"proposal.md", "use-cases.md", "specs.md"} <= set(hashes)


def test_a_specs_md_that_cannot_be_split_is_a_missing_input_not_a_crash(tmp_path):
    write_nested(tmp_path / "specs" / "changes" / "e")
    config = _config(tmp_path, "e/a")
    (config.change_dir / "specs.md").write_text(_specs_md("cap-a", "cap-a"))
    assert ChangeManager(config.specs_dir).missing_inputs(config.change_dir) == [
        "requirements/*.md"
    ]
    assert _read_specs(config) == "(no specs yet)"


def test_an_old_layout_change_still_builds_its_readers(tmp_path):
    config = _config(tmp_path, "old")
    write_old_layout(config.change_dir)
    cm = ChangeManager(config.specs_dir)
    assert cm.missing_inputs(config.change_dir) == []
    deps = cm.artifact_context(config.change_dir, "tasks")["dependencies"]
    assert deps["requirements"] == "requirements/old-cap.md"
    assert deps["proposal"] == "proposal.md"
    assert "### old-cap\n" in _read_specs(config)
    assert "### old-cap" in _gather_spec_summaries(config.change_dir)
    assert "### requirements/old-cap.md" in specs_summary(config.change_dir)
    assert "## Requirements: requirements/old-cap.md\n" in assemble_context(
        _block(), config, "implementer"
    )
    assert registry_names(config.change_dir) == ["old-cap"]
    # A requirements file the proposal names but that is missing is still
    # a missing input.
    (config.change_dir / "requirements" / "old-cap.md").unlink()
    assert cm.missing_inputs(config.change_dir) == ["requirements/*.md"]


def test_a_components_pr_title_is_its_plans(tmp_path):
    write_nested(tmp_path / "specs" / "changes" / "e")
    component = tmp_path / "specs" / "changes" / "e" / "components" / "a"
    assert (
        pr_title("ENG-40-e", ("ENG-41",), component, "e/a")
        == "[ENG-40-e] ENG-41 Part a"
    )
    # A change keeps its proposal's title.
    assert (
        pr_title("main", ("ENG-40",), component.parent.parent, "e")
        == "[main] ENG-40 Stop logging people out"
    )


# --- item 26: prototype paths reach the agents that write the code -----------------


def test_the_prototype_files_reach_the_test_writer_and_implementer(tmp_path):
    write_nested(tmp_path / "specs" / "changes" / "e")
    config = _config(tmp_path, "e/a")
    for role in ("test_writer", "implementer"):
        ctx = assemble_context(_block(), config, role)
        section = ctx.split("\n## Prototype\n", 1)[1]
        assert "When this block writes UI or API code, match the prototype" in section
        assert "- specs/changes/e/prototype/settings.html" in section
        assert "- specs/changes/e/prototype/NOTES.md" in section
    assert "## Prototype" not in assemble_context(_block(), config, "refactorer")


def test_no_prototype_no_section(tmp_path):
    write_nested(tmp_path / "specs" / "changes" / "e", prototype=False)
    assert "## Prototype" not in assemble_context(
        _block(), _config(tmp_path, "e/a"), "implementer"
    )


# --- item 27: archiving a nested change merges into the registry -------------------


def test_archiving_a_nested_change_merges_every_component_into_the_registry(
    tmp_path,
):
    write_nested(tmp_path / "specs" / "changes" / "e")
    specs = tmp_path / "specs"
    (specs / "registry").mkdir()
    (specs / "registry" / "cap-shared.md").write_text(
        REQUIREMENT.format(title="before")
    )
    dest = ChangeManager(specs).archive_change(specs / "changes" / "e")
    assert dest == specs / "archive" / f"{date.today().isoformat()}-e"
    assert (dest / "components" / "b" / "specs.md").is_file()
    assert not (specs / "changes" / "e").exists()
    registry = sorted(p.name for p in (specs / "registry").glob("*.md"))
    assert registry == ["cap-a.md", "cap-b.md", "cap-shared.md"]
    shared = (specs / "registry" / "cap-shared.md").read_text()
    assert "Requirement: before" in shared and "Requirement: cap-shared" in shared


def test_the_archive_command_refuses_a_component_alone(tmp_path, capsys):
    from build_pipeline.__main__ import _main

    write_nested(tmp_path / "specs" / "changes" / "e")
    args = args_for(
        _config(tmp_path, "e/a"), command="archive", no_merge=False, session_id=""
    )
    assert _main(args) == 1
    assert "is a component; archive its change: --change e" in capsys.readouterr().err
    assert (tmp_path / "specs" / "changes" / "e" / "components" / "a").is_dir()


# --- the epic coordinator over nested components ------------------------------------


def _push_nested(config: BuildConfig, components=CHAIN) -> str:
    """`write_nested` in the project, committed and pushed to origin's
    `ENG-40-<change>`, as pm-spec's handoff does; the commit's sha."""
    project = Path(config.project_dir)
    write_nested(config.change_dir, components)
    rel = os.path.relpath(config.change_dir, project)
    _git(project, "add", "--", rel)
    _git(project, "-c", "commit.gpgsign=false", "commit", "-qm", "specs: ready")
    for cmd in (
        ["push", "-q", "origin", f"HEAD:refs/heads/ENG-40-{config.change_name}"],
        ["fetch", "-q", "origin"],
    ):
        subprocess.run([real_git(), *cmd], cwd=project, check=True, capture_output=True)
    return _git(project, "rev-parse", "HEAD").strip()


def _launched(root: Path) -> list[str]:
    import json

    calls = [json.loads(x) for x in (root / "calls.log").read_text().splitlines()]
    return [c["argv"][c["argv"].index("--change") + 1] for c in calls]


def test_buildme_on_a_nested_change_builds_its_components_in_order_then_archives_it(
    tmp_path, monkeypatch
):
    use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)
    fakes = Fakes(tmp_path, monkeypatch, {"a": [{"commit": {"a.txt": "a\n"}}]})
    config = make_build(project)
    config.epic = EpicSettings(max_parallel=2, merge="manual")
    _push_nested(config)
    origin = tmp_path / "o" / "r.git"

    # b depends on a: only a starts, as `e/a`, from the feature branch.
    assert run_coordinator(config) == 3 and merge_waiting() == [pr_url("e-a")]
    assert _launched(tmp_path) == ["e/a"]
    pr = fakes.prs()[pr_url("e-a")]
    assert pr["base"] == "ENG-40-e" and pr["branch"] == "buildme/e-a"

    fakes.set_pr(pr_url("e-a"), "MERGED")
    assert run_coordinator(config) == 3 and merge_waiting() == [pr_url("e-b")]
    assert _launched(tmp_path) == ["e/a", "e/b"]
    # Nothing is archived while a component is not merged.
    assert "e" in _git(origin, "ls-tree", "--name-only", "ENG-40-e:specs/changes")

    fakes.set_pr(pr_url("e-b"), "MERGED")
    assert run_coordinator(config) == 0
    archived = f"{date.today().isoformat()}-e"
    assert _git(origin, "ls-tree", "--name-only", "ENG-40-e:specs/archive").split() == [
        archived
    ]
    assert _git(
        origin, "ls-tree", "--name-only", "ENG-40-e:specs/registry"
    ).split() == [
        "cap-a.md",
        "cap-b.md",
        "cap-shared.md",
    ]
    assert (
        _git(origin, "ls-tree", "-r", "--name-only", "ENG-40-e", "--", "specs/changes")
        == ""
    )
    assert _git(origin, "show", "ENG-40-e:a.txt").strip() == "a"
    assert _git(origin, "log", "-1", "--format=%s", "ENG-40-e").strip() == (
        "chore(specs): archive e"
    )
    # Run again: the change is archived once, and nothing is launched.
    head = _git(origin, "rev-parse", "ENG-40-e")
    assert run_coordinator(config) == 0
    assert _git(origin, "rev-parse", "ENG-40-e") == head
    assert _launched(tmp_path) == ["e/a", "e/b"]


def test_a_component_depending_on_an_unknown_id_is_refused(
    tmp_path, monkeypatch, capsys
):
    use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)
    Fakes(tmp_path, monkeypatch)
    config = make_build(project)
    _push_nested(config, {"a": (("cap-a",), ("zz",), None)})
    capsys.readouterr()
    assert run_build(config, args_for(config), coordinator=True) == 2
    out = capsys.readouterr().out
    assert "SPEC:NOT_READY" in out and "depends on zz" in out
    assert not (tmp_path / "calls.log").exists()


# --- a component's --auto build does not archive at PUBLISH -------------------------


def _accepted_at_publish(tmp_path, monkeypatch, change: str):
    """The real orchestrator's last phase for *change* (`e/a`, a component, or
    `e`, the same specs as a single change), with `--auto`: an accepted plan,
    a built block and a passing test command, in a real repository. Only the
    push is doubled, as `test_completion_contract.accepted_build` does."""
    import argparse
    import sys
    from unittest.mock import Mock

    from build_pipeline import orchestrator
    from build_pipeline.config import GitToggles
    from build_pipeline.models import BlockStatus, BuildPhase
    from build_pipeline.spec_generator import reconcile_plan
    from build_pipeline.state import BuildState, TDDState

    monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
    project = tmp_path / "p"
    project.mkdir()
    _git(project, "init", "-qb", "ENG-40-e")
    _git(project, "config", "user.email", "t@t")
    _git(project, "config", "user.name", "t")
    _git(project, "config", "commit.gpgsign", "false")
    config = BuildConfig(
        project_dir=project,
        change_name=change,
        auto=True,
        skip_research=True,
        git=GitToggles(worktree=False, push=True, pr="none"),
        test_command=f'{sys.executable} -c "from app import value; assert value == 1"',
    )
    config.specs_dir = project / "specs"
    root = project / "specs" / "changes" / "e"
    if change == "e":
        root.mkdir(parents=True)
        (root / "proposal.md").write_text("# Stop logging people out\n\n## Why\n\nx\n")
        (root / "use-cases.md").write_text("# Use cases\n\n## UC-1\n\nx\n")
        (root / "plan.md").write_text("# Plan: e\n")
        (root / "specs.md").write_text(_specs_md("cap-a"))
        write_change_meta(root, {"ticket_refs": {"epic": "ENG-40", "children": []}})
        mark_ready(root, now=READY_AT)
    else:
        write_nested(root, {"a": (("cap-a",), (), "ENG-41")}, prototype=False)
    (config.specs_dir / "registry").mkdir()
    (config.specs_dir / "registry" / "cap-a.md").write_text(
        REQUIREMENT.format(title="before")
    )
    for name in ("design", "unknowns", "tasks", "rubric"):
        (config.change_dir / f"{name}.md").write_text(f"# {name}\n")
    (project / "app.py").write_text("value = 1\n")
    _git(project, "add", ".")
    _git(project, "commit", "-qm", "accepted implementation")
    # pm-spec pushed the feature branch the component's `.change.yaml` names.
    origin = tmp_path / "o.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(project, "remote", "add", "origin", str(origin))
    _git(project, "push", "-q", "origin", "ENG-40-e")
    _git(project, "fetch", "-q", "origin")
    config.pipeline_branch = "ENG-40-e"
    state = BuildState(
        change_name=change,
        tier="advanced",
        phase=BuildPhase.PUBLISH,
        branch=config.pipeline_branch,
        worktree_path=str(project),
        state_file=str(config.state_file_path()),
        tdd=TDDState(
            blocks=[
                BlockInfo(
                    number=1,
                    name="Form",
                    description="Keep the form.",
                    status=BlockStatus.DONE,
                )
            ],
            current_block=1,
            baseline_tests_pass=True,
            refactor_all_done=True,
            mutation_done=True,
            e2e_done=True,
        ),
    )
    reconcile_plan(config, state)
    state.spec_gen.record_step("design_audit", "passed")
    state.spec_gen.record_step("plan_review", "passed")
    state.checkpoint(config.state_file_path())
    args = argparse.Namespace(
        change=change,
        project_dir=str(project),
        auto=True,
        spec_only=False,
        skip_research=True,
        research_path="",
        session_id="",
        block=None,
    )
    monkeypatch.setattr(orchestrator, "PRE_PUBLISH_CONTRIBUTORS", [])
    monkeypatch.setattr(git_ops, "has_remote", lambda *a: True)
    push = Mock(return_value=git_ops.GitResult([], 0, "", ""))
    monkeypatch.setattr(git_ops, "push_branch", push)
    return config, args, push


@pytest.mark.parametrize("change", ["e/a", "e"])
def test_an_auto_component_build_publishes_without_archiving(
    tmp_path, monkeypatch, capsys, change
):
    import asyncio

    from build_pipeline.orchestrator import run_orchestrator

    config, args, push = _accepted_at_publish(tmp_path, monkeypatch, change)
    specs = config.specs_dir
    registry = (specs / "registry" / "cap-a.md").read_text()
    capsys.readouterr()

    assert asyncio.run(run_orchestrator(config, args)) == 0
    out = capsys.readouterr().out
    push.assert_called_once()
    assert "PUBLICATION:published" in out and "RESULT:SUCCESS" in out
    last = _git(config.project_dir, "log", "-1", "--format=%s").strip()
    if change == "e/a":
        # The component's dir, its change and the registry are as they were;
        # its change archives once, after its last component merged.
        assert "PHASE:ARCHIVE" not in out
        assert (specs / "changes" / "e" / "components" / "a" / "specs.md").is_file()
        assert not (specs / "archive").exists()
        assert (specs / "registry" / "cap-a.md").read_text() == registry
        assert last != "chore(specs): archive e/a"
    else:
        # The same specs as a single change do archive: the check above is
        # not vacuous.
        assert "PHASE:ARCHIVE:COMPLETE" in out
        assert not (specs / "changes" / "e").exists()
        assert "Requirement: cap-a" in (specs / "registry" / "cap-a.md").read_text()
        assert last == "chore(specs): archive e"
