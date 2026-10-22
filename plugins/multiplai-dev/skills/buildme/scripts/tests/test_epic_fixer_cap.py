"""The epic's fixer is held to the epic's spend and blocking severities
(epic-e2e-report-2026-10-13-2.md, defects 1 and 2).

1. `findings --max-usd <usd>`: the loop stops between rounds once what this
   process spent plus the reviews of its PR it ran (read from the review
   ledger) reaches the ceiling and the fetch found something to act on,
   printing `FINDINGS:BUDGET:<rounds>:<spent>`; with nothing to act on it
   ends `FINDINGS:DONE` as before. The coordinator passes the epic's
   remaining budget less what fixers still running were given, and waits,
   as every other paid launch does, while they hold all of it. A fixer that
   stopped there with the epic's spend at the cap is a budget stop: the run
   exits 3 with `EPIC:BUDGET`, and a re-run relaunches it only once
   `epic.max_usd` is raised. Under the cap, it runs again once the others
   end.
2. `findings --severities HIGH,MEDIUM --code-only`: under `--auto` a
   re-review posts, so the fixer acts on, only Code findings of those
   severities. The epic passes what it blocks on, and its own post keeps the
   same findings.

Git is real; every model call, review and GitHub call is a fake.
"""

from __future__ import annotations

import json
import subprocess
import time
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

from .epic_row_helpers import (
    CallLog,
    child,
    merge_waiting,
    pr_number,
    pr_url,
    use_workspace,
)
from .test_epic_fix import CHAIN, TWO, Epic, N, high

# --- findings: --max-usd ------------------------------------------------------------------


class CostlyReview(FakeReview):
    """A re-review that costs *cost* in the review ledger, under the PR's
    review slug (`<repo dir>--pr-<n>`: the test repo has no origin), while the
    fixer's own calls in the round cost *own*. Each round also writes a
    review of another PR, which is not this fixer's spend."""

    def __init__(self, pr, repo: Path, ledger: Path, *, cost: float, own: float, slug: str = f"repo--pr-{PR}"):
        super().__init__(pr, new_threads_per_round=1)
        self.repo, self.ledger, self.cost, self.own, self.slug = repo, ledger, cost, own, slug

    def review(self, run, rnd):
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True,
                              text=True, check=True).stdout.strip()
        budget.add_process_spend(self.own, 10)
        _ledger(self.ledger, self.slug, f"/reviews/r{rnd.number}", head, self.cost)
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
    async def test_with_an_origin_its_reviews_are_read_under_the_origins_slug(self, built, fake_pr, tmp_path):
        """With an `origin` remote, the PR's reviews are in the ledger under
        `<owner>-<repo>--pr-<n>` (here `o-r`), as the epic counts them, not
        under the checkout's directory name, whose $50 line is not this
        fixer's spend."""
        repo, change, config = built
        config.findings.max_rounds = 5
        subprocess.run(["git", "init", "-q", "--bare", str(tmp_path / "o" / "r.git")], check=True)
        subprocess.run(["git", "remote", "add", "origin", str(tmp_path / "o" / "r.git")], cwd=repo, check=True)
        _ledger(self.ledger, f"repo--pr-{PR}", "/reviews/dir-name", "0" * 40, 50.0)
        fake_pr.add_thread("alice", "first finding", fid="f0")
        review = CostlyReview(fake_pr, repo, self.ledger, cost=0.6, own=0.1, slug=f"o-r--pr-{PR}")
        run, lines = _run(config, change, triage_call=FakeTriage({}), review=review)
        run.max_usd = 1.0
        assert await loop.run_findings(run) == 0
        assert lines[-1] == "FINDINGS:BUDGET:2:1.40"
        assert review.calls == 2

    @pytest.mark.asyncio
    async def test_float_noise_does_not_buy_a_round_past_the_ceiling(self, built, fake_pr):
        """$0.10 of its own and a $0.70 review make $0.80, the ceiling; added
        as floats they make 0.7999999999999999. The spend is rounded to the 6
        decimals `--max-usd` is passed with, as the epic rounds its own, so no
        second round starts."""
        repo, change, config = built
        config.findings.max_rounds = 5
        fake_pr.add_thread("alice", "first finding", fid="f0")
        review = CostlyReview(fake_pr, repo, self.ledger, cost=0.7, own=0.1)
        run, lines = _run(config, change, triage_call=FakeTriage({}), review=review)
        run.max_usd = 0.8
        assert await loop.run_findings(run) == 0
        assert lines[-1] == "FINDINGS:BUDGET:1:0.80"
        assert review.calls == 1

    @pytest.mark.asyncio
    async def test_a_round_that_leaves_nothing_to_act_on_ends_done_past_the_ceiling(self, built, fake_pr):
        """Round 1 costs $0.70, past the $0.50 ceiling, and its re-review posts
        nothing new: the fetch finds nothing to act on, so the loop ends
        `FINDINGS:DONE`, not `FINDINGS:BUDGET`, and the epic can use its
        clean review."""
        repo, change, config = built
        config.findings.max_rounds = 5
        fake_pr.add_thread("alice", "first finding", fid="f0")
        review = CostlyReview(fake_pr, repo, self.ledger, cost=0.6, own=0.1)
        review.new = 0
        run, lines = _run(config, change, triage_call=FakeTriage({}), review=review)
        run.max_usd = 0.5
        assert await loop.run_findings(run) == 0
        assert lines[-1] == "FINDINGS:DONE:1"
        assert review.calls == 1

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

    def test_code_only_leaves_out_tests_findings(self, tmp_path):
        """What the epic blocks on (`epic.review.blockers`): Code findings at
        HIGH or MEDIUM, never a Tests finding, even one on a source file."""
        _findings_json(tmp_path)
        got = json.loads(decisions.write_decisions(tmp_path, severities=("HIGH", "MEDIUM"),
                                                   code_only=True).read_text())
        assert set(got) == {"c-high", "c-medium"}
        got = json.loads(decisions.write_decisions(tmp_path, code_only=True).read_text())
        assert set(got) == {"c-high", "c-medium", "c-low"}

    def test_the_epics_choice_matches_what_it_blocks_on(self, tmp_path):
        from build_pipeline.epic import fix, review

        _findings_json(tmp_path)
        assert set(json.loads(fix.write_decisions(tmp_path).read_text())) == {"c-high", "c-medium"}
        assert len(review.blockers(tmp_path)) == 2

    def test_without_severities_every_severity_is_kept(self, tmp_path):
        _findings_json(tmp_path)
        assert set(json.loads(decisions.write_decisions(tmp_path).read_text())) == {
            "c-high", "c-medium", "c-low", "t-low", "t-high"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("severities,code_only,posted", [
        (("HIGH", "MEDIUM"), True, {"c-high", "c-medium"}),
        (("HIGH", "MEDIUM"), False, {"c-high", "c-medium", "t-high"}),
        (None, False, {"c-high", "c-medium", "c-low", "t-low", "t-high"}),
    ], ids=["epic", "blocking", "all"])
    async def test_the_rereview_posts_only_those_severities(self, built, fake_pr, tmp_path, severities,
                                                           code_only, posted):
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
        run.severities, run.code_only = severities, code_only
        assert await loop.run_findings(run) == 0
        assert [set(s) for s in seen] == [posted]


# --- findings: the CLI ------------------------------------------------------------------------


class TestCli:
    def test_the_flags_are_listed(self, capsys):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["findings", "--help"])
        out = capsys.readouterr().out
        assert "--max-usd" in out and "--severities" in out and "--code-only" in out

    def test_they_parse(self):
        args = build_parser().parse_args(["findings", "--pr", "1", "--max-usd", "3.5",
                                          "--severities", "high,MEDIUM", "--code-only"])
        assert (args.max_usd, args.severities, args.code_only) == (3.5, ("HIGH", "MEDIUM"), True)
        args = build_parser().parse_args(["findings", "--pr", "1"])
        assert (args.max_usd, args.severities, args.code_only) == (None, None, False)

    def test_severities_are_trimmed_upper_cased_and_kept_once(self):
        args = build_parser().parse_args(["findings", "--pr", "1", "--severities", " high , MEDIUM,High,"])
        assert args.severities == ("HIGH", "MEDIUM")

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
             "--severities", "HIGH,MEDIUM", "--code-only"]))
        assert (run.max_usd, run.severities, run.code_only) == (2.25, ("HIGH", "MEDIUM"), True)
        run = cli.resolve(build_parser().parse_args(["findings", "--pr", str(PR), "--project-dir", str(repo)]))
        assert (run.max_usd, run.severities, run.code_only) == (None, None, False)


# --- the epic -----------------------------------------------------------------------------------


def _opt(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def test_the_epics_post_holds_only_the_severities_it_blocks_on(tmp_path, monkeypatch):
    """Defect 2: the first post, which the fixer acts on, leaves out a LOW
    Code finding and a Tests finding on a source file, neither of which the
    epic blocks on; the fixer is told the same for its re-reviews."""
    tests_high = {**high("f7"), "topic": "tests"}
    reviews = {str(N): [{"findings": [high("f1"), high("f8", "MEDIUM"), high("f9", "LOW"), tests_high]}]}
    e = Epic(tmp_path, monkeypatch, reviews=reviews, fixes={str(N): [{"rounds": [{}]}]})
    assert e.run() == 3
    assert e.posted() == [{"f1": {"decision": "accept"}, "f8": {"decision": "accept"}}]
    [fixer] = e.fakes.fix_launches()
    assert _opt(fixer["argv"], "--severities") == "HIGH,MEDIUM" and "--code-only" in fixer["argv"]
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


def _fake_spend(monkeypatch, e: Epic, start: float, per_fix: float) -> None:
    """The epic's spend: *start*, plus *per_fix* for each `fix` of any child
    that has its result (a running fixer's spend is not counted yet, as
    `spend.attempts_spend` does not count it)."""
    from build_pipeline.epic import coordinator, records

    monkeypatch.setattr(coordinator.Coordinator, "spent", lambda self: (
        start + per_fix * sum(1 for a in records.attempts(e.dir) if a.kind == "fix" and a.result), True))


def _fix_of(launch: dict) -> int:
    return int(_opt(launch["argv"], "--pr"))


def test_two_fixers_at_once_share_what_is_left(tmp_path, monkeypatch):
    """Two children's fixers under one `epic.max_usd` (max_parallel 2). The
    first is given all that is left ($10 of $20), so the second waits for it
    instead of being given the same $10; once the first has ended ($4
    spent), the second is given the $6 left. Without that, the two could
    spend the remainder twice. The launch hold (the next test) is off here,
    so the second child's review and post go ahead and its fixer is the one
    that waits."""
    from build_pipeline.epic import coordinator

    monkeypatch.setattr(coordinator.Coordinator, "_held_back", lambda self: False)
    na, nb = pr_number("e-a"), pr_number("e-b")
    reviews = {str(na): [{"findings": [high("f1")]}], str(nb): [{"findings": [high("g1")]}]}
    fixes = {str(na): [{"block": True, "rounds": [{}]}], str(nb): [{"block": True, "rounds": [{}]}]}
    e = Epic(tmp_path, monkeypatch, answer=TWO, reviews=reviews, fixes=fixes, max_usd=20.0)
    _fake_spend(monkeypatch, e, 10.0, 4.0)
    released: dict[int, float] = {}
    overlap: list[list[int]] = []
    first: list[float] = []

    def tick(coord):
        launched = [_fix_of(x) for x in e.fakes.fix_launches()]
        if not launched:
            return
        (e.root / "release").mkdir(exist_ok=True)
        if len(launched) > 1 and launched[0] not in released:
            overlap.append(launched)
        first.append(first[0] if first else time.monotonic())
        if launched[0] not in released and time.monotonic() - first[0] > 2.0:
            (e.root / "release" / f"fix-{launched[0]}").write_text("")
            released[launched[0]] = time.monotonic()
        for n in launched[1:]:
            if n not in released:
                (e.root / "release" / f"fix-{n}").write_text("")
                released[n] = time.monotonic()

    monkeypatch.setattr(coordinator, "_tick", tick)
    assert e.run() == 3 and sorted(merge_waiting()) == sorted([pr_url("e-a"), pr_url("e-b")])
    assert overlap == []
    one, two = e.fakes.fix_launches()
    assert (_opt(one["argv"], "--max-usd"), _opt(two["argv"], "--max-usd")) == ("10.000000", "6.000000")
    assert len(e.fakes.post_launches()) == 2


def test_nothing_new_launches_while_running_fixers_hold_what_is_left(tmp_path, monkeypatch):
    """Child a's fixer holds all that is left of `epic.max_usd`; child c,
    ready once b's review ends, is not built until a's fixer has ended, as
    its build would spend what a's fixer was given."""
    from build_pipeline.epic import coordinator

    from .epic_row_helpers import EpicSplitAnswer

    answer = EpicSplitAnswer(single=False, reason="Three parts.", children=[child("a"), child("b"), child("c")])
    na, nb = pr_number("e-a"), pr_number("e-b")
    reviews = {str(na): [{"findings": [high("f1")]}], str(nb): [{"block": True}]}
    fixes = {str(na): [{"block": True, "rounds": [{}]}]}
    e = Epic(tmp_path, monkeypatch, answer=answer, reviews=reviews, fixes=fixes, max_usd=20.0)
    _fake_spend(monkeypatch, e, 10.0, 4.0)
    early: list[str] = []
    since: list[float] = []

    def builds_of_c() -> list[dict]:
        return [x for x in CallLog(e.root / "calls.log").calls() if x["kind"] == "build" and "e-c" in x["argv"]]

    def tick(coord):
        if not e.fakes.fix_launches():
            return
        (e.root / "release").mkdir(exist_ok=True)
        (e.root / "release" / f"review-{nb}").write_text("")
        since.append(since[0] if since else time.monotonic())
        gate = e.root / "release" / f"fix-{na}"
        if gate.exists():
            return
        if builds_of_c():
            early.append("c built while a's fixer held what was left")
        if time.monotonic() - since[0] > 2.0:
            gate.write_text("")

    monkeypatch.setattr(coordinator, "_tick", tick)
    assert e.run() == 3
    assert sorted(merge_waiting()) == sorted(pr_url(f"e-{c}") for c in "abc")
    assert early == []
    assert len(builds_of_c()) == 1


def test_a_fixer_stopped_at_its_share_runs_again_once_the_others_end(tmp_path, monkeypatch, capsys):
    """A fixer launched with a small share ($1 of the $20 cap: $10 spent, the
    other fixer holding $9) stops there with `FINDINGS:BUDGET` while the
    epic's spend ($14) is under the cap. That is not a budget stop: once the
    other fixer has ended ($18 spent), it runs again with the $2 left, and
    both PRs wait for their merge. Before the fix the run stopped with
    `EPIC:BUDGET` under the cap. The launch hold is off, so b's review and
    post go ahead while a's fixer runs."""
    from build_pipeline.epic import coordinator, records

    monkeypatch.setattr(coordinator.Coordinator, "_held_back", lambda self: False)
    # a running fixer's PR has $1 of reviews since its launch: a's share of $10 holds $9
    monkeypatch.setattr(coordinator.Coordinator, "_pr_reviews", lambda self, c: 1.0 if c.id in self.shares else 0.0)
    na, nb = pr_number("e-a"), pr_number("e-b")
    reviews = {str(na): [{"findings": [high("f1")]}], str(nb): [{"block": True, "findings": [high("g1")]}]}
    fixes = {str(na): [{"block": True, "rounds": [{}]}], str(nb): [{"rounds": [{}], "budget": "1.000000"}, {}]}
    e = Epic(tmp_path, monkeypatch, answer=TWO, reviews=reviews, fixes=fixes, max_usd=20.0)
    _fake_spend(monkeypatch, e, 10.0, 4.0)

    def b_fixes() -> list[records.Attempt]:
        return [a for a in records.attempts(e.dir) if a.child == "b" and a.kind == "fix"]

    def tick(coord):
        if na not in [_fix_of(x) for x in e.fakes.fix_launches()]:
            return
        (e.root / "release").mkdir(exist_ok=True)
        (e.root / "release" / f"review-{nb}").write_text("")      # b is reviewed once a's fixer runs
        if any(a.result for a in b_fixes()):
            (e.root / "release" / f"fix-{na}").write_text("")

    monkeypatch.setattr(coordinator, "_tick", tick)
    assert e.run() == 3 and sorted(merge_waiting()) == sorted([pr_url("e-a"), pr_url("e-b")])
    assert not [x for x in capsys.readouterr().out.splitlines() if x.startswith("EPIC:BUDGET")]
    usd = {n: [_opt(x["argv"], "--max-usd") for x in e.fakes.fix_launches() if _fix_of(x) == n] for n in (na, nb)}
    assert usd == {na: ["10.000000"], nb: ["1.000000", "2.000000"]}
    first, again = b_fixes()
    assert (first.result["exit_code"], first.result["interrupted"]) == (0, True)
    assert (again.result["exit_code"], again.result["interrupted"]) == (0, False)
    assert e.status("b")["status"] != "parked"


def _hold_after(monkeypatch, hold: dict) -> None:
    """`_held_back` is True until the coordinator's clock reaches `hold["until"]`."""
    from build_pipeline.epic import coordinator

    monkeypatch.setattr(coordinator.Coordinator, "_held_back",
                        lambda self: hold["until"] is not None and coordinator.CLOCK.now() < hold["until"])


def test_a_running_review_step_launches_nothing_while_fixers_hold_what_is_left(tmp_path, monkeypatch):
    """A review step that is already running launches no review and no
    resume while running fixers hold all that is left (`_held_back`). Its
    first review is told another runs now (exit 2), so it asks again; the
    second finds that one interrupted (exit 4) and it is resumed. After each
    of those answers a fixer is made to hold all that is left for 120 s of
    the coordinator's clock (longer than the 60 s it waits after exit 2): the
    next launch waits for it. Before the fix only `_loop`'s launches waited."""
    from build_pipeline.epic import coordinator

    hold: dict = {"until": None}
    _hold_after(monkeypatch, hold)
    early: list[str] = []
    real = coordinator.Coordinator._run_review

    async def run_review(self, c, kind, argv, head):
        if self._held_back():
            early.append(kind)
        code, out = await real(self, c, kind, argv, head)
        if code in (2, 4):
            hold["until"] = coordinator.CLOCK.now() + 120.0
        return code, out

    monkeypatch.setattr(coordinator.Coordinator, "_run_review", run_review)
    e = Epic(tmp_path, monkeypatch, answer=CHAIN, reviews={str(N): [{"busy": True}, {}]})
    assert e.run() == 3 and merge_waiting() == [pr_url("e-a")]
    assert e.kinds() == ["build", "review", "review", "resume"]
    assert early == []


def test_the_merge_agent_waits_while_running_fixers_hold_what_is_left(tmp_path, monkeypatch):
    """The merge step's agent is a paid launch too: while running fixers hold
    all that is left (`_held_back`, here for 120 s of the coordinator's clock
    from the merge step's start), it waits, then resolves and the PR merges.
    Before the fix it was launched at once."""
    from build_pipeline.epic import coordinator

    from .test_epic_merge import MergeRoot

    r = MergeRoot(tmp_path, monkeypatch)
    hold: dict = {"until": None}
    _hold_after(monkeypatch, hold)
    real_point, agent, early = r.point, r.agent, []

    def point(coord, c, name):
        real_point(coord, c, name)
        if name == "start" and hold["until"] is None:
            hold["until"] = coordinator.CLOCK.now() + 120.0

    async def timed(config, w, files, name, default):
        if coordinator.CLOCK.now() < hold["until"]:
            early.append(files)
        return await agent(config, w, files, name, default)

    r.point, r.agent = point, timed
    assert r.run() == 0 and r.merged()
    assert len(r.calls("merge-agent")) == 1 and early == []


def _holding(monkeypatch, spent: float, share: float, at_launch: float, now: float):
    """A coordinator under a $20 cap with one running fixer (child a) given
    *share*, its PR's reviews *at_launch* when launched and *now* since:
    `spent` and the PR's reviews are fakes, the rest is the real code."""
    from build_pipeline.epic import coordinator

    coord = object.__new__(coordinator.Coordinator)
    coord.settings = EpicSettings(max_parallel=2, max_review_rounds=6, max_usd=20.0)
    coord.children = {"a": None, "b": None}
    coord.shares = {"a": coordinator.Share(share, at_launch)}
    coord.budget_stop = None
    monkeypatch.setattr(coordinator.Coordinator, "spent", lambda self: (spent, True))
    monkeypatch.setattr(coordinator.Coordinator, "_pr_reviews", lambda self, c: now)
    return coord


def test_float_noise_never_lifts_the_hold_or_gives_a_share_of_zero(monkeypatch):
    """Ledger costs have 6 decimals, but adding them leaves noise. Here $20
    was the cap, $0.697137 spent when a's fixer was given $19.302863 with its
    PR's reviews at $3.414154; another $2.639316 of its reviews has since
    finished, so $3.336453 is spent and a still holds $16.663547: all that is
    left. Unrounded, spent + held is about 4e-15 under the cap, so the hold
    lifted while a held its share, and b's fixer was given about 4e-15,
    passed as `--max-usd 0.000000`, which stopped at once (`FINDINGS:BUDGET`)
    and was relaunched at once, in a loop, until a ended."""
    coord = _holding(monkeypatch, 3.336453, 19.302863, 3.414154, 3.414154 + 2.639316)
    assert coord._held_back()
    assert coord._left("b") == 0.0
    assert not coord._over_budget()


def test_a_running_fixer_holds_its_share_less_its_prs_reviews_since_launch(tmp_path, monkeypatch):
    """`_held` with the real `_pr_reviews` and a real ledger. a's fixer was
    given $10 when its PR (`o-r--pr-12`) had $3 of reviews; another $2.50
    of them has finished since, so it holds $7.50. A review of another PR,
    and one under the bare directory name, are not a's."""
    from build_pipeline.epic import coordinator

    ledger = use_workspace(monkeypatch, tmp_path) / "reviews" / "ledger.jsonl"
    url = "https://github.com/o/r/pull/12"
    _ledger(ledger, "o-r--pr-12", "/r/pr-12/1", "h1", 3.0)
    coord = object.__new__(coordinator.Coordinator)
    coord.slug = "o-r"
    coord.children = {"a": coordinator.Child(id="a", depends_on=[], pr=url),
                      "b": coordinator.Child(id="b", depends_on=[])}
    coord.shares = {"a": coordinator.Share(10.0, coord._pr_reviews(coord.children["a"]))}
    assert coord._held("b") == 10.0
    _ledger(ledger, "o-r--pr-12", "/r/pr-12/2", "h2", 2.5)
    _ledger(ledger, "o-r--pr-99", "/r/pr-99", "h1", 40.0)
    _ledger(ledger, "r--pr-12", "/r/dir-name", "h1", 40.0)
    assert coord._held("b") == 7.5
    assert coord._held("a") == 0.0


def test_a_share_under_a_millionth_of_a_dollar_counts_as_none(monkeypatch):
    """What is left is rounded to the 6 decimals `--max-usd` is passed with:
    $0.0000004 left is $0, so the fixer waits instead of being launched with
    `--max-usd 0.000000`; $0.000001 is a share."""
    coord = _holding(monkeypatch, 10.0, 9.9999996, 0.0, 0.0)
    assert coord._left("b") == 0.0 and coord._held_back()
    coord = _holding(monkeypatch, 10.0, 9.999999, 0.0, 0.0)
    assert coord._left("b") == 0.000001 and not coord._held_back()
