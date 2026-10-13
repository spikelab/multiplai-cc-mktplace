"""Epic mode, PR B1: the approved build runs the children, each as its own
`python -m build_pipeline build` subprocess (plan-buildme-epic-mode-2026-10-12.md,
items 11-17). The build is a fake first on PATH that prints the real markers;
`gh` is a fake that reads `<root>/prs.json`."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from build_pipeline import git_ops, orchestrator
from build_pipeline.config import BuildConfig, EpicSettings
from build_pipeline.epic import coordinator, records
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
    die_at,
    make_build,
    make_project,
    pr_url,
    run_coordinator,
    run_killed,
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
    summary = {c["id"]: c for c in e.summary(1)["children"]}
    assert summary["api"]["reason"].startswith("SPLIT:REQUIRED")
    assert "api" in summary["docs"]["reason"]
    assert e.run() == 0                       # stays parked: nothing relaunched
    assert sorted(e.launches()) == ["api", "ui"]


def test_exit_0_without_a_pr_parks(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT, steps={"a": [{"no_pr": True}]})
    assert e.run() == 0
    assert e.status()["a"] == "parked"
    assert {c["id"]: c for c in e.summary(1)["children"]}["a"]["reason"] == "no PR opened"


def test_a_failure_is_relaunched_once_per_invocation(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT, steps={"a": [{"exit": 1}]})
    assert e.run() == 1
    assert e.launches().count("a") == 2 and e.status()["a"] == "failed"
    assert e.run() == 1
    assert e.launches().count("a") == 3


# --- the argv ------------------------------------------------------------------

def test_the_argv_holds_the_flags_and_absolute_paths(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT)
    (e.config.change_dir / "interview.md").write_text("# Interview\n")
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
    for i, a in enumerate(argv):
        if a in ("--plan-file", "--project-dir", "--context-files"):
            assert Path(argv[i + 1]).is_absolute()


def test_base_ref_reaches_a_new_child_and_a_relaunch_keeps_its_own(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT, steps={"a": [{"exit": 1}, {}]})
    origin = tmp_path / "o" / "r.git"
    sha = subprocess.run(["git", "rev-parse", "main"], cwd=origin, capture_output=True,
                         text=True, check=True).stdout.strip()
    assert e.run() == 0
    first, again = [a.started["argv"] for a in records.attempts(e.dir) if a.child == "a"]
    assert first[first.index("--base-ref") + 1] == sha
    assert again == first                      # the relaunch is the same command
    assert (tmp_path / "bases" / "b").read_text() == sha


def test_a_subdirectory_project_gets_that_subdirectory(tmp_path):
    repo, wt = tmp_path / "repo", tmp_path / "wt"
    for d in (repo / "sub", wt / "sub"):
        d.mkdir(parents=True)
    config = BuildConfig(project_dir=wt / "sub", change_name="e")
    state = BuildState(change_name="e", mode="brief", tier="advanced", source_repo=str(repo), worktree_path=str(wt))
    assert coordinator.source_project_dir(config, state) == repo / "sub"
    state = BuildState(change_name="e", mode="brief", tier="advanced")
    assert coordinator.source_project_dir(config, state) == wt / "sub"


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
    reasons = {c["id"]: c["reason"] for c in e.summary(2)["children"]}
    assert reasons["api"] == "PR closed without a merge"
    assert len(e.launches()) == 2


def test_a_merge_while_running_launches_the_dependant(tmp_path, monkeypatch):
    """The 30 s tick reads the waited-on PRs while anything runs."""
    e = Epic(tmp_path, monkeypatch, answer=EpicSplitAnswer(
        single=False, children=[child("api"), child("slow"), child("docs", ("api",))]),
        steps={"slow": [{"block": True}]})

    def tick(coord):
        if pr_url("e-api") in e.fakes.prs():
            e.fakes.set_pr(pr_url("e-api"), "MERGED")
        if "docs" in e.launches():
            e.fakes.release("slow")

    monkeypatch.setattr(coordinator, "_tick", tick)
    assert e.run() == 0
    assert e.launches()[-1] == "docs" and len(e.launches()) == 3


# --- summary ---------------------------------------------------------------------

def test_killed_after_one_of_three_the_rerun_launches_the_other_two(tmp_path, monkeypatch, capsys):
    """Done means 4."""
    e = Epic(tmp_path, monkeypatch, answer=INDEPENDENT, parallel=1)
    run_killed(lambda: e.run(), die_at(records, "finish", after=True))
    assert e.launches() == ["a"]
    capsys.readouterr()
    assert e.run() == 0
    assert e.launches() == ["a", "b", "c"]
    summary = e.summary(2)
    assert [c["id"] for c in summary["children"]] == ["a", "b", "c"]
    assert all(c["status"] == "built" for c in summary["children"])
    assert summary["totals"] == {"built": 3}
    assert f"EPIC:SUMMARY:{e.dir / 'runs' / '02' / 'summary.json'}" in capsys.readouterr().out


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
