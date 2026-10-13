"""Epic mode, PR B1: the approved build runs the children, each as its own
`python -m build_pipeline build` subprocess (plan-buildme-epic-mode-2026-10-12.md,
items 11-17). The build is a fake first on PATH that prints the real markers;
`gh` is a fake that reads `<root>/prs.json`."""

from __future__ import annotations

import copy
import json
import subprocess
import time
from pathlib import Path

import pytest

from build_pipeline.config import BuildConfig, EpicSettings
from build_pipeline.epic import coordinator, records, store
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic.split import EpicSplitAnswer
from build_pipeline.state import BuildState

from .epic_row_helpers import (
    THREE,
    CallLog,
    Fakes,
    FakeClock,
    approve,
    child,
    make_build,
    make_project,
    pr_url,
    run_coordinator,
    use_workspace,
)

INDEPENDENT = EpicSplitAnswer(single=False, reason="Three parts.",
                              children=[child("a"), child("b"), child("c")])


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


class Epic:
    """One root: project, workspace, the fakes on PATH and an approved split."""

    def __init__(self, root: Path, monkeypatch, answer=THREE, steps=None, parallel=2):
        self.root = root
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.fakes = Fakes(root, monkeypatch, steps)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=parallel)
        approve(self.config, answer, CallLog(root / "split.log"))
        self.dir = self.data / "buildme" / "epics" / "o-r--e"

    def run(self, **over) -> int:
        return run_coordinator(self.config, **over)

    def launches(self) -> list[str]:
        """The child id of each launch, in order."""
        out = []
        for line in (self.root / "calls.log").read_text().splitlines() if (self.root / "calls.log").exists() else []:
            argv = json.loads(line)["argv"]
            out.append(Path(argv[argv.index("--plan-file") + 1]).stem)
        return out

    def status(self) -> dict:
        return {k: v["status"] for k, v in json.loads((self.dir / "epic.json").read_text())["child_status"].items()}

    def reasons(self) -> dict:
        return {k: v["reason"] for k, v in json.loads((self.dir / "epic.json").read_text())["child_status"].items()}


# --- launching, in dependency order ----------------------------------------------

def test_children_launch_in_dependency_order_and_wait_for_a_merge(tmp_path, monkeypatch, capsys):
    e = Epic(tmp_path, monkeypatch)
    assert e.run() == 3
    out = capsys.readouterr().out
    assert sorted(e.launches()) == ["api", "ui"]
    assert f"EPIC:MERGE_WAITING:{pr_url('e-api')},{pr_url('e-ui')}" in out
    assert e.status() == {"api": "built", "ui": "built", "docs": "waiting"}

    e.fakes.set_pr(pr_url("e-api"), "MERGED")
    assert e.run() == 3                       # ui is still open
    assert sorted(e.launches()) == ["api", "ui"]
    e.fakes.set_pr(pr_url("e-ui"), "MERGED")
    assert e.run() == 0
    assert e.launches()[-1] == "docs" and len(e.launches()) == 3
    assert e.status() == {"api": "merged", "ui": "merged", "docs": "built"}


def test_a_merged_child_launches_nothing(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT)
    assert e.run() == 0
    for c in "abc":
        e.fakes.set_pr(pr_url(f"e-{c}"), "MERGED")
    assert e.run() == 0
    assert sorted(e.launches()) == ["a", "b", "c"]
    assert e.status() == {"a": "merged", "b": "merged", "c": "merged"}


def test_the_parallel_limit_holds(tmp_path, monkeypatch):
    block = [{"block": True}]
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT, steps={"a": block, "b": block, "c": block})
    seen = []

    def tick(coord):
        running = [a for a in records.attempts(e.dir) if a.result is None]
        seen.append(len(running))
        for a in running:
            e.fakes.release(a.child)

    monkeypatch.setattr(coordinator, "_tick", tick)
    assert e.run() == 0
    assert max(seen) == 2 and sorted(e.launches()) == ["a", "b", "c"]


def test_one_start_at_a_time(tmp_path, monkeypatch):
    """The next child starts only once the last one printed WORKTREE: (or exited)."""
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT)
    starts = []
    real = coordinator._spawn

    def spawn(argv, log_path):
        if starts:
            assert "WORKTREE:" in starts[-1].read_text()
        starts.append(log_path)
        return real(argv, log_path)

    monkeypatch.setattr(coordinator, "_spawn", spawn)
    assert e.run() == 0
    assert len(starts) == 3


def test_a_parked_child_skips_its_dependants(tmp_path, monkeypatch, capsys):
    e = Epic(tmp_path, monkeypatch, steps={"api": [{"marker": "SPLIT:REQUIRED:/x/plan.md"}]})
    assert e.run() == 0
    assert e.status() == {"api": "parked", "ui": "built", "docs": "skipped"}
    assert e.reasons()["api"].startswith("SPLIT:REQUIRED")
    assert "api" in e.reasons()["docs"]
    assert e.run() == 0                       # stays parked: nothing relaunched
    assert sorted(e.launches()) == ["api", "ui"]


def test_exit_0_without_a_pr_parks(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT, steps={"a": [{"no_pr": True}]})
    assert e.run() == 0
    assert e.status()["a"] == "parked"
    assert e.reasons()["a"] == "no PR opened"


@pytest.mark.parametrize("key", ["worktree", "push"])
def test_children_never_build_in_place(tmp_path, monkeypatch, capsys, key):
    """A specs/config.yaml that turns off a child's own worktree or its push
    is refused before anything is read, launched or written (B1.S23)."""
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT)
    yaml_path = e.project / "specs" / "config.yaml"
    yaml_path.write_text(f"git:\n  {key}: false\n")
    assert e.run() == 2
    assert f"git.{key}" in capsys.readouterr().err
    assert e.launches() == [] and not (e.dir / "agents").exists()
    yaml_path.write_text("git:\n  worktree: true\n")
    assert e.run() == 0 and sorted(e.launches()) == ["a", "b", "c"]


def test_a_failure_is_relaunched_once_per_invocation(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT, steps={"a": [{"exit": 1}]})
    assert e.run() == 1
    assert e.launches().count("a") == 2 and e.status()["a"] == "failed"
    assert e.run() == 1
    assert e.launches().count("a") == 3


def test_a_child_alive_after_the_stop_wait_keeps_no_result(tmp_path, monkeypatch):
    """Ctrl-C, and a child ignores it past STOP_WAIT: no result, so the next
    run adopts it instead of relaunching it. Then Ctrl-C while `ps` fails for
    that adopted child: still no result."""
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT, parallel=1,
             steps={"a": [{"block": True, "ignore_signals": True}]})
    monkeypatch.setattr(coordinator, "STOP_WAIT", 1.0)
    real_pid = coordinator.build_pid

    def ctrl_c(coord):
        raise KeyboardInterrupt

    def ps_fails_then_ctrl_c(coord):
        def fail(plan):
            raise coordinator.ReadFailed("ps failed")
        monkeypatch.setattr(coordinator, "build_pid", fail)
        raise KeyboardInterrupt

    try:
        monkeypatch.setattr(coordinator, "_tick", ctrl_c)
        assert e.run() == 130
        assert [a.result for a in records.attempts(e.dir)] == [None]
        monkeypatch.setattr(coordinator, "_tick", ps_fails_then_ctrl_c)
        assert e.run() == 130
        assert [a.result for a in records.attempts(e.dir)] == [None] and e.launches() == ["a"]
    finally:
        e.fakes.release("a")
        plan = e.dir / "plans" / "a.md"
        for _ in range(500):
            if real_pid(plan) is None:
                break
            time.sleep(0.02)


# --- the argv ------------------------------------------------------------------

def test_the_argv_holds_the_flags_and_absolute_paths(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT)
    (e.config.change_dir / "interview.md").write_text("# Interview\n")
    (e.config.change_dir / "research.md").write_text("# Research\n\nNot needed.\n")
    assert e.run() == 0
    argv = records.attempts(e.dir)[0].started["argv"]
    assert argv[:6] == ["python", "-m", "build_pipeline", "--session-id", "s", "build"]
    for flag in ("--auto", "--trust-repo", "--no-epic-split", "--pr-ready"):
        assert flag in argv
    assert argv[argv.index("--mode") + 1] == "brief"
    assert argv[argv.index("--change") + 1] == "e-a"
    assert argv[argv.index("--plan-file") + 1] == str(e.dir / "plans" / "a.md")
    assert argv[argv.index("--project-dir") + 1] == str(e.project)
    assert argv[argv.index("--context-files") + 1] == str(e.config.change_dir / "interview.md")
    # The parent's research decision stands: the child copies it, no second call.
    assert argv[argv.index("--research-path") + 1] == str(e.config.change_dir / "research.md")
    assert "--skip-research" not in argv
    for i, a in enumerate(argv):
        if a in ("--plan-file", "--project-dir", "--context-files", "--research-path"):
            assert Path(argv[i + 1]).is_absolute()


def test_a_parent_that_skipped_research_skips_it_in_each_child(tmp_path, monkeypatch):
    """The parent ran with --skip-research, which writes no research.md: each
    child gets --skip-research, so none decides on research again and parks
    with RESEARCH:NEEDED."""
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT)
    assert not (e.config.change_dir / "research.md").exists()
    assert e.run() == 0
    for a in records.attempts(e.dir):
        argv = a.started["argv"]
        assert "--skip-research" in argv and "--research-path" not in argv


def test_a_child_builds_in_the_source_checkout_when_the_parent_used_a_worktree(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT)
    wt = tmp_path / "parent-wt"
    (wt / "sub").mkdir(parents=True)
    config = copy.copy(e.config)
    config.project_dir = wt / "sub"
    state = BuildState(change_name="e", mode="scratch", tier="advanced",
                       source_repo=str(e.project), worktree_path=str(wt))
    coord = coordinator.Coordinator(config, state, None, e.dir, store.load_epic(e.dir))
    argv = coord.new_argv("a")
    assert argv[argv.index("--project-dir") + 1] == str(e.project / "sub")


def test_a_subdirectory_project_gets_that_subdirectory(tmp_path):
    repo, wt = tmp_path / "repo", tmp_path / "wt"
    for d in (repo / "sub", wt / "sub"):
        d.mkdir(parents=True)
    config = BuildConfig(project_dir=wt / "sub", change_name="e")
    state = BuildState(change_name="e", mode="brief", tier="advanced", source_repo=str(repo), worktree_path=str(wt))
    assert coordinator.source_project_dir(config, state) == repo / "sub"
    state = BuildState(change_name="e", mode="brief", tier="advanced")
    assert coordinator.source_project_dir(config, state) == wt / "sub"


def test_a_pr_is_found_through_the_worktree_list(tmp_path, monkeypatch):
    """Logs with only `WORKTREE:` lines (killed before `BRANCH:`, then a re-bound
    relaunch): the branch comes from `git worktree list --porcelain` (B1.S15)."""
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT)
    wt = tmp_path / "ws" / ".worktrees" / "buildme-e-a"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "buildme/e-a", str(wt)], cwd=e.project, check=True)
    (tmp_path / "prs.json").write_text(json.dumps({pr_url("e-a"): {"branch": "buildme/e-a", "state": "OPEN"}}))
    tries = []
    for _ in range(2):
        a = records.start(e.dir, "build", "a", ["x"])
        a.log.write_text(f"WORKTREE:{wt}\n")
        records.finish(a, None, interrupted=True)
        tries.append(a)
    coord = coordinator.Coordinator(e.config, BuildState(change_name="e", mode="scratch", tier="advanced"),
                                    None, e.dir, store.load_epic(e.dir))
    assert coord._find_pr(tries) == pr_url("e-a")


def test_record_names_keep_kind_and_child_apart(tmp_path):
    records.start(tmp_path, "merge-tests", "api", ["x"])
    records.start(tmp_path, "merge", "tests-api", ["y"])
    assert [(a.kind, a.child) for a in records.attempts(tmp_path)] == [("merge-tests", "api"), ("merge", "tests-api")]


# --- waiting for a dependency's merge ----------------------------------------------

def test_a_closed_dependency_parks_its_dependant(tmp_path, monkeypatch, capsys):
    e = Epic(tmp_path, monkeypatch)
    assert e.run() == 3
    e.fakes.set_pr(pr_url("e-api"), "CLOSED")
    e.fakes.set_pr(pr_url("e-ui"), "MERGED")
    capsys.readouterr()
    assert e.run() == 0
    assert "EPIC:MERGE_WAITING" not in capsys.readouterr().out
    assert e.status() == {"api": "parked", "ui": "merged", "docs": "skipped"}
    assert e.reasons()["api"] == "PR closed without a merge"
    assert len(e.launches()) == 2


def test_a_failed_pr_read_is_read_again(tmp_path, monkeypatch):
    """A PR whose read failed is `unknown`, and the next read (the 30 s tick,
    or the last one before exit) reads it again (B1.S24)."""
    e = Epic(tmp_path, monkeypatch)
    assert e.run() == 3
    coord = coordinator.Coordinator(e.config, BuildState(change_name="e", mode="scratch", tier="advanced"),
                                    None, e.dir, store.load_epic(e.dir))
    coord._recover()
    (tmp_path / "gh-fails").write_text("")
    coord._read_prs(coord._waited_on())
    assert [coord.children[c].status for c in ("api", "ui")] == ["unknown", "unknown"]
    (tmp_path / "gh-fails").unlink()
    e.fakes.set_pr(pr_url("e-api"), "MERGED")
    coord._read_prs(coord._waited_on())
    assert {cid: c.status for cid, c in coord.children.items()} == {"api": "merged", "ui": "built", "docs": "pending"}


def test_a_failed_child_shows_beside_merge_waiting(tmp_path, monkeypatch, capsys):
    """ui fails twice while docs waits on api's open PR: exit 3 (item 15a),
    and the failure is named on stderr."""
    e = Epic(tmp_path, monkeypatch, steps={"ui": [{"exit": 1}, {"exit": 1}]})
    assert e.run() == 3
    out = capsys.readouterr()
    assert f"EPIC:MERGE_WAITING:{pr_url('e-api')}" in out.out
    assert "ui failed" in out.err


def test_a_merge_while_running_launches_the_dependant(tmp_path, monkeypatch):
    """The 30 s tick reads the waited-on PRs while anything runs."""
    e = Epic(tmp_path, monkeypatch, answer=EpicSplitAnswer(
        single=False, children=[child("api"), child("slow"), child("docs", ("api",))]),
        steps={"slow": [{"block": True}]})

    def tick(coord):
        if e.fakes.prs().get(pr_url("e-api"), {}).get("state") == "OPEN":
            e.fakes.set_pr(pr_url("e-api"), "MERGED")
        if "docs" in e.launches():
            e.fakes.release("slow")

    monkeypatch.setattr(coordinator, "_tick", tick)
    assert e.run() == 0
    assert e.launches()[-1] == "docs" and len(e.launches()) == 3


# --- config ----------------------------------------------------------------------

@pytest.mark.parametrize("yaml_text, want", [
    ("", EpicSettings()),
    ("epic:\n  max_parallel: 3\n  max_review_rounds: 2\n  merge: auto\n  max_usd: 20\n",
     EpicSettings(max_parallel=3, max_review_rounds=2, merge="auto", max_usd=20.0)),
    ("epic:\n  max_parallel: 0\n  max_review_rounds: x\n  merge: yes-please\n  max_usd: lots\n",
     EpicSettings()),
    ("epic: [1]\n", EpicSettings()),
])
def test_epic_keys_defaults_and_fallbacks(tmp_path, yaml_text, want):
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "config.yaml").write_text("context: x\n" + yaml_text)
    config = BuildConfig(project_dir=tmp_path)
    config.specs_dir = tmp_path / "specs"
    config._load_specs_config()
    assert config.epic == want
    assert EpicSettings() == EpicSettings(max_parallel=2, max_review_rounds=6, merge="manual", max_usd=None)

