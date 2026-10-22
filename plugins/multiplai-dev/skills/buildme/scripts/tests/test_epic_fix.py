"""Epic mode, PR D2: a review with blockers runs the fix loop
(plan-buildme-epic-mode-2026-10-12.md, items 32-34). The build, `gh`, the
review skill (`review`, `post`) and the fixer (`python -m build_pipeline
findings`) are fakes first on PATH (`epic_row_helpers.Fakes`)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from build_pipeline.config import EpicSettings
from build_pipeline.epic import lock as epic_lock
from build_pipeline.epic import records

from .epic_row_helpers import (
    CallLog,
    EpicSplitAnswer,
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
    run_killed,
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
    `--max-rounds` = every round in its state + 1 (one round per launch); back at the review
    step, the fixer's last review is reused, and blockers on it start one
    more post and fix."""
    fixes = {str(N): [{"rounds": [{"findings": [high("f2")]}, {"findings": [high("f3", "MEDIUM")]}]},
                      {"rounds": [{}]}]}
    e = Epic(tmp_path, monkeypatch, fixes=fixes)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    assert e.kinds() == ["build", "review", "post", "fix", "review", "post", "fix", "review"]
    assert [x["kind"] for x in e.fakes.review_launches()] == ["review", "review"]      # a's first, b's
    first, second = (a.started["argv"] for a in e.fixes())
    session = e.tries()[0].started["argv"][4]           # the build's own --session-id
    assert first == ["python", "-m", "build_pipeline", "--session-id", session, "findings", "--pr", str(N),
                     "--change", "e-a", "--project-dir", str(e.worktree), "--auto", "--trust-repo",
                     "--max-rounds", "1", "--severities", "HIGH,MEDIUM", "--code-only", "--no-escalation"]
    assert (_rounds(first), _rounds(second)) == (0 + 1, 2 + 1)
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
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    assert e.kinds() == ["build", "review"]
    assert e.fakes.post_launches() == [] and e.fakes.fix_launches() == []


def test_at_the_cap_the_child_parks(tmp_path, monkeypatch):
    """Item 23 after the fix: the fixer's reviews count, and at the cap with
    blockers the child parks with them; a later run launches nothing."""
    fixes = {str(N): [{"rounds": [{"findings": [high("f2")]}, {"findings": [high("f3")]}]}]}
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, fixes=fixes, rounds=3)
    assert e.run() == 0
    assert [_rounds(a.started["argv"]) for a in e.fixes()] == [1]
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
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    st = e.status()
    assert st["status"] == "parked" and st["reason"].startswith("the fixer exited 2: ")
    assert "ERROR: PR 1 has no head branch" in st["reason"]


def test_a_failed_post_parks_and_runs_no_fixer(tmp_path, monkeypatch):
    reviews = {**BLOCKED, "post": [{"exit": 1, "stderr": "ERROR: gh api failed: HTTP 502"}]}
    e = Epic(tmp_path, monkeypatch, reviews=reviews)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    st = e.status()
    assert st["status"] == "parked" and st["reason"].startswith("posting the review exited 1: ")
    assert "HTTP 502" in st["reason"]
    assert e.fakes.fix_launches() == []
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
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
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    st = e.status()
    assert st["status"] == "parked" and st["reason"].startswith("cannot choose what to post: no findings.json at ")
    assert e.fakes.post_launches() == [] and e.fakes.fix_launches() == []


@pytest.mark.parametrize("step,end", [({}, "FINDINGS:DONE:0"), ({"max_rounds": True}, "FINDINGS:MAX_ROUNDS:0")],
                         ids=["done", "max-rounds"])
def test_a_fixer_that_ends_without_moving_the_head_parks_the_child(tmp_path, monkeypatch, step, end):
    """D2.S15: `findings` exited 0 saying it is done (or at its own cap) and
    the PR's head is the one reviewed with blockers: nothing will fix them,
    so the child parks with its blockers. Item 33: a head with a `fix` after
    its review goes to neither 32.1 nor 32.2 again; the next run reuses the
    review, launches nothing and parks the child again."""
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, fixes={str(N): [step]})
    assert e.run() == 0
    assert e.kinds() == ["build", "review", "post", "fix"]
    head = e.head()
    assert e.tries()[1].started["head_sha"] == head
    st = e.status()
    assert st["status"] == "parked"
    assert st["reason"] == (f"the fixer ended ({end}) and left {head[:12]} with 1 blocker(s): "
                            + "; ".join(e.tries()[1].result["blocking"]))
    assert e.status("b")["status"] == "skipped"
    assert e.run() == 0
    assert e.kinds() == ["build", "review", "post", "fix"]
    assert len(e.fakes.post_launches()) == 1 and len(e.fakes.fix_launches()) == 1
    assert e.status()["status"] == "parked" and e.status()["reason"] == st["reason"]
    assert len([x for x in e.fakes.review_launches() if str(N) in x["argv"]]) == 1


def test_a_fix_is_a_launch_the_epic_budget_stops(tmp_path, monkeypatch, capsys):
    """Decided 6: once a's review has a result the epic's spend is $6, past
    `epic.max_usd`, so neither the post nor the fixer starts."""
    from build_pipeline.epic import coordinator

    e = Epic(tmp_path, monkeypatch, max_usd=5.0)
    monkeypatch.setattr(coordinator.Coordinator, "spent",
                        lambda self: (6.0 if any(a.result for a in e.tries() if a.kind == "review") else 0.0, True))
    assert e.run() == 3
    assert "EPIC:BUDGET:6.00" in capsys.readouterr().out.splitlines()
    assert e.kinds() == ["build", "review"]
    assert e.fakes.post_launches() == [] and e.fakes.fix_launches() == []
    assert e.status()["status"] == "built"


def test_a_fixer_killed_alone_is_relaunched_by_the_next_run(tmp_path, monkeypatch):
    """D2.S5 with the coordinator alive: the fixer died of a signal, so its
    result is `interrupted` and the child is not parked; the next run
    relaunches it, and `findings` resumes itself."""
    e = Epic(tmp_path, monkeypatch, fixes={str(N): [{"die": True}, {"rounds": [{}]}]})
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    [fix] = e.fixes()
    assert (fix.result["exit_code"], fix.result["interrupted"]) == (-signal.SIGKILL, True)
    assert e.status()["status"] == "built"
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
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
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    assert [a.result["exit_code"] for a in e.fixes()][1:] == [0]
    assert e.kinds()[-1] == "review" and e.status()["status"] == "built"


# --- resumes that must not repeat a paid step, and the round count -----------------

def _state(project: Path, name: str, rounds: list[list[str]]) -> None:
    path = project / "specs" / "changes" / name / ".findings-state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"findings": {"rounds": [{"number": i + 1, "finished": f}
                                                        for i, f in enumerate(rounds)]}}))


ALL = ["fetch", "triage", "spec", "code", "publish", "review"]


def test_rounds_done_counts_reviewed_rounds_under_the_name_findings_uses(tmp_path):
    """`findings` keeps its state under the normalised change name, and a round
    whose review has not finished is not yet in `pr_round`: it is not counted."""
    from build_pipeline.epic import fix

    _state(tmp_path, "spk-12-a", [ALL, ALL, ["fetch", "triage"]])
    assert fix.rounds_done(tmp_path, "SPK_12-a") == 2
    assert fix.rounds_done(tmp_path, "spk-12-a") == 2


@pytest.mark.parametrize("text", ["[]", '{"findings": {"rounds": null}}', "{not json",
                                  '{"findings": {"rounds": [1, null]}}'])
def test_rounds_done_reads_a_malformed_state_as_no_rounds(tmp_path, text):
    from build_pipeline.epic import fix

    path = tmp_path / "specs" / "changes" / "e-a" / ".findings-state.json"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    assert fix.rounds_done(tmp_path, "e-a") == 0
    assert fix.rounds_done(tmp_path, "e-b") == 0          # no change directory


def test_a_fixer_killed_part_way_through_a_round_is_relaunched_within_the_cap(tmp_path, monkeypatch):
    """D2.S5/S11 with a round open: the killed fixer had finished round 1 and
    started round 2, whose review is not in the ledger yet. The relaunch gets
    the same `--max-rounds` as the first launch, so `findings` finishing round
    2 and running on stops where a fixer that never stopped would."""
    fixes = {str(N): [{"rounds": [{"findings": [high("f2")]}, {"stop": "fetch"}]}, {"rounds": [{}]}]}
    e = Epic(tmp_path, monkeypatch, fixes=fixes)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    assert e.status()["status"] == "built" and e.fixes()[0].result["interrupted"] is True
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    assert [_rounds(a.started["argv"]) for a in e.fixes()] == [1, 1]
    st = e.status()
    assert (st["status"], st["pr_round"]) == ("built", 3)


def test_a_fixer_killed_after_its_last_allowed_review_is_not_relaunched(tmp_path, monkeypatch):
    """The fixer's review of round 1 reached the cap, then it was killed before
    the round recorded it. No review round is left, so no fixer runs again:
    the review step reuses that review and parks at the cap."""
    fixes = {str(N): [{"rounds": [{"findings": [high("f2")], "stop": "review"}]}]}
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, fixes=fixes, rounds=2)
    assert e.run() == 3                                  # b waits for a's merge
    assert [_rounds(a.started["argv"]) for a in e.fixes()] == [1]
    assert e.status()["status"] == "built"
    assert e.run() == 0
    assert len(e.fakes.fix_launches()) == 1
    st = e.status()
    assert st["status"] == "parked" and "review round 2 of 2" in st["reason"] and "f2" in st["reason"]


@pytest.mark.parametrize("left", ["no result", "interrupted"])
def test_a_failed_fixer_whose_result_was_not_recorded_parks_on_resume(tmp_path, monkeypatch, left):
    """D2.S6 after a stop: `findings` printed `FINDINGS:FAILED` and exited,
    and the coordinator stopped before recording that (or recorded it as
    interrupted). The resume parks with the reason from the log and does not
    run `findings` again, which would repeat the failed step."""
    failed = "FINDINGS:FAILED:code:block 2 failed"
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, fixes={str(N): [{"exit": 1, "stdout": failed}, {"rounds": [{}]}]})
    assert e.run() == 0
    [fx] = e.fixes()
    if left == "no result":
        fx.result_path.unlink()
    else:
        fx.result_path.write_text(json.dumps({**fx.result, "interrupted": True, "parked": None}))
    assert e.run() == 0
    assert len(e.fakes.fix_launches()) == 1
    st = e.status()
    assert (st["status"], st["reason"]) == ("parked", "the fixer failed at code: block 2 failed")
    assert e.run() == 0
    assert len(e.fakes.fix_launches()) == 1 and e.status()["status"] == "parked"


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_a_fixer_stopped_alone_by_sigterm_or_sighup_is_relaunched(tmp_path, monkeypatch, sig):
    """D2.S5: `findings` turns SIGTERM and SIGHUP into exit 128 + signal. That
    is a stop, not a failure: the result is `interrupted`, the child stays
    `built`, and the next run relaunches it."""
    from build_pipeline.epic import coordinator

    e = Epic(tmp_path, monkeypatch, fixes={str(N): [{"block": True}, {"rounds": [{}]}]})
    sent = []

    def tick(coord):
        if not sent and e.fakes.fix_launches():
            pid = coordinator.fix_pid(N, e.project)
            if pid:
                os.kill(pid, sig)
                sent.append(pid)

    monkeypatch.setattr(coordinator, "_tick", tick)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    [fx] = e.fixes()
    assert (fx.result["exit_code"], fx.result["interrupted"]) == (128 + sig, True)
    assert e.status()["status"] == "built"
    monkeypatch.setattr(coordinator, "_tick", lambda coord: None)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    assert [a.result["exit_code"] for a in e.fixes()] == [128 + sig, 0]
    assert e.status()["status"] == "built"


def test_a_post_still_running_after_its_coordinator_died_is_waited_for(tmp_path, monkeypatch):
    """D2.S2 with the post alive: the coordinator died while `post` ran in its
    own session. The resume launches nothing while it lives, then writes its
    result and posts again; two posts never run at once."""
    from build_pipeline.epic import coordinator

    e = Epic(tmp_path, monkeypatch, reviews={**BLOCKED, "post": [{"block": True}, {}]},
             fixes={str(N): [{"rounds": [{}]}]})

    def setup():
        def die_while_posting(coord):
            c = coord.children["a"]
            if c.rattempt is not None and c.rattempt.kind == "post":
                os._exit(137)

        coordinator._tick = die_while_posting

    run_killed(e.run, setup)
    [post] = [a for a in e.tries() if a.kind == "post"]
    assert post.result is None
    target = str(e.target)
    pid = None
    for _ in range(500):
        pid = coordinator.post_pid(target)
        if pid:
            break
        time.sleep(0.02)
    assert pid and alive(pid)
    looks = []

    def tick(coord):
        if alive(pid):
            looks.append(1)
            assert len(e.fakes.post_launches()) == 1          # never a second post
            if len(looks) == 5:
                (e.root / "release" / "post-1").write_text("")
                for _ in range(500):
                    if not alive(pid):
                        break
                    time.sleep(0.02)

    monkeypatch.setattr(coordinator, "_tick", tick)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    assert len(looks) == 5
    posts = [a for a in e.tries() if a.kind == "post"]
    assert [(a.result["exit_code"], a.result["interrupted"]) for a in posts] == [(None, True), (0, False)]
    assert len(e.fakes.post_launches()) == 2 and len(e.fakes.fix_launches()) == 1
    assert e.status()["status"] == "built"


def test_a_post_killed_by_a_signal_is_posted_again_by_the_next_run(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch, reviews={**BLOCKED, "post": [{"die": True}, {}]},
             fixes={str(N): [{"rounds": [{}]}]})
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    [post] = [a for a in e.tries() if a.kind == "post"]
    assert (post.result["exit_code"], post.result["interrupted"]) == (-signal.SIGKILL, True)
    assert e.status()["status"] == "built" and e.fakes.fix_launches() == []
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    assert len(e.fakes.post_launches()) == 2 and len(e.fakes.fix_launches()) == 1
    assert e.status()["status"] == "built"


@pytest.mark.parametrize("code", [128 + signal.SIGKILL, 128 + signal.SIGTERM, 128 + signal.SIGHUP],
                         ids=["137", "143", "129"])
def test_a_post_that_uv_reports_stopped_by_a_signal_is_posted_again_by_the_next_run(tmp_path, monkeypatch, code):
    """D2.S12: the post runs under `uv run`, which reports a child killed by a
    signal as 128 + signal, not as a negative code. That is a stop, not a
    failure: the result is `interrupted`, the child stays `built`, and the
    next run posts again and runs the fixer."""
    e = Epic(tmp_path, monkeypatch, reviews={**BLOCKED, "post": [{"exit": code}, {}]},
             fixes={str(N): [{"rounds": [{}]}]})
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    [post] = [a for a in e.tries() if a.kind == "post"]
    assert (post.result["exit_code"], post.result["interrupted"], post.result.get("parked")) == (code, True, None)
    assert e.status()["status"] == "built" and e.fakes.fix_launches() == []
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    assert len(e.fakes.post_launches()) == 2 and len(e.fakes.fix_launches()) == 1
    assert e.status()["status"] == "built"


@pytest.mark.parametrize("step", [{"die": True}, {"exit": 128 + signal.SIGKILL}, {"exit": 128 + signal.SIGTERM}],
                         ids=["killed", "137", "143"])
def test_a_review_stopped_by_a_signal_is_not_parked_and_the_next_run_reviews_again(tmp_path, monkeypatch, step):
    """C.S3 with the coordinator alive, the same rule as the post's: a review
    killed by a signal (negative, or 128 + signal as `uv run` reports it) gets
    an `interrupted` result and the child stays `built`; the next run asks the
    review again, which resumes a killed run (exit 4 with the hint)."""
    e = Epic(tmp_path, monkeypatch, reviews={str(N): [step, {}]})
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    [rv] = [a for a in e.tries() if a.kind == "review"]
    assert rv.result["interrupted"] is True and rv.result.get("parked") is None
    assert e.status()["status"] == "built"
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    want = ["review", "review", "resume"] if step.get("die") else ["review", "review"]
    assert [a.kind for a in e.tries() if a.kind in ("review", "resume")] == want
    assert e.status()["status"] == "built" and not e.status().get("reason")


def _hand_fixer(pr: int, *extra: str) -> subprocess.Popen:
    """A process whose command line reads as `findings` started by hand, as
    buildme-findings' SKILL.md shows it: no `--change`."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "-m", "build_pipeline",
                             "findings", *extra])


def _git_init(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)
    return path


def test_fix_pid_sees_a_findings_started_by_hand_on_the_same_pr_and_repo(tmp_path):
    """D2.S9: a `findings` on the PR counts without `--change`, and with a
    `--project-dir` naming the source repo or another worktree of it; one on
    the same PR number of another repo does not."""
    from build_pipeline.epic import coordinator

    repo, other = _git_init(tmp_path / "repo"), _git_init(tmp_path / "other")
    tree = tmp_path / "tree"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "x", str(tree)], check=True)
    procs = []
    try:
        procs.append(_hand_fixer(4321, "--pr", "4321", "--project-dir", str(tree)))
        for _ in range(250):
            if coordinator.fix_pid(4321, repo):
                break
            time.sleep(0.02)
        assert coordinator.fix_pid(4321, repo) == procs[0].pid
        assert coordinator.fix_pid(4321, other) is None
        assert coordinator.fix_pid(432, repo) is None
        procs.append(_hand_fixer(4321, "--pr=4321"))          # no --project-dir: it may be any repo
        for _ in range(250):
            if coordinator.fix_pid(4321, other):
                break
            time.sleep(0.02)
        assert coordinator.fix_pid(4321, other) == procs[1].pid
    finally:
        for p in procs:
            p.kill()
            p.wait()


def test_a_fixer_started_by_hand_on_the_pr_is_waited_for(tmp_path, monkeypatch):
    """D2.S9: `findings --pr <n> --project-dir <repo>` started by hand (no
    `--change`) runs on the child's worktree too; the coordinator launches no
    second fixer while it lives, then runs its own."""
    from build_pipeline.epic import coordinator

    e = Epic(tmp_path, monkeypatch, fixes={str(N): [{"rounds": [{}]}]})
    hand = _hand_fixer(N, "--pr", str(N), "--project-dir", str(e.project))
    looks = []

    def tick(coord):
        posted = [a for a in e.tries() if a.kind == "post" and a.result is not None]
        if posted and hand.poll() is None:
            looks.append(1)
            assert e.fakes.fix_launches() == []                # never a second fixer
            if len(looks) == 5:
                hand.terminate()
                hand.wait()

    monkeypatch.setattr(coordinator, "_tick", tick)
    try:
        assert e.run() == 3 and merge_waiting() == [pr_url("e-a"), pr_url("e-b")]
    finally:
        hand.kill()
        hand.wait()
    assert len(looks) == 5
    assert len(e.fakes.fix_launches()) == 1 and e.status()["status"] == "built"


def test_a_decisions_file_that_cannot_be_written_parks_the_child(tmp_path, monkeypatch):
    """28's rule for any OSError, not only a missing `findings.json`: the child
    parks with the reason and the run goes on."""
    from build_pipeline.epic import coordinator

    def refuse(review_dir):
        raise PermissionError(f"[Errno 13] Permission denied: '{review_dir}/buildme-decisions.json'")

    monkeypatch.setattr(coordinator, "write_decisions", refuse)
    e = Epic(tmp_path, monkeypatch)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    st = e.status()
    assert st["status"] == "parked" and st["reason"].startswith("cannot choose what to post: [Errno 13]")
    assert [a.result["parked"] for a in e.tries() if a.kind == "post"] == [st["reason"]]
    assert e.fakes.post_launches() == [] and e.fakes.fix_launches() == []


def test_the_fixer_runs_in_the_worktree_joined_with_the_projects_offset(tmp_path):
    """`<P>` (32.2): a project in a subdirectory of its repo is fixed in the
    same subdirectory of the child's worktree."""
    from types import SimpleNamespace

    from build_pipeline.epic import coordinator

    repo = tmp_path / "repo"
    (repo / "services" / "api").mkdir(parents=True)
    worktree = tmp_path / "wt"
    me = SimpleNamespace(project_dir=repo / "services" / "api", repo=repo, _worktree=lambda c: worktree)
    assert coordinator.Coordinator._child_project(me, None) == (worktree / "services" / "api").absolute()
    me.project_dir = repo
    assert coordinator.Coordinator._child_project(me, None) == worktree.absolute()


def test_ps_failing_while_a_fixer_may_still_run_launches_nothing(tmp_path, monkeypatch):
    """D2.S9 when `ps` cannot answer: a fixer with no result may still run, so
    the child is `unknown` and no second `findings` starts."""
    e = Epic(tmp_path, monkeypatch, fixes={str(N): [{"die": True}, {"rounds": [{}]}]})
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    e.fixes()[0].result_path.unlink()
    e.fakes.ps_fails(monkeypatch)
    assert e.run() == 1
    assert len(e.fakes.fix_launches()) == 1
    st = e.status()
    assert st["status"] == "unknown" and st["reason"].startswith("cannot tell whether its fixer is still running")


def test_an_unparsable_findings_file_parks_before_posting(tmp_path, monkeypatch):
    e = Epic(tmp_path, monkeypatch)
    from build_pipeline.epic import coordinator

    real = coordinator.Coordinator._post

    async def post(self, c, rec, head):
        (Path(rec.result["review_dir"]) / "findings.json").write_text("{not json")
        return await real(self, c, rec, head)

    monkeypatch.setattr(coordinator.Coordinator, "_post", post)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-b")]
    st = e.status()
    assert st["status"] == "parked" and st["reason"].startswith("cannot choose what to post: ")
    assert e.fakes.post_launches() == [] and e.fakes.fix_launches() == []
