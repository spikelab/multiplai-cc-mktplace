"""Row tests for PR C's "Stop and resume states" table
(plan-buildme-epic-mode-2026-10-12.md, rows marked `Test: yes`: C.S2, C.S3, C.S4).

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
from build_pipeline.epic.split import EpicSplitAnswer

from .epic_row_helpers import (
    CallLog,
    Fakes,
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
        return [a for a in records.attempts(self.dir) if a.child == "a" and a.kind in ("review", "resume")]

    def finished(self) -> bool:
        ledger = self.data / "reviews" / "ledger.jsonl"
        lines = [json.loads(x) for x in ledger.read_text().splitlines()] if ledger.exists() else []
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

    def snapshot(self) -> dict:
        names = commit_names([self.root / "o" / "r.git", self.project])
        return name_commits({"epic": epic_snapshot(self.dir, self.root),
                             "records": records_snapshot(self.dir, self.root),
                             "summary": last_summary(self.dir, self.root)}, names)

    def launches(self) -> list[dict]:
        return [*CallLog(self.root / "calls.log").calls(), *self.fakes.review_launches()]


def _roots(tmp_path, monkeypatch, reviews) -> tuple[Root, Root]:
    return (Root(tmp_path / "stopped", monkeypatch, reviews),
            Root(tmp_path / "clean", monkeypatch, reviews))


def _die_once_reviewing(coord) -> None:
    """A seam for `run_killed`: exit 137 at the first look while a's review runs."""
    if coord.children["a"].status == "reviewing":
        os._exit(137)


def _kill_at_review() -> None:
    coordinator._tick = _die_once_reviewing


def _release_when_reviewed(root: Root):
    def tick(coord):
        if any(a.started.get("head_sha") for a in root.reviews()) and not root.finished():
            root.release_and_wait()
    return tick


def _assert_same(stopped: Root, clean: Root, want_a: list[tuple], extra: list[str]) -> None:
    """Same epic files and summary; b's records equal, a's records as *want_a*
    (with "<review>" for the review argv and "<build>" for a's build record);
    launches equal plus one review skill launch per kind in *extra*."""
    s, c = stopped.snapshot(), clean.snapshot()
    assert s["epic"] == c["epic"]
    assert s["summary"] == c["summary"]
    assert s["records"]["b"] == c["records"]["b"]
    build, review = c["records"]["a"][0], c["records"]["a"][1]
    argv = {"review": review[1],
            "resume": [*review[1][:7], "resume", f"<root>/ws/.multiplai/data/reviews/o-r/pr-{N}", "--trust-repo"]}
    want = [build if w == "<build>" else (w[0], argv[w[0]], *w[2:]) for w in want_a]
    assert s["records"]["a"] == want

    def key(calls):
        return sorted(json.dumps(x, sort_keys=True) for x in calls)

    review_call = next(x for x in clean.fakes.review_launches() if f"--pr {N}" in " ".join(x["argv"]))
    extra_calls = [review_call if k == "review" else
                   next(x for x in stopped.fakes.review_launches() if x["kind"] == "resume") for k in extra]
    assert key(stopped.launches()) == key([*clean.launches(), *extra_calls])


def _result(root: Root, i: int) -> dict:
    return root.reviews()[i].result


def test_C_S2_coordinator_killed_with_the_review_alive_waits_for_it(tmp_path, monkeypatch):
    """C.S2: the resumed `review` answers exit 2 `is running now`; the
    coordinator waits 60 s and asks again until the review finished, then
    writes the killed record's result from the ledger. Never two reviews.
    One more launch and pair."""
    reviews = {str(N): [{"block": True}, {}]}
    stopped, clean = _roots(tmp_path, monkeypatch, reviews)
    stopped.killed(_kill_at_review)
    pid = stopped.review_pid()
    assert subprocess.run(["kill", "-0", str(pid)]).returncode == 0      # the review outlived it

    def tick(coord):
        answered = [a for a in stopped.reviews() if (a.result or {}).get("exit_code") == 2]
        if answered and not stopped.finished():
            assert len([a for a in stopped.reviews() if a.result is None]) == 1   # never two
            stopped.release_and_wait()

    assert stopped.run(tick) == 0
    assert clean.run(_release_when_reviewed(clean)) == 0
    _assert_same(stopped, clean, ["<build>", ("review", None, None, [], True), ("review", None, 2, [], False)],
                 ["review"])
    old = _result(stopped, 0)
    assert (old["reused"], old["pr_round"], old["head_sha"]) == (True, 1, _result(clean, 0)["head_sha"])
    assert old["reused_pipeline_run"] == {"target_dir": str(stopped.target), "round": "01"}


def test_C_S3_review_killed_with_its_coordinator_is_resumed(tmp_path, monkeypatch):
    """C.S3: the killed record's result is written; `review` exits 4 with the
    hint and the coordinator runs `resume`. Two more launches and pairs."""
    reviews = {str(N): [{"block": True}, {}]}
    stopped, clean = _roots(tmp_path, monkeypatch, reviews)
    stopped.killed(_kill_at_review)
    pid = stopped.review_pid()
    os.killpg(pid, signal.SIGKILL)
    for _ in range(500):
        stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
        if not stat.strip() or stat.strip().startswith("Z"):
            break
        time.sleep(0.02)
    assert stopped.run() == 0
    assert clean.run(_release_when_reviewed(clean)) == 0
    _assert_same(stopped, clean, ["<build>", ("review", None, None, [], True), ("review", None, 4, [], False),
                                  ("resume", None, 0, [], False)], ["review", "resume"])
    last = _result(stopped, 2)
    assert (last["reused"], last["pr_round"], last["review_dir"]) == (False, 1, str(stopped.target))
    assert _result(stopped, 0).get("reused") is None


def test_C_S4_finished_review_whose_result_was_not_recorded_is_reused(tmp_path, monkeypatch):
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
    assert stopped.run() == 0
    assert clean.run() == 0
    _assert_same(stopped, clean, ["<build>", ("review", None, None, [], True)], [])
    got, want = _result(stopped, 0), _result(clean, 0)
    assert (got["reused"], got["pr_round"], want["pr_round"]) == (True, 1, 1)
    keys = ("head_sha", "base_sha", "review_dir", "cost_usd", "blocking", "conflicts")
    assert {k: str(got[k]).replace(str(stopped.root), "<root>") for k in keys} == \
        {k: str(want[k]).replace(str(clean.root), "<root>") for k in keys}
