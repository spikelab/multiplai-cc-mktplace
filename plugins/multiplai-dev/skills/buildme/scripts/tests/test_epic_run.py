"""Epic mode, PR B1: the approved build runs the children, each as its own
`python -m build_pipeline build` subprocess (plan-buildme-epic-mode-2026-10-12.md,
items 11-17). The build is a fake first on PATH that prints the real markers;
`gh` is a fake that reads `<root>/prs.json`."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from build_pipeline import git_ops, intake, orchestrator
from build_pipeline.change_manager import ChangeManager
from build_pipeline.config import BuildConfig, EpicSettings
from build_pipeline.epic import coordinator, records, store
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic.split import EpicSplitAnswer
from build_pipeline.git_ops import GitResult
from build_pipeline.models import BuildPhase
from build_pipeline.progress import ProgressWriter
from build_pipeline.state import BuildState

from .epic_row_helpers import (
    THREE,
    CallLog,
    Fakes,
    FakeClock,
    alive,
    approve,
    child,
    die_at,
    make_build,
    make_project,
    merge_waiting,
    move_origin,
    pr_url,
    run_coordinator,
    run_killed,
    run_signalled,
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

    def summary(self, n: int) -> dict:
        return json.loads((self.dir / "runs" / f"{n:02d}" / "summary.json").read_text())

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
    assert e.run() == 3 and merge_waiting() == [pr_url("e-docs")]
    assert e.launches()[-1] == "docs" and len(e.launches()) == 3
    assert e.status() == {"api": "merged", "ui": "merged", "docs": "built"}


def test_a_merged_child_launches_nothing(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b"), pr_url("e-c")]
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
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b"), pr_url("e-c")]
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
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b"), pr_url("e-c")]
    assert len(starts) == 3


def test_a_parked_child_skips_its_dependants(tmp_path, monkeypatch, capsys):
    e = Epic(tmp_path, monkeypatch, steps={"api": [{"marker": "SPLIT:REQUIRED:/x/plan.md"}]})
    assert e.run() == 3 and merge_waiting() == [pr_url("e-ui")]
    assert e.status() == {"api": "parked", "ui": "built", "docs": "skipped"}
    assert e.reasons()["api"].startswith("SPLIT:REQUIRED")
    assert "api" in e.reasons()["docs"]
    assert e.run() == 3 and merge_waiting() == [pr_url("e-ui")]                       # stays parked: nothing relaunched
    assert sorted(e.launches()) == ["api", "ui"]


def test_exit_0_without_a_pr_parks(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT, steps={"a": [{"no_pr": True}]})
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b"), pr_url("e-c")]
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
    assert e.launches() == [] and not (e.dir / "agents").exists() and not (e.dir / "runs").exists()
    yaml_path.write_text("git:\n  worktree: true\n")
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b"), pr_url("e-c")] and sorted(e.launches()) == ["a", "b", "c"]


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
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b"), pr_url("e-c")]
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
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b"), pr_url("e-c")]
    for a in [a for a in records.attempts(e.dir) if a.kind == "build"]:
        argv = a.started["argv"]
        assert "--skip-research" in argv and "--research-path" not in argv


def test_base_ref_reaches_a_new_child_and_a_relaunch_keeps_its_own(tmp_path, monkeypatch):
    """origin moves right after a's first launch: b, launched next, branches
    from the new sha (B1.S14); a's relaunch reuses its first command (item 15)."""
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT, steps={"a": [{"exit": 1}, {}]})
    old = _sha(tmp_path / "o" / "r.git", "main")
    real, moved = coordinator._spawn, []

    def spawn(argv, log_path):
        proc = real(argv, log_path)
        moved.append(moved[0] if moved else move_origin(tmp_path))
        return proc

    monkeypatch.setattr(coordinator, "_spawn", spawn)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b"), pr_url("e-c")]
    first, again = [a.started["argv"] for a in records.attempts(e.dir) if a.child == "a" and a.kind == "build"]
    assert first[first.index("--base-ref") + 1] == old
    assert again == first                      # the relaunch is the same command
    assert (tmp_path / "bases" / "b").read_text() == moved[0] != old


def test_a_child_builds_in_the_source_checkout_when_the_parent_used_a_worktree(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT)
    wt = tmp_path / "parent-wt"
    (wt / "sub").mkdir(parents=True)
    config = copy.copy(e.config)
    config.project_dir = wt / "sub"
    state = BuildState(change_name="e", mode="scratch", tier="advanced",
                       source_repo=str(e.project), worktree_path=str(wt))
    coord = coordinator.Coordinator(config, state, None, e.dir, store.load_epic(e.dir))
    argv = coord.new_argv("a", None)
    assert argv[argv.index("--project-dir") + 1] == str(e.project / "sub")


def test_a_hung_fetch_times_out(tmp_path, monkeypatch):
    """`git fetch` before a launch is bounded: a hung one gives origin's ref as it is."""
    use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)
    shim = tmp_path / "gitbin"
    shim.mkdir()
    (shim / "git").write_text(f'#!/bin/sh\n[ "$1" = fetch ] && exec sleep 30\nexec {shutil.which("git")} "$@"\n')
    (shim / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    monkeypatch.setattr(coordinator, "FETCH_TIMEOUT", 1)
    began = time.monotonic()
    assert asyncio.run(coordinator.base_sha(project, need_fetch=False)) == _sha(project)
    assert time.monotonic() - began < 10
    with pytest.raises(coordinator.ReadFailed, match="timed out"):
        asyncio.run(coordinator.base_sha(project, need_fetch=True))


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


def test_a_child_refuses_a_config_yaml_that_is_not_on_its_base_ref(tmp_path, monkeypatch):
    """With --base-ref, the worktree holds the base's specs/config.yaml while the
    build reads the checkout's: a version committed but not pushed, or one
    origin has since changed, is refused, as an uncommitted one is."""
    use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)
    base = _sha(project)
    (project / "specs").mkdir()
    (project / "specs" / "config.yaml").write_text("context: x\n")
    subprocess.run(["git", "add", "specs"], cwd=project, check=True)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "config"], cwd=project, check=True)
    config = BuildConfig(project_dir=project, change_name="c")
    config.base_ref = base
    state = BuildState(change_name="c", mode="brief", tier="advanced")
    with pytest.raises(intake.ConfigNotCommittedError, match="Push it, or pull"):
        asyncio.run(orchestrator._run_bootstrap(config, state, None, None))
    assert state.worktree_path is None
    config.base_ref = _sha(project)
    assert not intake.config_yaml_off_ref(project, config.base_ref)
    # Behind origin, which changed the file after this checkout's version was
    # pushed: the build would read the old version and its worktree the new one.
    subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=project, check=True)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "o" / "r.git"), str(other)], check=True)
    (other / "specs" / "config.yaml").write_text("context: y\n")
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qam", "config y"], cwd=other, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=other, check=True)
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=project, check=True)
    assert intake.config_yaml_off_ref(project, _sha(project, "origin/main"))
    subprocess.run(["git", "merge", "-q", "--ff-only", "origin/main"], cwd=project, check=True)
    assert not intake.config_yaml_off_ref(project, _sha(project, "origin/main"))
    # No config.yaml at HEAD, as when a child's PR added bootstrap's stub on
    # origin: the build reads no file, so there is nothing to refuse.
    subprocess.run(["git", "checkout", "-q", base], cwd=project, check=True)
    assert not intake.config_yaml_off_ref(project, _sha(project, "origin/main"))


def test_a_build_inside_its_own_worktree_is_not_refused_on_its_base_ref(tmp_path, monkeypatch):
    """A relaunch re-binds to its own worktree with the same --base-ref; a
    config.yaml the build committed there is its own, not the user's."""
    use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)
    base = _sha(project)
    (project / "specs").mkdir()
    (project / "specs" / "config.yaml").write_text("context: changed by the build\n")
    subprocess.run(["git", "add", "specs"], cwd=project, check=True)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "config"], cwd=project, check=True)
    assert intake.config_yaml_off_ref(project, base)

    class Reached(Exception):
        pass

    def setup(config, state):
        raise Reached

    monkeypatch.setattr(orchestrator, "_setup_worktree", setup)
    config = BuildConfig(project_dir=project, change_name="c")
    config.base_ref = base
    rebound = BuildState(change_name="c", mode="brief", tier="advanced", worktree_path=str(project))
    # Neither refused nor sent to _setup_worktree: it keeps the worktree it is in.
    asyncio.run(orchestrator._run_bootstrap(config, rebound, None, None))
    assert rebound.phase == BuildPhase.BOOTSTRAP
    with pytest.raises(intake.ConfigNotCommittedError):
        asyncio.run(orchestrator._run_bootstrap(config, BuildState(change_name="c", mode="brief",
                                                                    tier="advanced"), None, None))


def _commit(repo: Path, rel: str, text: str, msg: str) -> str:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text)
    subprocess.run(["git", "add", rel], cwd=repo, check=True)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", msg], cwd=repo, check=True)
    return _sha(repo)


def test_a_config_yaml_not_on_origin_launches_nothing_until_pushed(tmp_path, monkeypatch, capsys):
    """B1.S22: a specs/config.yaml committed but not pushed is refused before
    the first launch, so no started record keeps the old base. Once pushed,
    the next run fetches again and launches every child on the new sha."""
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT)
    _commit(e.project, "specs/config.yaml", "context: local\n", "config")
    assert e.run() == 1
    assert e.launches() == [] and records.attempts(e.dir) == []
    assert set(e.status().values()) == {"failed"}
    assert "Push it, or pull" in e.reasons()["a"]
    assert "Push it, or pull" in capsys.readouterr().err
    subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=e.project, check=True)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b"), pr_url("e-c")] and sorted(e.launches()) == ["a", "b", "c"]
    pushed = _sha(e.project)
    for a in [a for a in records.attempts(e.dir) if a.kind == "build"]:
        assert a.started["argv"][a.started["argv"].index("--base-ref") + 1] == pushed == a.started["head_sha"]


def _git_shim(root: Path, on_fetch: str) -> Path:
    """A `git` first on PATH that runs *on_fetch* (sh) for `git fetch`, then the real git."""
    d = root / "gitshim" / "bin"
    d.mkdir(parents=True)
    (d / "git").write_text(f'#!/bin/sh\nif [ "$1" = fetch ]; then\n{on_fetch}\nfi\n'
                           f'exec {shutil.which("git")} "$@"\n')
    (d / "git").chmod(0o755)
    return d


def test_a_failed_fetch_launches_no_dependant(tmp_path, monkeypatch):
    """A dependant needs a base that holds its dependencies' merges: when `git
    fetch` fails, it is `unknown` and gets no record; the next run fetches."""
    e = Epic(tmp_path, monkeypatch)
    assert e.run() == 3
    for cid in ("api", "ui"):
        e.fakes.set_pr(pr_url(f"e-{cid}"), "MERGED")
    path = os.environ["PATH"]
    shim = _git_shim(tmp_path, "echo 'fatal: unable to access origin' >&2; exit 1")
    monkeypatch.setenv("PATH", f"{shim}:{path}")
    moved = move_origin(tmp_path)
    assert e.run() == 1
    assert sorted(e.launches()) == ["api", "ui"]
    assert e.status()["docs"] == "unknown" and "git fetch origin" in e.reasons()["docs"]
    assert [a.child for a in records.attempts(e.dir) if a.child == "docs"] == []
    monkeypatch.setenv("PATH", path)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-docs")] and e.launches()[-1] == "docs"
    docs = [a for a in records.attempts(e.dir) if a.child == "docs" and a.kind == "build"]
    assert len(docs) == 1 and docs[0].started["head_sha"] == moved
    assert (tmp_path / "bases" / "docs").read_text() == moved


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
def test_a_signal_during_a_slow_fetch_stops_the_children_and_writes_the_summary(tmp_path, monkeypatch, sig):
    """B1.S1: a runs while the fetch before b's launch hangs. The signal is
    handled at once: a's group is stopped, the fetch is killed, b gets no
    record (nothing new on disk), and the run's summary is written."""
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT, parallel=2,
             steps={"a": [{"block": True, "grandchild": True}]})
    marker, fetching = tmp_path / "fetched", tmp_path / "fetching"
    shim = _git_shim(tmp_path, f'if [ -e {marker} ]; then echo $$ > {fetching}.tmp; '
                               f'mv {fetching}.tmp {fetching}; exec sleep 60; fi\ntouch {marker}')
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    monkeypatch.setattr(coordinator, "STOP_WAIT", 3000.0)
    began: list[float] = []

    def ready() -> bool:
        if fetching.exists() and len(e.fakes.grandchildren()) == 1:
            began.append(time.monotonic())
            return True
        return False

    try:
        rc = run_signalled(lambda: e.run(), ready, sig)
        assert rc == 128 + sig
        assert time.monotonic() - began[0] < 20
        fetch_pid = int(fetching.read_text())
        for _ in range(250):
            if not alive(fetch_pid) and not any(alive(p) for p in e.fakes.grandchildren()):
                break
            time.sleep(0.02)
        assert not alive(fetch_pid)
        assert not any(alive(p) for p in e.fakes.grandchildren())
        tries = records.attempts(e.dir)
        assert [a.child for a in tries] == ["a"] and tries[0].result["interrupted"] is True
        assert [c["id"] for c in e.summary(1)["children"]] == ["a", "b", "c"]
    finally:
        for pid in e.fakes.grandchildren() + ([int(fetching.read_text())] if fetching.exists() else []):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("where", ["waiting", "in_sync_code"])
def test_sighup_leaves_the_children_running_and_writes_the_summary(tmp_path, monkeypatch, where):
    """A SIGHUP (a closed terminal) ends only the coordinator, as it did before
    `build` held SIGHUP for its whole run (`__main__.main`): exit 129, every
    running child left running with no result, so the next run waits for it
    (B1.S4). The run's summary is written. Both while the loop waits and while
    synchronous coordinator code runs."""
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT, parallel=2,
             steps={"a": [{"block": True, "grandchild": True}], "b": [{"block": True, "grandchild": True}]})
    in_sync = tmp_path / "in_sync"
    if where == "in_sync_code":
        def tick(coord):
            if len(e.fakes.grandchildren()) == 2 and not in_sync.exists():
                in_sync.write_text("")
                time.sleep(3)
        monkeypatch.setattr(coordinator, "_tick", tick)

    def as_main() -> int:      # what `__main__.main` holds around a `build`
        try:
            with git_ops._exit_on_termination(interrupt=False):
                return e.run()
        except SystemExit as stop:
            return stop.code

    def ready() -> bool:
        return len(e.fakes.grandchildren()) == 2 and (where == "waiting" or in_sync.exists())

    try:
        rc = run_signalled(as_main, ready, signal.SIGHUP)
        assert rc == 128 + signal.SIGHUP
        time.sleep(0.5)
        assert all(alive(p) for p in e.fakes.grandchildren())
        assert [(a.child, a.result) for a in records.attempts(e.dir)] == [("a", None), ("b", None)]
        assert e.status()["a"] == e.status()["b"] == "running"
        assert {c["id"]: c["status"] for c in e.summary(1)["children"]}["a"] == "running"
    finally:
        for pid in e.fakes.grandchildren():
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGTERM], ids=["SIGHUP", "SIGTERM"])
def test_an_ignored_signal_stays_ignored(tmp_path, monkeypatch, capfd, sig):
    """Under `nohup` SIGHUP is ignored, and the coordinator leaves it so: a
    closed terminal does not stop the run (B2.S13 is only for a SIGHUP that is
    not ignored). The same for an ignored SIGTERM. The signal arrives while a
    and b run; they then finish, c is launched, and the run ends as one that
    got no signal."""
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT, parallel=2,
             steps={"a": [{"block": True}], "b": [{"block": True}]})
    released: list[threading.Timer] = []

    def under_nohup() -> int:      # `nohup` sets SIG_IGN; `__main__.main` then holds the rest
        signal.signal(sig, signal.SIG_IGN)
        try:
            with git_ops._exit_on_termination(interrupt=False):
                return e.run()
        except SystemExit as stop:
            return stop.code

    def ready() -> bool:
        if sorted(e.launches()) != ["a", "b"]:
            return False
        timer = threading.Timer(1.0, lambda: [e.fakes.release(c) for c in ("a", "b")])
        timer.start()
        released.append(timer)
        return True

    try:
        rc = run_signalled(under_nohup, ready, sig)
        waiting = ",".join(pr_url(f"e-{c}") for c in "abc")      # all three wait for a human merge
        assert rc == 3 and f"EPIC:MERGE_WAITING:{waiting}\n" in capfd.readouterr().out
        assert sorted(e.launches()) == ["a", "b", "c"]
        assert e.status() == {"a": "built", "b": "built", "c": "built"}
    finally:
        for timer in released:
            timer.join()
        for c in ("a", "b"):
            e.fakes.release(c)


def test_the_coordinator_puts_back_the_signal_handlers_it_found(tmp_path, monkeypatch):
    """The coordinator's own SIGTERM and SIGHUP handling lasts only for its
    loop. Afterwards the handlers `__main__.main` installed are back (asyncio
    alone would leave SIG_DFL), so a stop later in the run still prints SPEND."""
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT)

    def mine(_signum, _frame):
        pass

    before = {s: signal.signal(s, mine) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        e.run()
        assert signal.getsignal(signal.SIGTERM) is mine and signal.getsignal(signal.SIGHUP) is mine
    finally:
        for s, handler in before.items():
            signal.signal(s, handler)


def test_no_origin_default_branch_launches_without_base_ref(tmp_path, monkeypatch):
    """origin has no default-branch ref: no --base-ref, so the child branches
    from the source checkout's HEAD."""
    use_workspace(monkeypatch, tmp_path)
    repo = tmp_path / "solo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _commit(repo, "README.md", "x\n", "init")
    subprocess.run(["git", "init", "-q", "--bare", str(tmp_path / "empty.git")], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(tmp_path / "empty.git")], cwd=repo, check=True)
    assert asyncio.run(coordinator.base_sha(repo, need_fetch=True)) is None
    e = Epic(tmp_path / "e", monkeypatch, answer=INDEPENDENT)
    coord = coordinator.Coordinator(e.config, BuildState(change_name="e", mode="scratch", tier="advanced"),
                                    None, e.dir, store.load_epic(e.dir))
    assert "--base-ref" not in coord.new_argv("a", None)


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
    assert e.run() == 3 and merge_waiting() == [pr_url("e-slow"), pr_url("e-docs")]
    assert e.launches()[-1] == "docs" and len(e.launches()) == 3


# --- summary ---------------------------------------------------------------------

def test_killed_after_one_of_three_the_rerun_launches_the_other_two(tmp_path, monkeypatch, capsys):
    """Done means 4."""
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT, parallel=1)
    run_killed(lambda: e.run(), die_at(records, "finish", after=True))
    assert e.launches() == ["a"]
    capsys.readouterr()
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b"), pr_url("e-c")]
    assert e.launches() == ["a", "b", "c"]
    summary = e.summary(2)
    assert [c["id"] for c in summary["children"]] == ["a", "b", "c"]
    assert all(c["status"] == "built" for c in summary["children"])
    assert summary["totals"] == {"built": 3}
    assert f"EPIC:SUMMARY:{e.dir / 'runs' / '02' / 'summary.json'}" in capsys.readouterr().out


def test_the_summary_holds_each_childs_pr_and_park_reason(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT, steps={"a": [{"marker": "SPLIT:REQUIRED"}]})
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b"), pr_url("e-c")]
    summary = e.summary(1)
    got = {c["id"]: (c["status"], c["pr"], c["reason"]) for c in summary["children"]}
    assert got == {"a": ("parked", None, "SPLIT:REQUIRED"),
                   "b": ("built", pr_url("e-b"), ""), "c": ("built", pr_url("e-c"), "")}
    assert summary["totals"] == {"parked": 1, "built": 2}


def test_an_unexpected_error_still_writes_the_summary(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT)

    def spawn(argv, log_path):
        raise FileNotFoundError("python")

    monkeypatch.setattr(coordinator, "_spawn", spawn)
    assert e.run() == 1
    assert [c["id"] for c in e.summary(1)["children"]] == ["a", "b", "c"]


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


# --- --base-ref in the build itself -------------------------------------------------

def _sha(repo: Path, ref: str = "HEAD") -> str:
    return subprocess.run(["git", "rev-parse", ref], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()


def test_create_worktree_takes_a_start_point(tmp_path, monkeypatch):
    use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)
    start = _sha(project)
    (project / "x.txt").write_text("x\n")
    subprocess.run(["git", "add", "x.txt"], cwd=project, check=True)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "local"], cwd=project, check=True)
    dest = tmp_path / "wt"
    git_ops.create_worktree(project, "b", dest, start_point=start)
    assert _sha(dest) == start != _sha(project)


def test_the_build_branches_from_base_ref_and_counts_an_empty_branch_against_it(tmp_path, monkeypatch):
    """A first attempt killed before its first commit left `buildme/c` at the base
    ref, which local main has not reached: the next attempt makes `buildme/c-2`."""
    use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "o" / "r.git"), str(other)], check=True)
    (other / "y.txt").write_text("y\n")
    subprocess.run(["git", "add", "y.txt"], cwd=other, check=True)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", "moved"], cwd=other, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=other, check=True)
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=project, check=True)
    base = _sha(project, "origin/main")
    subprocess.run(["git", "branch", "buildme/c", base], cwd=project, check=True)

    config = BuildConfig(project_dir=project, change_name="c")
    config.base_ref = base
    state = BuildState(change_name="c", mode="brief", tier="advanced")
    orchestrator._setup_worktree(config, state)
    assert state.branch == "buildme/c-2"
    assert _sha(Path(state.worktree_path)) == base != _sha(project)


def test_base_ref_is_a_build_flag(tmp_path):
    from build_pipeline.__main__ import build_parser

    args = build_parser().parse_args(["build", "--project-dir", str(tmp_path), "--base-ref", "abc123"])
    config = BuildConfig(project_dir=tmp_path)
    config._apply_git_cli_overrides(args)
    assert config.base_ref == "abc123"
    config = BuildConfig(project_dir=tmp_path)
    config._apply_git_cli_overrides(argparse.Namespace())
    assert config.base_ref == ""


@pytest.mark.parametrize("on_main", [True, False], ids=["on-main", "on-a-feature-branch"])
def test_the_pr_targets_the_branch_its_base_ref_is_on(tmp_path, monkeypatch, on_main):
    """Item 12: `_pr_open_argv` passes `--base` when the build's --base-ref is
    not on the default branch, and the manual fallback names it too."""
    use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)
    if on_main:
        base = _sha(project)
    else:
        subprocess.run(["git", "checkout", "-qb", "feature"], cwd=project, check=True)
        base = _commit(project, "f.txt", "f\n", "feature")
        subprocess.run(["git", "push", "-q", "origin", "feature"], cwd=project, check=True)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=project, check=True)
    config = BuildConfig(project_dir=project, change_name="c")
    config.specs_dir = project / "specs"
    config.base_ref = base
    state = BuildState(change_name="c", mode="brief", tier="advanced",
                       state_file=str(config.state_file_path()))
    asyncio.run(orchestrator._run_bootstrap(config, state, ChangeManager(config.specs_dir),
                                            config.state_file_path()))
    seen: dict = {}

    def fake_pr(argv, cwd=None, timeout=None):
        seen["argv"] = argv
        return GitResult(argv, 0, "PR:https://github.com/o/r/pull/9\n", "")

    monkeypatch.setattr(git_ops, "_run_pr_skill", fake_pr)
    assert orchestrator._run_publish(config, state, ProgressWriter(config.progress_file_path())) is True
    argv = seen["argv"]
    if on_main:
        assert "--base" not in argv
    else:
        assert argv[argv.index("--base") + 1] == "feature"
    assert orchestrator._pr_base_branch(project, "origin/feature" if not on_main else "origin/main") == (
        None if on_main else "feature")
    text = git_ops.manual_finish_commands(tmp_path, "b", "t", base="feature")
    assert "gh pr create --title t --body-file - --base feature --draft" in text
