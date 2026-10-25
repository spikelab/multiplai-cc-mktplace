"""The epic entry fed by pm-spec (plan-pm-spec-split-2026-10-21.md, Decision
11, and plan-spec-feature-branch-2026-10-22.md): `/buildme <parent>` reads the
parent's `children` and each child's `depends_on` from the change dirs as the
epic's feature branch on origin (`ENG-40-<parent>`) holds them, refuses
`SPEC:NOT_READY` (exit 2, nothing written) unless every listed child is
ready there, and runs the coordinator. Every child starts from that branch
and its PR targets it; one with dependencies starts after they merged into
it."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from build_pipeline.config import EpicSettings
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic import phase, records, store
from multiplai_core.specs import mark_ready, write_change_meta

from .epic_row_helpers import (
    READY_AT,
    THREE,
    EpicSplitAnswer,
    Fakes,
    approve,
    args_for,
    child,
    make_build,
    make_project,
    merge_waiting,
    pr_url,
    run_build,
    run_coordinator,
    use_workspace,
)

CHAIN = EpicSplitAnswer(children=[child("a"), child("b", ("a",))])


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(
        ["git", *a], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit_all(project: Path, msg: str) -> str:
    """Commit the checkout's specs and push them to the feature branch, as
    `pm-spec revise` does (on top of what merged into it since)."""
    _git(project, "add", "specs")
    _git(project, "-c", "commit.gpgsign=false", "commit", "-qm", msg)
    _git(project, "fetch", "-q", "origin")
    _git(
        project,
        "-c",
        "commit.gpgsign=false",
        "merge",
        "-q",
        "--no-edit",
        "origin/ENG-40-e",
    )
    _git(project, "push", "-q", "origin", "HEAD:refs/heads/ENG-40-e")
    return _git(project, "rev-parse", "HEAD")


class Epic:
    def __init__(self, root: Path, monkeypatch, answer=THREE, steps=None):
        self.root = root
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.fakes = Fakes(root, monkeypatch, steps)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=2, merge="manual")
        self.specs = approve(self.config, answer)
        self.dir = self.data / "buildme" / "epics" / "o-r--e"
        self.changes = self.config.specs_dir / "changes"

    def run(self) -> int:
        return run_coordinator(self.config)

    def refused(self, capsys) -> tuple[int, str]:
        """One run with the real coordinator wired (it must not be reached)."""
        capsys.readouterr()
        rc = run_build(self.config, args_for(self.config), coordinator=True)
        return rc, capsys.readouterr().out

    def status(self) -> dict:
        return json.loads((self.dir / "epic.json").read_text())["child_status"]

    def build_records(self, cid: str) -> list:
        return [
            a
            for a in records.attempts(self.dir)
            if a.child == cid and a.kind == "build"
        ]


def _assert_nothing_written(e: Epic) -> None:
    assert not e.dir.exists()
    assert not (e.config.change_dir / ".build-state.json").exists()
    assert not (e.root / "calls.log").exists()


# --- which change is an epic -------------------------------------------------------


def test_is_epic_reads_the_parents_children(tmp_path, monkeypatch):
    use_workspace(monkeypatch, tmp_path)
    config = make_build(make_project(tmp_path))
    assert not phase.is_epic(config)
    write_change_meta(config.change_dir, {"children": []})
    assert not phase.is_epic(config)
    write_change_meta(config.change_dir, {"children": ["e-a"]})
    assert phase.is_epic(config)
    (config.change_dir / ".change.yaml").write_text("{")
    assert not phase.is_epic(config)


def test_a_child_that_names_the_parent_but_is_not_listed_is_not_built(
    tmp_path, monkeypatch
):
    """The list, not a scan for `parent`, says which children the epic has."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN)
    stray = e.changes / "e-old"
    stray.mkdir()
    write_change_meta(stray, {"parent": "e", "depends_on": []})
    _commit_all(e.project, "a dropped child")
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert set(e.status()) == {"a", "b"}
    assert [
        json.loads(x)["argv"][json.loads(x)["argv"].index("--change") + 1]
        for x in (tmp_path / "calls.log").read_text().splitlines()
    ] == ["e-a"]


# --- refusals: SPEC:NOT_READY, exit 2, nothing written -----------------------------


def test_a_listed_child_not_ready_is_refused(tmp_path, monkeypatch, capsys):
    """(a) A requirement edited after `mark_ready`, and committed: the child's
    specs are no longer the ones marked ready."""
    e = Epic(tmp_path, monkeypatch)
    req = e.changes / "e-ui" / "requirements" / "cap-ui.md"
    req.write_text(
        req.read_text() + "\n### Requirement: More\nThe system SHALL do more.\n\n"
        "#### Scenario: more\n- **WHEN** it runs\n- **THEN** more is done\n"
    )
    _commit_all(e.project, "edit after ready")
    rc, out = e.refused(capsys)
    assert rc == 2
    lines = out.splitlines()
    assert lines[0].startswith(
        "SPEC:NOT_READY:child e-ui on origin/ENG-40-e: the specs changed after they were marked ready"
    )
    assert lines[1] == "Run /multiplai-pm:pm-spec e-ui first"
    _assert_nothing_written(e)


def test_uncommitted_edits_in_the_checkout_do_not_matter(tmp_path, monkeypatch):
    """(b), (c) The children start from the feature branch, so the checkout's
    own copy is never read: an uncommitted file in the parent's change dir
    and in a child's does not refuse the run (a child build still refuses a
    checkout whose tracked files have uncommitted changes)."""
    e = Epic(tmp_path, monkeypatch)
    (e.config.change_dir / "notes.md").write_text("not committed\n")
    (e.changes / "e-api" / "draft.md").write_text("not committed\n")
    assert e.run() == 3
    assert sorted(merge_waiting()) == [pr_url("e-api"), pr_url("e-ui")]


def test_a_child_not_ready_on_the_feature_branch_is_refused(
    tmp_path, monkeypatch, capsys
):
    """Ready in the checkout is not enough: the feature branch is what the
    children build. A requirement edited and pushed there after `mark_ready`
    refuses, though the checkout's copy is still the ready one."""
    e = Epic(tmp_path, monkeypatch)
    other = tmp_path / "pm-spec"
    _git(
        tmp_path,
        "clone",
        "-q",
        "-b",
        "ENG-40-e",
        str(tmp_path / "o" / "r.git"),
        str(other),
    )
    req = other / "specs" / "changes" / "e-ui" / "requirements" / "cap-ui.md"
    req.write_text(req.read_text() + "\nThe system SHALL do more.\n")
    _git(other, "-c", "commit.gpgsign=false", "commit", "-qam", "edit after ready")
    _git(other, "push", "-q", "origin", "ENG-40-e")
    rc, out = e.refused(capsys)
    assert rc == 2
    assert out.splitlines()[0].startswith(
        "SPEC:NOT_READY:child e-ui on origin/ENG-40-e: the specs changed after they were marked ready"
    )
    _assert_nothing_written(e)


def test_an_epic_with_no_feature_branch_is_refused_with_pm_spec_resume(
    tmp_path, monkeypatch, capsys
):
    """T8 for an epic: a split handed off before pm-spec pushed (ready and
    committed in the checkout, no `ENG-40-e` on origin) is refused with
    `pm-spec resume e`."""
    e = Epic(tmp_path, monkeypatch)
    _git(e.project, "push", "-q", "origin", "--delete", "ENG-40-e")
    _git(e.project, "fetch", "-q", "--prune", "origin")
    rc, out = e.refused(capsys)
    assert rc == 2
    assert out.splitlines()[0].startswith("SPEC:NOT_READY:")
    assert "Run /multiplai-pm:pm-spec resume e first" in out
    _assert_nothing_written(e)


def test_depends_on_naming_an_unlisted_child_is_refused(tmp_path, monkeypatch, capsys):
    """(d)"""
    e = Epic(tmp_path, monkeypatch)
    write_change_meta(e.changes / "e-docs", {"depends_on": ["e-api", "e-ghost"]})
    _commit_all(e.project, "a ghost dependency")
    rc, out = e.refused(capsys)
    assert rc == 2
    assert out.splitlines()[0] == (
        "SPEC:NOT_READY:child e-docs depends on e-ghost, which the parent e does "
        "not list as a child"
    )
    _assert_nothing_written(e)


@pytest.mark.parametrize(
    "damage, reason",
    [
        (
            lambda e: write_change_meta(
                e.config.change_dir, {"children": ["e-api", "other-ui"]}
            ),
            "the parent e's children list names 'other-ui', which is not e-<id>",
        ),
        (
            lambda e: write_change_meta(
                e.config.change_dir, {"children": ["e-api", "e-ui", "e-gone"]}
            ),
            "child e-gone: its change dir",
        ),
        (
            lambda e: write_change_meta(
                e.changes / "e-api", {"depends_on": ["e-docs"]}
            ),
            "the children's depends_on have a cycle: ",
        ),
    ],
    ids=["name", "missing-dir", "cycle"],
)
def test_a_split_buildme_cannot_build_from_is_refused(
    tmp_path, monkeypatch, capsys, damage, reason
):
    e = Epic(tmp_path, monkeypatch)
    damage(e)
    _commit_all(e.project, "damage")
    rc, out = e.refused(capsys)
    assert rc == 2 and out.startswith(f"SPEC:NOT_READY:{reason}")
    assert "Run /multiplai-pm:pm-spec e first" in out
    _assert_nothing_written(e)


# --- the bases ---------------------------------------------------------------------


def test_a_dependant_starts_from_the_feature_branch_after_its_dependency_merged(
    tmp_path, monkeypatch
):
    """(f) a starts from origin's ENG-40-e, the commit pm-spec pushed; b waits
    for a, and once a's PR merged into ENG-40-e, b starts from ENG-40-e, which
    then holds a's merge. Both PRs target ENG-40-e; origin's main is untouched."""
    e = Epic(
        tmp_path, monkeypatch, answer=CHAIN, steps={"a": [{"commit": {"a.txt": "a\n"}}]}
    )
    e.config.epic = EpicSettings(max_parallel=2, merge="manual")
    main = _git(tmp_path / "o" / "r.git", "rev-parse", "main")
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert (tmp_path / "bases" / "a").read_text() == e.specs
    assert e.status()["b"]["status"] == "waiting" and e.build_records("b") == []
    e.fakes.set_pr(pr_url("e-a"), "MERGED")
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    feature = _git(tmp_path / "o" / "r.git", "rev-parse", "ENG-40-e")
    for cid in ("a", "b"):
        argv = e.build_records(cid)[0].started["argv"]
        assert argv[argv.index("--base-ref") + 1] == "origin/ENG-40-e"
        assert argv[argv.index("--branch") + 1] == "ENG-40-e"
    assert (tmp_path / "bases" / "b").read_text() == feature != e.specs
    assert _git(tmp_path / "o" / "r.git", "show", "ENG-40-e:a.txt") == "a"
    assert {p["base"] for p in e.fakes.prs().values()} == {"ENG-40-e"}
    assert _git(tmp_path / "o" / "r.git", "rev-parse", "main") == main


def test_a_specs_revision_pushed_mid_epic_is_what_a_later_child_builds(
    tmp_path, monkeypatch
):
    """(g) T7: after a merged, pm-spec revised b's specs and pushed them to
    ENG-40-e: b starts from the branch with the revision, and nothing compares
    it with an earlier specs commit."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN)
    e.config.epic = EpicSettings(max_parallel=2, merge="manual")
    assert e.run() == 3
    e.fakes.set_pr(pr_url("e-a"), "MERGED")
    use_cases = e.changes / "e-b" / "use-cases.md"
    use_cases.write_text(use_cases.read_text() + "Revised.\n")
    mark_ready(e.changes / "e-b", now=READY_AT)
    revised = _commit_all(e.project, "specs(e-b): revised")
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    assert len(e.build_records("b")) == 1 and e.status()["b"]["status"] == "built"
    base = (tmp_path / "bases" / "b").read_text()
    assert _git(e.project, "merge-base", "--is-ancestor", revised, base) == ""
    assert _git(e.project, "show", f"{base}:specs/changes/e-b/use-cases.md").endswith(
        "Revised."
    )


@pytest.mark.parametrize("interrupted", [False, True], ids=["failed", "stopped"])
def test_a_saved_child_command_from_buildmes_own_split_is_rebuilt(
    tmp_path, monkeypatch, interrupted
):
    """(j) An epic buildme once split saved its child's command with flags
    this version removed (`--mode brief --plan-file ...`), which argparse
    would refuse (exit 2) on every relaunch. The relaunch builds a new
    command instead."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN)
    old = [
        "python",
        "-m",
        "build_pipeline",
        "--session-id",
        "",
        "build",
        "--mode",
        "brief",
        "--change",
        "e-a",
        "--plan-file",
        str(e.dir / "plans" / "a.md"),
        "--skip-research",
        "--auto",
        "--trust-repo",
        "--no-epic-split",
        "--epic-child",
        "--pr-ready",
        "--project-dir",
        str(e.project),
    ]
    records.finish(
        records.start(e.dir, "build", "a", old),
        None if interrupted else 1,
        interrupted=interrupted,
    )
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a")]
    relaunch = e.build_records("a")[-1].started["argv"]
    assert relaunch == e_new_argv(e, "a")
    for flag in ("--mode", "--plan-file", "--no-epic-split"):
        assert flag not in relaunch


def e_new_argv(e: Epic, cid: str) -> list[str]:
    from build_pipeline.epic import coordinator

    coord = coordinator.Coordinator(
        e.config, args_for(e.config), e.dir, store.load_epic(e.dir), "ENG-40-e"
    )
    return coord.new_argv(cid)


def _archive(e: Epic, child: str) -> Path:
    """What a merged `--auto` child's PR leaves in the checkout after a pull:
    its change dir moved to specs/archive/<date>-<child>/, committed."""
    dest = e.config.specs_dir / "archive" / f"2026-10-22-{child}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(e.project, "mv", f"specs/changes/{child}", str(dest.relative_to(e.project)))
    _commit_all(e.project, f"specs: archive {child}")
    return dest


def test_a_merged_childs_archived_change_dir_does_not_stop_the_resume(
    tmp_path, monkeypatch
):
    """(h) a merged and its change dir moved to specs/archive/ (children run
    with --auto): the next run takes a as merged and launches b."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN)
    assert e.run() == 3
    e.fakes.set_pr(pr_url("e-a"), "MERGED")
    _archive(e, "e-a")
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    assert e.status()["a"]["status"] == "merged"
    assert len(e.build_records("b")) == 1 and len(e.build_records("a")) == 1


def test_a_listed_child_gone_and_never_merged_is_still_refused(
    tmp_path, monkeypatch, capsys
):
    """A listed child whose change dir is gone, with no archive and no
    merged status saved, is refused as before."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN)
    _git(e.project, "rm", "-rq", "specs/changes/e-a")
    _commit_all(e.project, "drop e-a")
    rc, out = e.refused(capsys)
    assert rc == 2
    assert "SPEC:NOT_READY:child e-a: its change dir" in out and "does not exist" in out
    _assert_nothing_written(e)


def test_a_child_saved_as_merged_whose_dir_is_gone_is_accepted(tmp_path, monkeypatch):
    """The saved status says merged and the archive is not in this checkout
    (pulled elsewhere, or archived under another date): accepted, with the
    saved dependencies."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN)
    assert e.run() == 3
    e.fakes.set_pr(pr_url("e-a"), "MERGED")
    assert e.run() == 3 and e.status()["a"]["status"] == "merged"
    _git(e.project, "rm", "-rq", "specs/changes/e-a")
    _commit_all(e.project, "e-a gone")
    rc = e.run()
    assert rc == 3 and e.status()["a"]["status"] == "merged"


# --- epic.json -----------------------------------------------------------------------


def test_an_epic_json_from_before_pm_spec_loads(tmp_path):
    """A file buildme's own split saved (state, hashes, plans, a child's repo,
    and a status with repo_park and slug) still loads; the extra keys are dropped."""
    old = {
        "name": "e",
        "state": "approved",
        "reason": "Three parts.",
        "hashes": {"split.md": "x"},
        "approved_at": "2026-10-13T12:00:00+00:00",
        "created_at": "2026-10-13T11:00:00+00:00",
        "children": [
            {
                "id": "a",
                "title": "A",
                "depends_on": [],
                "repo": "../other",
                "plan": "# Plan: A\n",
            },
            {"id": "b", "title": "B", "depends_on": ["a"], "repo": None, "plan": ""},
        ],
        "child_status": {
            "a": {
                "status": "merged",
                "pr": "https://x/pull/1",
                "repo_park": "",
                "slug": "o-r",
            }
        },
        "spent_usd": 1.5,
        "spend_complete": True,
    }
    (tmp_path / "epic.json").write_text(json.dumps(old))
    epic = store.load_epic(tmp_path)
    assert [(c.id, c.depends_on) for c in epic.children] == [("a", []), ("b", ["a"])]
    assert epic.child_status["a"].status == "merged" and epic.spent_usd == 1.5
    assert (
        "repo" not in epic.children[0].model_dump()
        and "slug" not in epic.child_status["a"].model_dump()
    )


def test_the_children_come_from_the_change_dirs_on_every_run(tmp_path, monkeypatch):
    """A saved epic.json's children are replaced by the change dirs' records;
    a child no longer listed loses its saved status, a listed one keeps it
    (here the merge step's park, which keeps a parked and so b skipped)."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN)
    e.dir.mkdir(parents=True)
    saved = store.Epic(
        name="e",
        children=[store.EpicChild(id="a"), store.EpicChild(id="gone")],
        child_status={
            "a": store.ChildStatus(status="parked", reason="kept", merge_park="kept"),
            "gone": store.ChildStatus(status="built"),
        },
    )
    store.save_epic(e.dir, saved)
    assert e.run() == 0
    data = json.loads((e.dir / "epic.json").read_text())
    assert data["children"] == [
        {"id": "a", "depends_on": []},
        {"id": "b", "depends_on": ["a"]},
    ]
    assert set(data["child_status"]) == {"a", "b"}
    assert (
        data["child_status"]["a"]["status"] == "parked"
        and data["child_status"]["b"]["status"] == "skipped"
    )


def test_a_build_of_the_same_change_in_another_project_is_not_this_child(tmp_path):
    """`build_pid` matches `--change` and `--project-dir`: the change names
    `e-a` in two projects are two children, and a fixer of the same change
    (`findings`) is not its build."""
    import os
    import signal
    import sys
    import time

    from build_pipeline.epic import coordinator

    def spawn(*argv: str) -> subprocess.Popen:
        stub = (
            tmp_path / "build_pipeline"
        )  # first on `-m`'s path, so no real build runs
        stub.mkdir(exist_ok=True)
        (stub / "__init__.py").write_text("")
        (stub / "__main__.py").write_text("import time\ntime.sleep(60)\n")
        return subprocess.Popen(
            [sys.executable, "-m", "build_pipeline", *argv],
            cwd=tmp_path,
            start_new_session=True,
        )

    here, there = tmp_path / "p", tmp_path / "q"
    other = spawn(
        "--session-id", "s", "build", "--change", "e-a", "--project-dir", str(there)
    )
    fixer = spawn(
        "--session-id", "s", "findings", "--change", "e-a", "--project-dir", str(here)
    )
    try:
        for _ in range(250):
            if (
                coordinator.build_pid("e-a", str(there)) == other.pid
                and coordinator._ps_pid("findings", f"--project-dir {here}")
                == fixer.pid
            ):
                break
            time.sleep(0.02)
        assert coordinator.build_pid("e-a", str(there)) == other.pid
        assert coordinator.build_pid("e-a", str(here)) is None
    finally:
        for p in (other, fixer):
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()


# --- spec issues and archived children (plan-pm-spec-gaps-2026-10-25.md) ------------


def test_a_child_with_open_spec_issues_is_refused_before_launch(
    tmp_path, monkeypatch, capsys
):
    """Item 6: an open `spec_issues` on the feature branch refuses the epic
    with the single build's `SPEC:ISSUES:<ref>:<path>` and exit 4, before the
    lock: nothing is written and no child is launched."""
    e = Epic(tmp_path, monkeypatch)
    child_dir = e.changes / "e-ui"
    (child_dir / "spec-issues.md").write_text("# Spec issues: e-ui\n\n## A\n\nvague\n")
    write_change_meta(child_dir, {"spec_issues": "spec-issues.md"})
    _commit_all(e.project, "spec issues")
    rc, out = e.refused(capsys)
    assert rc == 4
    lines = out.splitlines()
    assert lines[0] == "SPEC:ISSUES:origin/ENG-40-e:specs/changes/e-ui/spec-issues.md"
    assert lines[1] == "Run /multiplai-pm:pm-spec revise e-ui, then run the build again"
    _assert_nothing_written(e)


def test_an_archived_child_with_no_records_is_not_launched(tmp_path, monkeypatch):
    """Item 8, row S8: a merged, archived on the branch, and its records and
    saved status lost: it is merged, never launched again, and b launches."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN)
    assert e.run() == 3
    e.fakes.set_pr(pr_url("e-a"), "MERGED")
    _archive(e, "e-a")
    for a in e.build_records("a"):
        for p in a.path.parent.glob(a.path.name + ".*"):
            p.unlink()
    saved = json.loads((e.dir / "epic.json").read_text())
    saved["child_status"]["a"]["status"] = "built"
    (e.dir / "epic.json").write_text(json.dumps(saved))
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    assert e.build_records("a") == [] and len(e.build_records("b")) == 1
    assert e.status()["a"]["status"] == "merged"
