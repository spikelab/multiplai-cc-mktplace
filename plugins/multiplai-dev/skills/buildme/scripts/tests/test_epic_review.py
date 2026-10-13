"""Epic mode, PR C: each child PR is reviewed (plan-buildme-epic-mode-2026-10-12.md,
items 21-25). The build, `gh` and the review skill are fakes first on PATH
(`epic_row_helpers.Fakes`); the review writes the review skill's ledger
lines under `<root>/ws/.multiplai/data/reviews/`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from build_pipeline.config import EpicSettings
from build_pipeline.epic import coordinator, records
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic import review
from build_pipeline.epic.split import EpicSplitAnswer

from .epic_row_helpers import (
    CallLog,
    Fakes,
    approve,
    child,
    make_build,
    make_project,
    pr_number,
    pr_url,
    run_coordinator,
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
    def __init__(self, root: Path, monkeypatch, answer=TWO, steps=None, reviews=None, rounds=6):
        self.root = root
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.fakes = Fakes(root, monkeypatch, steps, reviews)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=2, max_review_rounds=rounds)
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
    assert e.run() == 0
    assert sorted(e.launched()) == ["review", "review"]
    [a] = e.reviews("a")
    assert a.started["argv"][:6] == ["uv", "run", "--directory", str(review.review_skill_dir()),
                                     "python", "-m"]
    assert a.started["argv"][6:] == ["review_pipeline", "review", "--repo",
                                     str(e.root / "ws" / ".worktrees" / "buildme-e-a"),
                                     "--pr", str(N), "--trust-repo"]
    assert a.started["head_sha"] == e.head() and a.started["pr_round"] == 0
    r = a.result
    assert (r["exit_code"], r["interrupted"], r["reused"], r["reused_pipeline_run"]) == (0, False, False, None)
    assert (r["head_sha"], r["base_sha"], r["review_dir"]) == (e.head(), "b" * 40, str(e.target()))
    assert (r["cost_usd"], r["pr_round"], r["conflicts"], r["blocking"]) == (1.5, 1, False, [])
    assert e.child_status("a")["status"] == "built" and e.child_status("a")["pr_round"] == 1


def test_a_head_already_in_the_ledger_starts_no_review(tmp_path, monkeypatch):
    """Done means 5: no review starts, and the record has `reused: true`."""
    e = Epic(tmp_path, monkeypatch)
    e.add_ledger({"event": "finished", "slug": f"o-r--pr-{N}", "pr": N, "target_dir": str(e.target()),
                  "head_sha": e.head(), "base_sha": "c" * 40, "round": "03", "stopped": None,
                  "run": {"cost_usd": 2.0}})
    assert e.run() == 0
    assert e.launched() == ["review"]                       # b's only
    [a] = e.reviews("a")
    assert a.started["argv"][6:8] == ["review_pipeline", "review"]
    r = a.result
    assert (r["exit_code"], r["markers"], r["interrupted"], r["reused"]) == (0, [], False, True)
    assert r["reused_pipeline_run"] == {"target_dir": str(e.target()), "round": "03"}
    assert (r["base_sha"], r["cost_usd"], r["pr_round"]) == ("c" * 40, 2.0, 1)


def test_a_stopped_review_of_another_head_does_not_count(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch)
    for head, stopped in (("d" * 40, None), ("e" * 40, "budget"), ("f" * 40, None)):
        e.add_ledger({"event": "finished", "slug": f"o-r--pr-{N}", "pr": N, "target_dir": str(e.target()),
                      "head_sha": head, "stopped": stopped, "run": {"cost_usd": 1.0}})
    e.add_ledger({"event": "finished", "slug": f"o-r--pr-{N + 1}", "pr": N + 1, "head_sha": "a" * 40,
                  "stopped": None})
    e.add_ledger({"event": "finished", "slug": f"x-y--pr-{N}", "pr": N, "head_sha": "9" * 40, "stopped": None})
    assert e.run() == 0
    assert e.reviews("a")[-1].result["pr_round"] == 3


def test_a_new_head_is_a_new_round_and_the_round_count_survives_a_resume(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch)
    assert e.run() == 0
    e.fakes.set_head("e-a", "c" * 40)
    assert e.run() == 0
    assert e.launched().count("review") == 3
    assert e.reviews("a")[-1].result["pr_round"] == 2 and e.child_status("a")["pr_round"] == 2
    assert e.run() == 0                                       # same head: nothing new
    assert e.launched().count("review") == 3 and len(e.reviews("a")) == 2
    assert e.child_status("a")["pr_round"] == 2


def test_an_interrupted_review_is_resumed(tmp_path, monkeypatch):
    """A review of this head that started and never finished (its `started`
    ledger line and saved state, as a killed review leaves them): `review`
    exits 4 with the hint, and the coordinator runs `resume`."""
    e = Epic(tmp_path, monkeypatch)
    e.target().mkdir(parents=True)
    (e.target() / "review-state.json").write_text(json.dumps({"head": e.head()}))
    e.add_ledger({"event": "started", "slug": f"o-r--pr-{N}", "target_dir": str(e.target()),
                  "head_sha": e.head()})
    assert e.run() == 0
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
    assert e.run() == 0
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


def test_a_merged_pr_is_not_reviewed(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, steps={"a": [{"pr": {"state": "MERGED"}}]})
    assert e.run() == 0
    assert e.reviews("a") == [] and e.child_status("a")["status"] == "merged"


def test_the_summary_counts_the_review_rounds(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch)
    assert e.run() == 0
    summary = json.loads((e.dir / "runs" / "01" / "summary.json").read_text())
    assert {c["id"]: c["rounds"] for c in summary["children"]} == {"a": 1, "b": 1}


# --- the pieces -------------------------------------------------------------------------

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
