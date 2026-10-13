"""PR D1 of plan-buildme-epic-mode-2026-10-12.md: the fixer looks for the
same mistake (item 26), the round checks its own commits once (item 27),
`--auto` chooses what gets posted outside the worktree (item 28), and
`findings --max-rounds` (item 29).

Git is real; every model call and every GitHub call is a fake.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from build_pipeline import git_ops, stop_record
from build_pipeline.__main__ import build_parser
from build_pipeline.findings import cli, code, decisions, loop, publish
from build_pipeline.findings.models import PRComment, Triage, TriageRecord, Verdict
from build_pipeline.models import AgentResult, BlockInfo, BlockStatus, ReviewResult, ReviewScore
from build_pipeline.models import TestQualityAudit as QualityAudit  # Test* name breaks collection
from build_pipeline.state import BuildState, FindingsRound, TDDState
from tests.test_findings_round import (  # noqa: F401  (fixtures)
    FIX,
    PR,
    REPO,
    FakePR,
    FakeReview,
    FakeTriage,
    _git,
    _make_built,
    _run,
    _Stubs,
    _subjects,
    built,
    fake_pr,
)

PROBLEM = "calc.py:2 sub() has the same sign mistake as add()"


def _log(path: Path | None, *entry: str) -> None:
    if path is not None:
        with path.open("a") as f:
            f.write(json.dumps(list(entry)) + "\n")


def calls(path: Path) -> list[tuple[str, ...]]:
    if not path.exists():
        return []
    return [tuple(json.loads(line)) for line in path.read_text().splitlines()]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def block_stubs(repo: Path, log: Path | None = None, *, searched: str = "calc.py add and sub; none",
                stop_at: tuple[str, str] | None = None, stop=None):
    """The TDD engine's agents and suite runs, stubbed for any number of
    blocks: each writes from its block's name only, so a block that runs again
    after a discard writes the same bytes. Every agent call is logged as
    (kind, block name). With *stop_at* = (kind, block name), *stop()* runs at
    that call, after it is logged."""

    def hit(kind: str, name: str) -> None:
        _log(log, kind, name)
        if stop is not None and stop_at == (kind, name):
            stop()

    async def test_writer(*_a, **kw):
        name = kw.get("block_name", "")
        hit("test_writer", name)
        p = repo / "tests" / f"test_{_slug(name)}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"from calc import add\n\ndef test_{_slug(name)}():\n    assert add(2, 2) == 4\n")
        return AgentResult(success=True, output="tests written")

    async def implementer(*_a, **kw):
        name = kw.get("block_name", "")
        hit("implementer", name)
        calc = repo / "calc.py"
        if name.startswith("Fix finding"):
            calc.write_text("def add(a, b):\n    return a + b\n")
        else:
            text = calc.read_text()
            line = f"# {name}\n"
            if line not in text:
                calc.write_text(text + line)
        return AgentResult(success=True, output=f"done\n\nSEARCHED: {searched}")

    async def refactorer(*_a, **kw):
        hit("refactorer", kw.get("block_name", ""))
        return AgentResult(success=True, output="nothing to refactor")

    async def review(block, config):
        hit("review", block.name)
        return ReviewResult(scores=[ReviewScore(dimension="Q", weight=2, score=5, evidence="e")])

    red = (1, "FAILED tests/test_calc.py::test_add - AssertionError")
    return [
        patch("build_pipeline.tdd_engine.run_test_writer", side_effect=test_writer),
        patch("build_pipeline.tdd_engine.run_implementer", side_effect=implementer),
        patch("build_pipeline.tdd_engine.run_refactorer", side_effect=refactorer),
        patch("build_pipeline.tdd_engine.run_test_suite", return_value=red),
        patch("build_pipeline.tdd_engine._audit_test_quality", new_callable=AsyncMock,
              return_value=QualityAudit(passed=True)),
        patch("build_pipeline.tdd_engine._run_quality_review", side_effect=review),
        patch("build_pipeline.tdd_engine._adjudicate_review_findings", new_callable=AsyncMock,
              side_effect=lambda review, *a, **k: review),
        patch("build_pipeline.tdd_engine._run_refactor_all", new_callable=AsyncMock,
              return_value=True),
    ]


class FakeCheck:
    """The self-check agent: answers *reports* in turn ("" = no problem),
    logging each call and the diff it was given."""

    def __init__(self, reports: list[str], log: Path | None = None, stop=None):
        self.reports = list(reports)
        self.diffs: list[str] = []
        self.log = log
        self.stop = stop

    async def __call__(self, config, diff):
        self.diffs.append(diff)
        _log(self.log, "check", "")
        if self.stop is not None:
            self.stop()
        return self.reports.pop(0) if self.reports else ""


def _state(change: Path) -> BuildState:
    return BuildState.load(change / ".findings-state.json")


def _engine_blocks(change: Path, n: int = 1) -> list[BlockInfo]:
    return BuildState.load(change / code.engine_state_name(n)).tdd.blocks


# --- item 26: the same mistake --------------------------------------------------


def _fix_record(cid=101, finding_id="ab12"):
    return TriageRecord(
        round=1,
        comment=PRComment(id=cid, kind="review", author="alice", allowed=True,
                          finding_id=finding_id, path="calc.py", line=2, reply_to=cid),
        triage=Triage(verdict=Verdict.CODE_FIX, reason="add subtracts",
                      locations=["calc.py:2"], failing_test="add(2, 2) returns 4"),
    )


class TestSameMistake:
    def test_the_brief_asks_for_the_same_mistake_a_searched_section_and_nothing_else(self, tmp_path):
        text, _ = code.render_tasks(1, [_fix_record()])
        path = tmp_path / "t.md"
        path.write_text(text)
        from build_pipeline.tdd_engine import parse_blocks
        task = parse_blocks(path)[0].description
        for needle in ("same function", "its other branches", "its callers",
                       "the rest of the PR's diff", "same mistake", "fix each"):
            assert needle in task, needle
        assert "SEARCHED:" in task
        assert "change nothing else" in task.lower()

    def test_the_last_searched_section_is_read(self):
        report = ("did it\n\nSEARCHED: calc.py only\n\n[review-fix 1]\nfixed the review\n\n"
                  "SEARCHED: calc.py add() and sub(), and main.py; sub() fixed too")
        assert code.searched_text(report) == "calc.py add() and sub(), and main.py; sub() fixed too"

    def test_a_searched_section_stops_at_a_later_review_fix(self):
        report = "did it\nSEARCHED: calc.py\n\n[review-fix 1]\nfixed the review"
        assert code.searched_text(report) == "calc.py"

    def test_a_report_without_one_gives_empty_and_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert code.searched_text("done, nothing else to say") == ""
        assert any("SEARCHED" in r.getMessage() for r in caplog.records)

    def test_fixes_from_state_carries_searched(self, tmp_path):
        state = BuildState(change_name="calc", mode="only", tier="advanced",
                           state_file=str(tmp_path / "s.json"))
        state.tdd = TDDState(blocks=[BlockInfo(
            number=1, name="Fix finding ab12", description="d", status=BlockStatus.DONE,
            test_commit="t" * 40, impl_commit="i" * 40,
            implementer_report="ok\nSEARCHED: calc.py add and sub; none")])
        state.checkpoint(tmp_path / "s.json")
        fixes = code.fixes_from_state(tmp_path / "s.json", {1: "finding:ab12"})
        assert fixes["finding:ab12"]["searched"] == "calc.py add and sub; none"

    def _outcome(self, searched: str) -> str:
        r = _fix_record()
        rnd = FindingsRound(number=1, comments=[r.comment], triaged={r.comment.id: r.triage},
                            fixes={r.comment.key: {"test": "a" * 40, "impl": "b" * 40,
                                                   "status": "done", "searched": searched}})
        return publish.outcome(rnd, publish.records_of(rnd)[0])

    def test_the_reply_says_where_it_searched_after_fixed_in(self):
        text = self._outcome("calc.py add and sub; sub fixed too")
        assert "Searched for the same mistake: calc.py add and sub; sub fixed too" in text
        assert text.index("Fixed in") < text.index("Searched for the same mistake")

    def test_no_searched_sentence_when_empty(self):
        assert "Searched" not in self._outcome("")


# --- item 27: check the round's own commits once ---------------------------------


async def _one_round(built, fake_pr, check, stubs=None, review=None):
    repo, change, config = built
    fake_pr.add_thread("alice", "add subtracts", fid="ab12")
    run, lines = _run(config, change, triage_call=FakeTriage({"add subtracts": FIX}),
                      review=review or FakeReview(fake_pr), self_check=check)
    with _Stubs(stubs if stubs is not None else block_stubs(repo)):
        exit_code = await loop.run_findings(run)
    return exit_code, lines


class TestSelfCheck:
    @pytest.mark.asyncio
    async def test_no_problem_writes_nothing(self, built, fake_pr):
        repo, change, config = built
        check = FakeCheck([""])
        exit_code, lines = await _one_round(built, fake_pr, check)
        assert exit_code == 0, lines
        assert len(check.diffs) == 1
        assert not (change / code.self_check_file_name(1)).exists()
        rnd = _state(change).findings.rounds[0]
        assert rnd.self_check == {"head": rnd.self_check["head"], "report": "", "block": None}
        assert rnd.self_check["head"] == _git(repo, "rev-parse", "HEAD").strip()
        assert [b.number for b in _engine_blocks(change)] == [1]
        assert "## 2." not in (change / code.tasks_file_name(1)).read_text()
        assert not any("self-check" in s for s in _subjects(repo))

    @pytest.mark.asyncio
    async def test_one_problem_adds_one_block_and_one_commit_of_both_records(self, built, fake_pr):
        repo, change, config = built
        check = FakeCheck([PROBLEM])
        exit_code, lines = await _one_round(built, fake_pr, check)
        assert exit_code == 0, lines
        assert len(check.diffs) == 1
        report = change / code.self_check_file_name(1)
        tasks = change / code.tasks_file_name(1)
        assert PROBLEM in report.read_text()
        assert "## 2. " in tasks.read_text() and PROBLEM in tasks.read_text()
        # Both records in one commit, made before block 2's commits.
        rel = lambda p: p.relative_to(repo).as_posix()  # noqa: E731
        sha = _git(repo, "log", "--format=%H", "-1", "--", rel(report)).strip()
        assert sorted(_git(repo, "show", "--name-only", "--format=", sha).split()) == \
            sorted([rel(report), rel(tasks)])
        assert _git(repo, "log", "--format=%H", "-1", "--", rel(tasks)).strip() == sha
        assert _git(repo, "merge-base", "--is-ancestor", sha,
                    _git(repo, "log", "--format=%H", "-1", "--grep", "^impl(block-2)").strip()) == ""
        assert [b.number for b in _engine_blocks(change)] == [1, 2]
        assert all(b.status == BlockStatus.DONE for b in _engine_blocks(change))
        rnd = _state(change).findings.rounds[0]
        assert rnd.self_check["block"] == 2 and rnd.self_check["report"] == PROBLEM
        assert rnd.blocks == {1: "finding:ab12", 2: "block:2"}
        assert rnd.fixes["block:2"]["impl"]
        # Nothing left to commit: a re-render matches the committed bytes.
        assert git_ops.tree_is_clean(repo)

    @pytest.mark.asyncio
    async def test_the_check_reads_the_round_s_fix_commits(self, built, fake_pr):
        repo, change, config = built
        check = FakeCheck([""])
        await _one_round(built, fake_pr, check)
        rnd = _state(change).findings.rounds[0]
        records = _git(repo, "log", "--format=%H", "-1", "--grep",
                       r"^chore(findings-r1)").strip()
        assert rnd.code_base == records
        diff = check.diffs[0]
        assert "+    return a + b" in diff
        assert "findings-r1" not in diff          # the change directory is left out

    @pytest.mark.asyncio
    async def test_the_check_never_runs_twice(self, built, fake_pr):
        repo, change, config = built
        check = FakeCheck([PROBLEM, PROBLEM])
        await _one_round(built, fake_pr, check)
        run, lines = _run(config, change, triage_call=FakeTriage({}), review=FakeReview(fake_pr),
                          self_check=check)
        with _Stubs(block_stubs(repo)):
            assert await loop.run_findings(run) == 0, lines
        assert len(check.diffs) == 1

    @pytest.mark.asyncio
    async def test_a_failed_records_commit_fails_the_step(self, built, fake_pr, monkeypatch):
        repo, change, config = built
        real = git_ops.commit_paths

        def commit_paths(worktree, message, paths):
            if any(code.self_check_file_name(1) in p for p in paths):
                return None
            return real(worktree, message, paths)

        monkeypatch.setattr(git_ops, "commit_paths", commit_paths)
        exit_code, lines = await _one_round(built, fake_pr, FakeCheck([PROBLEM]))
        assert exit_code == 1
        assert lines[-1] == "FINDINGS:FAILED:code:could not commit the self-check records", lines

    @pytest.mark.asyncio
    async def test_a_failed_check_agent_fails_the_step_and_saves_nothing(self, built, fake_pr):
        repo, change, config = built

        async def fails(config, diff):
            raise RuntimeError("the self-check agent failed: timed out")

        exit_code, lines = await _one_round(built, fake_pr, fails)
        assert exit_code == 1
        assert lines[-1].startswith("FINDINGS:FAILED:code:RuntimeError"), lines
        assert _state(change).findings.rounds[0].self_check is None


class TestTheCheckAgent:
    @pytest.mark.asyncio
    async def test_read_only_tools_and_its_own_step(self, built):
        repo, change, config = built
        seen = {}

        async def agent_call(prompt, **kw):
            seen.update(kw, prompt=prompt)
            return AgentResult(success=True, output="NO PROBLEMS")

        with patch("build_pipeline.findings.code.agent_call", side_effect=agent_call):
            assert await code.run_self_check(config, "diff --git a/calc.py b/calc.py") == ""
        assert sorted(seen["allowed_tools"]) == ["Glob", "Grep", "Read"]
        assert seen["budget_label"] == "findings_self_check"
        assert seen["model"] == config.model_for("findings_self_check")
        assert "diff --git a/calc.py" in seen["prompt"]

    @pytest.mark.asyncio
    async def test_a_problem_list_is_returned(self, built):
        repo, change, config = built
        with patch("build_pipeline.findings.code.agent_call", new_callable=AsyncMock,
                   return_value=AgentResult(success=True, output=f"- {PROBLEM}\n")):
            assert await code.run_self_check(config, "d") == f"- {PROBLEM}"

    @pytest.mark.asyncio
    async def test_a_failed_agent_raises(self, built):
        repo, change, config = built
        with patch("build_pipeline.findings.code.agent_call", new_callable=AsyncMock,
                   return_value=AgentResult(success=False, error="timed out")):
            with pytest.raises(RuntimeError, match="self-check"):
                await code.run_self_check(config, "d")


class TestAddBlock:
    def test_appends_once_as_pending_and_keeps_the_spend(self, tmp_path):
        tasks = tmp_path / "tasks.md"
        text, _ = code.render_tasks(1, [_fix_record()],
                                    self_check={"head": "h", "report": PROBLEM, "block": 2})
        tasks.write_text(text)
        state_path = tmp_path / "s.json"
        state = BuildState(change_name="calc", mode="only", tier="advanced",
                           state_file=str(state_path))
        state.tdd = TDDState(blocks=[BlockInfo(number=1, name="Fix finding ab12", description="d",
                                               status=BlockStatus.DONE)], current_block=1)
        state.checkpoint(state_path)
        before = json.loads(state_path.read_text()).get("budget")
        code.add_block(state_path, tasks, 2)
        code.add_block(state_path, tasks, 2)
        after = BuildState.load(state_path)
        assert [(b.number, b.status) for b in after.tdd.blocks] == [
            (1, BlockStatus.DONE), (2, BlockStatus.PENDING)]
        assert after.tdd.current_block == 1
        assert json.loads(state_path.read_text()).get("budget") == before


# --- item 28: what gets posted ------------------------------------------------------


def _findings_json(review_dir: Path) -> None:
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "findings.json").write_text(json.dumps({"findings": [
        {"id": "c1", "topic": None, "file": "calc.py", "severity": "LOW", "status": "confirmed"},
        {"id": "c2", "topic": "code", "file": "README.md", "severity": "HIGH", "status": "confirmed"},
        {"id": "t1", "topic": "tests", "file": "calc.py", "severity": "MEDIUM", "status": "confirmed"},
        {"id": "t2", "topic": "tests", "file": "tests/test_calc.py", "severity": "HIGH",
         "status": "confirmed"},
        {"id": "t3", "topic": "tests", "file": "README.md", "severity": "HIGH", "status": "confirmed"},
        {"id": "d1", "topic": "docs", "file": "README.md", "severity": "HIGH", "status": "confirmed"},
    ]}))


class TestDecisions:
    def test_every_code_finding_and_tests_findings_on_source_files(self, tmp_path):
        review_dir = tmp_path / "reviews" / "acme-widgets--pr-7" / "r1"
        _findings_json(review_dir)
        path = decisions.write_decisions(review_dir)
        assert path == review_dir / "buildme-decisions.json"
        assert json.loads(path.read_text()) == {
            "c1": {"decision": "accept"}, "c2": {"decision": "accept"},
            "t1": {"decision": "accept"}}

    def test_no_findings_json_raises_naming_it_and_writes_nothing(self, tmp_path):
        with pytest.raises(FileNotFoundError, match=re.escape(str(tmp_path / "findings.json"))):
            decisions.write_decisions(tmp_path)
        assert not (tmp_path / "buildme-decisions.json").exists()

    def test_an_unreadable_findings_json_raises_value_error(self, tmp_path):
        (tmp_path / "findings.json").write_text("{not json")
        with pytest.raises(ValueError, match=re.escape(str(tmp_path / "findings.json"))):
            decisions.write_decisions(tmp_path)
        assert not (tmp_path / "buildme-decisions.json").exists()

    @pytest.mark.asyncio
    async def test_no_findings_json_fails_the_review_step(self, built, fake_pr):
        repo, change, config = built
        fake_pr.add_thread("alice", "is this right?", fid="c1")
        q = Triage(verdict=Verdict.QUESTION, reason="It is.")
        run, lines = _run(config, change, triage_call=FakeTriage({"is this right": q}),
                          review=FakeReview(fake_pr), decisions=None)
        assert await loop.run_findings(run) == 1
        assert lines[-1].startswith("FINDINGS:FAILED:review:cannot choose what to post: "), lines
        assert "/reviews/r1/findings.json" in lines[-1]

    @pytest.mark.asyncio
    async def test_the_poster_gets_the_decisions_path(self, built, fake_pr, tmp_path):
        repo, change, config = built
        review_dir = tmp_path / "data" / "r1"
        _findings_json(review_dir)
        fake_pr.add_thread("alice", "is this right?", fid="c1")
        q = Triage(verdict=Verdict.QUESTION, reason="It is.")
        got = []
        review = FakeReview(fake_pr)
        review.review = lambda run, rnd: loop.ReviewOutcome(str(review_dir), True)
        review.post = lambda run, run_dir, path: (got.append((run_dir, path)), (True, ""))[1]
        run, lines = _run(config, change, triage_call=FakeTriage({"is this right": q}),
                          review=review, decisions=None)
        assert await loop.run_findings(run) == 0, lines
        assert got == [(str(review_dir), review_dir / "buildme-decisions.json")]
        assert (review_dir / "buildme-decisions.json").exists()

    def test_post_review_passes_the_decisions_file(self, built, monkeypatch, tmp_path):
        repo, change, config = built
        seen = []

        def review_skill(argv, cwd=None, timeout=None):
            seen.append(argv)
            return git_ops.GitResult(argv, 0, "posted", "")

        monkeypatch.setattr(git_ops, "_run_review_skill", review_skill)
        run, _ = _run(config, change, triage_call=FakeTriage({}), review=FakeReview(FakePR()))
        ok, _ = loop.post_review(run, "/data/r1", tmp_path / "buildme-decisions.json")
        assert ok
        argv = seen[0]
        assert argv[argv.index("post") + 1] == "/data/r1"
        assert argv[argv.index("--decisions") + 1] == str(tmp_path / "buildme-decisions.json")


# --- item 29: --max-rounds -----------------------------------------------------------


class TestMaxRounds:
    def test_the_flag_is_listed(self, capsys):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["findings", "--help"])
        assert "--max-rounds" in capsys.readouterr().out

    def test_it_overrides_the_config(self, built, monkeypatch):
        repo, change, _ = built

        def gh(argv, cwd=None, timeout=None):
            if argv[:3] == ["gh", "repo", "view"]:
                return git_ops.GitResult(argv, 0, REPO + "\n", "")
            return git_ops.GitResult(argv, 0, json.dumps(
                {"headRefName": "buildme/calc", "isCrossRepository": False}), "")

        monkeypatch.setattr(git_ops, "_run_gh", gh)
        args = build_parser().parse_args(
            ["findings", "--pr", str(PR), "--project-dir", str(repo), "--max-rounds", "1"])
        assert cli.resolve(args).config.findings.max_rounds == 1
        args = build_parser().parse_args(["findings", "--pr", str(PR), "--project-dir", str(repo)])
        assert cli.resolve(args).config.findings.max_rounds == 3

    def test_zero_is_refused(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["findings", "--pr", "1", "--max-rounds", "0"])


# --- two rounds of the real loop leave nothing to refuse ------------------------------


def _with_bare_origin(base: Path, repo: Path) -> Path:
    origin = base / "o" / "calc.git"
    origin.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "buildme/calc")
    return origin


def _assert_nothing_to_refuse(repo: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert stop_record.others_work(repo, None, head=head) is None
    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    for line in status.splitlines():
        rel = line[3:]
        assert _git(repo, "status", "--porcelain", "--untracked-files=all", "--", rel,
                    *git_ops.BOOKKEEPING_EXCLUDES) == "", f"{rel} is not bookkeeping"


class TestTwoRounds:
    @pytest.mark.asyncio
    async def test_two_rounds_leave_nothing_to_refuse(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUILDME_TRUST_REPO", "1")
        repo, change, config = _make_built(tmp_path)
        config.findings.max_rounds = 2
        _with_bare_origin(tmp_path, repo)
        pr = FakePR()
        monkeypatch.setattr(git_ops, "_run_gh", pr.gh)
        monkeypatch.setattr(git_ops, "_run_pr_skill", pr.pr_skill)   # the push is real
        pr.add_thread("alice", "add subtracts", fid="ab12")
        second = Triage(verdict=Verdict.CODE_FIX, reason="the doc is wrong",
                        locations=["calc.py:1"], failing_test="add(1, 1) returns 2")
        triage_call = FakeTriage({"add subtracts": FIX, "round 1 finding": second})
        data = tmp_path / "data" / "reviews"
        checked: list[int] = []

        def review_run(run, rnd):
            d = data / f"r{rnd.number}"
            _findings_json(d)
            return loop.ReviewOutcome(str(d), True)

        def post(run, run_dir, path):
            _assert_nothing_to_refuse(repo)
            checked.append(1)
            assert Path(path).exists() and data in Path(path).parents
            if len(checked) == 1:
                pr.add_thread("alice", "round 1 finding 0", fid="r10")
            return True, ""

        review = FakeReview(pr)
        review.review, review.post = review_run, post
        check = FakeCheck([PROBLEM, ""])
        run, lines = _run(config, change, triage_call=triage_call, review=review,
                          self_check=check, decisions=None)
        with _Stubs(block_stubs(repo)):
            assert await loop.run_findings(run) == 0, lines
        assert lines[-1] == "FINDINGS:MAX_ROUNDS:2", lines
        assert len(checked) == 2 and len(check.diffs) == 2
        _assert_nothing_to_refuse(repo)
        assert not list(repo.rglob("buildme-decisions.json"))
        assert _git(repo, "rev-parse", "HEAD").strip() == \
            _git(repo, "rev-parse", "origin/buildme/calc").strip()
