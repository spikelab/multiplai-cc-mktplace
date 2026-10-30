"""Row tests for the epic running without hand edits
(plan-factory-merged-2026-10-29.md, release R2c, items 24-27 and 30; rows
S21-S26 and S43).

Each case stops the step at its row's point (or applies the row's change),
resumes, and compares the result with the same scenario run without a stop
in a second temporary root, except where the row's "Resume does" says
otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from build_pipeline import git_ops, tdd_engine
from build_pipeline.config import BuildConfig, EpicSettings, GitToggles
from build_pipeline.epic import coordinator, records
from build_pipeline.epic import lock as epic_lock
from build_pipeline.models import AgentResult, BlockInfo, BuildPhase
from build_pipeline.spec_generator import reconcile_plan
from build_pipeline.state import BuildState, TDDState
from multiplai_core.specs import mark_ready

from .epic_row_helpers import (
    CallLog,
    EpicSplitAnswer,
    Fakes,
    approve,
    child,
    make_build,
    make_project,
    pr_url,
    run_coordinator,
    use_workspace,
)


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


# --- S25, S26: resume after RED continues the block (item 27, G33) -------------------


class Block:
    """A one-block build in TDD, in a repository at *root*, with agents that
    write a test, implement it and refactor it, and spies on the quality and
    RED gates."""

    def __init__(self, root: Path, monkeypatch):
        root.mkdir(parents=True)
        self.root, self.mp = root, monkeypatch
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        _git(root, "init", "-qb", "buildme/contract")
        _git(root, "config", "user.email", "row@example.invalid")
        _git(root, "config", "user.name", "Row Test")
        _git(root, "config", "commit.gpgsign", "false")
        self.config = config = BuildConfig(
            project_dir=root,
            change_name="contract",
            auto=False,
            git=GitToggles(worktree=False, push=False, pr="none"),
            test_command=f"{sys.executable} -m pytest -q -p no:cacheprovider",
        )
        config.specs_dir = root / "specs"
        cdir = config.change_dir
        (cdir / "requirements").mkdir(parents=True)
        for name, content in {
            "proposal.md": "## Capabilities\n- `value`\n",
            "requirements/value.md": "### Requirement: Value\n#### Scenario: Read\nWHEN read\nTHEN returns two\n",
            "implementation-plan.md": "# Design\nA constant",
            "use-cases.md": "# Use cases\nRead two",
            "unknowns.md": "# Unknowns\nNone",
            "tasks.md": "## 1. Value\nReturn two",
            "rubric.md": "# Rubric\nCorrect value",
        }.items():
            (cdir / name).write_text(content)
        (root / "app.py").write_text("value = 1\n")
        mark_ready(cdir)
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "accepted plan")
        config.pipeline_branch = "buildme/contract"
        state = BuildState(
            change_name="contract",
            tier="advanced",
            phase=BuildPhase.TDD_BUILD,
            branch=config.pipeline_branch,
            worktree_path=str(root),
            state_file=str(config.state_file_path()),
            tdd=TDDState(
                blocks=[BlockInfo(number=1, name="Value", description="Return two")],
                refactor_all_done=True,
                mutation_done=True,
                e2e_done=True,
                baseline_tests_pass=True,
            ),
        )
        reconcile_plan(config, state)
        state.spec_gen.record_step("plan_review", "passed")
        state.checkpoint(config.state_file_path())
        self.args = argparse.Namespace(
            change="contract",
            project_dir=str(root),
            auto=False,
            spec_only=False,
            skip_research=True,
            research_path="",
            session_id="",
            block=None,
        )
        self.calls: list[str] = []
        self.stop_at: str | None = None

    def agent(self, name: str, write: dict[str, str]):
        async def run(**kwargs):
            self.calls.append(name)
            if self.stop_at == name:
                self.stop_at = None
                raise asyncio.CancelledError()
            for rel, text in write.items():
                (self.root / rel).write_text(text)
            return AgentResult(success=True, output="STATUS: DONE\n")

        return run

    def use(self) -> None:
        mp = self.mp
        mp.setattr(
            tdd_engine,
            "run_test_writer",
            self.agent(
                "write",
                {
                    "test_value.py": "from app import value\n\n\ndef test_value():\n    assert value == 2\n"
                },
            ),
        )
        mp.setattr(
            tdd_engine,
            "run_implementer",
            self.agent("implement", {"app.py": "value = 2\n"}),
        )
        mp.setattr(
            tdd_engine,
            "run_refactorer",
            self.agent("refactor", {"app.py": "value = 1 + 1\n"}),
        )
        for gate in ("_enforce_test_quality", "_enforce_red_gate"):
            real = getattr(tdd_engine, gate)

            async def spy(*a, _real=real, _name=gate, **kw):
                self.calls.append(_name)
                return await _real(*a, **kw)

            mp.setattr(tdd_engine, gate, spy)
        mp.setattr(
            tdd_engine, "_run_integration_and_review", AsyncMock(return_value=True)
        )

    async def run(self) -> int:
        self.use()
        return await tdd_engine.run_tdd_engine(self.config, self.args)

    def result(self) -> dict:
        done = BuildState.load(
            self.config.state_file_path().with_name(".build-state.complete.json")
        )
        block = done.tdd.blocks[0]
        return {
            "files": {
                n: (self.root / n).read_text() for n in ("app.py", "test_value.py")
            },
            "commits": _git(self.root, "log", "--format=%s").splitlines(),
            "verified": bool(done.verification and done.verification["passed"]),
            "block": (block.status, bool(block.test_commit), bool(block.impl_commit)),
        }


@pytest.mark.asyncio
async def test_s25_resume_with_test_and_implementation_committed_starts_at_refactor(
    tmp_path, monkeypatch
):
    never = Block(tmp_path / "never", monkeypatch)
    assert await never.run() == 0
    expected = never.result()

    stopped = Block(tmp_path / "stopped", monkeypatch)
    stopped.stop_at = "refactor"
    with pytest.raises(asyncio.CancelledError):
        await stopped.run()
    saved = BuildState.load(stopped.config.state_file_path()).tdd.blocks[0]
    assert saved.test_commit and saved.impl_commit and not saved.refactor_commit
    assert _git(stopped.root, "rev-parse", "HEAD") == saved.impl_commit
    before = list(stopped.calls)
    assert await stopped.run() == 0
    # Starts at REFACTOR: no test writer, gate or implementer again.
    assert stopped.calls[len(before) :] == ["refactor"]
    assert stopped.result() == expected


@pytest.mark.asyncio
async def test_s25_a_block_past_its_refactor_goes_on_to_the_review(
    tmp_path, monkeypatch
):
    """A commit after the implementation (the refactor's) means the block
    went past the refactor: resume runs neither it nor anything before it."""
    b = Block(tmp_path / "b", monkeypatch)
    b.use()
    review = AsyncMock(side_effect=[asyncio.CancelledError(), True])
    monkeypatch.setattr(tdd_engine, "_run_integration_and_review", review)
    with pytest.raises(asyncio.CancelledError):
        await tdd_engine.run_tdd_engine(b.config, b.args)
    assert BuildState.load(b.config.state_file_path()).tdd.blocks[0].refactor_commit
    before = list(b.calls)
    assert await tdd_engine.run_tdd_engine(b.config, b.args) == 0
    assert b.calls[len(before) :] == []
    assert review.await_count == 2


@pytest.mark.asyncio
async def test_s26_resume_with_only_the_test_commit_runs_the_implementer(
    tmp_path, monkeypatch
):
    never = Block(tmp_path / "never", monkeypatch)
    assert await never.run() == 0
    expected = never.result()

    stopped = Block(tmp_path / "stopped", monkeypatch)
    stopped.stop_at = "implement"
    with pytest.raises(asyncio.CancelledError):
        await stopped.run()
    saved = BuildState.load(stopped.config.state_file_path()).tdd.blocks[0]
    assert saved.test_commit and saved.red_evidence and not saved.impl_commit
    before = list(stopped.calls)
    assert await stopped.run() == 0
    # No new test writer, and no quality or RED gate again.
    assert stopped.calls[len(before) :] == ["implement", "refactor"]
    assert stopped.result() == expected


# --- S21-S24: the coordinator (items 24-26) -------------------------------------------

TWO = EpicSplitAnswer(
    single=False, reason="Two parts.", children=[child("a"), child("b")]
)
BRANCH = "ENG-40-e"  # the feature branch `approve` pushes


class Root:
    """One root: project, workspace, the fakes on PATH, an approved two-child epic."""

    def __init__(self, root: Path, monkeypatch, steps: dict, **epic):
        self.root, self.mp = root, monkeypatch
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.fakes = Fakes(root, monkeypatch, steps)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=1, merge="manual", **epic)
        approve(self.config, TWO, CallLog(root / "split.log"))
        self.dir = self.data / "buildme" / "epics" / "o-r--e"

    def run(self, tick=None, **epic) -> int:
        use_workspace(self.mp, self.root)
        self.fakes.use(self.mp)
        if epic:
            self.config.epic = EpicSettings(**{**self.config.epic.__dict__, **epic})
        self.mp.setattr(coordinator, "_tick", tick or (lambda coord: None))
        return run_coordinator(self.config)

    def child_status(self) -> dict:
        return json.loads((self.dir / "epic.json").read_text())["child_status"]

    def status(self) -> dict:
        return {k: v["status"] for k, v in self.child_status().items()}

    def launches(self, cid: str) -> list[list[str]]:
        return [
            c["argv"]
            for c in CallLog(self.root / "calls.log").calls()
            if c.get("kind") == "build"
            and c["argv"][c["argv"].index("--change") + 1] == f"e-{cid}"
        ]

    def builds(self, cid: str) -> list[records.Attempt]:
        return [
            a
            for a in records.attempts(self.dir)
            if a.kind == "build" and a.child == cid
        ]

    def worktree(self, cid: str) -> Path:
        return self.data.parents[1] / ".worktrees" / f"buildme-e-{cid}"

    def open_pr_by_hand(self, cid: str) -> str:
        """What a build of the child run by hand leaves: its branch pushed
        and a PR from it into the feature branch."""
        url = pr_url(f"e-{cid}")
        prs_file = self.root / "prs.json"
        prs = json.loads(prs_file.read_text()) if prs_file.exists() else {}
        prs[url] = {"branch": f"buildme/e-{cid}", "base": BRANCH, "state": "OPEN"}
        prs_file.write_text(json.dumps(prs))
        _git(self.worktree(cid), "push", "-q", "-u", "origin", f"buildme/e-{cid}")
        return url

    def coord(self) -> coordinator.Coordinator:
        from build_pipeline.epic import store

        return coordinator.Coordinator(
            self.config, None, self.dir, store.load_epic(self.dir), BRANCH
        )


SPLIT = {"marker": "SPLIT:REQUIRED:/x/split-proposal.md", "exit": 3}


def test_s21_a_child_parked_on_split_required_goes_back_to_pending(
    tmp_path, monkeypatch
):
    """Parked by a run without `epic.accept_size`; the next run with it
    launches the child again with `--accept-size`, as a run that had the key
    from the start launches it."""
    stopped = Root(tmp_path / "stopped", monkeypatch, {"a": [SPLIT, {}], "b": [{}]})
    assert stopped.run() == 3
    assert stopped.child_status()["a"]["status"] == "parked"
    assert stopped.child_status()["a"]["reason"].startswith("SPLIT:REQUIRED:")
    assert stopped.run() == 3  # without the key it stays parked
    assert len(stopped.launches("a")) == 1
    assert stopped.run(accept_size=True) == 3
    relaunch = stopped.launches("a")[-1]
    assert "--accept-size" in relaunch and len(stopped.launches("a")) == 2

    never = Root(
        tmp_path / "never", monkeypatch, {"a": [{}], "b": [{}]}, accept_size=True
    )
    assert never.run() == 3
    assert stopped.status() == never.status() == {"a": "built", "b": "built"}
    assert relaunch == [
        arg.replace("/never/", "/stopped/") for arg in never.launches("a")[-1]
    ]


def test_s22_a_failed_child_with_a_pr_opened_outside_the_epic_is_built(
    tmp_path, monkeypatch
):
    stopped = Root(tmp_path / "stopped", monkeypatch, {"a": [{"exit": 1}], "b": [{}]})
    assert stopped.run(max_child_attempts=1) == 1
    assert stopped.child_status()["a"]["status"] == "failed"
    record = stopped.builds("a")[-1].result_path.read_text()
    url = stopped.open_pr_by_hand("a")
    assert stopped.run() == 3
    assert stopped.child_status()["a"] == {
        **stopped.child_status()["a"],
        "status": "built",
        "pr": url,
    }
    assert len(stopped.launches("a")) == 1  # no launch
    assert stopped.builds("a")[-1].result_path.read_text() == record  # not rewritten

    never = Root(tmp_path / "never", monkeypatch, {"a": [{}], "b": [{}]})
    assert never.run() == 3
    assert stopped.status() == never.status()
    assert stopped.child_status()["a"]["pr"] == never.child_status()["a"]["pr"]


def test_s22_a_review_park_stays_parked_with_a_pr_open(tmp_path, monkeypatch):
    """A park with open findings is the review's record, not the build's: the
    PR's being open does not unpark it."""
    r = Root(tmp_path / "r", monkeypatch, {"a": [{"exit": 1}], "b": [{}]})
    assert r.run(max_child_attempts=1) == 1
    r.open_pr_by_hand("a")
    why = "the fixer ended and left 1 blocker(s): MEDIUM f1 a.py:1 x"
    records.finish(
        records.start(r.dir, "review", "a", ["review"]),
        0,
        interrupted=False,
        extra={"parked": why},
    )
    assert r.run() in (1, 3)
    assert r.child_status()["a"]["status"] == "parked"
    assert r.child_status()["a"]["reason"] == why
    assert len(r.launches("a")) == 1


def test_s23_a_failed_pr_lookup_leaves_the_child_unknown_and_unlaunched(
    tmp_path, monkeypatch
):
    r = Root(tmp_path / "r", monkeypatch, {"a": [{"exit": 1}, {}], "b": [{}]})
    assert r.run(max_child_attempts=1) == 1
    (r.root / "gh-fails").write_text("")
    assert r.run() == 1
    status = r.child_status()["a"]
    assert status["status"] == "unknown"
    assert "cannot tell whether a PR is open from its branch" in status["reason"]
    assert len(r.launches("a")) == 1
    (r.root / "gh-fails").unlink()
    assert r.run() == 3  # a lookup answers (no PR): launched again
    assert len(r.launches("a")) == 2
    assert r.status()["a"] == "built"


def _write_state(r: Root, cid: str, *, blocked: bool = False) -> None:
    """A checkpoint in the child's worktree at the plan revision its files
    have now, as a build that planned it leaves; *blocked*: its planning check
    blocked at that revision."""
    from build_pipeline import spec_generator
    from build_pipeline.state import SpecGenState

    coord = r.coord()
    config = coord._child_config(coord.children[cid])
    hashes = spec_generator._plan_hashes(config)
    sg = SpecGenState(revision=spec_generator._revision(hashes), artifact_hashes=hashes)
    if blocked:
        sg.record_step(
            "plan_review",
            "blocked",
            "findings",
            blocking_findings="F1",
            blocked_revision=sg.step_revision("plan_review"),
        )
    state = BuildState(change_name=f"e-{cid}", tier="standard", spec_gen=sg)
    config.state_file_path().parent.mkdir(parents=True, exist_ok=True)
    config.state_file_path().write_text(state.model_dump_json())


def test_s24_a_child_failing_twice_in_a_run_is_not_launched_a_third_time(
    tmp_path, monkeypatch
):
    """Two failures in one run: no third launch in that run. Nothing shows the
    next build would fail the same way, so the next run launches it again with
    nothing changed."""
    fails = {"a": [{"exit": 1}] * 2 + [{}], "b": [{}]}
    r = Root(tmp_path / "r", monkeypatch, fails)
    assert r.run() == 1
    assert len(r.launches("a")) == 2  # relaunched within the run, once
    assert [b.result["exit_code"] for b in r.builds("a")] == [1, 1]
    _write_state(r, "a")  # same plan revision, same specs
    r.run()
    assert len(r.launches("a")) == 3
    assert r.status()["a"] == "built"


def test_s24_a_child_whose_plan_check_is_blocked_is_not_relaunched(
    tmp_path, monkeypatch
):
    """A failure whose checkpoint says its planning check is blocked at the
    plan revision its files have: no relaunch in the run, nor in the next
    one, until the plan changes."""
    r = Root(
        tmp_path / "r", monkeypatch, {"a": [{"exit": 1, "block": True}, {}], "b": [{}]}
    )

    def tick(coord):
        wt = r.worktree("a")
        gate = r.root / "release" / "a"
        if wt.is_dir() and not gate.exists():
            _write_state(r, "a", blocked=True)
            gate.parent.mkdir(exist_ok=True)
            gate.write_text("")

    assert r.run(tick) == 1
    assert len(r.launches("a")) == 1
    assert coordinator.PLAN_BLOCKED in r.child_status()["a"]["reason"]
    assert r.run() == 1
    assert len(r.launches("a")) == 1
    # The plan is edited: the check would run again, so the child is launched.
    plan = r.worktree("a") / "specs" / "changes" / "e-a" / "tasks.md"
    plan.write_text("## 1. Edited\nA fix for F1\n")
    assert r.run() == 3
    assert len(r.launches("a")) == 2


# --- S43: planning files stay off product branches (item 30) --------------------------

PLANNING = (
    "specs/changes/e-a/facts.json",
    "specs/changes/e-a/codebase-analysis.md",
    "specs/changes/e-a/plan-edits-1.diff",
)


def _child_repo(root: Path, *, committed: bool) -> tuple[Path, str]:
    """A child's worktree after its base commit: with *committed*, a version
    before item 30 that committed the planning files."""
    root.mkdir(parents=True)
    _git(root, "init", "-qb", "buildme/e-a")
    _git(root, "config", "user.email", "row@example.invalid")
    _git(root, "config", "user.name", "Row Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "specs" / "changes" / "e-a").mkdir(parents=True)
    (root / "specs" / "changes" / "e-a" / "specs.md").write_text("# specs\n")
    (root / "app").mkdir()
    (root / "app" / "facts.json").write_text('{"app": 1}\n')
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    if committed:
        for rel in PLANNING:
            (root / rel).write_text("old\n")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "codebase analysis (before item 30)")
    return root, base


def _planning_run(root: Path) -> None:
    """What a resumed build does: writes the planning files again, changes
    an application file of the same name, and commits a stage."""
    for rel in PLANNING:
        (root / rel).write_text("new\n")
    (root / "app" / "facts.json").write_text('{"app": 2}\n')
    git_ops.commit_tree(root, "block 1: impl")


def test_s43_committed_planning_files_stay_as_committed_with_no_delete_commit(
    tmp_path,
):
    stopped, _ = _child_repo(tmp_path / "stopped", committed=True)
    head = _git(stopped, "rev-parse", "HEAD")
    _planning_run(stopped)
    # No delete commit: the files are in HEAD as committed before.
    for rel in PLANNING:
        assert _git(stopped, "show", f"HEAD:{rel}") == "old"
        assert (stopped / rel).read_text() == "new\n"  # the build's copy on disk
    assert _git(stopped, "diff", "--name-only", head, "HEAD").splitlines() == [
        "app/facts.json"
    ]
    assert git_ops.tree_is_clean(stopped)

    never, base = _child_repo(tmp_path / "never", committed=False)
    _planning_run(never)
    # Done means 12: a child built now has no planning file on its branch.
    assert _git(never, "diff", "--name-only", base, "HEAD").splitlines() == [
        "app/facts.json"
    ]
    for rel in PLANNING:
        assert (never / rel).read_text() == (stopped / rel).read_text()
    assert _git(never, "show", "HEAD:app/facts.json") == _git(
        stopped, "show", "HEAD:app/facts.json"
    )


# --- item 29: a child built outside the epic gets the epic's branch (G34) -------------


def _component(tmp_path: Path, branch: str = "") -> BuildConfig:
    from multiplai_core.specs import write_change_meta

    config = BuildConfig(project_dir=tmp_path, change_name="e/a")
    config.specs_dir = tmp_path / "specs"
    config.feature_branch = branch
    parent = config.specs_dir / "changes" / "e"
    config.change_dir.mkdir(parents=True)
    write_change_meta(parent, {"schema": "spec-driven", "branch": BRANCH})
    return config


def test_a_component_built_by_hand_builds_as_the_epics_child(tmp_path):
    from build_pipeline import orchestrator

    config = _component(tmp_path)
    assert orchestrator._as_epic_child(config) is None
    assert (config.feature_branch, config.epic_child) == (BRANCH, True)
    same = _component(tmp_path / "same", BRANCH)
    assert orchestrator._as_epic_child(same) is None
    assert (same.feature_branch, same.epic_child) == (BRANCH, True)


def test_another_branch_for_a_component_is_refused_with_why(tmp_path, capsys):
    from build_pipeline import orchestrator

    config = _component(tmp_path, "production")
    assert orchestrator._as_epic_child(config) == 2
    err = capsys.readouterr().err
    assert "--branch production is not the feature branch of the epic e" in err
    assert f"pass --branch {BRANCH}" in err
    assert not config.epic_child


def test_a_change_that_is_not_a_component_is_left_alone(tmp_path):
    from build_pipeline import orchestrator

    config = BuildConfig(project_dir=tmp_path, change_name="e")
    config.specs_dir = tmp_path / "specs"
    assert orchestrator._as_epic_child(config) is None
    assert (config.feature_branch, config.epic_child) == ("", False)
