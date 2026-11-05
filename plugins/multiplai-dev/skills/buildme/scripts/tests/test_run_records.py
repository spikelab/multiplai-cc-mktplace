"""The shared run rows (`multiplai_core.runs`): where an epic's folder is,
the cause of a child that stopped short, and the rows a run appends."""

from __future__ import annotations

import importlib
import inspect
import json

import pytest
from build_pipeline import build_row
from build_pipeline.epic import causes, coordinator, store
from multiplai_core import runs

from .epic_row_helpers import make_project, use_workspace

# --- the epic's folder -----------------------------------------------------------


def _epic(d, name="e"):
    d.mkdir(parents=True)
    (d / store.EPIC_FILE).write_text(json.dumps({"name": name, "children": []}))


def test_a_new_epic_lives_in_its_branchs_change_folder(tmp_path, monkeypatch):
    data = use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)
    assert store.epic_dir_for(project, "e", "ENG-40-e") == (
        data / "changes" / "r" / "ENG-40-e" / "buildme"
    )


def test_an_old_folder_not_yet_moved_is_kept(tmp_path, monkeypatch):
    data = use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)
    old = data / "buildme" / "epics" / "o-r--e"
    _epic(old)
    assert store.epic_dir_for(project, "e", "ENG-40-e") == old
    assert store.epic_dir_for(project, "e") == old


def test_a_moved_folder_is_found_through_its_symlink_and_by_name(tmp_path, monkeypatch):
    data = use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)
    new = data / "changes" / "r" / "ENG-40-e" / "buildme"
    _epic(new)
    old = data / "buildme" / "epics" / "o-r--e"
    old.parent.mkdir(parents=True)
    old.symlink_to(new)
    assert store.epic_dir_for(project, "e", "ENG-40-e") == new
    assert store.epic_dir_for(project, "e") == new


def test_two_branches_with_one_epic_name_are_refused(tmp_path, monkeypatch):
    data = use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)
    _epic(data / "changes" / "r" / "ENG-40-e" / "buildme")
    _epic(data / "changes" / "r" / "ENG-41-e" / "buildme")
    with pytest.raises(store.AmbiguousEpic):
        store.epic_dir_for(project, "e")
    assert store.epic_dir_for(project, "e", "ENG-41-e").parent.name == "ENG-41-e"


# --- causes ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,reason,cause",
    [  # reasons as runs 19-21 of a real epic wrote them
        (
            "parked",
            "review round 3 of 3 left 3 blocker(s): MEDIUM 0994 x.py:41",
            "review-cap",
        ),
        (
            "failed",
            "the fixer left review findings with no decision: eb90b46ffd",
            "fixer-undecided",
        ),
        (
            "parked",
            "the fixer failed at code: the PR's test suite fails before any fix (the TDD engine's baseline gate)",
            "baseline-tests-failing",
        ),
        (
            "parked",
            "the fixer failed at publish: Final verification failed: Tests failing (exit 1)",
            "final-verification-failed",
        ),
        ("parked", "the fixer failed at code: block 2 failed", "fixer-failed"),
        (
            "parked",
            "merge tests failed: bash -c 'x' exited 1, see a.log",
            "merge-tests-failed",
        ),
        (
            "parked",
            "merging origin/DF-1-x conflicts in a.py, which the merge step may not resolve",
            "merge-conflict",
        ),
        ("parked", "SPEC:ISSUES:/w/specs/changes/x/spec-issues.md", "spec-issues"),
        ("skipped", "dependency om-account-identity parked", "dependency"),
        ("waiting", "waiting on owner-permissions-core", "dependency"),
        ("failed", "exit 1", "exit-code"),
        ("failed", "Claude usage limit reached; resets 5pm", "usage-limit"),
        (
            "failed",
            "exit 1: You've hit your session limit · resets 9:40am (UTC)",
            "usage-limit",
        ),
        ("failed", "exit 4: an agent timed out", "timeout"),
        ("parked", "something nobody wrote yet", "other"),
        ("merged", "", None),
        ("built", "", None),
    ],
)
def test_cause_of(status, reason, cause):
    assert causes.cause_of(status, reason) == cause


@pytest.mark.parametrize("pattern", causes.PATTERNS, ids=lambda p: f"{p[0]}:{p[2]}")
def test_every_pattern_is_still_written_by_its_module(pattern):
    """A reworded message fails here instead of falling into `other` unseen."""
    _, module, words, _ = pattern
    source = inspect.getsource(importlib.import_module(f"build_pipeline.{module}"))
    assert words.rstrip(": (") in source


def test_run_outcome():
    assert coordinator.run_outcome({"merged": 3}) == "succeeded"
    assert coordinator.run_outcome({"merged": 1, "failed": 1, "parked": 1}) == "failed"
    assert coordinator.run_outcome({"merged": 1, "parked": 1}) == "parked"
    assert coordinator.run_outcome({"merged": 1, "waiting": 2}) == "waiting"


def test_previous_spend(tmp_path):
    runs_dir = tmp_path / "runs"
    for n, usd in (("01", 2.0), ("02", 5.5)):
        (runs_dir / n).mkdir(parents=True)
        (runs_dir / n / "summary.json").write_text(json.dumps({"spent_usd": usd}))
    (runs_dir / "03").mkdir()
    assert coordinator._previous_spend(runs_dir / "03") == 5.5
    assert coordinator._previous_spend(runs_dir / "01") == 0.0


def test_the_producer_names_this_plugins_version():
    assert coordinator.PRODUCER.startswith("multiplai-dev:buildme ")
    assert not coordinator.PRODUCER.endswith(" unknown")
    assert build_row.PRODUCER == coordinator.PRODUCER


# --- a single build's row ----------------------------------------------------------


@pytest.mark.parametrize(
    "code,marker,expected",
    [
        (0, None, ("succeeded", None)),
        (3, "PLAN:WAITING", ("waiting", "PLAN:WAITING")),
        (2, "SPEC:NOT_READY", ("failed", "SPEC:NOT_READY")),
        (1, None, ("failed", "exit-1")),
        (143, None, ("stopped", "signal-15")),
        (None, None, ("stopped", "interrupted")),
    ],
)
def test_build_outcome(code, marker, expected):
    assert build_row.outcome_of(code, marker) == expected


def test_stop_markers_only():
    assert (
        build_row.MARKER.match("PLAN:WAITING:/x/plan-approval.md").group(1)
        == "PLAN:WAITING"
    )
    assert build_row.MARKER.match("BOARD:ENG-1:Planning") is None
    assert build_row.MARKER.match("PR:https://x/pull/1") is None


def test_a_build_appends_one_row_and_an_epic_none(tmp_path, monkeypatch):
    data = use_workspace(monkeypatch, tmp_path)
    project = make_project(tmp_path)

    class Config:
        project_dir = project
        feature_branch = "ENG-1-c"
        change_name = "c"

        def state_file_path(self):
            return project / "specs" / "changes" / "c" / ".build-state.json"

    monkeypatch.setattr(build_row, "_epic_ran", False)
    build_row.append(Config(), 3, "2026-11-04T00:00:00Z", "PLAN:WAITING")
    rows, skipped = runs.read_rows(data / "runs.jsonl")
    assert skipped == 0 and len(rows) == 1
    row = rows[0]
    assert (row["kind"], row["outcome"], row["cause"]) == (
        "build",
        "waiting",
        "PLAN:WAITING",
    )
    assert row["target"] == {
        "repo": "r",
        "remote": row["target"]["remote"],
        "branch": "ENG-1-c",
        "change": "c",
    }
    assert row["producer"] == build_row.PRODUCER

    build_row.mark_epic()
    build_row.append(Config(), 0, "2026-11-04T00:00:00Z", None)
    assert len(runs.read_rows(data / "runs.jsonl")[0]) == 1
    monkeypatch.setattr(build_row, "_epic_ran", False)
