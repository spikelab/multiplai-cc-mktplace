"""Row tests for PR C's "Stop and resume states" table
(plan-buildme-epic-mode-2026-10-12.md, rows marked `Test: yes`: C.S2, C.S3, C.S4; and C.S19,
which the PR adds).

Each case stops the coordinator at the row's point (a forked child exiting
137, as SIGKILL would), resumes, and compares with the same scenario run
without a stop in a second temporary root: the epic directory, the records
per child as (kind, argv, exit_code, markers, interrupted), the last summary
and the launches (builds and reviews). The only differences allowed are the
ones the row's "Resume does" names.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
from build_pipeline.config import EpicSettings
from build_pipeline.epic import coordinator, records
from build_pipeline.epic import lock as epic_lock

from .epic_row_helpers import (
    CallLog,
    EpicSplitAnswer,
    Fakes,
    approve,
    child,
    commit_names,
    epic_snapshot,
    last_summary,
    make_build,
    make_project,
    merge_waiting,
    name_commits,
    pr_number,
    pr_url,
    records_snapshot,
    run_coordinator,
    run_killed,
    use_workspace,
)

TWO = EpicSplitAnswer(
    single=False, reason="Two parts.", children=[child("a"), child("b")]
)
N = pr_number("e-a")


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


class Root:
    """One root: project, workspace, fakes, an approved two-child epic, one at a time."""

    def __init__(self, root: Path, monkeypatch, reviews: dict):
        self.root, self.mp = root, monkeypatch
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.fakes = Fakes(root, monkeypatch, {}, reviews)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=1)
        approve(self.config, TWO, CallLog(root / "split.log"))
        self.dir = self.data / "buildme" / "epics" / "o-r--e"
        self.target = self.data / "reviews" / "o-r" / f"pr-{N}"

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

    def reviews(self) -> list[records.Attempt]:
        return [
            a
            for a in records.attempts(self.dir)
            if a.child == "a" and a.kind in ("review", "resume")
        ]

    def finished(self) -> bool:
        ledger = self.data / "reviews" / "ledger.jsonl"
        lines = (
            [json.loads(x) for x in ledger.read_text().splitlines()]
            if ledger.exists()
            else []
        )
        return any(x["event"] == "finished" and x.get("pr") == N for x in lines)

    def release_and_wait(self) -> None:
        """Let a's blocked review finish, and wait for its `finished` line."""
        self.fakes.release_review("e-a")
        for _ in range(500):
            if self.finished():
                return
            time.sleep(0.02)
        raise AssertionError("the review never finished")

    def review_pid(self) -> int:
        for _ in range(500):
            lock = self.fakes.review_lock()
            if lock.exists() and lock.read_text():
                return int(lock.read_text())
            time.sleep(0.02)
        raise AssertionError("no review is running")

    def started_review_pid(self) -> int:
        """The running review's pid once it has started: its lock names it, and
        its `review-state.json` and `started` line for the PR are written, so a
        kill now leaves an interrupted run for the next review to find."""
        target = self.data / "reviews" / "o-r" / f"pr-{N}"
        ledger = self.data / "reviews" / "ledger.jsonl"
        pid = self.review_pid()
        for _ in range(500):
            lines = (
                [json.loads(x) for x in ledger.read_text().splitlines()]
                if ledger.exists()
                else []
            )
            if (target / "review-state.json").exists() and any(
                x["event"] == "started" and x.get("target_dir") == str(target)
                for x in lines
            ):
                return pid
            time.sleep(0.02)
        raise AssertionError("the review never started")

    def snapshot(self) -> dict:
        names = commit_names([self.root / "o" / "r.git", self.project])
        return name_commits(
            {
                "epic": epic_snapshot(self.dir, self.root),
                "records": records_snapshot(self.dir, self.root),
                "summary": last_summary(self.dir, self.root),
            },
            names,
        )

    def launches(self) -> list[dict]:
        return [
            *CallLog(self.root / "calls.log").calls(),
            *self.fakes.review_launches(),
        ]


def _roots(tmp_path, monkeypatch, reviews) -> tuple[Root, Root]:
    return (
        Root(tmp_path / "stopped", monkeypatch, reviews),
        Root(tmp_path / "clean", monkeypatch, reviews),
    )


def _die_once_reviewing(coord) -> None:
    """A seam for `run_killed`: exit 137 at the first look while a's review runs."""
    if coord.children["a"].status == "reviewing":
        os._exit(137)


def _kill_at_review() -> None:
    coordinator._tick = _die_once_reviewing


def _release_when_reviewed(root: Root):
    def tick(coord):
        if (
            any(a.started.get("head_sha") for a in root.reviews())
            and not root.finished()
        ):
            root.release_and_wait()

    return tick


def _assert_same(
    stopped: Root, clean: Root, want_a: list[tuple], extra: list[str]
) -> None:
    """Same epic files and summary; b's records equal, a's records as *want_a*
    (with "<review>" for the review argv and "<build>" for a's build record);
    launches equal plus one review skill launch per kind in *extra*."""
    s, c = stopped.snapshot(), clean.snapshot()
    assert s["epic"] == c["epic"]
    assert s["summary"] == c["summary"]
    assert s["records"]["b"] == c["records"]["b"]
    build, review = c["records"]["a"][0], c["records"]["a"][1]
    argv = {
        "review": review[1],
        "resume": [
            *review[1][:7],
            "resume",
            f"<root>/ws/.multiplai/data/reviews/o-r/pr-{N}",
            "--trust-repo",
        ],
    }
    want = [build if w == "<build>" else (w[0], argv[w[0]], *w[2:]) for w in want_a]
    assert s["records"]["a"] == want

    def key(calls):
        return sorted(json.dumps(x, sort_keys=True) for x in calls)

    review_call = next(
        x for x in clean.fakes.review_launches() if f"--pr {N}" in " ".join(x["argv"])
    )
    extra_calls = [
        review_call
        if k == "review"
        else next(x for x in stopped.fakes.review_launches() if x["kind"] == "resume")
        for k in extra
    ]
    assert key(stopped.launches()) == key([*clean.launches(), *extra_calls])


def _result(root: Root, i: int) -> dict:
    return root.reviews()[i].result


@pytest.mark.parametrize("start_delay", [0, 0.5], ids=["prompt", "slow-start"])
def test_C_S2_coordinator_killed_with_the_review_alive_waits_for_it(
    tmp_path, monkeypatch, start_delay
):
    """C.S2: the ledger's `started` line for this head and the review's run
    lock held: the coordinator launches nothing, waits 60 s and looks again
    until the lock is free, then reads the ledger and writes the killed
    record's result from it. Never two reviews; no launch and no pair.
    The coordinator runs again only once the review has started (`slow-start`
    widens the gap between its lock naming its pid and its `started` line)."""
    reviews = {str(N): [{"block": True, "start_delay": start_delay}, {}]}
    stopped, clean = _roots(tmp_path, monkeypatch, reviews)
    stopped.killed(_kill_at_review)
    pid = stopped.started_review_pid()
    assert (
        subprocess.run(["kill", "-0", str(pid)]).returncode == 0
    )  # the review outlived it
    looks, launched = [], stopped.fakes.review_launches()

    def tick(coord):
        if coord.children["a"].status == "reviewing" and not stopped.finished():
            looks.append(1)
            assert stopped.fakes.review_launches() == launched  # never a second review
            if len(looks) == 5:
                stopped.release_and_wait()

    assert stopped.run(tick) == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    assert len(looks) == 5
    assert clean.run(_release_when_reviewed(clean)) == 3 and merge_waiting() == [
        pr_url("e-a"),
        pr_url("e-b"),
    ]
    _assert_same(stopped, clean, ["<build>", ("review", None, None, [], True)], [])
    old = _result(stopped, 0)
    assert (old["reused"], old["pr_round"], old["head_sha"]) == (
        True,
        1,
        _result(clean, 0)["head_sha"],
    )
    assert old["reused_pipeline_run"] == {
        "target_dir": str(stopped.target),
        "round": "01",
    }


@pytest.mark.parametrize("start_delay", [0, 0.5], ids=["prompt", "slow-start"])
def test_C_S3_review_killed_with_its_coordinator_is_resumed(
    tmp_path, monkeypatch, start_delay
):
    """C.S3: the killed record's result is written; `review` exits 4 with the
    hint and the coordinator runs `resume`. Two more launches and pairs.
    The review is killed only once it has started (`slow-start` widens the
    gap between its lock naming its pid and its `started` line)."""
    reviews = {str(N): [{"block": True, "start_delay": start_delay}, {}]}
    stopped, clean = _roots(tmp_path, monkeypatch, reviews)
    stopped.killed(_kill_at_review)
    pid = stopped.started_review_pid()
    os.killpg(pid, signal.SIGKILL)
    for _ in range(500):
        stat = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        ).stdout
        if not stat.strip() or stat.strip().startswith("Z"):
            break
        time.sleep(0.02)
    assert stopped.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    assert clean.run(_release_when_reviewed(clean)) == 3 and merge_waiting() == [
        pr_url("e-a"),
        pr_url("e-b"),
    ]
    _assert_same(
        stopped,
        clean,
        [
            "<build>",
            ("review", None, None, [], True),
            ("review", None, 4, [], False),
            ("resume", None, 0, [], False),
        ],
        ["review", "resume"],
    )
    last = _result(stopped, 2)
    assert (last["reused"], last["pr_round"], last["review_dir"]) == (
        False,
        1,
        str(stopped.target),
    )
    assert _result(stopped, 0).get("reused") is None


def test_C_S4_finished_review_whose_result_was_not_recorded_is_reused(
    tmp_path, monkeypatch
):
    """C.S4: no review; the record's result is written from the ledger with
    `exit_code: null`, `interrupted: true`, `reused: true`; `pr_round` counts once."""
    stopped, clean = _roots(tmp_path, monkeypatch, {})

    def setup():
        real = records.finish

        def dying(attempt, *a, **k):
            if attempt.kind == "review":
                os._exit(137)
            return real(attempt, *a, **k)

        records.finish = dying

    stopped.killed(setup)
    assert stopped.finished() and _result(stopped, 0) is None
    assert stopped.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    assert clean.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    _assert_same(stopped, clean, ["<build>", ("review", None, None, [], True)], [])
    got, want = _result(stopped, 0), _result(clean, 0)
    assert (got["reused"], got["pr_round"], want["pr_round"]) == (True, 1, 1)
    keys = ("head_sha", "base_sha", "review_dir", "cost_usd", "blocking", "conflicts")
    assert {k: str(got[k]).replace(str(stopped.root), "<root>") for k in keys} == {
        k: str(want[k]).replace(str(clean.root), "<root>") for k in keys
    }


def test_C_S19_head_moved_while_reviewed_and_stopped_before_the_reuse(
    tmp_path, monkeypatch
):
    """C.S19: a commit lands after the coordinator read a's head and before the
    review read it, so the review reviews the new head. Killed right after the
    old head's record gets its plain result: the next run reads the PR again
    and writes one pair for the new head from its review. No second review."""
    moved = "c" * 40
    stopped, clean = _roots(tmp_path, monkeypatch, {str(N): [{}]})
    real_run = coordinator.Coordinator._run_review

    def pushing(root: Root):
        done = []

        async def run_review(self, c, kind, argv, head):
            if c.id == "a" and not done:
                done.append(1)
                root.fakes.set_head("e-a", moved)
            return await real_run(self, c, kind, argv, head)

        return run_review

    def setup():
        coordinator.Coordinator._run_review = pushing(stopped)
        real = records.finish

        def dying(attempt, code, *, interrupted, extra=None):
            out = real(attempt, code, interrupted=interrupted, extra=extra)
            if attempt.kind == "review" and code == 0 and not extra:
                os._exit(137)
            return out

        records.finish = dying

    stopped.killed(setup)
    [first] = stopped.reviews()
    assert first.started["head_sha"] != moved and first.result["exit_code"] == 0
    assert stopped.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    monkeypatch.setattr(coordinator.Coordinator, "_run_review", pushing(clean))
    assert clean.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    _assert_same(
        stopped,
        clean,
        ["<build>", ("review", None, 0, [], False), ("review", None, 0, [], False)],
        [],
    )
    got, want = _result(stopped, 1), _result(clean, 1)
    assert (got["head_sha"], got["reused"], got["pr_round"]) == (moved, True, 1)
    assert (want["head_sha"], want["reused"], want["pr_round"]) == (moved, True, 1)
    status = json.loads((stopped.dir / "epic.json").read_text())["child_status"]["a"]
    assert (status["status"], status["pr_round"]) == ("built", 1)
