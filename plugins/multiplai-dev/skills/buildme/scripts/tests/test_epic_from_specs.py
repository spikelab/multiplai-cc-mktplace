"""The epic entry fed by pm-spec (plan-pm-spec-split-2026-10-21.md, Decision
11): `/buildme <parent>` reads the parent's `children` and each child's
`depends_on` from the change dirs, refuses `SPEC:NOT_READY` (exit 2, nothing
written) unless every listed child is ready and every change dir committed,
and runs the coordinator. A child with no dependency branches from the local
commit that holds the specs; one with dependencies from origin's default
branch once that holds its change dir as the specs commit does."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from multiplai_core.specs import mark_ready, write_change_meta

from build_pipeline.config import EpicSettings
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic import phase, records, store

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
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _commit_all(project: Path, msg: str) -> str:
    _git(project, "add", "specs")
    _git(project, "-c", "commit.gpgsign=false", "commit", "-qm", msg)
    return _git(project, "rev-parse", "HEAD")


class Epic:
    def __init__(self, root: Path, monkeypatch, answer=THREE, steps=None):
        self.root = root
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.fakes = Fakes(root, monkeypatch, steps)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=2)
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
        return [a for a in records.attempts(self.dir) if a.child == cid and a.kind == "build"]


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


def test_a_child_that_names_the_parent_but_is_not_listed_is_not_built(tmp_path, monkeypatch):
    """The list, not a scan for `parent`, says which children the epic has."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN)
    stray = e.changes / "e-old"
    stray.mkdir()
    write_change_meta(stray, {"parent": "e", "depends_on": []})
    _commit_all(e.project, "a dropped child")
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert set(e.status()) == {"a", "b"}
    assert [json.loads(x)["argv"][json.loads(x)["argv"].index("--change") + 1]
            for x in (tmp_path / "calls.log").read_text().splitlines()] == ["e-a"]


# --- refusals: SPEC:NOT_READY, exit 2, nothing written -----------------------------

def test_a_listed_child_not_ready_is_refused(tmp_path, monkeypatch, capsys):
    """(a) A requirement edited after `mark_ready`, and committed: the child's
    specs are no longer the ones marked ready."""
    e = Epic(tmp_path, monkeypatch)
    req = e.changes / "e-ui" / "requirements" / "cap-ui.md"
    req.write_text(req.read_text() + "\n### Requirement: More\nThe system SHALL do more.\n\n"
                   "#### Scenario: more\n- **WHEN** it runs\n- **THEN** more is done\n")
    _commit_all(e.project, "edit after ready")
    rc, out = e.refused(capsys)
    assert rc == 2
    lines = out.splitlines()
    assert lines[0].startswith("SPEC:NOT_READY:child e-ui: the specs changed after they were marked ready")
    assert lines[1] == "Run /multiplai-pm:pm-spec e-ui first"
    _assert_nothing_written(e)


def test_the_parent_dir_uncommitted_is_refused(tmp_path, monkeypatch, capsys):
    """(b)"""
    e = Epic(tmp_path, monkeypatch)
    (e.config.change_dir / "notes.md").write_text("not committed\n")
    rc, out = e.refused(capsys)
    assert rc == 2
    assert out.startswith(f"SPEC:NOT_READY:{e.config.change_dir} has uncommitted changes (specs/changes/e/notes.md); "
                          "the children branch from the commit that holds the specs")
    assert "Run /multiplai-pm:pm-spec e first" in out
    _assert_nothing_written(e)


def test_a_child_dir_uncommitted_is_refused(tmp_path, monkeypatch, capsys):
    """(c) Ready on disk, but not in the commit the child would branch from."""
    e = Epic(tmp_path, monkeypatch)
    use_cases = e.changes / "e-api" / "use-cases.md"
    use_cases.write_text(use_cases.read_text() + "More.\n")
    mark_ready(e.changes / "e-api", now=READY_AT)
    rc, out = e.refused(capsys)
    assert rc == 2
    assert out.startswith(f"SPEC:NOT_READY:{e.changes / 'e-api'} has uncommitted changes (")
    assert "specs/changes/e-api/use-cases.md" in out and "Run /multiplai-pm:pm-spec e-api first" in out
    _assert_nothing_written(e)


def test_depends_on_naming_an_unlisted_child_is_refused(tmp_path, monkeypatch, capsys):
    """(d)"""
    e = Epic(tmp_path, monkeypatch)
    write_change_meta(e.changes / "e-docs", {"depends_on": ["e-api", "e-ghost"]})
    _commit_all(e.project, "a ghost dependency")
    rc, out = e.refused(capsys)
    assert rc == 2
    assert out.splitlines()[0] == ("SPEC:NOT_READY:child e-docs depends on e-ghost, which the parent e does "
                                   "not list as a child")
    _assert_nothing_written(e)


@pytest.mark.parametrize("damage, reason", [
    (lambda e: write_change_meta(e.config.change_dir, {"children": ["e-api", "other-ui"]}),
     "the parent e's children list names 'other-ui', which is not e-<id>"),
    (lambda e: write_change_meta(e.config.change_dir, {"children": ["e-api", "e-ui", "e-gone"]}),
     "child e-gone: its change dir"),
    (lambda e: (write_change_meta(e.changes / "e-api", {"depends_on": ["e-docs"]})),
     "the children's depends_on have a cycle: "),
], ids=["name", "missing-dir", "cycle"])
def test_a_split_buildme_cannot_build_from_is_refused(tmp_path, monkeypatch, capsys, damage, reason):
    e = Epic(tmp_path, monkeypatch)
    damage(e)
    _commit_all(e.project, "damage")
    rc, out = e.refused(capsys)
    assert rc == 2 and out.startswith(f"SPEC:NOT_READY:{reason}")
    assert "Run /multiplai-pm:pm-spec e first" in out
    _assert_nothing_written(e)


# --- the bases ---------------------------------------------------------------------

def test_a_dependant_branches_from_origin_after_its_dependency_merged(tmp_path, monkeypatch):
    """(f) b waits for a; once a's PR merged (carrying the specs commit to
    origin), b branches from origin's default branch, which holds its change
    dir as the specs commit does. The parent's change dir is left committed."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert e.status()["b"]["status"] == "waiting" and e.build_records("b") == []
    e.fakes.set_pr(pr_url("e-a"), "MERGED")
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    origin_main = _git(tmp_path / "o" / "r.git", "rev-parse", "main")
    argv = e.build_records("b")[0].started["argv"]
    assert argv[argv.index("--base-ref") + 1] == origin_main != e.specs
    assert (tmp_path / "bases" / "b").read_text() == origin_main
    rel = "specs/changes/e-b"
    assert _git(e.project, "rev-parse", f"{origin_main}:{rel}") == _git(e.project, "rev-parse", f"{e.specs}:{rel}")
    assert _git(e.project, "status", "--porcelain", "--", "specs") == ""


def test_a_dependant_whose_base_lacks_its_specs_is_not_launched(tmp_path, monkeypatch):
    """(g) After a merged, pm-spec revised b's specs locally: origin's default
    branch holds the old version, so b is not launched; it is `failed` with
    the reason, and nothing is recorded for it."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN)
    assert e.run() == 3
    e.fakes.set_pr(pr_url("e-a"), "MERGED")
    use_cases = e.changes / "e-b" / "use-cases.md"
    use_cases.write_text(use_cases.read_text() + "Revised.\n")
    mark_ready(e.changes / "e-b", now=READY_AT)
    revised = _commit_all(e.project, "specs(e-b): revised")
    assert e.run() == 1
    assert e.build_records("b") == []
    b = e.status()["b"]
    assert b["status"] == "failed"
    assert "does not hold specs/changes/e-b as the commit with the ready specs" in b["reason"]
    assert revised[:12] in b["reason"]


# --- epic.json -----------------------------------------------------------------------

def test_an_epic_json_from_before_pm_spec_loads(tmp_path):
    """A file buildme's own split saved (state, hashes, plans, a child's repo,
    and a status with repo_park and slug) still loads; the extra keys are dropped."""
    old = {"name": "e", "state": "approved", "reason": "Three parts.", "hashes": {"split.md": "x"},
           "approved_at": "2026-10-13T12:00:00+00:00", "created_at": "2026-10-13T11:00:00+00:00",
           "children": [{"id": "a", "title": "A", "depends_on": [], "repo": "../other", "plan": "# Plan: A\n"},
                        {"id": "b", "title": "B", "depends_on": ["a"], "repo": None, "plan": ""}],
           "child_status": {"a": {"status": "merged", "pr": "https://x/pull/1", "repo_park": "", "slug": "o-r"}},
           "spent_usd": 1.5, "spend_complete": True}
    (tmp_path / "epic.json").write_text(json.dumps(old))
    epic = store.load_epic(tmp_path)
    assert [(c.id, c.depends_on) for c in epic.children] == [("a", []), ("b", ["a"])]
    assert epic.child_status["a"].status == "merged" and epic.spent_usd == 1.5
    assert "repo" not in epic.children[0].model_dump() and "slug" not in epic.child_status["a"].model_dump()


def test_the_children_come_from_the_change_dirs_on_every_run(tmp_path, monkeypatch):
    """A saved epic.json's children are replaced by the change dirs' records;
    a child no longer listed loses its saved status, a listed one keeps it
    (here the merge step's park, which keeps a parked and so b skipped)."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN)
    e.dir.mkdir(parents=True)
    saved = store.Epic(name="e", children=[store.EpicChild(id="a"), store.EpicChild(id="gone")],
                       child_status={"a": store.ChildStatus(status="parked", reason="kept", merge_park="kept"),
                                     "gone": store.ChildStatus(status="built")})
    store.save_epic(e.dir, saved)
    assert e.run() == 0
    data = json.loads((e.dir / "epic.json").read_text())
    assert data["children"] == [{"id": "a", "depends_on": []}, {"id": "b", "depends_on": ["a"]}]
    assert set(data["child_status"]) == {"a", "b"}
    assert data["child_status"]["a"]["status"] == "parked" and data["child_status"]["b"]["status"] == "skipped"


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
        stub = tmp_path / "build_pipeline"      # first on `-m`'s path, so no real build runs
        stub.mkdir(exist_ok=True)
        (stub / "__init__.py").write_text("")
        (stub / "__main__.py").write_text("import time\ntime.sleep(60)\n")
        return subprocess.Popen([sys.executable, "-m", "build_pipeline", *argv], cwd=tmp_path,
                                start_new_session=True)

    here, there = tmp_path / "p", tmp_path / "q"
    other = spawn("--session-id", "s", "build", "--change", "e-a", "--project-dir", str(there))
    fixer = spawn("--session-id", "s", "findings", "--change", "e-a", "--project-dir", str(here))
    try:
        for _ in range(250):
            if (coordinator.build_pid("e-a", str(there)) == other.pid
                    and coordinator._ps_pid("findings", f"--project-dir {here}") == fixer.pid):
                break
            time.sleep(0.02)
        assert coordinator.build_pid("e-a", str(there)) == other.pid
        assert coordinator.build_pid("e-a", str(here)) is None
    finally:
        for p in (other, fixer):
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
