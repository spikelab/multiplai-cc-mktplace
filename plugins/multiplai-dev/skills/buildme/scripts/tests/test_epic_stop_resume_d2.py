"""Row tests for PR D2's "Stop and resume states" table
(plan-buildme-epic-mode-2026-10-12.md, rows marked `Test: yes`: D2.S3, D2.S4, D2.S7).

Each case stops the coordinator at the row's point (a forked child exiting
137, as SIGKILL would), resumes, and compares with the same scenario run
without a stop in a second temporary root: the epic directory, the records
per child as (kind, argv, exit_code, markers, interrupted), the last summary
and the launches (builds, the fixer, reviews and posts). The only differences
allowed are the ones the row's "Resume does" names. The fixer and the review
skill are fakes first on PATH (`epic_row_helpers.Fakes`).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from build_pipeline.config import EpicSettings
from build_pipeline.epic import coordinator, records
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic.split import EpicSplitAnswer

from .epic_row_helpers import (
    CallLog,
    Fakes,
    alive,
    approve,
    child,
    commit_names,
    epic_snapshot,
    last_summary,
    make_build,
    make_project,
    name_commits,
    pr_number,
    records_snapshot,
    run_coordinator,
    run_killed,
    use_workspace,
)

TWO = EpicSplitAnswer(single=False, reason="Two parts.", children=[child("a"), child("b")])
N = pr_number("e-a")
HIGH = {"id": "f1", "severity": "HIGH", "status": "confirmed", "claim": "f1 is wrong",
        "file": "src/app.py", "line_start": 3}


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


class Root:
    """One root: project, workspace, fakes, an approved two-child epic, one at a time.
    a's first review has a blocker; the fixer's one round leaves a clean head."""

    def __init__(self, root: Path, monkeypatch, fixes: dict):
        self.root, self.mp = root, monkeypatch
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.fakes = Fakes(root, monkeypatch, {}, {str(N): [{"findings": [HIGH]}]}, fixes)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=1)
        approve(self.config, TWO, CallLog(root / "split.log"))
        self.dir = self.data / "buildme" / "epics" / "o-r--e"

    def use(self) -> None:
        use_workspace(self.mp, self.root)
        self.fakes.use(self.mp)

    def run(self, tick=None) -> int:
        self.use()
        self.mp.setattr(coordinator, "_tick", tick or (lambda coord: None))
        return run_coordinator(self.config)

    def killed(self, setup) -> None:
        self.use()
        self.mp.setattr(coordinator, "_tick", lambda coord: None)
        run_killed(lambda: run_coordinator(self.config), setup)

    def tries(self, kind: str) -> list[records.Attempt]:
        return [a for a in records.attempts(self.dir) if a.child == "a" and a.kind == kind]

    def fix_pid(self) -> int:
        """The pid of the fixer the stopped run left blocked, once it has started."""
        for _ in range(500):
            pid = coordinator.fix_pid(N, self.project)
            if pid:
                return pid
            time.sleep(0.02)
        raise AssertionError("no fixer is running")

    def snapshot(self) -> dict:
        names = commit_names([self.root / "o" / "r.git", self.project])
        return name_commits({"epic": epic_snapshot(self.dir, self.root),
                             "records": records_snapshot(self.dir, self.root),
                             "summary": last_summary(self.dir, self.root)}, names)

    def launches(self) -> list[dict]:
        return [*CallLog(self.root / "calls.log").calls(), *self.fakes.review_launches(),
                *self.fakes.post_launches()]


def _roots(tmp_path, monkeypatch, fixes) -> tuple[Root, Root]:
    return (Root(tmp_path / "stopped", monkeypatch, fixes),
            Root(tmp_path / "clean", monkeypatch, fixes))


def _key(calls: list[dict]) -> list[str]:
    return sorted(json.dumps(x, sort_keys=True) for x in calls)


def _assert_same(stopped: Root, clean: Root, *, extra_fixes: int = 0) -> None:
    """Same epic files and summary; b's records equal; a's records equal but for
    *extra_fixes* more `fix` pairs right after the first, the first one then
    `interrupted` with no exit code; launches equal plus that many fixer launches."""
    s, c = stopped.snapshot(), clean.snapshot()
    assert s["epic"] == c["epic"]
    assert s["summary"] == c["summary"]
    assert s["records"]["b"] == c["records"]["b"]
    want = list(c["records"]["a"])
    if extra_fixes:
        i = next(k for k, r in enumerate(want) if r[0] == "fix")
        kind, argv, _code, _markers, _ = want[i]
        got = s["records"]["a"]
        assert got[i][:2] == (kind, argv) and (got[i][2], got[i][4]) == (None, True)
        again = [r for r in got[i + 1:i + 1 + extra_fixes]]
        assert [r[:2] for r in again] == [(kind, argv)] * extra_fixes
        assert [(r[2], r[4]) for r in again] == [(0, False)] * extra_fixes
        want[i:i + 1] = [got[i], *again]
    assert s["records"]["a"] == want
    fix_call = next(x for x in clean.fakes.fix_launches())
    assert _key(stopped.launches()) == _key([*clean.launches(), *[fix_call] * extra_fixes])


def test_D2_S3_stopped_after_the_post_runs_the_fixer_not_a_review(tmp_path, monkeypatch):
    """D2.S3 (Done means 6): the post's result is written, then the coordinator
    dies. The resume starts `findings`, not a review or a second post, and
    `pr_round` keeps counting. Calls equal."""
    fixes = {str(N): [{"rounds": [{}]}]}
    stopped, clean = _roots(tmp_path, monkeypatch, fixes)

    def setup():
        real = records.finish

        def dying(attempt, *a, **k):
            out = real(attempt, *a, **k)
            if attempt.kind == "post":
                os._exit(137)
            return out

        records.finish = dying

    stopped.killed(setup)
    assert [a.result["exit_code"] for a in stopped.tries("post")] == [0] and stopped.tries("fix") == []
    assert stopped.run() == 0
    assert clean.run() == 0
    _assert_same(stopped, clean)
    assert len(stopped.fakes.post_launches()) == 1
    assert [x["kind"] for x in stopped.fakes.review_launches()] == ["review", "review"]   # a's first, b's
    status = json.loads((stopped.dir / "epic.json").read_text())["child_status"]["a"]
    assert (status["status"], status["pr_round"]) == ("built", 2)


def test_D2_S4_coordinator_killed_with_the_fixer_alive_waits_for_it(tmp_path, monkeypatch):
    """D2.S4: the coordinator dies while `findings` runs. The resume launches
    nothing while it lives, then writes its result (`exit_code: null`,
    `interrupted: true`) and relaunches it, which ends at once. Never two at
    once. One more launch and pair."""
    fixes = {str(N): [{"block": True, "rounds": [{}]}, {"spend": 0}]}
    stopped, clean = _roots(tmp_path, monkeypatch, fixes)

    def setup():
        def die_while_fixing(coord):
            c = coord.children["a"]
            if c.rattempt is not None and c.rattempt.kind == "fix":
                os._exit(137)

        coordinator._tick = die_while_fixing

    stopped.killed(setup)
    pid = stopped.fix_pid()
    assert alive(pid)                                        # the fixer outlived its coordinator
    looks = []

    def tick(coord):
        if alive(pid):
            looks.append(1)
            assert len(stopped.fakes.fix_launches()) == 1     # never a second fixer
            if len(looks) == 5:
                stopped.fakes.release_fix("e-a")
                for _ in range(500):
                    if not alive(pid):
                        break
                    time.sleep(0.02)

    assert stopped.run(tick) == 0
    assert len(looks) == 5

    def release(coord):
        if clean.fakes.fix_launches():
            clean.fakes.release_fix("e-a")

    assert clean.run(release) == 0
    _assert_same(stopped, clean, extra_fixes=1)
    assert len(stopped.fakes.post_launches()) == 1


def test_D2_S7_fixer_ended_with_its_result_unrecorded_reuses_its_last_review(tmp_path, monkeypatch):
    """D2.S7: `findings` ended (its reviews are in the ledger) but the coordinator
    died before writing its result. The resume writes it, relaunches `findings`
    (which ends at once), reuses the fixer's last review and takes `pr_round`
    from the ledger. One more `findings` launch and pair; no review launch."""
    fixes = {str(N): [{"rounds": [{}]}, {"spend": 0}]}
    stopped, clean = _roots(tmp_path, monkeypatch, fixes)

    def setup():
        real = records.finish

        def dying(attempt, *a, **k):
            if attempt.kind == "fix":
                os._exit(137)
            return real(attempt, *a, **k)

        records.finish = dying

    stopped.killed(setup)
    [fix] = stopped.tries("fix")
    assert fix.result is None
    assert stopped.run() == 0
    assert clean.run() == 0
    _assert_same(stopped, clean, extra_fixes=1)
    assert [x["kind"] for x in stopped.fakes.review_launches()] == ["review", "review"]   # a's first, b's
    status = json.loads((stopped.dir / "epic.json").read_text())["child_status"]["a"]
    assert (status["status"], status["pr_round"]) == ("built", 2)
