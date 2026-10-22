"""An epic on its feature branch (plan-spec-feature-branch-2026-10-22.md, T5
and T6): every child starts from origin's `change/<parent>`, its PR targets it
and, with `epic.merge` unset, merges on buildme's own review. Before the run
and before each later wave, origin's main is merged into the feature branch
and pushed; a conflict there launches nothing more and exits 3 with
`FEATURE_BRANCH:CONFLICT`. Git and the bare origin are real; the build,
`gh`, the review and the test command are `epic_row_helpers.Fakes`."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from build_pipeline import feature_branch
from build_pipeline.config import EpicSettings
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic import records

from .epic_row_helpers import (
    CallLog,
    EpicSplitAnswer,
    Fakes,
    approve,
    child,
    make_build,
    make_project,
    merge_waiting,
    pr_url,
    real_git,
    run_coordinator,
    use_workspace,
)

ABC = EpicSplitAnswer(children=[child("a"), child("b", ("a",)), child("c")])


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


def git(cwd: Path, *a: str) -> str:
    """The real git, so a test's own pushes are never logged as the run's."""
    return subprocess.run(
        [real_git(), *a], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def push(root: Path, branch: str, files: dict[str, str], message: str) -> str:
    """Commit *files* onto origin's *branch* from a scratch clone; the new sha."""
    clone = root / "scratch"
    if not clone.exists():
        git(root, "clone", "-q", str(root / "o" / "r.git"), str(clone))
    git(clone, "fetch", "-q", "origin")
    git(clone, "checkout", "-q", "-B", branch, f"origin/{branch}")
    for rel, text in files.items():
        (clone / rel).parent.mkdir(parents=True, exist_ok=True)
        (clone / rel).write_text(text)
    git(clone, "add", "--", *files)
    git(clone, "-c", "commit.gpgsign=false", "commit", "-qm", message)
    git(clone, "push", "-q", "origin", f"HEAD:{branch}")
    return git(clone, "rev-parse", "HEAD")


class Epic:
    """Three children (b depends on a), each build committing `<id>.txt`;
    `epic.merge` unset; the merge step's test command is `fake-tests`."""

    def __init__(self, root: Path, monkeypatch):
        self.root, self.origin = root, root / "o" / "r.git"
        self.data = use_workspace(monkeypatch, root)
        monkeypatch.setenv(  # another root's fakes off PATH
            "PATH",
            os.pathsep.join(
                p
                for p in os.environ["PATH"].split(os.pathsep)
                if not (Path(p).parent / "fakes.json").exists()
            ),
        )
        self.project = make_project(root)
        push(
            root,
            "main",
            {"specs/config.yaml": "tdd:\n  test_command: fake-tests\n"},
            "config",
        )
        git(self.project, "pull", "-q", "origin", "main")
        self.fakes = Fakes(
            root,
            monkeypatch,
            {cid: [{"commit": {f"{cid}.txt": f"{cid}\n"}}] for cid in ("a", "b", "c")},
        )
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=2)
        self.specs = approve(self.config, ABC, CallLog(root / "split.log"))
        self.dir = self.data / "buildme" / "epics" / "o-r--e"

    def run(self) -> int:
        return run_coordinator(self.config)

    def status(self) -> dict:
        return json.loads((self.dir / "epic.json").read_text())["child_status"]

    def started(self, cid: str) -> list[dict]:
        return [
            a.started
            for a in records.attempts(self.dir)
            if a.child == cid and a.kind == "build"
        ]

    def base(self, cid: str) -> str:
        return (self.root / "bases" / cid).read_text()

    def launched(self) -> list[str]:
        return [
            x["argv"][x["argv"].index("--change") + 1]
            for x in CallLog(self.root / "calls.log").calls()
            if x["kind"] == "build"
        ]


def test_children_start_from_the_feature_branch_and_merge_into_it(
    tmp_path, monkeypatch
):
    """T5: a and c start from the commit pm-spec pushed; b starts after a
    merged. Every PR targets change/e and auto-merges with `epic.merge` unset.
    A commit pushed to main while the run goes on is merged into change/e
    before b's wave, so b starts from it; origin's main never gets a child."""
    e = Epic(tmp_path, monkeypatch)
    real = feature_branch.merge_default
    calls = []

    def merge_default(repo, branch):
        calls.append(branch)
        if len(calls) == 2:  # b's wave: main moved since the run started
            calls.append(push(tmp_path, "main", {"main.txt": "main\n"}, "main moved"))
        return real(repo, branch)

    monkeypatch.setattr(feature_branch, "merge_default", merge_default)

    assert e.run() == 0, e.status()
    assert merge_waiting() is None
    assert {cid: s["status"] for cid, s in e.status().items()} == {
        "a": "merged",
        "b": "merged",
        "c": "merged",
    }
    moved = calls[2]
    assert calls[:2] == ["change/e", "change/e"] and len(calls) == 3
    assert e.base("a") == e.base("c") == e.specs
    for cid in ("a", "b", "c"):
        argv = e.started(cid)[0]["argv"]
        assert argv[argv.index("--base-ref") + 1] == "origin/change/e"
        assert argv[argv.index("--branch") + 1] == "change/e"
    assert {p["base"] for p in e.fakes.prs().values()} == {"change/e"}
    # One `gh pr merge` each, and another for a PR whose base moved under it
    # (the merge of main into change/e can land while c merges: BEHIND, again).
    assert len(e.fakes.merges()) >= 3
    # b started after a merged into change/e, and after main was merged in.
    assert git(e.origin, "show", f"{e.base('b')}:a.txt") == "a"
    assert git(e.origin, "merge-base", "--is-ancestor", moved, e.base("b")) == ""
    assert (
        git(e.origin, "log", "--format=%s", f"{moved}..{e.base('b')}").count(
            "Merge origin/main into change/e"
        )
        == 1
    )
    feature = git(e.origin, "ls-tree", "--name-only", "change/e")
    assert {"a.txt", "b.txt", "c.txt", "main.txt"} <= set(feature.split())
    on_main = git(e.origin, "ls-tree", "--name-only", "main").split()
    assert not {"a.txt", "b.txt", "c.txt"} & set(on_main)


def test_an_explicit_manual_merge_wins_over_the_feature_branch_default(
    tmp_path, monkeypatch
):
    """Decision 4: `epic.merge: manual` in config.yaml leaves the PRs to a
    human, feature branch or not."""
    e = Epic(tmp_path, monkeypatch)
    e.config.epic = EpicSettings(max_parallel=2, merge="manual")
    assert e.run() == 3
    assert sorted(merge_waiting()) == [pr_url("e-a"), pr_url("e-c")]
    assert e.fakes.merges() == []


def _no_merge_in_progress(root: Path) -> None:
    w = root / "ws" / ".worktrees" / "buildme-feature-change-e"
    assert w.is_dir()
    assert (
        subprocess.run(
            [real_git(), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
            cwd=w,
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )
    assert git(w, "status", "--porcelain") == ""


def test_a_conflict_merging_main_at_the_start_launches_nothing(
    tmp_path, monkeypatch, capsys
):
    """T6: main and change/e changed the same line: the run prints the marker
    with the file, exits 3, leaves no merge in progress, pushes nothing and
    launches no child."""
    e = Epic(tmp_path, monkeypatch)
    push(tmp_path, "main", {"README.md": "# main\n"}, "main's readme")
    before = push(tmp_path, "change/e", {"README.md": "# feature\n"}, "feat's readme")
    capsys.readouterr()

    assert e.run() == 3

    out = capsys.readouterr().out
    assert "FEATURE_BRANCH:CONFLICT:change/e:README.md" in out.splitlines()
    _no_merge_in_progress(tmp_path)
    assert git(e.origin, "rev-parse", "change/e") == before
    assert e.launched() == []
    assert not e.dir.exists() or not list(records.attempts(e.dir))


def test_a_conflict_merging_main_before_a_later_wave_launches_it_not(
    tmp_path, monkeypatch, capsys
):
    """T6, mid-run: main gets a change that conflicts with a's after a
    merged: b's wave is not launched, and the run exits 3 with the marker."""
    e = Epic(tmp_path, monkeypatch)
    real = feature_branch.merge_default
    calls = []

    def merge_default(repo, branch):
        calls.append(branch)
        if len(calls) == 2:
            push(tmp_path, "main", {"a.txt": "main's a\n"}, "main's a")
        return real(repo, branch)

    monkeypatch.setattr(feature_branch, "merge_default", merge_default)
    capsys.readouterr()

    assert e.run() == 3

    out = capsys.readouterr().out
    assert "FEATURE_BRANCH:CONFLICT:change/e:a.txt" in out.splitlines()
    assert sorted(e.launched()) == ["e-a", "e-c"]
    status = e.status()
    assert status["a"]["status"] == "merged" and status["b"]["status"] == "pending"
    _no_merge_in_progress(tmp_path)
