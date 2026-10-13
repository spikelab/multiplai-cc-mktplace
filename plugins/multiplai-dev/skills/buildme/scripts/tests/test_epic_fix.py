"""Epic mode, PR D2: a review with blockers runs the fix loop
(plan-buildme-epic-mode-2026-10-12.md, items 32-34). The build, `gh`, the
review skill (`review`, `post`) and the fixer (`python -m build_pipeline
findings`) are fakes first on PATH (`epic_row_helpers.Fakes`)."""

from __future__ import annotations

import json
import signal
import subprocess
from pathlib import Path

import pytest

from build_pipeline.config import EpicSettings
from build_pipeline.epic import records
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic.split import EpicSplitAnswer

from .epic_row_helpers import (
    CallLog,
    Fakes,
    approve,
    child,
    make_build,
    make_project,
    pr_number,
    run_coordinator,
    run_signalled,
    use_workspace,
)

TWO = EpicSplitAnswer(single=False, reason="Two parts.", children=[child("a"), child("b")])
CHAIN = EpicSplitAnswer(single=False, reason="Two parts.", children=[child("a"), child("b", ("a",))])
N = pr_number("e-a")


def high(fid: str = "f1", severity: str = "HIGH") -> dict:
    return {"id": fid, "severity": severity, "status": "confirmed", "claim": f"{fid} is wrong",
            "file": "src/app.py", "line_start": 3}


BLOCKED = {str(N): [{"findings": [high()]}]}


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    epic_lock.release_all()


class Epic:
    def __init__(self, root: Path, monkeypatch, answer=TWO, reviews=None, fixes=None, rounds=6, max_usd=None):
        self.root = root
        self.data = use_workspace(monkeypatch, root)
        self.project = make_project(root)
        self.fakes = Fakes(root, monkeypatch, {}, BLOCKED if reviews is None else reviews, fixes)
        self.config = make_build(self.project)
        self.config.epic = EpicSettings(max_parallel=2, max_review_rounds=rounds, max_usd=max_usd)
        approve(self.config, answer, CallLog(root / "split.log"))
        self.dir = self.data / "buildme" / "epics" / "o-r--e"
        self.target = self.data / "reviews" / "o-r" / f"pr-{N}"
        self.worktree = root / "ws" / ".worktrees" / "buildme-e-a"

    def run(self) -> int:
        return run_coordinator(self.config)

    def status(self, cid: str = "a") -> dict:
        return json.loads((self.dir / "epic.json").read_text())["child_status"][cid]

    def tries(self, cid: str = "a") -> list[records.Attempt]:
        return [a for a in records.attempts(self.dir) if a.child == cid]

    def kinds(self, cid: str = "a") -> list[str]:
        return [a.kind for a in self.tries(cid)]

    def fixes(self) -> list[records.Attempt]:
        return [a for a in self.tries() if a.kind == "fix"]

    def head(self) -> str:
        return subprocess.run(["git", "rev-parse", "refs/heads/buildme/e-a"], cwd=self.project,
                              capture_output=True, text=True, check=True).stdout.strip()

    def posted(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted((self.root / "posted").glob("post.*.json"))]


def _rounds(arg: list[str]) -> int:
    return int(arg[arg.index("--max-rounds") + 1])


def test_blockers_lead_to_a_post_then_a_fix_and_round_counts_add_up(tmp_path, monkeypatch):
    """32.1-32.3: the review's blockers are posted, then the fixer runs with
    `--max-rounds` = its rounds so far + the rounds left; back at the review
    step, the fixer's last review is reused, and blockers on it start one
    more post and fix."""
    fixes = {str(N): [{"rounds": [{"findings": [high("f2")]}, {"findings": [high("f3", "MEDIUM")]}]},
                      {"rounds": [{}]}]}
    e = Epic(tmp_path, monkeypatch, fixes=fixes)
    assert e.run() == 0
    assert e.kinds() == ["build", "review", "post", "fix", "review", "post", "fix", "review"]
    assert [x["kind"] for x in e.fakes.review_launches()] == ["review", "review"]      # a's first, b's
    first, second = (a.started["argv"] for a in e.fixes())
    assert first == ["python", "-m", "build_pipeline", "--session-id", "", "findings", "--pr", str(N),
                     "--change", "e-a", "--project-dir", str(e.worktree), "--auto", "--trust-repo",
                     "--max-rounds", "5"]
    assert (_rounds(first), _rounds(second)) == (0 + 6 - 1, 2 + 6 - 3)
    assert [x["cwd"] for x in e.fakes.fix_launches()] == ["<root>/ws/.worktrees/buildme-e-a"] * 2
    posts = [a for a in e.tries() if a.kind == "post"]
    decisions = e.target / "buildme-decisions.json"
    assert all(a.started["argv"][6:] == ["review_pipeline", "post", str(e.target), "--decisions", str(decisions)]
               for a in posts)
    assert [x["cwd"] for x in e.fakes.post_launches()] == ["<root>/ws/.worktrees/buildme-e-a"] * 2
    assert e.posted() == [{"f1": {"decision": "accept"}}, {"f3": {"decision": "accept"}}]
    assert [a.result["exit_code"] for a in e.tries() if a.kind in ("post", "fix")] == [0, 0, 0, 0]
    last = e.tries()[-1]
    assert (last.started["head_sha"], last.result["reused"], last.result["blocking"]) == (e.head(), True, [])
    st = e.status()
    assert (st["status"], st["pr_round"]) == ("built", 4)
    summary = json.loads(sorted((e.dir / "runs").glob("*/summary.json"))[-1].read_text())
    assert next(c for c in summary["children"] if c["id"] == "a")["rounds"] == 4


def test_a_clean_review_runs_no_post_and_no_fix(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, reviews={})
    assert e.run() == 0
    assert e.kinds() == ["build", "review"]
    assert e.fakes.post_launches() == [] and e.fakes.fix_launches() == []


def test_at_the_cap_the_child_parks(tmp_path, monkeypatch):
    """Item 23 after the fix: the fixer's reviews count, and at the cap with
    blockers the child parks with them; a later run launches nothing."""
    fixes = {str(N): [{"rounds": [{"findings": [high("f2")]}, {"findings": [high("f3")]}]}]}
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, fixes=fixes, rounds=3)
    assert e.run() == 0
    assert [_rounds(a.started["argv"]) for a in e.fixes()] == [2]
    st = e.status()
    assert st["status"] == "parked" and "review round 3 of 3" in st["reason"] and "f3" in st["reason"]
    assert e.status("b")["status"] == "skipped"
    assert e.run() == 0
    assert len(e.fakes.fix_launches()) == 1 and len(e.fakes.post_launches()) == 1
    assert e.status()["status"] == "parked"


def test_findings_failed_parks_with_the_reason_and_stays_parked(tmp_path, monkeypatch):
    """D2.S6: `FINDINGS:FAILED:<step>:<reason>` parks the child with the reason."""
    failed = "FINDINGS:FAILED:fetch:/w has uncommitted changes; commit or stash them first"
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, fixes={str(N): [{"exit": 1, "stdout": failed}]})
    assert e.run() == 0
    st = e.status()
    assert st["status"] == "parked"
    assert st["reason"] == "the fixer failed at fetch: /w has uncommitted changes; commit or stash them first"
    assert e.fixes()[0].result["parked"] == st["reason"]
    assert e.status("b")["status"] == "skipped"
    assert e.run() == 0
    assert len(e.fakes.fix_launches()) == 1 and e.status()["status"] == "parked"


def test_a_fixer_that_fails_otherwise_parks_with_its_output_tail(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, fixes={str(N): [{"exit": 2, "stdout": "ERROR: PR 1 has no head branch"}]})
    assert e.run() == 0
    st = e.status()
    assert st["status"] == "parked" and st["reason"].startswith("the fixer exited 2: ")
    assert "ERROR: PR 1 has no head branch" in st["reason"]


def test_a_failed_post_parks_and_runs_no_fixer(tmp_path, monkeypatch):
    reviews = {**BLOCKED, "post": [{"exit": 1, "stderr": "ERROR: gh api failed: HTTP 502"}]}
    e = Epic(tmp_path, monkeypatch, reviews=reviews)
    assert e.run() == 0
    st = e.status()
    assert st["status"] == "parked" and st["reason"].startswith("posting the review exited 1: ")
    assert "HTTP 502" in st["reason"]
    assert e.fakes.fix_launches() == []
    assert e.run() == 0
    assert len(e.fakes.post_launches()) == 1 and e.status()["status"] == "parked"


def test_a_review_folder_without_findings_parks_before_posting(tmp_path, monkeypatch):
    """28's rule in the coordinator: no `findings.json` to choose from parks
    the child, naming the file; nothing is posted."""
    e = Epic(tmp_path, monkeypatch)
    from build_pipeline.epic import coordinator

    real = coordinator.Coordinator._post

    async def post(self, c, rec, head):
        (Path(rec.result["review_dir"]) / "findings.json").unlink()
        return await real(self, c, rec, head)

    monkeypatch.setattr(coordinator.Coordinator, "_post", post)
    assert e.run() == 0
    st = e.status()
    assert st["status"] == "parked" and st["reason"].startswith("cannot choose what to post: no findings.json at ")
    assert e.fakes.post_launches() == [] and e.fakes.fix_launches() == []


def test_a_fix_that_leaves_the_head_where_it_was_runs_once(tmp_path, monkeypatch):
    """Item 33: a head with a `fix` record after its review goes to neither
    32.1 nor 32.2 again, in this run or the next."""
    e = Epic(tmp_path, monkeypatch)
    assert e.run() == 0
    assert e.kinds() == ["build", "review", "post", "fix"]
    assert e.status()["status"] == "built"
    assert e.run() == 0
    assert e.kinds() == ["build", "review", "post", "fix"]


def test_a_fix_is_a_launch_the_epic_budget_stops(tmp_path, monkeypatch, capsys):
    """Decided 6: the review's $1.50 reaches `epic.max_usd`, so neither the
    post nor the fixer starts."""
    e = Epic(tmp_path, monkeypatch, max_usd=1.0)
    assert e.run() == 3
    assert "EPIC:BUDGET:" in capsys.readouterr().out
    assert e.kinds() == ["build", "review"]
    assert e.fakes.post_launches() == [] and e.fakes.fix_launches() == []


def test_a_fixer_killed_alone_is_relaunched_by_the_next_run(tmp_path, monkeypatch):
    """D2.S5 with the coordinator alive: the fixer died of a signal, so its
    result is `interrupted` and the child is not parked; the next run
    relaunches it, and `findings` resumes itself."""
    e = Epic(tmp_path, monkeypatch, fixes={str(N): [{"die": True}, {"rounds": [{}]}]})
    assert e.run() == 0
    [fix] = e.fixes()
    assert (fix.result["exit_code"], fix.result["interrupted"]) == (-signal.SIGKILL, True)
    assert e.status()["status"] == "built"
    assert e.run() == 0
    assert e.kinds() == ["build", "review", "post", "fix", "fix", "review"]
    assert e.fixes()[1].result["exit_code"] == 0
    assert len(e.fakes.post_launches()) == 1


def test_a_signal_stops_the_fixer_and_the_next_run_relaunches_it(tmp_path, monkeypatch):
    """SIGTERM stops the fixer this run started (in its own process group);
    its result is `interrupted`, and the next run relaunches it (D2.S11)."""
    e = Epic(tmp_path, monkeypatch, fixes={str(N): [{"block": True}, {"rounds": [{}]}]})
    rc = run_signalled(e.run, lambda: len(e.fakes.fix_launches()) == 1, signal.SIGTERM)
    assert rc == 128 + signal.SIGTERM
    [fix] = e.fixes()
    assert fix.result["interrupted"] is True
    assert e.run() == 0
    assert [a.result["exit_code"] for a in e.fixes()][1:] == [0]
    assert e.kinds()[-1] == "review" and e.status()["status"] == "built"
