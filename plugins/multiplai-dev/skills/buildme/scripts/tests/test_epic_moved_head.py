"""plan-buildme-epic-moved-head-2026-11-03.md: a child parked by its fixer is
left `built` for the review step once its PR head is no longer the head that
fixer was launched on; at an unchanged head it stays parked. Rows M1-M7 of
its "Stop and resume states" table. The build, `gh`, the review skill and the
fixer are fakes first on PATH (`epic_row_helpers.Fakes`)."""

from __future__ import annotations

import subprocess
import sys

import pytest
from build_pipeline.epic import records

from .test_epic_env_relaunch import BASELINE_TIMEOUT, DONE, ENV, HIGH, Epic, N

BASELINE = (
    "the PR's test suite fails before any fix (the TDD engine's baseline gate), "
    "so no fix can be tested; make the suite pass, then re-run"
)
SELF_CHECK = (
    "RuntimeError: the self-check agent failed: Reached maximum number of turns (30)"
)
NEW = "c" * 40
NEWER = "d" * 40


def _park(why: str) -> dict:
    return {"exit": 1, "stdout": f"FINDINGS:FAILED:code:{why}"}


def _reviews(*rounds: list) -> dict:
    return {str(N): [{"findings": f} for f in rounds]}


def _parked_epic(tmp_path, monkeypatch, fixes, reviews=None, relaunches=2) -> Epic:
    e = Epic(
        tmp_path,
        monkeypatch,
        fixes,
        reviews=_reviews([HIGH], []) if reviews is None else reviews,
        relaunches=relaunches,
    )
    assert e.run() == 0
    assert e.status()["status"] == "parked"
    return e


def test_m1_a_moved_head_is_left_built_and_reviewed(tmp_path, monkeypatch, caplog):
    """M1: the fix record holds `parked` at the old head; the PR head moved.
    The child is left `built`, and the review step reviews the new head."""
    e = _parked_epic(tmp_path, monkeypatch, [_park(BASELINE), DONE])
    [parked] = e.fixes()
    old = parked.started["head_sha"]
    assert old and old != NEW
    e.fakes.set_head("e-a", NEW)
    with caplog.at_level("INFO"):
        assert e.run() == 3  # EPIC:MERGE_WAITING: reviewed clean
    assert f"fix park at {old[:12]} left for review: PR head is now {NEW[:12]}" in (
        caplog.text
    )
    reviews = e.fakes.review_launches()
    assert len(reviews) == 2
    assert len(e.fakes.fix_launches()) == 1  # no relaunch at any head
    assert e.status()["status"] == "built"


def test_m2_a_review_of_the_new_head_killed_part_way_is_resumed(tmp_path, monkeypatch):
    """M2: the review of the new head has a started record and no result.
    The child is not parked again; the review step handles the review."""
    e = _parked_epic(tmp_path, monkeypatch, [_park(BASELINE), DONE])
    e.fakes.set_head("e-a", NEW)
    assert e.run() == 3  # EPIC:MERGE_WAITING: reviewed clean

    [*_, review] = [
        a for a in records.attempts(e.dir) if a.child == "a" and a.kind == "review"
    ]
    assert review.started["head_sha"] == NEW
    review.result_path.unlink()
    assert e.run() == 3  # EPIC:MERGE_WAITING: reviewed clean
    assert e.status()["status"] == "built"
    assert len(e.fakes.fix_launches()) == 1


def test_m3_an_unchanged_head_stays_parked(tmp_path, monkeypatch):
    """M3: the PR head equals the fixer's `head_sha`: parked as in 0.155.0."""
    e = _parked_epic(tmp_path, monkeypatch, [_park(BASELINE), DONE])
    assert e.run() == 0
    assert e.status()["status"] == "parked"
    assert len(e.fakes.review_launches()) == 1
    assert len(e.fakes.fix_launches()) == 1


def test_m4_a_failed_head_read_makes_the_child_unknown(tmp_path, monkeypatch):
    """M4: `gh pr view` fails: the child is `unknown`, nothing is launched
    for it and the run exits 1; the next run reads again."""
    e = _parked_epic(tmp_path, monkeypatch, [_park(BASELINE), DONE])
    e.fakes.set_head("e-a", NEW)
    (e.root / "gh-fails").write_text("")
    assert e.run() == 1
    assert e.status()["status"] == "unknown"
    assert len(e.fakes.review_launches()) == 1
    assert len(e.fakes.fix_launches()) == 1
    (e.root / "gh-fails").unlink()
    assert e.run() == 3  # EPIC:MERGE_WAITING: reviewed clean
    assert len(e.fakes.review_launches()) == 2
    assert e.status()["status"] == "built"


def test_m5_a_new_park_at_the_new_head_stays_parked(tmp_path, monkeypatch):
    """M5: the new head is reviewed, its fixer parks again at that head; the
    next run keeps it parked (M3) until the head moves again."""
    e = _parked_epic(
        tmp_path,
        monkeypatch,
        [_park(BASELINE), _park(BASELINE), DONE],
        reviews=_reviews([HIGH], [HIGH], []),
    )
    e.fakes.set_head("e-a", NEW)
    assert e.run() == 0
    assert len(e.fakes.fix_launches()) == 2
    assert e.fixes()[-1].started["head_sha"] == NEW
    assert e.status()["status"] == "parked"
    assert e.run() == 0
    assert e.status()["status"] == "parked"
    assert len(e.fakes.review_launches()) == 2


def test_m6_a_head_moved_twice_is_reviewed_at_the_newest(tmp_path, monkeypatch):
    """M6: two pushes between runs: as M1, with the newest head."""
    e = _parked_epic(tmp_path, monkeypatch, [_park(BASELINE), DONE])
    e.fakes.set_head("e-a", NEW)
    e.fakes.set_head("e-a", NEWER)
    assert e.run() == 3  # EPIC:MERGE_WAITING: reviewed clean

    [*_, review] = [
        a for a in records.attempts(e.dir) if a.child == "a" and a.kind == "review"
    ]
    assert review.started["head_sha"] == NEWER
    assert e.status()["status"] == "built"


def test_m7_a_second_coordinator_is_refused_by_the_lock(tmp_path, monkeypatch):
    """M7: another process holds the epic lock: the run exits 2 and launches
    nothing, whatever the PR head is."""
    e = _parked_epic(tmp_path, monkeypatch, [_park(BASELINE), DONE])
    e.fakes.set_head("e-a", NEW)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, sys, time\n"
                "f = open(sys.argv[1], 'a')\n"
                "fcntl.flock(f, fcntl.LOCK_EX)\n"
                "print('held', flush=True)\n"
                "time.sleep(120)\n"
            ),
            str(e.dir / ".lock"),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        assert e.run() == 2
        assert len(e.fakes.review_launches()) == 1
        assert len(e.fakes.fix_launches()) == 1
    finally:
        holder.kill()
        holder.wait()
    assert e.run() == 3  # EPIC:MERGE_WAITING: reviewed clean
    assert len(e.fakes.review_launches()) == 2


@pytest.mark.parametrize(
    "fixes, relaunches",
    [
        pytest.param([_park(BASELINE), DONE], 2, id="red-baseline"),
        pytest.param([_park(SELF_CHECK), DONE], 2, id="self-check-cap"),
        pytest.param([_park(BASELINE_TIMEOUT), DONE], 2, id="timeout"),
        pytest.param([ENV, DONE], 0, id="environment-at-cap"),
    ],
)
def test_each_park_reason_is_reviewed_at_a_moved_head(
    tmp_path, monkeypatch, fixes, relaunches
):
    """Every fix park, decision or environment at its cap, is left for the
    review step once the head moved, and stays parked at an unchanged head."""
    e = _parked_epic(tmp_path, monkeypatch, fixes, relaunches=relaunches)
    assert e.run() == 0
    assert e.status()["status"] == "parked"
    e.fakes.set_head("e-a", NEW)
    assert e.run() == 3  # EPIC:MERGE_WAITING: reviewed clean
    assert len(e.fakes.review_launches()) == 2
    assert len(e.fakes.fix_launches()) == 1
    assert e.status()["status"] == "built"


def test_a_later_review_park_keeps_today_s_behaviour(tmp_path, monkeypatch):
    """The rule applies only when the fix park is the child's latest park: a
    review park written after it keeps the child parked."""
    e = _parked_epic(tmp_path, monkeypatch, [_park(BASELINE), DONE])

    [fixed] = e.fixes()
    later = records.start(
        e.dir, "review", "a", ["review"], head_sha=fixed.started["head_sha"]
    )
    records.finish(
        later, 2, interrupted=False, extra={"parked": "the review failed: boom"}
    )
    e.fakes.set_head("e-a", NEW)
    assert e.run() == 0
    assert e.status()["status"] == "parked"
    assert e.status()["reason"] == "the review failed: boom"
    assert len(e.fakes.review_launches()) == 1
