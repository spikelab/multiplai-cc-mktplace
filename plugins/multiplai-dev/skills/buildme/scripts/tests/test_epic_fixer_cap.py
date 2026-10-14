"""The epic's fixer is held to the epic's spend and blocking severities
(epic-e2e-report-2026-10-13-2.md, defects 1 and 2).

1. `findings --max-usd <usd>`: the loop stops between rounds once what this
   process spent plus the reviews of its PR it ran (read from the review
   ledger) reaches the ceiling, printing `FINDINGS:BUDGET:<rounds>:<spent>`.
   The coordinator passes the epic's remaining budget, and a fixer that
   stopped there is a budget stop: the run exits 3 with `EPIC:BUDGET`, and a
   re-run relaunches it only once `epic.max_usd` is raised.
2. `findings --severities HIGH,MEDIUM`: under `--auto` a re-review posts, so
   the fixer acts on, only findings of those severities. The epic passes the
   severities it blocks on, and its own post uses them too.

Git is real; every model call, review and GitHub call is a fake.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from build_pipeline import budget, git_ops
from build_pipeline.__main__ import build_parser
from build_pipeline.config import EpicSettings
from build_pipeline.findings import cli, decisions, loop
from tests.test_findings_round import (  # noqa: F401  (fixtures)
    PR,
    REPO,
    FakeReview,
    FakeTriage,
    _run,
    built,
    fake_pr,
)

from .epic_row_helpers import merge_waiting, pr_url, use_workspace
from .test_epic_fix import CHAIN, N, Epic, high

# --- findings: --max-usd ------------------------------------------------------------------


class CostlyReview(FakeReview):
    """A re-review that costs *cost* in the review ledger, under the PR's
    review slug (`<repo dir>--pr-<n>`: the test repo has no origin), while the
    fixer's own calls in the round cost *own*. Each round also writes a
    review of another PR, which is not this fixer's spend."""

    def __init__(self, pr, repo: Path, ledger: Path, *, cost: float, own: float):
        super().__init__(pr, new_threads_per_round=1)
        self.repo, self.ledger, self.cost, self.own = repo, ledger, cost, own

    def review(self, run, rnd):
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True,
                              text=True, check=True).stdout.strip()
        budget.add_process_spend(self.own, 10)
        _ledger(self.ledger, f"repo--pr-{PR}", f"/reviews/r{rnd.number}", head, self.cost)
        _ledger(self.ledger, "repo--pr-999", f"/other/r{rnd.number}", head, 50.0)
        return super().review(run, rnd)


def _ledger(ledger: Path, slug: str, target: str, head: str, cost: float) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a") as f:
        for event in ("started", "finished"):
            entry = {"event": event, "slug": slug, "target_dir": target, "head_sha": head}
            if event == "finished":
                entry["run"] = {"cost_usd": cost, "started_at": target}
            f.write(json.dumps(entry) + "\n")


class TestSpendCeiling:
    @pytest.fixture(autouse=True)
    def fresh_process(self, monkeypatch, tmp_path):
        monkeypatch.setattr(budget, "_process", budget.ProcessSpend())
        self.ledger = use_workspace(monkeypatch, tmp_path) / "reviews" / "ledger.jsonl"

    @pytest.mark.asyncio
    async def test_it_stops_between_rounds_once_its_spend_reaches_the_ceiling(self, built, fake_pr):
        """Round 1 costs $0.70 (its own $0.10 and its review's $0.60), under
        the $1 ceiling, so round 2 runs; after it $1.40 is spent and no round
        3 starts. The PR's review from before the fixer started ($9) and
        another PR's reviews are not this fixer's spend."""
        repo, change, config = built
        config.findings.max_rounds = 5
        _ledger(self.ledger, f"repo--pr-{PR}", "/reviews/epic", "0" * 40, 9.0)
        fake_pr.add_thread("alice", "first finding", fid="f0")
        review = CostlyReview(fake_pr, repo, self.ledger, cost=0.6, own=0.1)
        run, lines = _run(config, change, triage_call=FakeTriage({}), review=review)
        run.max_usd = 1.0
        assert await loop.run_findings(run) == 0
        assert lines[-1] == "FINDINGS:BUDGET:2:1.40"
        assert review.calls == 2
        assert [r.number for r in loop.load_state(run).findings.rounds] == [1, 2]

    @pytest.mark.asyncio
    async def test_without_a_ceiling_it_runs_to_its_round_cap(self, built, fake_pr):
        repo, change, config = built
        config.findings.max_rounds = 3
        fake_pr.add_thread("alice", "first finding", fid="f0")
        review = CostlyReview(fake_pr, repo, self.ledger, cost=0.6, own=0.1)
        run, lines = _run(config, change, triage_call=FakeTriage({}), review=review)
        assert await loop.run_findings(run) == 0
        assert lines[-1] == "FINDINGS:MAX_ROUNDS:3"
        assert review.calls == 3

    @pytest.mark.asyncio
    async def test_a_ceiling_of_zero_starts_no_round(self, built, fake_pr):
        repo, change, config = built
        fake_pr.add_thread("alice", "first finding", fid="f0")
        review = CostlyReview(fake_pr, repo, self.ledger, cost=0.6, own=0.1)
        run, lines = _run(config, change, triage_call=FakeTriage({}), review=review)
        run.max_usd = 0.0
        assert await loop.run_findings(run) == 0
        assert lines == ["FINDINGS:BUDGET:0:0.00"]
        assert review.calls == 0 and fake_pr.events == []


# --- findings: --severities -------------------------------------------------------------------


def _findings_json(review_dir: Path) -> None:
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "findings.json").write_text(json.dumps({"findings": [
        {"id": f"c-{s.lower()}", "topic": "code", "file": "calc.py", "severity": s, "status": "confirmed"}
        for s in ("HIGH", "MEDIUM", "LOW")
    ] + [{"id": "t-low", "topic": "tests", "file": "calc.py", "severity": "LOW", "status": "confirmed"},
         {"id": "t-high", "topic": "tests", "file": "calc.py", "severity": "HIGH", "status": "confirmed"}]}))


class TestSeverities:
    def test_write_decisions_keeps_only_the_severities_asked_for(self, tmp_path):
        _findings_json(tmp_path)
        got = json.loads(decisions.write_decisions(tmp_path, severities=("HIGH", "MEDIUM")).read_text())
        assert set(got) == {"c-high", "c-medium", "t-high"}

    def test_without_severities_every_severity_is_kept(self, tmp_path):
        _findings_json(tmp_path)
        assert set(json.loads(decisions.write_decisions(tmp_path).read_text())) == {
            "c-high", "c-medium", "c-low", "t-low", "t-high"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("severities,posted", [
        (("HIGH", "MEDIUM"), {"c-high", "c-medium", "t-high"}),
        (None, {"c-high", "c-medium", "c-low", "t-low", "t-high"}),
    ], ids=["blocking", "all"])
    async def test_the_rereview_posts_only_those_severities(self, built, fake_pr, tmp_path, severities, posted):
        repo, change, config = built
        config.findings.max_rounds = 1
        review_dir = tmp_path / "reviews" / "r1"
        _findings_json(review_dir)
        fake_pr.add_thread("alice", "first finding", fid="f0")
        seen: list[dict] = []

        class Review(FakeReview):
            def review(self, run, rnd):
                self.calls += 1
                return loop.ReviewOutcome(str(review_dir), True)

            def post(self, run, run_dir, chosen):
                seen.append(json.loads(Path(chosen).read_text()))
                return True, ""

        run, lines = _run(config, change, triage_call=FakeTriage({}), review=Review(fake_pr), decisions=None)
        run.severities = severities
        assert await loop.run_findings(run) == 0
        assert [set(s) for s in seen] == [posted]


# --- findings: the CLI ------------------------------------------------------------------------


class TestCli:
    def test_the_flags_are_listed(self, capsys):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["findings", "--help"])
        out = capsys.readouterr().out
        assert "--max-usd" in out and "--severities" in out

    def test_they_parse(self):
        args = build_parser().parse_args(["findings", "--pr", "1", "--max-usd", "3.5",
                                          "--severities", "high,MEDIUM"])
        assert (args.max_usd, args.severities) == (3.5, ("HIGH", "MEDIUM"))
        args = build_parser().parse_args(["findings", "--pr", "1"])
        assert (args.max_usd, args.severities) == (None, None)

    @pytest.mark.parametrize("argv", [["--max-usd", "-1"], ["--max-usd", "abc"], ["--max-usd", "nan"],
                                      ["--severities", "CRITICAL"], ["--severities", ""]])
    def test_bad_values_are_refused(self, argv):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["findings", "--pr", "1", *argv])

    def test_resolve_puts_them_on_the_run(self, built, monkeypatch):
        repo, change, _ = built

        def gh(argv, cwd=None, timeout=None):
            if argv[:3] == ["gh", "repo", "view"]:
                return git_ops.GitResult(argv, 0, REPO + "\n", "")
            return git_ops.GitResult(argv, 0, json.dumps(
                {"headRefName": "buildme/calc", "isCrossRepository": False}), "")

        monkeypatch.setattr(git_ops, "_run_gh", gh)
        run = cli.resolve(build_parser().parse_args(
            ["findings", "--pr", str(PR), "--project-dir", str(repo), "--max-usd", "2.25",
             "--severities", "HIGH,MEDIUM"]))
        assert (run.max_usd, run.severities) == (2.25, ("HIGH", "MEDIUM"))
        run = cli.resolve(build_parser().parse_args(["findings", "--pr", str(PR), "--project-dir", str(repo)]))
        assert (run.max_usd, run.severities) == (None, None)


# --- the epic -----------------------------------------------------------------------------------


def _opt(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def test_the_epics_post_holds_only_the_severities_it_blocks_on(tmp_path, monkeypatch):
    """Defect 2: the first post, which the fixer acts on, leaves a LOW Code
    finding out; the fixer is told the same severities for its re-reviews."""
    reviews = {str(N): [{"findings": [high("f1"), high("f8", "MEDIUM"), high("f9", "LOW")]}]}
    e = Epic(tmp_path, monkeypatch, reviews=reviews, fixes={str(N): [{"rounds": [{}]}]})
    assert e.run() == 3
    assert e.posted() == [{"f1": {"decision": "accept"}, "f8": {"decision": "accept"}}]
    [fixer] = e.fakes.fix_launches()
    assert _opt(fixer["argv"], "--severities") == "HIGH,MEDIUM"
    assert _opt(fixer["argv"], "--max-usd") is None          # no epic.max_usd: no ceiling


def test_a_fixer_stopped_at_the_epics_remaining_budget_stops_the_epic(tmp_path, monkeypatch, capsys):
    """Defect 1. The fixer starts with $10 of the $20 left and is given that
    as `--max-usd`. It stops there (`FINDINGS:BUDGET`): the run launches
    nothing more and exits 3 with `EPIC:BUDGET`, the child is not parked, and
    the fix is recorded as stopped. A re-run under the same cap launches
    nothing; once `epic.max_usd` is raised, the fixer runs again with what is
    left of the new cap, and `findings` resumes."""
    from build_pipeline.epic import coordinator

    fixes = {str(N): [{"rounds": [{}], "budget": "10.000000"}, {}]}
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, fixes=fixes, max_usd=20.0)
    monkeypatch.setattr(coordinator.Coordinator, "spent",
                        lambda self: (25.0 if any(a.result for a in e.fixes()) else 10.0, True))
    assert e.run() == 3
    assert "EPIC:BUDGET:25.00" in capsys.readouterr().out.splitlines()
    assert e.kinds() == ["build", "review", "post", "fix"]
    [fix] = e.fixes()
    assert _opt(fix.started["argv"], "--max-usd") == "10.000000"
    assert _opt(fix.started["argv"], "--severities") == "HIGH,MEDIUM"
    assert (fix.result["exit_code"], fix.result["interrupted"]) == (0, True)
    assert "FINDINGS:BUDGET:1:10.000000" in fix.result["markers"]
    assert e.status()["status"] == "built" and e.status("b")["status"] == "waiting"

    assert e.run() == 3                       # still over: nothing launched
    assert "EPIC:BUDGET:25.00" in capsys.readouterr().out.splitlines()
    assert e.kinds() == ["build", "review", "post", "fix"]
    assert len(e.fakes.fix_launches()) == 1 and len(e.fakes.post_launches()) == 1

    e.config.epic = EpicSettings(max_parallel=2, max_review_rounds=6, max_usd=100.0)
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert e.kinds() == ["build", "review", "post", "fix", "fix", "review"]
    again = e.fixes()[1]
    assert _opt(again.started["argv"], "--max-usd") == "75.000000"
    assert (again.result["exit_code"], again.result["interrupted"]) == (0, False)
    assert len(e.fakes.post_launches()) == 1
    assert e.tries()[-1].result["reused"] is True
