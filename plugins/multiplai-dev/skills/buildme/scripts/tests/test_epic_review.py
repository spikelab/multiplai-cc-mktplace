"""Epic mode, PR C: each child PR is reviewed (plan-buildme-epic-mode-2026-10-12.md,
items 21-25). The build, `gh` and the review skill are fakes first on PATH
(`epic_row_helpers.Fakes`); the review writes the review skill's ledger
lines under `<root>/ws/.multiplai/data/reviews/`."""

from __future__ import annotations

import fcntl
import json
import signal
import subprocess
import time
from pathlib import Path

import pytest

from build_pipeline import git_ops
from build_pipeline.config import EpicSettings
from build_pipeline.epic import coordinator, records
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic import review
from build_pipeline.epic.split import EpicSplitAnswer

from .epic_row_helpers import (
    CallLog,
    FakeClock,
    Fakes,
    alive,
    approve,
    child,
    make_build,
    make_project,
    merge_waiting,
    pr_number,
    pr_url,
    run_coordinator,
    run_signalled,
    use_workspace,
)

TWO = EpicSplitAnswer(single=False, reason="Two parts.", children=[child("a"), child("b")])
CHAIN = EpicSplitAnswer(single=False, reason="Two parts.", children=[child("a"), child("b", ("a",))])
N = pr_number("e-a")


def high(fid: str = "f1", topic: str | None = None, severity: str = "HIGH") -> dict:
    return {"id": fid, "severity": severity, "status": "confirmed", "claim": f"{fid} is wrong",
            "file": "src/app.py", "line_start": 3, "topic": topic}


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


class Epic:
    def __init__(self, root: Path, monkeypatch, answer=TWO, steps=None, reviews=None, rounds=6,
                 parallel=2, max_usd=None):
        self.root = root
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.fakes = Fakes(root, monkeypatch, steps, reviews)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=parallel, max_review_rounds=rounds, max_usd=max_usd)
        approve(self.config, answer, CallLog(root / "split.log"))
        self.dir = self.data / "buildme" / "epics" / "o-r--e"
        self.ledger = self.data / "reviews" / "ledger.jsonl"

    def run(self) -> int:
        return run_coordinator(self.config)

    def child_status(self, cid: str) -> dict:
        return json.loads((self.dir / "epic.json").read_text())["child_status"][cid]

    def reviews(self, cid: str = "a") -> list[records.Attempt]:
        return [a for a in records.attempts(self.dir) if a.child == cid and a.kind in ("review", "resume")]

    def launched(self) -> list[str]:
        return [x["kind"] for x in self.fakes.review_launches()]

    def head(self) -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.project, capture_output=True,
                              text=True, check=True).stdout.strip()

    def target(self, change: str = "e-a") -> Path:
        return self.data / "reviews" / "o-r" / f"pr-{pr_number(change)}"

    def add_ledger(self, entry: dict) -> None:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a") as f:
            f.write(json.dumps(entry) + "\n")


# --- one review per head ------------------------------------------------------------

def test_each_built_child_is_reviewed_and_the_result_recorded(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    assert sorted(e.launched()) == ["review", "review"]
    [a] = e.reviews("a")
    assert a.started["argv"][:6] == ["uv", "run", "--directory", str(review.review_skill_dir()),
                                     "python", "-m"]
    assert a.started["argv"][6:] == ["review_pipeline", "review", "--repo",
                                     str(e.root / "ws" / ".worktrees" / "buildme-e-a"),
                                     "--pr", str(N), "--trust-repo", "--fetch"]
    assert a.started["head_sha"] == e.head() and a.started["pr_round"] == 0
    r = a.result
    assert (r["exit_code"], r["interrupted"], r["reused"], r["reused_pipeline_run"]) == (0, False, False, None)
    assert (r["head_sha"], r["base_sha"], r["review_dir"]) == (e.head(), "b" * 40, str(e.target()))
    assert (r["cost_usd"], r["pr_round"], r["conflicts"], r["blocking"]) == (1.5, 1, False, [])
    assert e.child_status("a")["status"] == "built" and e.child_status("a")["pr_round"] == 1


def test_a_head_already_in_the_ledger_starts_no_review(tmp_path, monkeypatch):
    """Done means 5: no review starts, and the record has `reused: true`."""
    e = Epic(tmp_path, monkeypatch)
    e.target().mkdir(parents=True)
    (e.target() / "findings.json").write_text(json.dumps({"mode": None, "findings": [high()]}))
    e.add_ledger({"event": "finished", "slug": f"o-r--pr-{N}", "pr": N, "target_dir": str(e.target()),
                  "head_sha": e.head(), "base_sha": "c" * 40, "round": "03", "stopped": None,
                  "run": {"cost_usd": 2.0}})
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    assert e.launched() == ["review"]                       # b's only
    [a] = e.reviews("a")
    assert a.started["argv"][6:8] == ["review_pipeline", "review"]
    r = a.result
    assert (r["exit_code"], r["markers"], r["interrupted"], r["reused"]) == (0, [], False, True)
    assert r["reused_pipeline_run"] == {"target_dir": str(e.target()), "round": "03"}
    assert (r["base_sha"], r["cost_usd"], r["pr_round"]) == ("c" * 40, 2.0, 1)
    assert [b.split()[:2] for b in r["blocking"]] == [["HIGH", "f1"]] and "parked" not in r
    assert (e.child_status("a")["status"], e.child_status("a")["reason"]) == ("built", "")


def test_a_head_reviewed_elsewhere_during_a_wait_gets_one_result_with_its_fields(tmp_path, monkeypatch):
    """Item 24: another process holds the review lock (exit 2 `is running now`)
    and reviews this head during the 60 s wait. The exit-2 pair holds no
    review fields, so one more pair is written with them (`reused: true`,
    the blockers); a later run writes nothing more."""
    running = {"exit": 2, "stderr": "ERROR: a review of x is running now (pid 1); wait for it"}
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, reviews={str(N): [running, {"findings": [high()]}]})
    real = FakeClock.sleep

    async def sleep(clock, seconds):
        if seconds == review.REVIEW_WAIT and not e.ledger.exists():   # the other process's review
            subprocess.run(review.review_argv(e.project, N), check=True, capture_output=True)
        await real(clock, seconds)

    monkeypatch.setattr(FakeClock, "sleep", sleep)
    assert e.run() == 3                                       # b waits for a's merge
    first, second = e.reviews("a")
    assert (first.result["exit_code"], "blocking" in first.result) == (2, False)
    r = second.result
    assert (r["exit_code"], r["interrupted"], r["reused"], r["pr_round"]) == (0, False, True, 1)
    assert [b.split()[:2] for b in r["blocking"]] == [["HIGH", "f1"]]
    assert e.child_status("a")["status"] == "built"
    assert e.run() == 3 and len(e.reviews("a")) == 2


def test_a_review_that_ends_just_after_the_ledger_read_is_not_run_again(tmp_path, monkeypatch):
    """C.S2: a review of this head is running (its `started` line, its run
    lock held) and ends right after the coordinator reads the ledger. The
    coordinator reads the ledger again once the lock is free, so it reuses
    that review and launches none."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN)
    e.target().mkdir(parents=True)
    e.add_ledger({"event": "started", "slug": f"o-r--pr-{N}", "target_dir": str(e.target()),
                  "head_sha": e.head()})
    lock_file = e.fakes.review_lock()
    lock_file.parent.mkdir(parents=True)
    held = lock_file.open("a")
    fcntl.flock(held, fcntl.LOCK_EX)
    real = review.ledger_lines

    def ledger_lines():
        lines = real()
        if not held.closed:              # the review ends now: its findings, its line, its lock
            (e.target() / "findings.json").write_text(json.dumps({"mode": None, "findings": []}))
            e.add_ledger({"event": "finished", "slug": f"o-r--pr-{N}", "pr": N, "target_dir": str(e.target()),
                          "head_sha": e.head(), "base_sha": "b" * 40, "round": "01", "stopped": None,
                          "run": {"cost_usd": 1.5}})
            held.close()
        return lines

    monkeypatch.setattr(review, "ledger_lines", ledger_lines)
    try:
        assert e.run() == 3
    finally:
        held.close()
    assert e.launched() == []
    [a] = e.reviews("a")
    assert (a.result["exit_code"], a.result["reused"], a.result["pr_round"]) == (0, True, 1)


def test_no_review_is_launched_while_the_review_lock_is_held(tmp_path, monkeypatch):
    """C.S2: while a review of this head runs (its `started` line, its run
    lock held), the coordinator waits and looks again; it launches nothing."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN)
    e.target().mkdir(parents=True)
    e.add_ledger({"event": "started", "slug": f"o-r--pr-{N}", "target_dir": str(e.target()),
                  "head_sha": e.head()})
    lock_file = e.fakes.review_lock()
    lock_file.parent.mkdir(parents=True)
    held = lock_file.open("a")
    fcntl.flock(held, fcntl.LOCK_EX)
    waits = []
    real = FakeClock.sleep

    async def sleep(clock, seconds):
        if seconds == review.REVIEW_WAIT:
            waits.append(seconds)
            if len(waits) == 3:          # the review ends
                (e.target() / "findings.json").write_text(json.dumps({"mode": None, "findings": []}))
                e.add_ledger({"event": "finished", "slug": f"o-r--pr-{N}", "pr": N,
                              "target_dir": str(e.target()), "head_sha": e.head(), "base_sha": "b" * 40,
                              "round": "01", "stopped": None, "run": {"cost_usd": 1.5}})
                held.close()
        await real(clock, seconds)

    monkeypatch.setattr(FakeClock, "sleep", sleep)
    try:
        assert e.run() == 3
    finally:
        held.close()
    assert len(waits) == 3 and e.launched() == []
    [a] = e.reviews("a")
    assert (a.result["exit_code"], a.result["reused"]) == (0, True)


def test_a_stopped_review_of_another_head_does_not_count(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch)
    for head, stopped in (("d" * 40, None), ("e" * 40, "budget"), ("f" * 40, None)):
        e.add_ledger({"event": "finished", "slug": f"o-r--pr-{N}", "pr": N, "target_dir": str(e.target()),
                      "head_sha": head, "stopped": stopped, "run": {"cost_usd": 1.0}})
    e.add_ledger({"event": "finished", "slug": f"o-r--pr-{N + 1}", "pr": N + 1, "head_sha": "a" * 40,
                  "stopped": None})
    e.add_ledger({"event": "finished", "slug": f"x-y--pr-{N}", "pr": N, "head_sha": "9" * 40, "stopped": None})
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    assert e.reviews("a")[-1].result["pr_round"] == 3


def test_a_new_head_is_a_new_round_and_the_round_count_survives_a_resume(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    e.fakes.set_head("e-a", "c" * 40)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    assert e.launched().count("review") == 3
    assert e.reviews("a")[-1].result["pr_round"] == 2 and e.child_status("a")["pr_round"] == 2
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]                                       # same head: nothing new
    assert e.launched().count("review") == 3 and len(e.reviews("a")) == 2
    assert e.child_status("a")["pr_round"] == 2


def _push_once_reviewing(e: Epic, monkeypatch, sha: str) -> None:
    """A commit lands on a's PR branch after the coordinator read its head and
    before the review reads it: the review reviews *sha*."""
    real, moved = coordinator.Coordinator._run_review, []

    async def run_review(self, c, kind, argv, head):
        if c.id == "a" and not moved:
            moved.append(1)
            e.fakes.set_head("e-a", sha)
        return await real(self, c, kind, argv, head)

    monkeypatch.setattr(coordinator.Coordinator, "_run_review", run_review)


def test_a_head_that_moves_as_the_review_starts_is_reviewed_not_parked(tmp_path, monkeypatch):
    """C.S8: the review exits 0 with its `finished` line for the new head. The
    coordinator reads the PR again and uses that review: one launch, a's record
    of the old head gets a plain result, and one pair holds the new head's
    fields. A later run launches nothing."""
    moved = "c" * 40
    e = Epic(tmp_path, monkeypatch, reviews={str(N): [{"findings": [high()]}]})
    old = e.head()
    _push_once_reviewing(e, monkeypatch, moved)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    assert sorted(e.launched()) == ["review", "review"]
    first, pair = e.reviews("a")
    assert first.started["head_sha"] == old
    assert (first.result["exit_code"], first.result["interrupted"]) == (0, False)
    assert "blocking" not in first.result and "parked" not in first.result
    assert pair.started["head_sha"] == moved
    r = pair.result
    assert (r["head_sha"], r["review_dir"], r["pr_round"], r["reused"]) == (moved, str(e.target()), 1, True)
    assert [b.split()[:2] for b in r["blocking"]] == [["HIGH", "f1"]]
    st = e.child_status("a")
    assert (st["status"], st["reason"], st["pr_round"]) == ("built", "", 1)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    assert sorted(e.launched()) == ["review", "review"] and len(e.reviews("a")) == 2
    assert e.child_status("a")["status"] == "built"


def test_a_review_that_exits_0_without_a_complete_line_for_an_unmoved_head_parks(tmp_path, monkeypatch):
    """The head did not move and the review's `finished` line is not complete
    (`stopped` is set): the child parks, and stays parked."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, reviews={str(N): [{"stopped": "budget"}]})
    assert e.run() == 0
    st = e.child_status("a")
    assert st["status"] == "parked" and "review exited 0 without a findings line" in st["reason"]
    [a] = e.reviews("a")
    assert a.result["exit_code"] == 0 and a.result["parked"] == st["reason"]
    assert e.child_status("b")["status"] == "skipped"
    assert e.run() == 0 and e.launched() == ["review"]


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
def test_a_signal_stops_the_review_this_run_started(tmp_path, monkeypatch, sig):
    """Ctrl-C or SIGTERM signals the group of a review this run started and
    writes its record's result with `interrupted: true`."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, reviews={str(N): [{"block": True}]})
    lock = e.fakes.review_lock()
    try:
        rc = run_signalled(lambda: e.run(), lambda: lock.exists() and bool(lock.read_text()), sig)
        assert rc == 128 + sig
        pid = int(lock.read_text())
        for _ in range(250):
            if not alive(pid):
                break
            time.sleep(0.02)
        assert not alive(pid)
        [a] = e.reviews("a")
        assert a.result is not None and a.result["interrupted"] is True
    finally:
        e.fakes.release_review("e-a")


def test_a_signal_leaves_a_review_an_earlier_run_left_running(tmp_path, monkeypatch):
    """A review this run did not start (left by a run a SIGHUP ended, waited for
    through its run lock) is not signalled: it may be one the epic never
    started. It keeps running, and its record keeps no result."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, reviews={str(N): [{"block": True}]})
    lock = e.fakes.review_lock()

    def as_main() -> int:
        try:
            with git_ops._exit_on_termination(interrupt=False):
                return e.run()
        except SystemExit as stop:
            return stop.code

    waiting, real = tmp_path / "waiting", FakeClock.sleep

    async def sleep(clock, seconds):
        if seconds == review.REVIEW_WAIT:      # the C.S2 wait for that review
            waiting.write_text("")
        await real(clock, seconds)

    try:
        assert run_signalled(as_main, lambda: lock.exists() and bool(lock.read_text()), signal.SIGHUP) == 129
        pid = int(lock.read_text())
        monkeypatch.setattr(FakeClock, "sleep", sleep)
        assert run_signalled(lambda: e.run(), waiting.exists, signal.SIGTERM) == 128 + signal.SIGTERM
        time.sleep(0.3)
        assert alive(pid)
        assert [a.result for a in e.reviews("a")] == [None]
    finally:
        e.fakes.release_review("e-a")


def test_an_interrupted_review_is_resumed(tmp_path, monkeypatch):
    """A review of this head that started and never finished (its `started`
    ledger line and saved state, as a killed review leaves them): `review`
    exits 4 with the hint, and the coordinator runs `resume`."""
    e = Epic(tmp_path, monkeypatch)
    e.target().mkdir(parents=True)
    (e.target() / "review-state.json").write_text(json.dumps({"head": e.head()}))
    e.add_ledger({"event": "started", "slug": f"o-r--pr-{N}", "target_dir": str(e.target()),
                  "head_sha": e.head()})
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    kinds = [(a.kind, a.result["exit_code"], a.result["interrupted"]) for a in e.reviews("a")]
    assert kinds == [("review", 4, False), ("resume", 0, False)]
    resume = e.reviews("a")[1]
    assert resume.started["argv"][6:] == ["review_pipeline", "resume", str(e.target()), "--trust-repo"]
    assert (resume.result["pr_round"], resume.result["review_dir"]) == (1, str(e.target()))


def test_the_coordinator_waits_for_the_review_subprocess(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, reviews={str(N): [{"block": True}]})
    seen = []

    def tick(coord):
        if coord.children["a"].status == "reviewing":
            seen.append(e.reviews("a")[0].result)
            if len(seen) == 5:
                e.fakes.release_review("e-a")

    monkeypatch.setattr(coordinator, "_tick", tick)
    assert e.run() == 3                                       # b waits for a's merge
    assert seen[:5] == [None] * 5                             # no result while the review runs
    assert e.reviews("a")[0].result["exit_code"] == 0
    assert e.child_status("b")["status"] == "waiting"


def test_sighup_leaves_a_running_review_running(tmp_path, monkeypatch):
    """A SIGHUP ends only the coordinator (PR B2): the review it started keeps
    running and its record keeps no result; the next run reuses the review
    that process finished (C.S4) and launches none."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, reviews={str(N): [{"block": True}]})
    lock = e.fakes.review_lock()

    def as_main() -> int:      # what `__main__.main` holds around a `build`
        try:
            with git_ops._exit_on_termination(interrupt=False):
                return e.run()
        except SystemExit as stop:
            return stop.code

    try:
        rc = run_signalled(as_main, lambda: lock.exists() and bool(lock.read_text()), signal.SIGHUP)
        assert rc == 128 + signal.SIGHUP
        pid = int(lock.read_text())
        time.sleep(0.3)
        assert alive(pid)
        assert [a.result for a in e.reviews("a")] == [None]
    finally:
        e.fakes.release_review("e-a")
    for _ in range(250):
        if not alive(pid):
            break
        time.sleep(0.02)
    assert e.run() == 3                                       # b waits for a's merge
    assert e.launched() == ["review"]
    [a] = e.reviews("a")
    assert (a.result["reused"], a.result["interrupted"], a.result["pr_round"]) == (True, True, 1)


# --- what blocks, and what does not ---------------------------------------------------

def test_conflicts_never_block(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=CHAIN,
             steps={"a": [{"pr": {"mergeable": "CONFLICTING", "merge_state": "DIRTY"}}]})
    assert e.run() == 3
    assert e.reviews("a")[0].result["conflicts"] is True
    assert e.child_status("a") | {"reason": ""} == e.child_status("a")
    assert (e.child_status("a")["status"], e.child_status("a")["conflicts"]) == ("built", True)


def test_a_base_move_updates_conflicts_without_a_review(tmp_path, monkeypatch):
    """C.S9: no new review; the cached `conflicts` follows the PR."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN)
    assert e.run() == 3
    prs = e.fakes.prs()
    prs[pr_url("e-a")]["mergeable"] = "CONFLICTING"
    (e.root / "prs.json").write_text(json.dumps(prs))
    assert e.run() == 3
    assert e.launched() == ["review"] and len(e.reviews("a")) == 1
    assert e.child_status("a")["conflicts"] is True


def test_blockers_are_listed_and_only_code_high_or_medium_block(tmp_path, monkeypatch):
    findings = [high("f1"), high("f2", severity="MEDIUM"), high("f3", severity="LOW"),
                high("f4", topic="tests"), high("f5", topic="docs"),
                {**high("f6"), "status": "refuted"}]
    e = Epic(tmp_path, monkeypatch, reviews={str(N): [{"findings": findings}]})
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    blocking = e.reviews("a")[0].result["blocking"]
    assert [b.split()[:2] for b in blocking] == [["HIGH", "f1"], ["MEDIUM", "f2"]]
    assert e.child_status("a")["status"] == "built"           # below the cap: blockers go to D2


def test_at_the_cap_with_blockers_the_child_parks_and_stays_parked(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, rounds=1, reviews={str(N): [{"findings": [high()]}]})
    assert e.run() == 0
    st = e.child_status("a")
    assert st["status"] == "parked" and "f1" in st["reason"] and "1 of 1" in st["reason"]
    assert e.child_status("b")["status"] == "skipped"
    e.fakes.set_head("e-a", "c" * 40)                         # a later commit does not un-park it
    assert e.run() == 0
    assert e.launched() == ["review"] and e.child_status("a")["status"] == "parked"


@pytest.mark.parametrize("code, stderr", [
    (4, "STOPPED: over budget\nResume with: python -m review_pipeline resume <out> --max-cost-usd <higher>"),
    (2, "ERROR: not a pull request"),
    (1, "Traceback: boom"),
])
def test_a_review_that_fails_otherwise_parks_with_the_stderr_tail(tmp_path, monkeypatch, code, stderr):
    """C.S12: exit 4 without the hint, exit 2 without `is running now`, anything else."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, reviews={str(N): [{"exit": code, "stderr": stderr}]})
    assert e.run() == 0
    st = e.child_status("a")
    assert st["status"] == "parked" and stderr.splitlines()[0] in st["reason"]
    assert e.child_status("b")["status"] == "skipped"
    assert e.reviews("a")[0].result["exit_code"] == code
    assert e.run() == 0 and e.launched() == ["review"]       # stays parked


READ_FAILURES = [
    "ERROR: git fetch --quiet origin failed: error: cannot lock ref 'refs/remotes/origin/main': "
    "is at 1111111 but expected 2222222",
    f"ERROR: gh pr view {N} failed: HTTP 502: Bad Gateway (https://api.github.com/graphql)",
    f"ERROR: /w/buildme-e-a: PR #{N} head cccccccccccc is not in this clone (pass --fetch)",
]


@pytest.mark.parametrize("stderr", READ_FAILURES, ids=["fetch", "gh-pr-view", "head-not-fetched"])
def test_a_review_that_cannot_read_git_or_github_leaves_the_child_unknown(tmp_path, monkeypatch, capsys, stderr):
    """C.S20: the review's own `git fetch` or `gh pr view` failed, or its fetch
    missed a head pushed just before (exit 2 `ERROR:`). That is a failed read,
    as the coordinator's own: the child is `unknown`, the run exits 1, nothing
    is parked, and the next run reviews the head."""
    e = Epic(tmp_path, monkeypatch, reviews={str(N): [{"exit": 2, "stderr": stderr}, {"findings": [high()]}]})
    assert e.run() == 1
    st = e.child_status("a")
    assert st["status"] == "unknown" and stderr.splitlines()[0] in st["reason"]
    assert "EPIC: child a unknown: " in capsys.readouterr().err
    [a] = e.reviews("a")
    assert a.result["exit_code"] == 2 and "parked" not in a.result and "blocking" not in a.result
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    first, second = e.reviews("a")
    assert first.result == a.result
    assert (second.result["exit_code"], second.result["reused"], second.result["pr_round"]) == (0, False, 1)
    assert [b.split()[:2] for b in second.result["blocking"]] == [["HIGH", "f1"]]
    assert e.child_status("a")["status"] == "built"


def test_a_failed_read_in_the_review_skips_no_dependant(tmp_path, monkeypatch):
    """C.S20 under a chain: b still waits on a's open PR, and is not skipped."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, reviews={str(N): [{"exit": 2, "stderr": READ_FAILURES[0]}, {}]})
    assert e.run() == 3                                       # b waits for a's merge
    assert e.child_status("b")["status"] == "waiting"
    assert "parked" not in e.reviews("a")[0].result
    assert e.run() == 3
    assert [a.result["exit_code"] for a in e.reviews("a")] == [2, 0]
    assert e.reviews("a")[1].result["blocking"] == []
    assert e.child_status("b")["status"] == "waiting"


def test_a_child_whose_worktree_is_gone_is_reviewed_from_the_source_repo(tmp_path, monkeypatch):
    """The child's worktree was removed while its PR is open: the review runs
    with `--repo` set to the source repo, as its working directory is."""
    e = Epic(tmp_path, monkeypatch)
    tree = e.root / "ws" / ".worktrees" / "buildme-e-a"
    real = coordinator.Coordinator._review

    async def review_(self, c):
        if c.id == "a" and tree.exists():
            subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=e.project, check=True)
        return await real(self, c)

    monkeypatch.setattr(coordinator.Coordinator, "_review", review_)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    [a] = e.reviews("a")
    argv = a.started["argv"]
    assert argv[argv.index("--repo") + 1] == str(e.project)
    assert a.result["exit_code"] == 0 and e.child_status("a")["status"] == "built"


def test_a_merged_pr_is_not_reviewed(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, steps={"a": [{"pr": {"state": "MERGED"}}]})
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    assert e.reviews("a") == [] and e.child_status("a")["status"] == "merged"


def test_the_summary_counts_the_review_rounds(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    summary = json.loads((e.dir / "runs" / "01" / "summary.json").read_text())
    assert {c["id"]: c["rounds"] for c in summary["children"]} == {"a": 1, "b": 1}


def test_a_review_is_a_launch_the_epic_budget_stops(tmp_path, monkeypatch, capsys):
    """Item 19 (PR B2): once `epic.max_usd` is reached nothing more is
    launched, a review included; the run exits 3 with `EPIC:BUDGET`."""
    e = Epic(tmp_path, monkeypatch, steps={"a": [{"spend": 6.0}]}, parallel=1, max_usd=5.0)
    assert e.run() == 3
    assert "EPIC:BUDGET:6.00" in capsys.readouterr().out.splitlines()
    assert e.launched() == [] and e.reviews("a") == []
    assert e.child_status("a")["status"] == "built"


def _spend_after_first_review(e: Epic, monkeypatch) -> None:
    """The epic's spend reaches $6 once a's first review attempt has a result."""
    monkeypatch.setattr(coordinator.Coordinator, "spent",
                        lambda self: (6.0 if any(a.result for a in e.reviews("a")) else 0.0, True))


def test_a_resume_is_a_launch_the_epic_budget_stops(tmp_path, monkeypatch, capsys):
    """Decided 6: the budget is checked before each launch, the `resume`
    after an exit 4 with the hint included."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, max_usd=5.0)
    hint = f"STOPPED: interrupted run found; continue with: python -m review_pipeline resume {e.target()}"
    e.fakes.root.joinpath("reviews.json").write_text(json.dumps({str(N): [{"exit": 4, "stderr": hint}]}))
    _spend_after_first_review(e, monkeypatch)
    assert e.run() == 3
    assert "EPIC:BUDGET:6.00" in capsys.readouterr().out.splitlines()
    assert e.launched() == ["review"]
    assert e.child_status("a")["status"] == "built"


def test_asking_again_after_is_running_now_is_a_launch_the_epic_budget_stops(tmp_path, monkeypatch, capsys):
    running = {"exit": 2, "stderr": "ERROR: a review of x is running now (pid 1); wait for it"}
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, max_usd=5.0, reviews={str(N): [running, {}]})
    _spend_after_first_review(e, monkeypatch)
    assert e.run() == 3
    assert "EPIC:BUDGET:6.00" in capsys.readouterr().out.splitlines()
    assert e.launched() == ["review"]
    assert e.child_status("a")["status"] == "built"


# --- the pieces -------------------------------------------------------------------------

def test_records_past_999_are_seen_and_numbered_in_order(tmp_path):
    agents = tmp_path / records.AGENTS_DIR
    agents.mkdir()
    for n in range(1, 1001):
        (agents / f"{n:03d}-review-a.started.json").write_text(
            json.dumps({"kind": "review", "child": "a", "argv": []}))
    b = records.start(tmp_path, "build", "b", ["x"])
    assert b.n == 1001
    got = records.attempts(tmp_path)
    assert [a.n for a in got] == list(range(1, 1002))
    assert (got[-1].kind, got[-1].child) == ("build", "b")


def test_conflicts_reads_mergeable_and_merge_state():
    assert review.conflicts({"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}) is True
    assert review.conflicts({"mergeable": "MERGEABLE", "mergeStateStatus": "BEHIND"}) is False
    assert review.conflicts({"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}) is None
    assert review.conflicts({}) is None


def test_blockers_follows_what_the_review_lists(tmp_path):
    (tmp_path / "findings.json").write_text(json.dumps({"mode": "critical", "findings": [
        {**high("f1"), "impact": "breaks-users"}, high("f2"),
        {**high("f3"), "assessment": {"label": "repeat"}}]}))
    assert [b.split()[1] for b in review.blockers(tmp_path)] == ["f1"]
    assert review.blockers(tmp_path / "missing") is None
